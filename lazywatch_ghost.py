"""The Downtime Ghost: lazywatch for status pages, with a sense of humour.

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
at most MAX_POSTS_PER_DAY posts a day; never the same incident twice (its memory of what it
posted, and a check of its own live feed before every post); the ghost never pins, never
replies to anyone else, never follows and never messages (there is simply no code for it).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lazywatch

MAX_POSTS_PER_DAY = 12
MAX_ATTEMPTS = 6            # hourly runs we keep trying to write a poem for one incident before giving up
POST_LIMIT = 295            # Bluesky allows 300 graphemes; keep a margin for emoji
MAX_LINES = 16
BSKY = "https://bsky.social/xrpc"
PUB = "https://public.api.bsky.app/xrpc"
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
# The ghost sings about products, never people and never the news. An archived home page can carry a news
# ticker; a line that trips this is never quoted and never shown to the model.
NEWSY = re.compile(r"\b(Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Pres)\.|\b(said|says|police|investigat\w*|charged|arrest\w*|lawsuit|"
                   r"trafficking|killed|dead|dies|died|death|shot|shooting|court|trial|crime|murder|war|attack\w*|"
                   r"hospital|senator|president|minister|governor|mayor|election|campaign|abuse|assault|victim\w*|"
                   r"indict\w*|allegation\w*|scandal|protest\w*|troops|military|terror\w*)\b", re.I)


# ---------------------------------------------------------------- the feeds
def fetch_feed(site):
    with urllib.request.urlopen(urllib.request.Request(site["url"], headers=lazywatch.UA), timeout=30) as r:
        raw = r.read()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "replace")  # AWS sends UTF-16
    return text if site.get("format") == "rss" else json.loads(text)


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


def _incident(site, ident, title, impact, status, url, update, since, affected, service=None):
    """service: the product people search for, when one feed covers several (Gmail, iCloud Mail); the id keeps the feed's name."""
    return {"service": service or site["name"], "id": f"{site['name']}:{ident}", "title": _text(title), "impact": str(impact or ""),
            "status": str(status or ""), "url": url or site["page"], "page": site["page"], "update": _text(update),
            "since": str(since or ""), "affected": [_text(a) for a in affected if a]}


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
                             [inc.get("service_name")], inc.get("service_name") if site.get("by_product") else None))
    return out


FRESH_DAYS = 7          # a feed's long-running event (a region down for months) is not news: only recent starts


def _fresh(when):
    return bool(when) and (datetime.now(timezone.utc) - when).days < FRESH_DAYS


def _epoch(value, ms=False):
    try:
        return datetime.fromtimestamp(float(value) / (1000 if ms else 1), timezone.utc)
    except (TypeError, ValueError):
        return None


def _aws(site, data):
    """AWS Health public dashboard (currentevents): open events only; the newest log entry is the update."""
    out = []
    for ev in data if isinstance(data, list) else []:
        since = _epoch(ev.get("date"))
        if str(ev.get("status")) == "0" or not _fresh(since):
            continue
        log = [e for e in ev.get("event_log") or [] if isinstance(e, dict)]
        last = max(log, key=lambda e: float(e.get("timestamp") or 0)) if log else {}
        title = f"{last.get('summary') or ev.get('summary')}: {ev.get('service_name')} in {ev.get('region_name')}"
        out.append(_incident(site, ev.get("arn"), title, ev.get("status"), "open", site["page"], last.get("message"),
                             since.isoformat(), [ev.get("service_name")]))
    return out


def _apple(site, data):
    """Apple System Status: an event still open on a service; maintenance is skipped. Each service is its own name."""
    out = []
    for svc in (data or {}).get("services") or []:
        for ev in svc.get("events") or []:
            if ev.get("eventStatus") in ("resolved", "completed") or ev.get("statusType") == "Maintenance":
                continue
            since = _epoch(ev.get("epochStartDate"), ms=True)
            if not _fresh(since):
                continue
            name = svc.get("serviceName")
            out.append(_incident(site, ev.get("messageId"), f"{name} {str(ev.get('statusType') or 'issue').lower()}",
                                 ev.get("statusType"), ev.get("eventStatus"), site["page"], ev.get("message"),
                                 since.isoformat(), [name], name))
    return out


def _paypal(site, data):
    """PayPal status: components only, no incident notes; a production component off 'Operational' is the incident."""
    out = []
    for c in (data or {}).get("result") or []:
        state = ((c.get("status") or {}).get("production") or "").strip()
        if not state or state.lower() == "operational" or "maintenance" in state.lower():
            continue
        name = c.get("displayName") or c.get("name")
        out.append(_incident(site, f"{c.get('id')}-{_slug(state)}", f"{name}: {state}", state, "open", site["page"],
                             f"{name} is reporting {state} on the PayPal status page", "", [name]))
    return out


def _rss(site, data):
    """An RSS status feed (Azure): every item is an open incident; the feed drops it once resolved."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()
        try:
            since = parsedate_to_datetime(get("pubDate")).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            since = ""
        out.append(_incident(site, get("guid") or get("link") or get("title"), get("title"), "", "open",
                             get("link") or site["page"], get("description"), since, []))
    return out


IMPACT = {"none": 0, "minor": 1, "major": 2, "critical": 3}
NEW_HOURS = 48          # an incident first seen after it has been open this long is old news: no verse


def worth_a_poem(site, inc, now):
    """The noise gate: a feed can ask for a minimum impact (Twilio files every carrier hiccup), and an incident
    that has been open for days when the ghost first sees it (a newly added feed, a forgotten advisory) is skipped."""
    floor = site.get("min_impact")
    if floor and IMPACT.get(str(inc.get("impact")).lower(), 0) < IMPACT.get(floor, 0):
        return False
    since = _parse(inc.get("since")) if inc.get("since") else None
    return not since or (now - since).total_seconds() < NEW_HOURS * 3600


PARSERS = {"statuspage": _statuspage, "slack": _slack, "google": _google, "aws": _aws, "apple": _apple,
           "paypal": _paypal, "rss": _rss}


def open_incidents(site, data):
    return PARSERS[site.get("format", "statuspage")](site, data)


# ---------------------------------------------------------------- the ghost's verse
def pick_form(incident_id):
    return FORMS[int(hashlib.sha1(incident_id.encode()).hexdigest(), 16) % len(FORMS)][0]


def ghost_prompt(incident, form):
    affected = ", ".join(incident["affected"][:4])
    return (
        "You are The Downtime Ghost, a theatrical but kind spirit who haunts status pages and writes verse "
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


def newsy(text):
    return bool(NEWSY.search(text))


def site_words(site_name):
    return {w.casefold() for w in re.findall(r"[A-Za-z]+", site_name)}


SELF_TALK = {"welcome", "free", "join", "sign", "members", "member", "account", "search", "download", "home", "homepage",
             "page", "pages", "email", "mail", "chat", "music", "photos", "friends", "community", "share", "browse",
             "explore", "ask", "answers", "questions", "news", "web", "internet", "online", "site", "click", "start",
             "try", "get", "create", "build", "new", "today"}


def about_the_site(paragraph, site_name):
    """Keep a paragraph from an archived home page only if it reads like the site talking about itself:
    nothing newsy, and no capitalised pair of words that could be a person's name (the site's own name is fine)."""
    if newsy(paragraph):
        return False
    ok = site_words(site_name) | {"home", "page", "web", "site", "sign", "log", "free", "new", "search", "help",
                                  "terms", "privacy", "all", "rights", "reserved", "yahoo", "google", "the", "my"}
    for a, b in re.findall(r"\b([A-Z][a-z]+) ([A-Z][a-z]+)\b", paragraph):
        if a.casefold() not in ok and b.casefold() not in ok:
            return False
    # and it must sound like the site itself: its own name, or the words home pages use about themselves.
    # Pages full of what users posted (Yahoo! Answers questions) carry neither, and may name real people in lower case.
    words = set(re.findall(r"[a-z0-9]+", paragraph.casefold()))
    return bool(words & (site_words(site_name) | SELF_TALK))


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
    """Split header, verse, quote and source link over as few posts as possible, each under the limit, the
    lines shared out evenly between the posts. The first post always carries the source link and the images;
    the quote comes last."""
    display, _url = link
    source = f"Source: {display}"

    def body(chunk, with_header, with_quote):
        parts = ([header] if with_header else []) + (["\n".join(chunk)] if chunk else [])
        if with_quote:
            parts.append(f"\u201c{quote}\u201d")
        if with_header:
            parts.append(source)
        return "\n\n".join(parts)

    def fits(text):
        return _length(text) <= POST_LIMIT

    lines = [_shorten(l, POST_LIMIT - _length(body([], True, False)) - 4) for l in lines]
    plan = None
    for n in range(1, max(len(lines), 1) + 1):            # the fewest posts whose even share of lines fits
        base, extra = divmod(len(lines), n)
        sizes = [base + (1 if i >= n - extra else 0) for i in range(n)]   # the first post, with the header, gets the smaller share
        chunks, at = [], 0
        for size in sizes:
            chunks.append(lines[at:at + size])
            at += size
        if all(fits(body(c, i == 0, False)) for i, c in enumerate(chunks)):
            plan = chunks
            break
    plan = plan or [lines]
    if fits(body(plan[-1], len(plan) == 1, True)):
        texts = [body(c, i == 0, i == len(plan) - 1) for i, c in enumerate(plan)]
    else:
        texts = [body(c, i == 0, False) for i, c in enumerate(plan)]
        room = POST_LIMIT - 2
        texts.append(f"\u201c{_shorten(quote, room)}\u201d")
    if not fits(texts[-1]):                               # a long quote on a single post: a shorter prefix is still verbatim
        room = POST_LIMIT - _length(texts[-1]) + _length(quote)
        texts[-1] = texts[-1].replace(f"\u201c{quote}\u201d", f"\u201c{_shorten(quote, max(room, 12))}\u201d")
    return [_post(t, link if i == 0 else None, images if i == 0 else None) for i, t in enumerate(texts) if t.strip()]


def status_post(incident, shot=None, now=None):
    """The thread's last post: the status page itself, written the way people search for an outage
    ("Is GitHub down?"): the product's name, what its status page reports, since when, and when the
    screenshot was taken. The screenshot is its only image."""
    now = now or datetime.now(timezone.utc)
    service, title = incident["service"], incident["title"]
    since = _when(incident["since"])
    taken = _when(now.isoformat())
    link = (_host(incident.get("page") or incident["url"]), incident["url"])

    def text_for(t):
        s = f"Is {service} down? Its status page has reported \u201c{t}\u201d" + (f" since {since}" if since else "") + "."
        s += f"\n\n{'Screenshot taken' if shot else 'Status page checked'} {taken}."
        return s + f"\n\nSource: {link[0]}"

    text = text_for(title)
    if _length(text) > POST_LIMIT:
        room = len(title) - (_length(text) - POST_LIMIT)
        text = text_for(_shorten(title, max(room, 12)))
    return _post(text, link, [shot] if shot else None)


def haunting_thread(incident, form, poem, quote, images=None, shot=None, now=None):
    """The verse (card on the first post, quote last), then the status page as the closing post."""
    header = f"\U0001F47B {HEADLINE[form]} {incident['service']}"
    since = _when(incident["since"])
    if since:
        header += f", down since {since}"
    lines = [l for l in poem.splitlines() if l.strip()]
    verse = thread(header, lines, quote, (_host(incident.get("page") or incident["url"]), incident["url"]), images)
    return verse + [status_post(incident, shot, now)]


def resurrection_thread(service, entry, now, images=None):
    since = _when(entry.get("since") or entry["first_seen"])
    lasted = _duration(entry.get("since") or entry["first_seen"], now)
    title = entry["title"]
    text = (f"\U0001F47B {service} has risen, back up after {lasted}. “{title}” haunted the status page"
            + (f" since {since}" if since else "") + ". Lights back on, candles out. \U0001F56F️")
    link = (_host(entry.get("page") or entry["url"]), entry["url"])
    tail = f"\n\nSource: {link[0]}"
    if _length(text + tail) > POST_LIMIT:
        room = len(title) - (_length(text + tail) - POST_LIMIT)
        text = text.replace(f"“{title}”", f"“{_shorten(title, max(room, 12))}”")
    return [_post(text + tail, link, images)]


# ---------------------------------------------------------------- cards and screenshots
def card_spec(kind, incident=None, form=None, poem="", quote="", entry=None, service=None, now=None, site=None, week=None):
    if kind == "haunting":
        tag = f"The Downtime Ghost · {incident['service']}"
        since = _when(incident["since"])
        return {"kind": kind, "tag": tag + (f" · down since {since}" if since else ""),
                "headline": f"{HEADLINE[form]} {incident['service']}", "sub": incident["title"],
                "poem": poem, "quote": quote, "source": f"Source: {_host(incident.get('page') or incident['url'])}"}
    if kind == "risen":
        lasted = _duration(entry.get("since") or entry["first_seen"], now)
        return {"kind": kind, "tag": f"The Downtime Ghost · {service} · risen {_when(now.isoformat())}",
                "headline": f"{service} has risen", "sub": entry["title"],
                "poem": f"Haunted the status page for {lasted}.\nLights back on, candles out.",
                "quote": "", "source": f"Source: {_host(entry.get('page') or entry['url'])}"}
    if kind == "ofrenda":
        return {"kind": kind, "tag": f"The Downtime Ghost · Ofrenda · week of {week}", "headline": "Ofrenda",
                "sub": "The week's fallen services, and their time in the underworld", "poem": poem, "quote": quote,
                "source": "Source: the official status pages"}
    return {"kind": "throwback", "tag": f"{site.get('occasion', 'Throwback Thursday')} · from the Wayback Machine · {site['born']} to {site['died']}",
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


# ---------------------------------------------------------------- never the same post twice
def fingerprint(text):
    """Words only: no links, punctuation, emoji or case, so a post is recognised however it was wrapped."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def live_feed(handle=None, limit=100):
    """Fingerprints of the account's own recent posts, read without logging in: the repeat check that
    holds even if the ghost's memory of what it posted is lost."""
    handle = handle or os.environ["BLUESKY_HANDLE"]
    q = urllib.parse.urlencode({"actor": handle, "limit": limit, "filter": "posts_no_replies"})
    req = urllib.request.Request(f"{PUB}/app.bsky.feed.getAuthorFeed?{q}", headers=lazywatch.UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return [fingerprint(item["post"]["record"].get("text", "")) for item in data.get("feed", []) if "post" in item]


class LiveFeedCheck:
    """seen(key): is a post with these words already on the account's feed? The feed is read once per run.
    If it cannot be read, every key counts as seen: a missed hour costs less than a double post."""
    def __init__(self, handle=None):
        self.handle = handle
        self.feed = None

    def __call__(self, key):
        if self.feed is None:
            try:
                self.feed = live_feed(self.handle)
            except Exception as exc:
                print(f"live feed unreadable ({exc.__class__.__name__}): posting nothing this run", file=sys.stderr)
                self.feed = False
        if self.feed is False:
            return True
        fp = fingerprint(key)
        return any(fp and fp in post for post in self.feed)


def haunting_key(incident, form):
    return f"{HEADLINE[form]} {incident['service']} down since {_when(incident['since'])}"


def resurrection_key(service, entry):
    return f"{service} has risen {entry['title']}"


def throwback_key(site):
    return f"in memoriam {site['name']}"


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


def bsky_delete(session, uri):
    """Takes down one of the account's own posts, by its at:// uri."""
    repo, collection, rkey = uri.removeprefix("at://").split("/")
    _xrpc("com.atproto.repo.deleteRecord", {"repo": repo, "collection": collection, "rkey": rkey}, session["jwt"])


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

    def delete(self, uri):
        self.session = self.session or bsky_login()
        bsky_delete(self.session, uri)


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


GRAVES_ON_THURSDAY = 6
SPECIAL_DAYS = Path(__file__).with_name("special_days.json")   # {"YYYY-MM-DD": {"graves": 3, "occasion": "..."}}


def special_day(now):
    """Graves on a day other than Thursday (a holiday, a make-up day), from special_days.json; {} on an ordinary day."""
    try:
        return json.loads(SPECIAL_DAYS.read_text()).get(now.strftime("%Y-%m-%d"), {})
    except (OSError, ValueError):
        return {}


def reserved(state, now):
    """Posts held back from the hauntings so a busy outage day cannot crowd out the scheduled ones: the rest of
    Thursday's graves, and Saturday's ofrenda until it is offered. They still count toward the daily cap."""
    day = now.strftime("%Y-%m-%d")
    today = [p for p in posts(state) if p.get("when", "").startswith(day) and p.get("uri")]
    graves = GRAVES_ON_THURSDAY if now.isoweekday() == 4 else special_day(now).get("graves", 0)
    held = max(0, graves - sum(1 for p in today if p.get("kind") == "throwback"))
    if now.isoweekday() == 6 and not any(p.get("kind") == "ofrenda" for p in today):
        held += 1
    return held


def haunt(sites, state, ask=ask_ghost, poster=None, fetch=fetch_feed, shooter=screenshot, cards=render_card,
          now=None, dry_run=False, out_dir=None, seen_live=None):
    """One pass over every status feed: new incidents get verse, cleared incidents get a resurrection notice.
    With dry_run nothing is posted and nothing is written; the posts are printed instead. Before any post,
    seen_live(key) checks the account's own feed so the same incident is never posted twice."""
    now = now or datetime.now(timezone.utc)
    live = poster is None and not dry_run          # posting to the real account: read its feed first
    poster = poster or (print_poster if dry_run else BlueskyPoster())
    seen_live = seen_live or (LiveFeedCheck() if live else (lambda key: False))
    seen = load_state(state)
    budget = MAX_POSTS_PER_DAY - posted_today(state, now) - reserved(state, now)
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

        for ident in current:                                                  # back on the page: not risen after all
            if ident in entries:
                entries[ident].pop("risen_at", None)
        for ident in [i for i in list(entries) if i not in current]:          # resolved: the resurrection
            entry = entries.pop(ident)
            if not entry.get("posted"):
                continue
            entry.setdefault("risen_at", now.isoformat())                      # when it cleared, even if the post waits
            risen = _parse(entry["risen_at"]) or now
            name = entry.get("service") or site["name"]
            if budget <= 0:
                entries[ident] = entry                                         # held by the cap: kept for the next pass
                report["skipped"] += 1
                continue
            if seen_live(resurrection_key(name, entry)):
                print(f"{name}: resurrection already on the feed, not posting again", file=sys.stderr)
                if not dry_run:
                    _log(state, {"when": now.isoformat(), "kind": "duplicate-skipped", "service": name, "id": ident,
                                 "title": entry["title"], "key": resurrection_key(name, entry)})
                continue
            card = cards(card_spec("risen", entry=entry, service=name, now=risen), out_dir, f"risen-{_slug(ident)}") if cards else None
            posts_ = resurrection_thread(name, entry, risen, [card] if card else None)
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
                             "title": entry["title"], "since": entry.get("since") or entry["first_seen"],
                             "risen_at": entry["risen_at"],
                             "key": resurrection_key(name, entry), "text": posts_[0]["text"],
                             "uri": uris[0]})

        name = site["name"]
        for ident, inc in current.items():                                     # new or still unwritten: the verse
            entry = entries.setdefault(ident, {"title": inc["title"], "since": inc["since"], "url": inc["url"],
                                               "service": inc["service"],
                                               "page": inc["page"], "first_seen": now.isoformat(), "form": pick_form(ident),
                                               "attempts": 0, "posted": None})
            if entry.get("posted") or entry["attempts"] >= MAX_ATTEMPTS:
                continue
            if not worth_a_poem(site, inc, now):
                continue
            if budget <= 0:
                report["skipped"] += 1
                continue
            if seen_live(haunting_key(inc, entry["form"])):           # the memory was lost, the feed was not
                print(f"{name}: already on the feed, not posting again", file=sys.stderr)
                entry["posted"] = {"when": now.isoformat(), "uri": "live-feed"}
                if not dry_run:
                    _log(state, {"when": now.isoformat(), "kind": "duplicate-skipped", "service": name, "id": ident,
                                 "title": inc["title"], "key": haunting_key(inc, entry["form"])})
                continue
            entry["attempts"] += 1
            try:
                reply = ask(inc, entry["form"])
            except Exception as exc:
                print(f"{name}: model unavailable ({exc.__class__.__name__})", file=sys.stderr)
                continue
            poem = clean_poem(reply)
            if not poem or too_mean(poem) or newsy(poem):
                print(f"{name}: poem rejected ({'empty' if not poem else 'tone'}), will retry", file=sys.stderr)
                continue
            quote, verified = verify_quote(reply, inc["update"], inc["title"])
            if newsy(quote):
                quote, verified = inc["title"], False
            card = cards(card_spec("haunting", inc, entry["form"], poem, quote), out_dir, _slug(ident)) if cards else None
            shot = shooter(inc["url"], name) if shooter else None
            posts_ = haunting_thread(inc, entry["form"], poem, quote, [card] if card else None, shot, now)
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
                             "form": entry["form"], "title": inc["title"], "since": inc["since"], "quote": quote, "verified": verified,
                             "lines": len(poem.splitlines()), "posts": len(posts_), "key": haunting_key(inc, entry["form"]),
                             "text": posts_[0]["text"], "uri": uris[0]})
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
    return url, [p for p in paragraphs if len(p) >= 30 and about_the_site(p, site["name"])][:40], seen


def throwback_prompt(site, paragraphs, occasion="Throwback Thursday"):
    text = "\n".join(f"- {p[:300]}" for p in paragraphs[:25])
    return (
        f"You are The Downtime Ghost, a theatrical but kind spirit. Tonight is {occasion} and you are "
        f"visiting the grave of {site['name']} ({site['born']} to {site['died']}): {site.get('note', '')}.\n"
        f"WHAT ITS ARCHIVED HOME PAGE SAID:\n{text}\n\n"
        f"Write an elegy for {site['name']}: 6 to 10 short lines, one line per line, fond and a little funny, "
        "about what the site was and what the web lost; no real people's names, no blame, no sadness that lingers; "
        "end on a warm note.\n"
        "Then copy ONE sentence or phrase word for word from the archived text above (do not change a single character).\n"
        "Reply with JSON only: {\"poem\": \"line one\\nline two\", \"quote\": \"the exact words\"}"
    )


def throwback(sites, state, ask=ask_model, poster=None, lookup=wayback, cards=render_card, now=None, dry_run=False,
              count=3, out_dir=None, seen_live=None, shooter=None, occasion="Throwback Thursday"):
    """Three graves a Thursday: the next unvisited sites in throwback.json, each with an elegy from its last words."""
    now = now or datetime.now(timezone.utc)
    live = poster is None and not dry_run          # posting to the real account: read its feed first
    poster = poster or (print_poster if dry_run else BlueskyPoster())
    seen_live = seen_live or (LiveFeedCheck() if live else (lambda key: False))
    shooter = shooter if shooter is not None else (lambda url, name: screenshot(url, name))
    done = load_state(state, "throwback.json", [])
    budget = MAX_POSTS_PER_DAY - posted_today(state, now)
    visited = 0
    for site in [s for s in sites if s["name"] not in done]:
        if visited >= count or budget <= 0:
            break
        if seen_live(throwback_key(site)):                               # visited before, by a ghost that forgot
            print(f"{site['name']}: already on the feed, grave marked as visited", file=sys.stderr)
            if not dry_run:
                done.append(site["name"])
                save_state(state, done, "throwback.json")
            continue
        try:
            url, paragraphs, seen = lookup(site)
        except Exception as exc:
            print(f"{site['name']}: grave not found ({exc.__class__.__name__})", file=sys.stderr)
            continue
        if not paragraphs:
            print(f"{site['name']}: the saved page has no words", file=sys.stderr)
            continue
        try:
            reply = ask(throwback_prompt(site, paragraphs, occasion))
        except Exception as exc:
            print(f"{site['name']}: model unavailable ({exc.__class__.__name__})", file=sys.stderr)
            break
        poem = clean_poem(reply)
        if not poem or too_mean(poem) or newsy(poem):
            print(f"{site['name']}: elegy rejected", file=sys.stderr)
            continue
        quote, verified = verify_quote(reply, " ".join(paragraphs), site["name"])
        if newsy(quote):
            quote, verified = site["name"], False
        spec = card_spec("throwback", poem=poem, quote=quote, site=dict(site, seen=seen, occasion=occasion))
        card = cards(spec, out_dir, f"throwback-{_slug(site['name'])}") if cards else None
        header = f"\U0001F56F️ {occasion}: in memoriam {site['name']} ({site['born']} to {site['died']})"
        posts_ = thread(header, [l for l in poem.splitlines() if l.strip()], quote, ("web.archive.org", url),
                        [card] if card else None)
        shot = shooter(_as_it_was(url), site["name"]) if shooter else None
        if shot and out_dir:                                                  # kept with the cards, so a dry run shows it
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / f"grave-shot-{_slug(site['name'])}.jpg").write_bytes(shot["data"])
        posts_.append(grave_post(site, url, seen, shot))
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
                         "lines": len(poem.splitlines()), "posts": len(posts_), "key": throwback_key(site),
                         "text": posts_[0]["text"], "uri": uris[0]})
            lazywatch._store("record_change", {"site": f"ghost:throwback", "url": url, "summary": f"elegy for {site['name']}",
                                               "quote": quote, "verified": verified, "added": [poem] + paragraphs[:10]})
    return visited


def _as_it_was(url):
    """The archived page without the Wayback toolbar, for the screenshot: if_ after the timestamp keeps the archive's
    rewritten links, so the page's images and styles load from the archive too. (id_ is the raw page: right for
    reading its text, but its images point at hosts that died with the site, so it renders as a broken page.)"""
    return re.sub(r"(/web/\d{8,14})/", r"\1if_/", url, count=1)


def grave_post(site, url, seen, shot=None):
    """The closing post of a grave: the home page as it last looked, from the Wayback Machine."""
    when = seen.replace("Last seen ", "") if seen else ""
    text = (f"{site['name']} as it looked" + (f" on {when}" if when else "") + ", the last time the Wayback Machine visited."
            + "\n\nSource: web.archive.org")
    if shot:
        shot = dict(shot, alt=f"Screenshot of the {site['name']} home page" + (f" as archived on {when}" if when else "")
                    + " by the Wayback Machine.")
    return _post(text, ("web.archive.org", url), [shot] if shot else None)


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
    still = {k: v for s in load_state(state).values() for k, v in s.items() if v.get("posted") and not v.get("risen_at")}
    if still:
        out.append("Still haunted: " + ", ".join(sorted(k.split(":")[0] for k in still)))
    return "\n".join(out)


# ---------------------------------------------------------------- Saturday ofrenda
def _span(minutes):
    if minutes >= 2 * 24 * 60:
        return f"{minutes // (24 * 60)} days"
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m} min" if h and m else (f"{h} h" if h else f"{m} min")


def fallen(state, now, days=7):
    """The week's hauntings from the ghost's own log: who fell, since when, risen when (or still haunted).
    Still haunted means still on its status page now (the open state), never just "no resurrection in the log":
    a haunting that cleared with no resurrection logged counts only the time up to its own post, a known minimum."""
    start = now - timedelta(days=days)
    rows = posts(state)
    risen = {(r.get("service"), r.get("id")): r.get("risen_at") or r["when"]
             for r in rows if r.get("kind") == "resurrection" and r.get("uri")}
    current = {(v.get("service") or site, ident): v for site, s in load_state(state).items()
               for ident, v in s.items() if v.get("posted")}
    out = []
    for r in rows:
        if r.get("kind") != "haunting" or not r.get("uri"):
            continue
        key = (r.get("service"), r.get("id"))
        held = current.get(key)
        up = risen.get(key) or (held or {}).get("risen_at")
        still = held is not None and not up
        posted, back = _parse(r.get("when")), _parse(up) if up else None
        if not posted or (posted < start and not (still or (back and back >= start))):
            continue
        since = _parse(r.get("since")) or posted
        end = back or (now if still else posted)
        minutes = max(int((end - since).total_seconds() // 60), 0)
        out.append({"service": r["service"], "title": r.get("title", ""), "since": since.isoformat(),
                    "risen": up, "open": still, "minutes": minutes})
    return out


def altar(rows, now, days=7):
    """(week label, [(service, {n, minutes, open})] longest first, total minutes)."""
    start = now - timedelta(days=days - 1)
    week = f"{start.strftime('%b')} {start.day} to " + (str(now.day) if start.month == now.month else f"{now.strftime('%b')} {now.day}")
    by = {}
    for f in rows:
        b = by.setdefault(f["service"], {"n": 0, "minutes": 0, "open": False})
        b["n"] += 1
        b["minutes"] += f["minutes"]
        b["open"] = b["open"] or f.get("open", not f["risen"])
    items = sorted(by.items(), key=lambda kv: (-kv[1]["minutes"], kv[0]))
    return week, items, sum(b["minutes"] for _, b in items)


def ofrenda_key(week):
    return f"Ofrenda week of {week}"


def ofrenda_text(week, items, total):
    head = f"\U0001F56F\ufe0f Ofrenda, week of {week}: "
    if not items:
        return head + "nobody fell. The candles burn for an empty altar. \U0001F47B"

    def line(name, b):
        return name + (f" ×{b['n']}" if b["n"] > 1 else "") + f" {_span(b['minutes'])}" + (" (still haunted)" if b["open"] else "")

    def build(k):
        more = len(items) - k
        return (head + f"{len(items)} service{'s' if len(items) != 1 else ''} fell. "
                + ", ".join(line(n, b) for n, b in items[:k]) + (f" and {more} more" if more > 0 else "")
                + f". {_span(total)} in the underworld in all. \U0001F47B")

    for k in range(len(items), 0, -1):
        if _length(build(k)) <= POST_LIMIT:
            return build(k)
    return _shorten(build(1), POST_LIMIT)


def ofrenda(state, poster=None, cards=render_card, now=None, dry_run=False, out_dir=None, seen_live=None, days=7,
            redo=False, deleter=None):
    """Saturday: the week's fallen services on one amber card, with their time in the underworld. One post.
    redo: a correction. This week's ofrenda is taken down first and replaced, one for one, so the cap is not
    asked again; if the old post cannot be taken down, nothing new is posted."""
    now = now or datetime.now(timezone.utc)
    live = poster is None and not dry_run
    poster = poster or (print_poster if dry_run else BlueskyPoster())
    seen_live = seen_live or (LiveFeedCheck() if live else (lambda key: False))
    week, items, total = altar(fallen(state, now, days), now, days)
    if redo:
        gone = {r.get("uri") for r in posts(state) if r.get("kind") == "ofrenda-withdrawn"}
        old = [r for r in posts(state) if r.get("kind") == "ofrenda" and r.get("key") == ofrenda_key(week)
               and r.get("uri") and r["uri"] not in gone]
        deleter = deleter or (getattr(poster, "delete", None) if not dry_run else (lambda uri: print(f"--- would delete {uri}")))
        for r in old:
            try:
                deleter(r["uri"])
            except Exception as exc:
                print(f"old ofrenda not taken down ({exc.__class__.__name__}: {str(exc)[:80]}): posting nothing", file=sys.stderr)
                return 0
            if not dry_run:
                _log(state, {"when": now.isoformat(), "kind": "ofrenda-withdrawn", "key": r["key"], "uri": r["uri"]})
        if not old:
            print("no ofrenda this week to correct", file=sys.stderr)
            return 0
    else:
        if MAX_POSTS_PER_DAY - posted_today(state, now) <= 0:
            print("ofrenda held back by the daily cap", file=sys.stderr)
            return 0
        if seen_live(ofrenda_key(week)):
            print("ofrenda already on the feed", file=sys.stderr)
            return 0
    text = ofrenda_text(week, items, total)
    lines = [f"{n}{' ×' + str(b['n']) if b['n'] > 1 else ''} · {_span(b['minutes'])}" + (" · still haunted" if b["open"] else "")
             for n, b in items[:MAX_LINES]] or ["Nobody fell this week."]
    lines += ["", f"{_span(total)} in the underworld in all." if items else "The candles burn for an empty altar."]
    card = cards(card_spec("ofrenda", poem="\n".join(lines), week=week), out_dir, "ofrenda") if cards else None
    post = _post(text, None, [card] if card else None)
    try:
        uris = poster([post])
    except Exception as exc:
        print(f"ofrenda not posted ({exc.__class__.__name__}: {str(exc)[:80]})", file=sys.stderr)
        return 0
    if not dry_run:
        _log(state, {"when": now.isoformat(), "kind": "ofrenda", "key": ofrenda_key(week), "text": text, "uri": uris[0]})
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="haunt", choices=["haunt", "graveyard", "rehearse", "throwback", "ofrenda"])
    ap.add_argument("--config", default="statuspages.json")
    ap.add_argument("--graves", default="throwback.json")
    ap.add_argument("--state", default="state")
    ap.add_argument("--only", help="run one service by name")
    ap.add_argument("--count", type=int, default=3, help="graves to visit (throwback)")
    ap.add_argument("--occasion", default="Throwback Thursday", help="the graves' header and card label (throwback)")
    ap.add_argument("--out", help="folder for the rendered cards")
    ap.add_argument("--dry-run", action="store_true", help="write the verse, post nothing, change nothing")
    ap.add_argument("--redo", action="store_true", help="take down this week's ofrenda and post a corrected one (ofrenda)")
    args = ap.parse_args(argv)
    if args.command == "graveyard":
        print(graveyard(args.state))
        return 0
    dry = args.dry_run or not os.environ.get("BLUESKY_APP_PASSWORD")
    if dry and not args.dry_run and args.command != "rehearse":
        print("BLUESKY_APP_PASSWORD not set: dry run", file=sys.stderr)
    if args.command == "throwback":
        sites = json.loads(Path(args.graves).read_text())
        n = throwback(sites, args.state, dry_run=dry, count=args.count, out_dir=args.out,
                      occasion=args.occasion)
        print(f"{n} graves visited" + (" (dry run)" if dry else ""))
        return 0 if n or dry else 3                      # nothing visited: the workflow does not count this run
    if args.command == "ofrenda":
        n = ofrenda(args.state, dry_run=dry, out_dir=args.out, redo=args.redo)
        print(("ofrenda posted" if n else "no ofrenda") + (" (dry run)" if dry else ""))
        return 0 if n or dry else 3                      # not offered: the next run tries again
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
