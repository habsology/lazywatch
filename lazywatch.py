"""lazywatch: watch public rule pages, wake a local open-weight model only when something changed.

Usage:
    python lazywatch.py check  [--config sites.json] [--state state]
    python lazywatch.py digest [--state state] [--days 7]
    python lazywatch.py peek   [--config sites.json] [--state state]   # what each snapshot holds

Stdlib only. The model (default gemma3:4b via Ollama) never sees a page that did
not change, and every quote it returns must appear verbatim in the changed text.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:4b"
UA = {"User-Agent": "lazywatch/0.1 (once-a-day public page watcher; dev.to Hacktoberfest demo)"}


class _Text(HTMLParser):
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "tr", "div", "section", "br"}
    SKIP = {"script", "style", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


_FEED = re.compile(r"\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*<(?:feed|rss)\b", re.S)
_FEED_TEXT = {"title", "summary", "content", "description"}


def _feed_paragraphs(xml):
    """Atom or RSS: each entry's title and summary become one paragraph each, tags stripped."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return None
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] in _FEED_TEXT:
            text = unescape(re.sub(r"<[^>]+>", " ", " ".join(el.itertext())))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 25:
                out.append(text)
    return out


def paragraphs(html):
    if _FEED.match(html[:2000]):
        found = _feed_paragraphs(html)
        if found is not None:
            return found
    parser = _Text()
    parser.feed(html)
    parser.close()  # flush text an unclosed tag may still hold
    lines = (re.sub(r"\s+", " ", p).strip() for p in "".join(parser.parts).split("\n"))
    return [p for p in lines if len(p) > 25]


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def changed_paragraphs(old, new):
    added, removed = [], []
    for line in difflib.ndiff(old, new):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])
    return added, removed


def ask_model(name, added, removed, model=MODEL, url=OLLAMA_URL):
    prompt = (
        f"A public rules page called '{name}' changed.\n"
        "NEW TEXT:\n" + "\n".join(f"- {p}" for p in added) + "\n"
        "REMOVED TEXT:\n" + "\n".join(f"- {p}" for p in removed) + "\n\n"
        "Reply with JSON only: {\"summary\": one plain sentence on what changed, "
        "\"quote\": one exact sentence copied word for word from NEW TEXT}."
    )
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.loads(r.read())["response"])


def verify(reply, added):
    """Keep the summary only if its quote really appears in the new text."""
    quote = str(reply.get("quote", "")).strip()
    summary = str(reply.get("summary", "")).strip()
    if summary and quote and any(quote in p for p in added):
        return {"summary": summary, "quote": quote, "verified": True}
    return {"summary": summary, "quote": quote, "verified": False}


def _key(site):
    return hashlib.sha1(site["url"].encode()).hexdigest()[:12]


def _browser():
    from lazywatch_browser import fetch_rendered
    return fetch_rendered


def pick_fetcher(site, state="state"):
    """Plain urllib by default; a real browser (Browserbase) for pages marked "render": true.

    "render": "auto" tries the plain fetch on the first snapshot and switches to the browser only
    if the page refuses it. The choice is remembered, so later checks never mix the two.
    """
    mode = site.get("render")
    if mode == "auto":
        marker = Path(state) / (_key(site) + ".browser")
        if marker.exists():
            return _browser()
        if (Path(state) / (_key(site) + ".json")).exists():
            return fetch

        def auto(url):
            try:
                return fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):  # the page is gone; a browser would only snapshot the error page
                    raise
                html = _browser()(url)
                marker.write_text(f"plain fetch failed (HTTP {exc.code}); using a real browser\n")
                return html
            except Exception as exc:
                html = _browser()(url)
                marker.write_text(f"plain fetch failed ({exc.__class__.__name__}); using a real browser\n")
                return html
        return auto
    if mode:
        return _browser()
    return fetch


def _mode(site, state):
    """Which fetch path a page is on right now."""
    return "browser" if site.get("render") is True or (Path(state) / (_key(site) + ".browser")).exists() else "plain"


def _load_snapshot(snap, site, state):
    data = json.loads(snap.read_text())
    if isinstance(data, list):  # written before snapshots recorded their fetch path
        return {"mode": "browser" if (Path(state) / (_key(site) + ".browser")).exists() else "plain", "paragraphs": data}
    return data


def _log(state, name, row):
    with (Path(state) / name).open("a") as f:
        f.write(json.dumps(row) + "\n")


def _store(fn, *args):
    """Optional Tiger Data memory; skipped unless TIGER_DATABASE_URL is set, never breaks a check."""
    if not os.environ.get("TIGER_DATABASE_URL"):
        return
    try:
        import lazywatch_store
        getattr(lazywatch_store, fn)(*args)
    except Exception as exc:
        print(f"store skipped: {exc.__class__.__name__}: {exc}", file=sys.stderr)


def check_site(site, state, ask=ask_model, fetcher=None):
    """Check one site. Returns a short status string; raises if the fetch fails so a caller can retry."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    status = _check_site(site, state, ask, fetcher or pick_fetcher(site, state))
    _log(state, "checks.jsonl", {"when": _now(), "site": site["name"], "url": site["url"], "status": status})
    _store("record_check", site, status)
    return status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_site(site, state, ask, fetcher):
    snap = state / (_key(site) + ".json")
    new = paragraphs(fetcher(site["url"]))
    mode = _mode(site, state)  # read after the fetch: an "auto" page may have just moved to the browser
    old = _load_snapshot(snap, site, state) if snap.exists() else None
    if old is None or old["mode"] != mode:
        # Never diff two different renderings of a page: a fetch-path change starts the page over.
        snap.write_text(json.dumps({"mode": mode, "paragraphs": new}))
        why = "" if old is None else f"; fetch path changed from {old['mode']}, old snapshot dropped"
        return f"first snapshot ({len(new)} paragraphs, {mode}{why})"
    added, removed = changed_paragraphs(old["paragraphs"], new)
    if not added and not removed:
        return f"unchanged ({len(new)} paragraphs), model not called"
    try:
        note = verify(ask(site["name"], added, removed), added)
    except Exception as exc:  # model down: still record the raw change
        note = {"summary": f"model unavailable ({exc.__class__.__name__})", "quote": "", "verified": False}
    entry = {"when": _now(), "site": site["name"], "url": site["url"],
             "added": added[:10], "removed": removed[:10], **note}
    _log(state, "changes.jsonl", entry)
    _store("record_change", entry)
    snap.write_text(json.dumps({"mode": mode, "paragraphs": new}))
    return "changed, " + ("verified" if note["verified"] else "UNVERIFIED")


def check(config, state, ask=ask_model, fetcher=None):
    return [(site["name"], check_site(site, state, ask, fetcher)) for site in json.loads(Path(config).read_text())]


def peek(config, state, n=4):
    """What the watcher holds for each page: fetch path, paragraph count, the first and last lines."""
    state = Path(state)
    out = []
    for site in json.loads(Path(config).read_text()):
        snap = state / (_key(site) + ".json")
        out.append(f"## {site['name']}")
        if not snap.exists():
            out += ["  no snapshot yet", ""]
            continue
        data = _load_snapshot(snap, site, state)
        paras = data["paragraphs"]
        out.append(f"  {'real browser' if data['mode'] == 'browser' else 'plain fetch'}, {len(paras)} paragraphs")
        shown = paras if len(paras) <= 2 * n else paras[:n] + ["..."] + paras[-n:]
        out += [f"  | {p[:110]}" for p in shown] + [""]
    return "\n".join(out)


def _rows(state, name, cutoff):
    log = Path(state) / name
    rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return [r for r in rows if datetime.fromisoformat(r["when"]) >= cutoff]


def effort_line(state, days=7):
    """How lazy the week was: checks made, times the model actually ran."""
    checks = _rows(state, "checks.jsonl", datetime.now(timezone.utc) - timedelta(days=days))
    woke = sum(1 for c in checks if c["status"].startswith("changed"))
    return f"{len(checks)} page checks in {days} days, model woken {woke} times."


def digest(state, days=7):
    rows = _rows(state, "changes.jsonl", datetime.now(timezone.utc) - timedelta(days=days))
    if not rows:
        return f"# Weekly rules digest\n\n{effort_line(state, days)} Nothing changed. Nothing to read.\n"
    out = ["# Weekly rules digest", "", effort_line(state, days), ""]
    for r in rows:
        out += [f"## {r['site']} ({r['when'][:10]})", r["url"], ""]
        if r["verified"]:
            out += [r["summary"], "", f"> {r['quote']}", ""]
        else:
            out += ["Summary NOT verified against the page text. Raw changes:", ""]
            out += [f"- added: {p}" for p in r["added"]] + [f"- removed: {p}" for p in r["removed"]] + [""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "digest", "peek"])
    ap.add_argument("--config", default="sites.json")
    ap.add_argument("--state", default="state")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    state = Path(a.state)
    if a.cmd == "check":
        for name, status in check(a.config, state):
            print(f"{name}: {status}")
    elif a.cmd == "peek":
        print(peek(a.config, state))
    else:
        print(digest(state, a.days))


if __name__ == "__main__":
    sys.exit(main())
