"""The Uptime Ghost: lazywatch for status pages, with a sense of humour.

Every hour it reads the official status feed of each service in statuspages.json.
When a new incident appears, Gemma 3 4B writes a short ode (or a ballad, a hymn, a
limerick, a sea shanty...) that pokes gentle fun at the outage. The ghost posts it to
Bluesky on an image card, with the date, one sentence quoted word for word from the
status page and a link to the source; a long poem continues in replies to its own post.
When the incident clears, a short resurrection notice follows. Scheduled maintenance is
ignored: nobody died. On Thursdays the ghost visits three graves of the old web through
the Wayback Machine (throwback.json) and writes each an elegy from what the page used to say.

House rules, enforced in code rather than left to the model: playful, never mean;
companies and products only, never people; every post quotes its source verbatim;
at most MAX_POSTS_PER_DAY posts a day; the ghost never replies to anyone else, never
follows and never messages (there is simply no code for it).

    python lazywatch_ghost.py --dry-run          # read the feeds, write the verse, post nothing
    python lazywatch_ghost.py                    # post (needs BLUESKY_HANDLE and BLUESKY_APP_PASSWORD)
    python lazywatch_ghost.py --only GitHub      # one service
    python lazywatch_ghost.py rehearse --out out # verse and cards for each service's LAST incident, posted nowhere
    python lazywatch_ghost.py throwback --dry-run
    python lazywatch_ghost.py graveyard          # what the ghost has seen so far

Optional: BROWSERBASE_API_KEY adds a screenshot of the status page as a second image;
TIGER_DATABASE_URL keeps every haunting in the Tiger Data memory (lazywatch_store.py).
Cards need Playwright with Chromium (ghost_card.py); without it the posts go out as text.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import lazywatch

MAX_POSTS_PER_DAY = 12
MAX_ATTEMPTS = 6            # hourly runs we keep trying to write a poem for one incident before giving up
POST_LIMIT = 295            # Bluesky allows 300 graphemes; keep a margin for emoji
MAX_LINES = 16
BSKY = "https://bsky.social/xrpc"
WAYBACK = "https://archive.org/wayback/available"
ACTIVE = {"investigating", "identified", "monitoring"}
# form, headline, and how long it usually runs (the model may bend it when it makes sense)
FORMS = [("ode", "An ode to", "6 to 8 short lines"),
         ("ballad", "A ballad for", "8 to 12 short lines in two or three stanzas"),
         ("hymn", "A hymn for", "6 to 8 short lines"),
         ("limerick", "A limerick about", "exactly 5 lines, rhyming AABBA"),
         ("proclamation", "A proclamation concerning", "4 to 6 lines in the voice of a royal decree"),
         ("haiku", "A haiku for", "exactly 3 lines of 5, 7 and 5 syllables"),
         ("sea shanty", "A sea shanty for", "8 to 12 short lines with a repeated refrain"),
         ("eulogy", "A eulogy for", "6 to 8 short lines"),
         ("lullaby", "A lullaby for", "6 to 8 short lines"),
         ("toast", "A toast to", "2 to 4 lines, glass raised"),
         ("couplet", "A couplet for", "exactly 2 rhyming lines"),
         ("epic", "An epic for", "14 to 16 short lines in four stanzas")]
HEADLINE = {f: h for f, h, _ in FORMS}
LENGTH = {f: n for f, _, n in FORMS}
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
        f"Write a {form} about this outage: {LENGTH[form]}, one line per line, naming {incident['service']}. "
        "Playful and affectionate, never cruel; no swearing, no insults, no real people's names, no blame; "
        "end with a wink of hope that it comes back.\n"
        "Then copy ONE sentence word for word from the latest update (do not change a single character).\n"
        "Reply with JSON only: {\"poem\": \"line one\\nline two\\nline three\", \"quote\": \"the exact sentence\"}"
    )


def ask_model(prompt, model=lazywatch.MODEL, url=lazywatch.OLLAMA_URL):
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0.9}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.loads(r.read())["response"])


def ask_ghost(incident, form, model=lazywatch.MODEL, url=lazywatch.OLLAMA_URL):
    return ask_model(ghost_prompt(incident, form), model, url)


def clean_poem(reply):
    poem = str(reply.get("poem", "")) if isinstance(reply, dict) else ""
    if isinstance(reply, dict) and isinstance(reply.get("poem"), list):
        poem = "\n".join(str(x) for x in reply["poem"])
    lines = [l.strip().strip('"“”') for l in poem.replace("\\n", "\n").splitlines()]
    lines = [re.sub(r"\s+", " ", l) for l in lines if l][:MAX_LINES]
    return "\n".join(lines)


def _plain(text):
    """For the comparison only: straight quotes, one case, no trailing full stop. The words must still match exactly."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return _text(text).casefold().rstrip(".!")


def verify_quote(reply, source_text, fallback):
    """The quote must appear word for word in the source; otherwise `fallback` (also lifted straight
    from the source) stands in, and the post is marked unverified."""
    quote = _text(reply.get("quote", "")).strip('"“”') if isinstance(reply, dict) else ""
    if len(quote) >= 12 and (_plain(quote) in _plain(source_text) or _plain(quote) == _plain(fallback)):
        return quote, True
    return fallback, False


def too_mean(poem):
    return bool(MEAN.search(poem))


def _length(text):
    return len(unicodedata.normalize("NFC", text))


def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _when(iso):
    """'Oct 27, 2026, 14:02 UTC' from any ISO timestamp the feeds use."""
    d = _parse(iso)
    return f"{d.strftime('%b')} {d.day}, {d.year}, {d.strftime('%H:%M')} UTC" if d else ""


def _host(url):
    return urllib.parse.urlsplit(url).netloc or url


def _shorten(text, room):
    if _length(text) <= room:
        return text
    cut = text[:max(room - 1, 1)]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:.") + "…"


def _duration(start_iso, end):
    start = _parse(start_iso)
    if not start:
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


# ---------------------------------------------------------------- posts and threads
def link_facet(text, display, url):
    """A clickable `display` inside `text`, as Bluesky wants it: UTF-8 byte offsets."""
    start = text.rfind(display)
    if start < 0:
        return []
    b0 = len(text[:start].encode("utf-8"))
    b1 = b0 + len(display.encode("utf-8"))
    return [{"index": {"byteStart": b0, "byteEnd": b1},
             "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}]


def _post(text, link=None, images=None):
    display, url = link if link else (None, None)
    return {"text": text, "facets": link_facet(text, display, url) if link else [], "images": images or []}


def thread(header, lines, quote, link, images=None):
    """Split header, verse, quote and source link over as few posts as possible, each under the limit.
    The first post always carries the source link and the images; the quote comes last."""
    display, _url = link
    source = f"Source: {display}"
    posts, chunk = [], []
    pending = list(lines)
    first = True

    def body(chunk, with_header, with_source, with_quote):
        parts = ([header] if with_header else []) + (["\n".join(chunk)] if chunk else [])
        if with_quote:
            parts.append(f"“{quote}”")
        if with_source:
            parts.append(source)
        return "\n\n".join(parts)

    while pending:
        line = pending[0]
        trial = chunk + [line]
        if _length(body(trial, first, first, False)) <= POST_LIMIT:
            chunk = trial
            pending.pop(0)
            continue
        if not chunk:                                   # one line longer than a post: cut it, keep going
            chunk = [_shorten(line, POST_LIMIT - _length(body([], first, first, False)) - 4)]
            pending.pop(0)
        posts.append((chunk, first))
        chunk, first = [], False
    # the quote: on the last chunk if it fits, else on a post of its own
    if _length(body(chunk, first, first, True)) <= POST_LIMIT:
        posts.append((chunk, first, True))
    else:
        posts.append((chunk, first, False))
        posts.append(([], False, True))
    out = []
    for n, item in enumerate(posts):
        chunk, with_header = item[0], item[1]
        with_quote = len(item) > 2 and item[2]
        text = body(chunk, with_header, with_header, with_quote)
        if with_quote and _length(text) > POST_LIMIT:   # a very long quote: a shorter prefix is still verbatim
            room = POST_LIMIT - _length(body(chunk, with_header, with_header, False)) - 6
            text = body(chunk, with_header, with_header, False) + f"\n\n“{_shorten(quote, max(room, 12))}”"
        if not text.strip():
            continue
        out.append(_post(text, link if with_header else None, images if with_header else None))
    return out


def haunting_thread(incident, form, poem, quote, images=None):
    header = f"\U0001F47B {HEADLINE[form]} {incident['service']}"
    since = _when(incident["since"])
    if since:
        header += f", down since {since}"
    lines = [l for l in poem.splitlines() if l.strip()]
    return thread(header, lines, quote, (_host(incident["url"]), incident["url"]), images)


def resurrection_thread(service, entry, now, images=None):
    since = _when(entry.get("since") or entry["first_seen"])
    lasted = _duration(entry.get("since") or entry["first_seen"], now)
    title = entry["title"]
    text = (f"\U0001F47B {service} has risen. “{title}” haunted the status page for {lasted}"
            + (f" (since {since})" if since else "") + ". Lights back on, candles out. \U0001F56F️")
    link = (_host(entry["url"]), entry["url"])
    tail = f"\n\nSource: {link[0]}"
    if _length(text + tail) > POST_LIMIT:
        room = len(title) - (_length(text + tail) - POST_LIMIT)
        text = text.replace(f"“{title}”", f"“{_shorten(title, max(room, 12))}”")
    return [_post(text + tail, link, images)]


# ---------------------------------------------------------------- cards and screenshots
def card_spec(kind, incident=None, form=None, poem="", quote="", entry=None, service=None, now=None, site=None):
    if kind == "haunting":
        tag = f"The Uptime Ghost · {incident['service']}"
        since = _when(incident["since"])
        return {"kind": kind, "tag": tag + (f" · down since {since}" if since else ""),
                "headline": f"{HEADLINE[form]} {incident['service']}", "sub": incident["title"],
                "poem": poem, "quote": quote, "source": f"Source: {_host(incident['url'])}"}
    if kind == "risen":
        lasted = _duration(entry.get("since") or entry["first_seen"], now)
        return {"kind": kind, "tag": f"The Uptime Ghost · {service} · risen {_when(now.isoformat())}",
                "headline": f"{service} has risen", "sub": entry["title"],
                "poem": f"Haunted the status page for {lasted}.\nLights back on, candles out.",
                "quote": "", "source": f"Source: {_host(entry['url'])}"}
    return {"kind": "throwback", "tag": f"Throwback Thursday · from the Wayback Machine · {site['born']} to {site['died']}",
            "headline": f"In memoriam: {site['name']}", "sub": site.get("seen", ""),
            "poem": poem, "quote": quote, "source": "Source: web.archive.org"}


def render_card(spec, out_dir=None, name=None):
    """The image card, or None when Playwright is missing; never fatal."""
    try:
        import ghost_card
        data = ghost_card.render(spec)
        alt = ghost_card.alt_text(spec)
    except Exception as exc:
        print(f"card skipped: {exc.__class__.__name__}: {str(exc)[:100]}", file=sys.stderr)
        return None
    if out_dir and name:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / f"{name}.jpg").write_bytes(data)
    return {"data": data, "alt": alt, "mime": "image/jpeg"}


def screenshot(url, service):
    """A JPEG of the status page as a second image; only when Browserbase is configured, never fatal."""
    if not os.environ.get("BROWSERBASE_API_KEY"):
        return None
    try:
        import lazywatch_browser
        return {"data": lazywatch_browser.screenshot(url), "mime": "image/jpeg",
                "alt": f"Screenshot of the {service} status page at {_when(datetime.now(timezone.utc).isoformat())}."}
    except Exception as exc:
        print(f"screenshot skipped: {exc.__class__.__name__}: {str(exc)[:80]}", file=sys.stderr)
        return None


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


def bsky_upload(session, data, mime="image/jpeg"):
    return _xrpc("com.atproto.repo.uploadBlob", data, session["jwt"], mime)["blob"]


def bsky_post(session, post, reply_to=None, now=None):
    created = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    record = {"$type": "app.bsky.feed.post", "text": post["text"], "createdAt": created, "langs": ["en"]}
    if post.get("facets"):
        record["facets"] = post["facets"]
    if post.get("images"):
        record["embed"] = {"$type": "app.bsky.embed.images", "images": [
            {"alt": img["alt"], "image": bsky_upload(session, img["data"], img.get("mime", "image/jpeg"))}
            for img in post["images"][:4]]}
    if reply_to:
        record["reply"] = reply_to
    r = _xrpc("com.atproto.repo.createRecord",
              {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}, session["jwt"])
    return {"uri": r["uri"], "cid": r["cid"]}


class BlueskyPoster:
    """Posts a thread for real: the first post, then each next one as a reply to the previous. Logs in once."""
    def __init__(self):
        self.session = None

    def __call__(self, posts):
        self.session = self.session or bsky_login()
        root = parent = None
        uris = []
        for post in posts:
            ref = bsky_post(self.session, post, {"root": root, "parent": parent} if root else None)
            root = root or ref
            parent = ref
            uris.append(ref["uri"])
        return uris


def print_poster(posts):
    """Dry run: show the thread, send nothing."""
    for n, post in enumerate(posts, 1):
        extras = [f"link → {f['features'][0]['uri']}" for f in post.get("facets", [])]
        extras += [f"image: {img['alt'][:70]}…" for img in post.get("images", [])]
        print(f"--- would post {n}/{len(posts)}" + (" (" + "; ".join(extras) + ")" if extras else ""))
        print(post["text"])
    return [None] * len(posts)


# ---------------------------------------------------------------- state and the hourly pass
def _state_file(state, name="open.json"):
    return Path(state) / "ghost" / name


def load_state(state, name="open.json", default=None):
    f = _state_file(state, name)
    return json.loads(f.read_text()) if f.exists() else ({} if default is None else default)


def save_state(state, data, name="open.json"):
    f = _state_file(state, name)
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


def haunt(sites, state, ask=ask_ghost, poster=None, fetch=fetch_feed, shooter=screenshot, cards=render_card,
          now=None, dry_run=False, out_dir=None):
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
            card = cards(card_spec("risen", entry=entry, service=name, now=now), out_dir, f"risen-{_slug(ident)}") if cards else None
            posts_ = resurrection_thread(name, entry, now, [card] if card else None)
            try:
                uris = poster(posts_)
            except Exception as exc:
                print(f"{name}: resurrection not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
                entries[ident] = entry
                continue
            budget -= 1
            report["posted"] += 1
            if not dry_run:
                _log(state, {"when": now.isoformat(), "kind": "resurrection", "service": name, "id": ident,
                             "title": entry["title"], "text": posts_[0]["text"], "uri": uris[0]})

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
            quote, verified = verify_quote(reply, inc["update"], inc["title"])
            images = []
            card = cards(card_spec("haunting", inc, entry["form"], poem, quote), out_dir, _slug(ident)) if cards else None
            if card:
                images.append(card)
            shot = shooter(inc["url"], name) if shooter else None
            if shot:
                images.append(shot)
            posts_ = haunting_thread(inc, entry["form"], poem, quote, images)
            try:
                uris = poster(posts_)
            except Exception as exc:
                print(f"{name}: not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
                continue
            budget -= 1
            report["posted"] += 1
            entry["posted"] = {"when": now.isoformat(), "uri": uris[0]}
            if not dry_run:
                _log(state, {"when": now.isoformat(), "kind": "haunting", "service": name, "id": ident,
                             "form": entry["form"], "title": inc["title"], "quote": quote, "verified": verified,
                             "lines": len(poem.splitlines()), "posts": len(posts_), "text": posts_[0]["text"],
                             "uri": uris[0]})
                lazywatch._store("record_change", {"site": f"ghost:{name}", "url": inc["url"],
                                                   "summary": f"{entry['form']} for {name}: {inc['title']}",
                                                   "quote": quote, "verified": verified, "added": [poem, inc["update"]]})
    if not dry_run:
        save_state(state, seen)
    return report


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ---------------------------------------------------------------- Throwback Thursday
def wayback(site, fetch_json=None, fetch_html=None):
    """The closest saved copy of a dead site's home page: (snapshot url for people, page text paragraphs)."""
    fetch_json = fetch_json or (lambda url: json.loads(lazywatch.fetch(url)))
    fetch_html = fetch_html or lazywatch.fetch
    q = urllib.parse.urlencode({"url": site["url"], "timestamp": site.get("when", "")})
    closest = (fetch_json(f"{WAYBACK}?{q}").get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available"):
        raise LookupError("no snapshot")
    url = closest["url"].replace("http://web.archive.org/", "https://web.archive.org/")
    raw = re.sub(r"(/web/\d{14})/", r"\1id_/", url, count=1)           # id_ = the page as it was, no archive toolbar
    paragraphs = lazywatch.paragraphs(fetch_html(raw))
    stamp = closest.get("timestamp", "")
    seen = f"Last seen {datetime.strptime(stamp[:8], '%Y%m%d').strftime('%B %-d, %Y')}" if len(stamp) >= 8 else ""
    return url, [p for p in paragraphs if len(p) >= 30][:40], seen


def throwback_prompt(site, paragraphs):
    text = "\n".join(f"- {p[:300]}" for p in paragraphs[:25])
    return (
        "You are The Uptime Ghost, a theatrical but kind spirit. Tonight is Throwback Thursday and you are "
        f"visiting the grave of {site['name']} ({site['born']} to {site['died']}): {site.get('note', '')}.\n"
        f"WHAT ITS ARCHIVED HOME PAGE SAID:\n{text}\n\n"
        f"Write an elegy for {site['name']}: 6 to 10 short lines, one line per line, fond and a little funny, "
        "about what the site was and what the web lost; no real people's names, no blame, no sadness that lingers; "
        "end on a warm note.\n"
        "Then copy ONE sentence or phrase word for word from the archived text above (do not change a single character).\n"
        "Reply with JSON only: {\"poem\": \"line one\\nline two\", \"quote\": \"the exact words\"}"
    )


def throwback(sites, state, ask=ask_model, poster=None, lookup=wayback, cards=render_card, now=None, dry_run=False,
              count=3, out_dir=None):
    """Three graves a Thursday: the next unvisited sites in throwback.json, each with an elegy from its last words."""
    now = now or datetime.now(timezone.utc)
    poster = poster or (print_poster if dry_run else BlueskyPoster())
    done = load_state(state, "throwback.json", [])
    budget = MAX_POSTS_PER_DAY - posted_today(state, now)
    visited = 0
    for site in [s for s in sites if s["name"] not in done]:
        if visited >= count or budget <= 0:
            break
        try:
            url, paragraphs, seen = lookup(site)
        except Exception as exc:
            print(f"{site['name']}: grave not found ({exc.__class__.__name__})", file=sys.stderr)
            continue
        if not paragraphs:
            print(f"{site['name']}: the saved page has no words", file=sys.stderr)
            continue
        try:
            reply = ask(throwback_prompt(site, paragraphs))
        except Exception as exc:
            print(f"{site['name']}: model unavailable ({exc.__class__.__name__})", file=sys.stderr)
            break
        poem = clean_poem(reply)
        if not poem or too_mean(poem):
            print(f"{site['name']}: elegy rejected", file=sys.stderr)
            continue
        quote, verified = verify_quote(reply, " ".join(paragraphs), site["name"])
        spec = card_spec("throwback", poem=poem, quote=quote, site=dict(site, seen=seen))
        card = cards(spec, out_dir, f"throwback-{_slug(site['name'])}") if cards else None
        header = f"\U0001F56F️ Throwback Thursday: in memoriam {site['name']} ({site['born']} to {site['died']})"
        posts_ = thread(header, [l for l in poem.splitlines() if l.strip()], quote, ("web.archive.org", url),
                        [card] if card else None)
        try:
            uris = poster(posts_)
        except Exception as exc:
            print(f"{site['name']}: not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
            continue
        visited += 1
        budget -= 1
        if not dry_run:
            done.append(site["name"])
            save_state(state, done, "throwback.json")
            _log(state, {"when": now.isoformat(), "kind": "throwback", "service": site["name"], "id": f"throwback:{site['name']}",
                         "form": "elegy", "title": site.get("note", ""), "quote": quote, "verified": verified,
                         "lines": len(poem.splitlines()), "posts": len(posts_), "text": posts_[0]["text"], "uri": uris[0]})
            lazywatch._store("record_change", {"site": f"ghost:throwback", "url": url, "summary": f"elegy for {site['name']}",
                                               "quote": quote, "verified": verified, "added": [poem] + paragraphs[:10]})
    return visited


# ---------------------------------------------------------------- rehearsal and tally
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


def rehearse(sites, ask=ask_ghost, fetch=fetch_feed, cards=render_card, out=print, out_dir=None):
    """Dry run on yesterday's news: verse and a card for each service's latest incident, posted nowhere."""
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
        quote, verified = verify_quote(reply, inc["update"], inc["title"])
        card = cards(card_spec("haunting", inc, form, poem, quote), out_dir, _slug(inc["id"])) if cards else None
        posts_ = haunting_thread(inc, form, poem, quote, [card] if card else None)
        out(f"--- {site['name']} ({form}, {len(poem.splitlines())} lines, {len(posts_)} post{'s' if len(posts_) > 1 else ''}, "
            f"quote {'verified' if verified else 'NOT verified, title used'}; {inc['url']})")
        for p in posts_:
            out(p["text"])
            out("")
        written += 1
    return written


def graveyard(state):
    """A tally of the ghost's work so far, for the write-up."""
    rows = posts(state)
    hauntings = [r for r in rows if r.get("kind") == "haunting"]
    risen = [r for r in rows if r.get("kind") == "resurrection"]
    graves = [r for r in rows if r.get("kind") == "throwback"]
    verified = sum(1 for r in hauntings + graves if r.get("verified"))
    by_service = {}
    for r in hauntings:
        by_service[r["service"]] = by_service.get(r["service"], 0) + 1
    out = [f"{len(hauntings)} hauntings, {len(risen)} resurrections, {len(graves)} graves visited, "
           f"{verified} quotes verified word for word."]
    for service, n in sorted(by_service.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"  {service}: {n}")
    still = {k: v for s in load_state(state).values() for k, v in s.items() if v.get("posted")}
    if still:
        out.append("Still haunted: " + ", ".join(sorted(k.split(":")[0] for k in still)))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="haunt", choices=["haunt", "graveyard", "rehearse", "throwback"])
    ap.add_argument("--config", default="statuspages.json")
    ap.add_argument("--graves", default="throwback.json")
    ap.add_argument("--state", default="state")
    ap.add_argument("--only", help="run one service by name")
    ap.add_argument("--count", type=int, default=3, help="graves to visit (throwback)")
    ap.add_argument("--out", help="folder for the rendered cards")
    ap.add_argument("--dry-run", action="store_true", help="write the verse, post nothing, change nothing")
    args = ap.parse_args(argv)
    if args.command == "graveyard":
        print(graveyard(args.state))
        return 0
    dry = args.dry_run or not os.environ.get("BLUESKY_APP_PASSWORD")
    if dry and not args.dry_run and args.command != "rehearse":
        print("BLUESKY_APP_PASSWORD not set: dry run", file=sys.stderr)
    if args.command == "throwback":
        sites = json.loads(Path(args.graves).read_text())
        n = throwback(sites, args.state, dry_run=dry, count=args.count, out_dir=args.out)
        print(f"{n} graves visited" + (" (dry run)" if dry else ""))
        return 0
    sites = json.loads(Path(args.config).read_text())
    if args.only:
        sites = [s for s in sites if s["name"].lower() == args.only.lower()]
    if args.command == "rehearse":
        print(f"{rehearse(sites, out_dir=args.out)} rehearsal threads written, none posted")
        return 0
    r = haunt(sites, args.state, dry_run=dry, out_dir=args.out)
    print(f"{r['checked']} feeds read, {r['open']} incidents open, {r['posted']} posts"
          + (f", {r['skipped']} held back by the daily cap" if r["skipped"] else "")
          + (f"; failed: {', '.join(r['failed'])}" if r["failed"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
