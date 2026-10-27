"""The Uptime Ghost: lazywatch for status pages, with a sense of humour.

Every hour it reads the official status feed of each service in statuspages.json.
When a new incident appears, Gemma 3 4B writes a short ode (or a ballad, a hymn, a
limerick, a sea shanty...) that pokes gentle fun at the outage, and the ghost posts it
to Bluesky with one sentence quoted word for word from the status page. When the
incident clears, the ghost posts a short resurrection notice. Scheduled maintenance is
ignored: nobody died.

House rules, enforced in code rather than left to the model: playful, never mean;
companies and products only, never people; every post quotes the status page verbatim;
at most MAX_POSTS_PER_DAY posts a day; the ghost never replies, follows or messages
anyone (there is simply no code for it).

    python lazywatch_ghost.py --dry-run          # read the feeds, write the poems, post nothing
    python lazywatch_ghost.py                    # post (needs BLUESKY_HANDLE and BLUESKY_APP_PASSWORD)
    python lazywatch_ghost.py --only GitHub      # one service
    python lazywatch_ghost.py graveyard          # what the ghost has seen so far
    python lazywatch_ghost.py rehearse           # verse for each service's LAST incident, posted nowhere

Optional: BROWSERBASE_API_KEY puts a screenshot of the status page on each post's link
card; TIGER_DATABASE_URL keeps every haunting in the Tiger Data memory (lazywatch_store.py).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import lazywatch

MAX_POSTS_PER_DAY = 12
MAX_ATTEMPTS = 6            # hourly runs we keep trying to write a poem for one incident before giving up
POST_LIMIT = 295            # Bluesky allows 300 graphemes; keep a margin for emoji
BSKY = "https://bsky.social/xrpc"
ACTIVE = {"investigating", "identified", "monitoring"}
FORMS = [("ode", "An ode to"), ("ballad", "A ballad for"), ("hymn", "A hymn for"),
         ("limerick", "A limerick about"), ("proclamation", "A proclamation concerning"),
         ("haiku", "A haiku for"), ("sea shanty", "A sea shanty for"), ("eulogy", "A eulogy for"),
         ("lullaby", "A lullaby for"), ("toast", "A toast to")]
# A poem that trips this is thrown away; the model gets another go next hour.
MEAN = re.compile(r"\b(idiots?|stupid|incompetent|useless|pathetic|garbage|trash|sucks?|worst|fired?|lazy|dumb|"
                  r"clowns?|amateurs?|shame on|blame)\b", re.I)


# ---------------------------------------------------------------- the feeds
def fetch_feed(site):
    with urllib.request.urlopen(urllib.request.Request(site["url"], headers=lazywatch.UA), timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _text(s):
    """Plain words, the way the page shows them: no HTML tags, no Markdown markers, one space between words."""
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = re.sub(r"(^|\s)#{1,6}\s+", r"\1", s)
    s = re.sub(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1", r"\2", s)
    return re.sub(r"\s+", " ", s).strip()


def _latest(items, body_key, date_key):
    items = [i for i in items or [] if isinstance(i, dict)]
    if not items:
        return ""
    dated = [i for i in items if i.get(date_key)]
    best = max(dated, key=lambda i: str(i[date_key])) if dated else items[0]
    return best.get(body_key, "")


def _incident(site, ident, title, impact, status, url, update, since, affected):
    return {"service": site["name"], "id": f"{site['name']}:{ident}", "title": _text(title), "impact": str(impact or ""),
            "status": str(status or ""), "url": url or site["page"], "update": _text(update), "since": str(since or ""),
            "affected": [_text(a) for a in affected if a]}


def _statuspage(site, data):
    """Atlassian Statuspage summary.json: the 'incidents' list holds only unresolved incidents;
    scheduled maintenance lives under another key that is never read."""
    out = []
    for inc in data.get("incidents") or []:
        if inc.get("status") not in ACTIVE:
            continue
        out.append(_incident(site, inc.get("id"), inc.get("name"), inc.get("impact", "none"), inc.get("status"),
                             inc.get("shortlink"), _latest(inc.get("incident_updates"), "body", "created_at"),
                             inc.get("created_at"), [c.get("name") for c in inc.get("components") or []]))
    return out


def _slack(site, data):
    out = []
    for inc in data.get("active_incidents") or []:
        if inc.get("type") == "notice":            # Slack files scheduled work as notices
            continue
        services = [s.get("name") if isinstance(s, dict) else s for s in inc.get("services") or []]
        out.append(_incident(site, inc.get("id"), inc.get("title"), inc.get("type", "incident"), inc.get("status"),
                             inc.get("url"), _latest(inc.get("notes"), "body", "date_created"),
                             inc.get("date_created"), services))
    return out


def _google(site, data):
    out = []
    for inc in data if isinstance(data, list) else []:
        if inc.get("end") or inc.get("status_impact") == "SERVICE_INFORMATION":
            continue
        uri = inc.get("uri") or ""
        url = uri if uri.startswith("http") else site["page"].rstrip("/") + "/" + uri.lstrip("/")
        out.append(_incident(site, inc.get("id"), inc.get("external_desc"),
                             str(inc.get("status_impact", "")).lower().replace("service_", ""), "open", url,
                             (inc.get("most_recent_update") or {}).get("text"), inc.get("begin"),
                             [inc.get("service_name")]))
    return out


PARSERS = {"statuspage": _statuspage, "slack": _slack, "google": _google}


def open_incidents(site, data):
    return PARSERS[site.get("format", "statuspage")](site, data)


# ---------------------------------------------------------------- the ghost's verse
def pick_form(incident_id):
    return FORMS[int(hashlib.sha1(incident_id.encode()).hexdigest(), 16) % len(FORMS)][0]


def ghost_prompt(incident, form):
    affected = ", ".join(incident["affected"][:4])
    return (
        "You are The Uptime Ghost, a theatrical but kind spirit who haunts status pages and writes verse "
        "when a service goes down. You tease the outage, never the people fixing it.\n"
        f"SERVICE: {incident['service']}\n"
        f"INCIDENT: {incident['title']}\n"
        + (f"AFFECTED: {affected}\n" if affected else "")
        + f"IMPACT: {incident['impact']}\n"
        f"LATEST UPDATE FROM THE STATUS PAGE: {incident['update'] or incident['title']}\n\n"
        f"Write a {form} about this outage: at most 4 short lines, under 160 characters in total, "
        f"naming {incident['service']}. Playful and affectionate, never cruel; no swearing, no insults, "
        "no real people's names, no blame; end with a wink of hope that it comes back.\n"
        "Then copy ONE sentence word for word from the latest update (do not change a single character).\n"
        "Reply with JSON only: {\"poem\": \"line one\\nline two\\nline three\", \"quote\": \"the exact sentence\"}"
    )


def ask_ghost(incident, form, model=lazywatch.MODEL, url=lazywatch.OLLAMA_URL):
    body = json.dumps({"model": model, "prompt": ghost_prompt(incident, form), "stream": False, "format": "json",
                       "options": {"temperature": 0.9}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.loads(r.read())["response"])


def clean_poem(reply):
    poem = str(reply.get("poem", "")) if isinstance(reply, dict) else ""
    lines = [l.strip().strip('"“”') for l in poem.replace("\\n", "\n").splitlines()]
    lines = [re.sub(r"\s+", " ", l) for l in lines if l][:4]
    return "\n".join(lines)


def _plain(text):
    """For the comparison only: straight quotes, one case, no trailing full stop. The words must still match exactly."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return _text(text).casefold().rstrip(".!")


def verify_quote(reply, incident):
    """The quote must appear word for word on the status page; otherwise the incident's own title
    (also lifted straight from the page) stands in, and the post is marked unverified."""
    quote = _text(reply.get("quote", "")).strip('"\u201c\u201d') if isinstance(reply, dict) else ""
    if len(quote) >= 12 and (_plain(quote) in _plain(incident["update"]) or _plain(quote) == _plain(incident["title"])):
        return quote, True
    return incident["title"], False


def too_mean(poem):
    return bool(MEAN.search(poem))


def _length(text):
    return len(unicodedata.normalize("NFC", text))


def _clock(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M")
    except ValueError:
        return ""


def _shorten(text, room):
    if _length(text) <= room:
        return text
    cut = text[:max(room - 1, 1)]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:.") + "…"


def compose(incident, form, poem, quote):
    header = dict(FORMS)[form]
    since = _clock(incident["since"])
    top = f"👻 {header} {incident['service']}" + (f", down since {since} UTC" if since else "")
    lines = [l for l in poem.splitlines() if l.strip()]
    text = ""
    while True:
        text = "\n\n".join([top, "\n".join(lines), f"“{quote}”"])
        if _length(text) <= POST_LIMIT or len(lines) <= 1:
            break
        lines = lines[:-1]
    if _length(text) > POST_LIMIT:              # last resort: a shorter prefix of the quote is still verbatim
        room = POST_LIMIT - _length("\n\n".join([top, "\n".join(lines), "“”"]))
        text = "\n\n".join([top, "\n".join(lines), f"“{_shorten(quote, max(room, 12))}”"])
    return text


def _duration(start_iso, end):
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return ""
    minutes = max(int((end - start).total_seconds() // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} day" + ("s" if days != 1 else "")] if days else []
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def resurrection_text(service, title, duration):
    tail = f" haunted the status page for {duration}." if duration else " has left the status page."
    text = f"👻 {service} has risen. “{title}”{tail} Lights back on, candles out. 🕯️"
    if _length(text) > POST_LIMIT:
        room = POST_LIMIT - _length(text) + _length(title)
        text = f"👻 {service} has risen. “{_shorten(title, max(room, 12))}”{tail} Lights back on, candles out. 🕯️"
    return text


# ---------------------------------------------------------------- Bluesky
def _xrpc(nsid, body, jwt=None, content_type="application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": content_type}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    req = urllib.request.Request(f"{BSKY}/{nsid}", data, headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def bsky_login(handle=None, password=None):
    handle = handle or os.environ["BLUESKY_HANDLE"]
    password = password or os.environ["BLUESKY_APP_PASSWORD"]
    r = _xrpc("com.atproto.server.createSession", {"identifier": handle, "password": password})
    return {"jwt": r["accessJwt"], "did": r["did"]}


def link_card(incident, thumb=None):
    """The post's link card: the status page itself, titled with the incident's own words."""
    card = {"uri": incident["url"], "title": (incident["title"] or incident["service"])[:200],
            "description": f"{incident['service']} status page, as quoted by The Uptime Ghost."}
    if thumb:
        card["thumb"] = thumb
    return {"$type": "app.bsky.embed.external", "external": card}


def bsky_post(session, text, embed=None, now=None):
    created = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created, "langs": ["en"]}
    if embed:
        record["embed"] = embed
    r = _xrpc("com.atproto.repo.createRecord",
              {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}, session["jwt"])
    return r["uri"]


def bsky_upload(session, data, mime="image/jpeg"):
    return _xrpc("com.atproto.repo.uploadBlob", data, session["jwt"], mime)["blob"]


class BlueskyPoster:
    """Posts for real. Logs in once, on the first post."""
    def __init__(self):
        self.session = None

    def __call__(self, text, incident=None, image=None):
        self.session = self.session or bsky_login()
        thumb = bsky_upload(self.session, image) if image else None
        return bsky_post(self.session, text, link_card(incident, thumb) if incident else None)


def print_poster(text, incident=None, image=None):
    """Dry run: show the post, send nothing."""
    print("--- would post" + (f" (card: {incident['url']}" + (", with screenshot)" if image else ")") if incident else ""))
    print(text)
    return None


def screenshot(url):
    """A JPEG of the status page for the link card; only when Browserbase is configured, never fatal."""
    if not os.environ.get("BROWSERBASE_API_KEY"):
        return None
    try:
        import lazywatch_browser
        return lazywatch_browser.screenshot(url)
    except Exception as exc:
        print(f"screenshot skipped: {exc.__class__.__name__}: {str(exc)[:80]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- state and the hourly pass
def _state_file(state):
    return Path(state) / "ghost" / "open.json"


def load_state(state):
    f = _state_file(state)
    return json.loads(f.read_text()) if f.exists() else {}


def save_state(state, data):
    f = _state_file(state)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1))


def _log(state, row):
    f = Path(state) / "ghost" / "posts.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def posts(state):
    f = Path(state) / "ghost" / "posts.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def posted_today(state, now):
    day = now.strftime("%Y-%m-%d")
    return sum(1 for p in posts(state) if p.get("when", "").startswith(day) and p.get("uri"))


def haunt(sites, state, ask=ask_ghost, poster=None, fetch=fetch_feed, shooter=screenshot, now=None, dry_run=False):
    """One pass over every status feed: new incidents get verse, cleared incidents get a resurrection notice.
    With dry_run nothing is posted and nothing is written; the posts are printed instead."""
    now = now or datetime.now(timezone.utc)
    poster = poster or (print_poster if dry_run else BlueskyPoster())
    seen = load_state(state)
    budget = MAX_POSTS_PER_DAY - posted_today(state, now)
    report = {"checked": 0, "failed": [], "open": 0, "posted": 0, "skipped": 0}

    for site in sites:
        name = site["name"]
        try:
            current = {i["id"]: i for i in open_incidents(site, fetch(site))}
        except Exception as exc:
            report["failed"].append(f"{name} ({exc.__class__.__name__})")
            if not dry_run:
                lazywatch._store("record_check", {"name": f"ghost:{name}", "url": site["url"]},
                                 f"failed ({exc.__class__.__name__})")
            continue          # a feed that failed says nothing about its incidents: leave them as they were
        report["checked"] += 1
        report["open"] += len(current)
        if not dry_run:
            lazywatch._store("record_check", {"name": f"ghost:{name}", "url": site["url"]},
                             f"{len(current)} open" if current else "ok")
        entries = seen.setdefault(name, {})

        for ident in [i for i in list(entries) if i not in current]:          # resolved: the resurrection
            entry = entries.pop(ident)
            if not entry.get("posted"):
                continue
            if budget <= 0:
                report["skipped"] += 1
                continue
            text = resurrection_text(name, entry["title"], _duration(entry.get("since") or entry["first_seen"], now))
            try:
                uri = poster(text)
            except Exception as exc:
                print(f"{name}: resurrection not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
                entries[ident] = entry
                continue
            budget -= 1
            report["posted"] += 1
            if not dry_run:
                _log(state, {"when": now.isoformat(), "kind": "resurrection", "service": name, "id": ident,
                             "title": entry["title"], "text": text, "uri": uri})

        for ident, inc in current.items():                                     # new or still unwritten: the verse
            entry = entries.setdefault(ident, {"title": inc["title"], "since": inc["since"], "url": inc["url"],
                                               "first_seen": now.isoformat(), "form": pick_form(ident),
                                               "attempts": 0, "posted": None})
            if entry.get("posted") or entry["attempts"] >= MAX_ATTEMPTS:
                continue
            if budget <= 0:
                report["skipped"] += 1
                continue
            entry["attempts"] += 1
            try:
                reply = ask(inc, entry["form"])
            except Exception as exc:
                print(f"{name}: model unavailable ({exc.__class__.__name__})", file=sys.stderr)
                continue
            poem = clean_poem(reply)
            if not poem or too_mean(poem):
                print(f"{name}: poem rejected ({'empty' if not poem else 'tone'}), will retry", file=sys.stderr)
                continue
            quote, verified = verify_quote(reply, inc)
            text = compose(inc, entry["form"], poem, quote)
            try:
                uri = poster(text, inc, shooter(inc["url"]) if shooter else None)
            except Exception as exc:
                print(f"{name}: not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
                continue
            budget -= 1
            report["posted"] += 1
            entry["posted"] = {"when": now.isoformat(), "uri": uri}
            if not dry_run:
                _log(state, {"when": now.isoformat(), "kind": "haunting", "service": name, "id": ident,
                             "form": entry["form"], "title": inc["title"], "quote": quote, "verified": verified,
                             "text": text, "uri": uri})
                lazywatch._store("record_change", {"site": f"ghost:{name}", "url": inc["url"],
                                                   "summary": f"{entry['form']} for {name}: {inc['title']}",
                                                   "quote": quote, "verified": verified, "added": [poem, inc["update"]]})
    if not dry_run:
        save_state(state, seen)
    return report


def graveyard(state):
    """A tally of the ghost's work so far, for the write-up."""
    rows = posts(state)
    hauntings = [r for r in rows if r.get("kind") == "haunting"]
    risen = [r for r in rows if r.get("kind") == "resurrection"]
    verified = sum(1 for r in hauntings if r.get("verified"))
    by_service = {}
    for r in hauntings:
        by_service[r["service"]] = by_service.get(r["service"], 0) + 1
    out = [f"{len(hauntings)} hauntings, {len(risen)} resurrections, {verified} quotes verified word for word."]
    for service, n in sorted(by_service.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"  {service}: {n}")
    still = {k: v for s in load_state(state).values() for k, v in s.items() if v.get("posted")}
    if still:
        out.append(f"Still haunted: " + ", ".join(sorted(k.split(":")[0] for k in still)))
    return "\n".join(out)


def last_incident(site, fetch=fetch_feed):
    """The most recent incident in a Statuspage history feed (incidents.json), resolved or not:
    the ghost's rehearsal material on a quiet day."""
    if site.get("format", "statuspage") != "statuspage":
        return None
    data = fetch(dict(site, url=site["url"].replace("summary.json", "incidents.json")))
    for inc in data.get("incidents") or []:
        found = _statuspage(site, {"incidents": [dict(inc, status="investigating")]})
        if found:
            return found[0]
    return None


def rehearse(sites, ask=ask_ghost, fetch=fetch_feed, out=print):
    """Dry run on yesterday's news: write verse for each service's latest incident, post nothing, touch no state."""
    written = 0
    for site in sites:
        try:
            inc = last_incident(site, fetch)
        except Exception as exc:
            out(f"{site['name']}: history unavailable ({exc.__class__.__name__})")
            continue
        if not inc:
            continue
        form = pick_form(inc["id"])
        try:
            reply = ask(inc, form)
        except Exception as exc:
            out(f"{site['name']}: model unavailable ({exc.__class__.__name__})")
            continue
        poem = clean_poem(reply)
        if not poem or too_mean(poem):
            out(f"{site['name']}: poem rejected ({'empty' if not poem else 'tone'})")
            continue
        quote, verified = verify_quote(reply, inc)
        out(f"--- {site['name']} ({form}, quote {'verified' if verified else 'NOT verified, title used'}; {inc['url']})")
        out(compose(inc, form, poem, quote))
        written += 1
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="haunt", choices=["haunt", "graveyard", "rehearse"])
    ap.add_argument("--config", default="statuspages.json")
    ap.add_argument("--state", default="state")
    ap.add_argument("--only", help="run one service by name")
    ap.add_argument("--dry-run", action="store_true", help="write the verse, post nothing, change nothing")
    args = ap.parse_args(argv)
    if args.command == "graveyard":
        print(graveyard(args.state))
        return 0
    sites = json.loads(Path(args.config).read_text())
    if args.only:
        sites = [s for s in sites if s["name"].lower() == args.only.lower()]
    if args.command == "rehearse":
        print(f"{rehearse(sites)} rehearsal posts written, none posted")
        return 0
    dry = args.dry_run or not os.environ.get("BLUESKY_APP_PASSWORD")
    if dry and not args.dry_run:
        print("BLUESKY_APP_PASSWORD not set: dry run", file=sys.stderr)
    r = haunt(sites, args.state, dry_run=dry)
    print(f"{r['checked']} feeds read, {r['open']} incidents open, {r['posted']} posts"
          + (f", {r['skipped']} held back by the daily cap" if r["skipped"] else "")
          + (f"; failed: {', '.join(r['failed'])}" if r["failed"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
