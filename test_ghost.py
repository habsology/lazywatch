import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import lazywatch_ghost as g

NOW = datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)
GITHUB = {"name": "GitHub", "url": "https://www.githubstatus.com/api/v2/summary.json", "page": "https://www.githubstatus.com"}
SLACK = {"name": "Slack", "url": "https://slack-status.com/api/v2.0.0/current", "page": "https://slack-status.com", "format": "slack"}
GOOGLE = {"name": "Google Cloud", "url": "https://status.cloud.google.com/incidents.json", "page": "https://status.cloud.google.com", "format": "google"}

STATUSPAGE = {"incidents": [
    {"id": "abc1", "name": "Incident with Actions", "status": "investigating", "impact": "major",
     "shortlink": "https://stspg.io/abc1", "created_at": "2026-10-28T14:02:11.000Z",
     "components": [{"name": "Actions"}],
     "incident_updates": [
         {"body": "We are investigating reports of degraded performance for Actions.", "created_at": "2026-10-28T14:02:11.000Z"},
         {"body": "We have identified the cause and are working on a fix.", "created_at": "2026-10-28T14:40:00.000Z"}]},
    {"id": "old1", "name": "Resolved thing", "status": "resolved", "impact": "minor", "incident_updates": []}],
    "scheduled_maintenances": [{"id": "m1", "name": "Database maintenance", "status": "scheduled"}]}
SLACK_FEED = {"status": "active", "active_incidents": [
    {"id": 1182, "title": "Trouble sending messages", "type": "outage", "status": "active",
     "url": "https://slack-status.com/2026-10/1182", "date_created": "2026-10-28T13:00:00Z", "services": ["Messaging"],
     "notes": [{"date_created": "2026-10-28T13:05:00Z", "body": "Some users may have trouble sending messages."}]},
    {"id": 1183, "title": "Planned maintenance", "type": "notice", "status": "active", "notes": []}]}
GOOGLE_FEED = [
    {"id": "g1", "begin": "2026-10-28T12:00:00+00:00", "end": None, "external_desc": "Issue with Cloud Run in us-east1",
     "status_impact": "SERVICE_DISRUPTION", "uri": "incidents/g1", "service_name": "Cloud Run",
     "most_recent_update": {"text": "Mitigation work is currently underway by our engineering team."}},
    {"id": "g2", "begin": "2026-10-20T12:00:00+00:00", "end": "2026-10-20T13:00:00+00:00", "external_desc": "Done",
     "status_impact": "SERVICE_OUTAGE"},
    {"id": "g3", "begin": "2026-10-28T12:00:00+00:00", "end": None, "external_desc": "FYI", "status_impact": "SERVICE_INFORMATION"}]

POEM = {"poem": "Oh GitHub, your Actions sleep,\nthe runners count no sheep.\nA green check waits in the wings,\nsoon the merge button sings.",
        "quote": "We have identified the cause and are working on a fix."}
LONG = {"poem": "\n".join(f"Verse {n}: the runners rest, the queue grows long and longer still tonight" for n in range(1, 15)),
        "quote": POEM["quote"]}
FAKE_CARD = lambda spec, out_dir=None, name=None: {"data": b"jpeg", "alt": "card: " + spec["headline"], "mime": "image/jpeg"}


class Parsing(unittest.TestCase):
    def test_statuspage_keeps_only_active_incidents(self):
        incs = g.open_incidents(GITHUB, STATUSPAGE)
        self.assertEqual([i["id"] for i in incs], ["GitHub:abc1"])
        self.assertEqual(incs[0]["update"], "We have identified the cause and are working on a fix.")  # the latest update
        self.assertEqual(incs[0]["url"], "https://stspg.io/abc1")
        self.assertEqual(incs[0]["affected"], ["Actions"])

    def test_slack_skips_notices(self):
        incs = g.open_incidents(SLACK, SLACK_FEED)
        self.assertEqual([i["title"] for i in incs], ["Trouble sending messages"])
        self.assertEqual(incs[0]["affected"], ["Messaging"])

    def test_google_keeps_open_disruptions_only(self):
        incs = g.open_incidents(GOOGLE, GOOGLE_FEED)
        self.assertEqual([i["id"] for i in incs], ["Google Cloud:g1"])
        self.assertEqual(incs[0]["url"], "https://status.cloud.google.com/incidents/g1")
        self.assertEqual(incs[0]["impact"], "disruption")

    def test_empty_feeds(self):
        self.assertEqual(g.open_incidents(GITHUB, {"incidents": []}), [])
        self.assertEqual(g.open_incidents(SLACK, {"active_incidents": []}), [])
        self.assertEqual(g.open_incidents(GOOGLE, []), [])

    def test_markdown_and_html_are_stripped(self):
        self.assertEqual(g._text("## Incident Report\n**Start**: <p>08 October</p> and 2*3"), "Incident Report Start: 08 October and 2*3")


class Verse(unittest.TestCase):
    def setUp(self):
        self.inc = g.open_incidents(GITHUB, STATUSPAGE)[0]

    def test_quote_must_be_verbatim(self):
        self.assertEqual(g.verify_quote(POEM, self.inc["update"], self.inc["title"]), (POEM["quote"], True))
        made_up = {"quote": "Everything is on fire and the cause is unknown."}
        self.assertEqual(g.verify_quote(made_up, self.inc["update"], self.inc["title"]), ("Incident with Actions", False))
        self.assertTrue(g.verify_quote({"quote": "  “We have identified the cause and are working on a fix.” "}, self.inc["update"], "t")[1])
        self.assertTrue(g.verify_quote({"quote": "we have identified the cause and are working on a fix"}, self.inc["update"], "t")[1])
        self.assertFalse(g.verify_quote({"quote": "the cause"}, self.inc["update"], "t")[1])                      # too short
        self.assertFalse(g.verify_quote({"quote": "We have identified the cause and are working on the fix."}, self.inc["update"], "t")[1])

    def test_first_post_has_date_source_link_and_card(self):
        posts = g.haunting_thread(self.inc, "ode", g.clean_poem(POEM), POEM["quote"], [FAKE_CARD({"headline": "x"})], now=NOW)
        self.assertEqual(len(posts), 2)                                   # the verse, then the status page
        text = posts[0]["text"]
        self.assertTrue(text.startswith("👻 An ode to GitHub, down since Oct 28, 2026, 14:02 UTC\n\n"))
        self.assertIn("“We have identified the cause and are working on a fix.”", text)
        self.assertTrue(text.endswith("Source: www.githubstatus.com"))
        self.assertLessEqual(len(text), 300)
        facet = posts[0]["facets"][0]
        self.assertEqual(facet["features"][0]["uri"], "https://stspg.io/abc1")
        b = text.encode("utf-8")
        self.assertEqual(b[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode(), "www.githubstatus.com")  # byte offsets past the emoji
        self.assertEqual(posts[0]["images"][0]["alt"], "card: x")
        last = posts[-1]                                                  # written the way people search for an outage
        self.assertEqual(last["text"], "Is GitHub down? Its status page has reported “Incident with Actions” since Oct 28, 2026, 14:02 UTC."
                                       "\n\nStatus page checked Oct 28, 2026, 15:30 UTC.\n\nSource: www.githubstatus.com")
        self.assertEqual(last["facets"][0]["features"][0]["uri"], "https://stspg.io/abc1")
        self.assertEqual(last["images"], [])

    def test_screenshot_closes_the_thread_on_its_own_post(self):
        shot = {"data": b"jpeg", "mime": "image/jpeg", "alt": "Screenshot of the GitHub status page at Oct 28, 2026, 15:30 UTC."}
        posts = g.haunting_thread(self.inc, "ode", g.clean_poem(POEM), POEM["quote"], [FAKE_CARD({"headline": "x"})], shot, NOW)
        self.assertEqual([len(p["images"]) for p in posts], [1, 1])
        self.assertEqual(posts[0]["images"][0]["alt"], "card: x")        # the card stays with the verse
        self.assertEqual(posts[-1]["images"][0], shot)                   # the screenshot is the last post's only image
        self.assertIn("Screenshot taken Oct 28, 2026, 15:30 UTC.", posts[-1]["text"])
        long_inc = dict(self.inc, title="t" * 400)
        self.assertLessEqual(len(g.status_post(long_inc, shot, NOW)["text"]), 300)

    def test_eight_lines_split_evenly_not_five_and_three(self):
        poem = "\n".join(f"Line {n} of a hymn that runs long tonight" for n in range(1, 9))
        posts = g.haunting_thread(self.inc, "hymn", poem, POEM["quote"])
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0]["text"].count("Line "), 4)
        self.assertEqual(posts[1]["text"].count("Line "), 4)

    def test_news_and_people_are_never_quoted(self):
        self.assertTrue(g.newsy("Investigators are examining whether Mr. Gaetz violated federal laws, the people said."))
        self.assertFalse(g.newsy("Get a free web site with easy-to-use site building tools"))
        self.assertTrue(g.about_the_site("Yahoo! GeoCities: Get a free web site with easy-to-use site building tools", "GeoCities"))
        self.assertFalse(g.about_the_site("Senate panel to question Jane Doe on Tuesday", "Yahoo! Answers"))
        self.assertFalse(g.about_the_site("A new album from Taylor Swift", "Yahoo! Answers"))
        self.assertTrue(g.about_the_site("Yahoo Answers is the best place to ask questions", "Yahoo! Answers"))
        self.assertFalse(g.about_the_site("If I was a corporate ceo at a oil company I too would be having nightmares about elon musk.",
                                          "Yahoo! Answers"))                 # a user's post, real person in lower case

    def test_long_verse_threads_into_replies(self):
        posts = g.haunting_thread(self.inc, "epic", g.clean_poem(LONG), LONG["quote"], [FAKE_CARD({"headline": "x"})])
        self.assertGreater(len(posts), 1)
        for p in posts:
            self.assertLessEqual(len(p["text"]), 300)
        self.assertIn("Source: www.githubstatus.com", posts[0]["text"])  # the link stays on the first post
        self.assertTrue(posts[0]["images"]); self.assertFalse(posts[1]["images"])
        self.assertIn("“We have identified", posts[-2]["text"])         # the quote closes the verse
        self.assertTrue(posts[-1]["text"].startswith("Is GitHub down?"))  # the status page closes the thread
        joined = "\n".join(p["text"] for p in posts)
        for n in range(1, 15):
            self.assertIn(f"Verse {n}:", joined)                        # nothing dropped

    def test_poem_keeps_up_to_16_lines(self):
        self.assertEqual(len(g.clean_poem({"poem": "\n".join(str(n) for n in range(30))}).splitlines()), 16)
        self.assertEqual(g.clean_poem({"poem": ["a", "b"]}), "a\nb")

    def test_tone_guard(self):
        self.assertTrue(g.too_mean("Their useless engineers broke it again"))
        self.assertFalse(g.too_mean("The runners rest, the queue grows long"))

    def test_form_is_stable_per_incident(self):
        self.assertEqual(g.pick_form("GitHub:abc1"), g.pick_form("GitHub:abc1"))
        self.assertIn(g.pick_form("GitHub:abc1"), g.HEADLINE)

    def test_resurrection(self):
        entry = {"title": "Incident with Actions", "since": "2026-10-28T13:16:00Z", "url": "https://stspg.io/abc1",
                 "page": "https://www.githubstatus.com", "first_seen": "x"}
        posts = g.resurrection_thread("GitHub", entry, NOW)
        self.assertEqual(posts[0]["text"], "👻 GitHub has risen, back up after 2 h 14 min. “Incident with Actions” haunted the status page "
                                           "since Oct 28, 2026, 13:16 UTC. Lights back on, candles out. 🕯️\n\nSource: www.githubstatus.com")
        self.assertEqual(posts[0]["facets"][0]["features"][0]["uri"], "https://stspg.io/abc1")
        long_entry = dict(entry, title="t" * 400)
        self.assertLessEqual(len(g.resurrection_thread("GitHub", long_entry, NOW)[0]["text"]), 300)
        self.assertEqual(g._duration("2026-10-26T15:30:00Z", NOW), "2 days")

    def test_card_specs(self):
        spec = g.card_spec("haunting", self.inc, "ode", "a\nb", "q")
        self.assertEqual(spec["headline"], "An ode to GitHub")
        self.assertEqual(spec["tag"], "The Downtime Ghost · GitHub · down since Oct 28, 2026, 14:02 UTC")
        self.assertEqual(spec["source"], "Source: www.githubstatus.com")
        risen = g.card_spec("risen", entry={"title": "t", "since": "2026-10-28T13:16:00Z", "url": "https://x.y/z", "first_seen": "x"},
                            service="GitHub", now=NOW)
        self.assertEqual(risen["headline"], "GitHub has risen")
        self.assertIn("2 h 14 min", risen["poem"])


class Haunting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.threads = []

    def poster(self, posts):
        self.threads.append(posts)
        return [f"at://did:plc:ghost/app.bsky.feed.post/{len(self.threads)}-{n}" for n in range(len(posts))]

    def run_pass(self, feeds, dry_run=False, now=NOW, ask=lambda inc, form: POEM):
        sites = [GITHUB, SLACK, GOOGLE]
        return g.haunt(sites, self.tmp, ask=ask, poster=self.poster, fetch=lambda s: feeds[s["name"]],
                       shooter=None, cards=FAKE_CARD, now=now, dry_run=dry_run)

    def test_new_incident_is_posted_once_then_resurrected(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        r = self.run_pass(feeds)
        self.assertEqual((r["checked"], r["open"], r["posted"]), (3, 1, 1))
        self.assertEqual(self.threads[0][0]["facets"][0]["features"][0]["uri"], "https://stspg.io/abc1")
        self.assertEqual(self.threads[0][0]["images"][0]["alt"], f"card: {g.HEADLINE[g.pick_form('GitHub:abc1')]} GitHub")
        self.assertEqual(self.run_pass(feeds)["posted"], 0)                 # still open: nothing new to say
        feeds["GitHub"] = {"incidents": []}
        self.assertEqual(self.run_pass(feeds)["posted"], 1)                 # cleared: the resurrection
        self.assertIn("GitHub has risen", self.threads[-1][0]["text"])
        self.assertEqual(self.threads[-1][0]["images"][0]["alt"], "card: GitHub has risen")
        self.assertEqual(g.load_state(self.tmp)["GitHub"], {})
        kinds = [p["kind"] for p in g.posts(self.tmp)]
        self.assertEqual(kinds, ["haunting", "resurrection"])
        self.assertTrue(g.posts(self.tmp)[0]["verified"])

    def test_dry_run_writes_nothing(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": SLACK_FEED, "Google Cloud": GOOGLE_FEED}
        r = self.run_pass(feeds, dry_run=True)
        self.assertEqual(r["posted"], 3)
        self.assertFalse((Path(self.tmp) / "ghost").exists())

    def test_failed_feed_keeps_its_incidents(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        self.run_pass(feeds)

        def fetch(site):
            if site["name"] == "GitHub":
                raise OSError("timed out")
            return feeds[site["name"]]
        r = g.haunt([GITHUB, SLACK, GOOGLE], self.tmp, ask=lambda i, f: POEM, poster=self.poster, fetch=fetch,
                    shooter=None, cards=FAKE_CARD, now=NOW)
        self.assertEqual(r["failed"], ["GitHub (OSError)"])
        self.assertEqual(r["posted"], 0)                                     # no false resurrection
        self.assertIn("GitHub:abc1", g.load_state(self.tmp)["GitHub"])

    def test_daily_cap(self):
        many = {"incidents": [dict(STATUSPAGE["incidents"][0], id=f"i{n}", name=f"Incident {n}") for n in range(15)]}
        feeds = {"GitHub": many, "Slack": {"active_incidents": []}, "Google Cloud": []}
        r = self.run_pass(feeds)
        self.assertEqual((r["posted"], r["skipped"]), (g.MAX_POSTS_PER_DAY, 15 - g.MAX_POSTS_PER_DAY))
        self.assertEqual(self.run_pass(feeds)["posted"], 0)                 # same day: cap holds
        tomorrow = datetime(2026, 10, 29, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(self.run_pass(feeds, now=tomorrow)["posted"], 3)   # next day: the rest go out

    def test_model_failures_retry_then_give_up(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}

        def broken(inc, form):
            raise OSError("ollama down")
        for _ in range(g.MAX_ATTEMPTS + 2):
            self.assertEqual(self.run_pass(feeds, ask=broken)["posted"], 0)
        self.assertEqual(g.load_state(self.tmp)["GitHub"]["GitHub:abc1"]["attempts"], g.MAX_ATTEMPTS)
        self.assertEqual(self.run_pass(feeds)["posted"], 0)                 # given up: the good model is not asked

    def test_mean_poem_is_rejected(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        mean = {"poem": "GitHub, you useless pile", "quote": POEM["quote"]}
        self.assertEqual(self.run_pass(feeds, ask=lambda i, f: mean)["posted"], 0)
        self.assertEqual(self.run_pass(feeds)["posted"], 1)

    def test_no_card_means_a_text_post(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        r = g.haunt([GITHUB, SLACK, GOOGLE], self.tmp, ask=lambda i, f: POEM, poster=self.poster,
                    fetch=lambda s: feeds[s["name"]], shooter=None, cards=lambda *a, **k: None, now=NOW)
        self.assertEqual(r["posted"], 1)
        self.assertEqual(self.threads[0][0]["images"], [])

    def test_graveyard(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": SLACK_FEED, "Google Cloud": GOOGLE_FEED}
        self.run_pass(feeds)
        out = g.graveyard(self.tmp)
        self.assertIn("3 hauntings, 0 resurrections, 0 graves visited", out)
        self.assertIn("Still haunted: GitHub, Google Cloud, Slack", out)


class NeverTwice(unittest.TestCase):
    def test_fingerprint_is_words_only(self):
        self.assertEqual(g.fingerprint("👻 An ode to GitHub, down since Oct 28, 2026, 14:02 UTC\n\nSource: www.githubstatus.com"),
                         "an ode to github down since oct 28 2026 14 02 utc source www githubstatus com")
        check = g.LiveFeedCheck(); check.feed = [g.fingerprint("👻 An ode to GitHub, down since Oct 28, 2026, 14:02 UTC\n\nverse")]
        inc = g.open_incidents(GITHUB, STATUSPAGE)[0]
        self.assertTrue(check(g.haunting_key(inc, "ode")))
        self.assertFalse(check(g.haunting_key(inc, "hymn")))
        self.assertFalse(check(g.haunting_key(dict(inc, since="2026-10-29T14:02:11.000Z"), "ode")))

    def test_unreadable_feed_means_no_posting(self):
        check = g.LiveFeedCheck(handle="nobody.invalid")
        check.feed = False
        self.assertTrue(check("anything"))

    def test_haunting_already_on_feed_is_not_posted_again(self):
        tmp = tempfile.mkdtemp(); threads = []
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        r = g.haunt([GITHUB, SLACK, GOOGLE], tmp, ask=lambda i, f: POEM, poster=lambda posts: threads.append(posts) or ["at://x"],
                    fetch=lambda s: feeds[s["name"]], shooter=None, cards=FAKE_CARD, now=NOW, seen_live=lambda key: True)
        self.assertEqual((r["posted"], len(threads)), (0, 0))
        self.assertEqual(g.load_state(tmp)["GitHub"]["GitHub:abc1"]["posted"]["uri"], "live-feed")
        self.assertEqual(g.posts(tmp)[0]["kind"], "duplicate-skipped")
        r = g.haunt([GITHUB, SLACK, GOOGLE], tmp, ask=lambda i, f: POEM, poster=lambda posts: threads.append(posts) or ["at://x"],
                    fetch=lambda s: feeds[s["name"]], shooter=None, cards=FAKE_CARD, now=NOW, seen_live=lambda key: False)
        self.assertEqual(r["posted"], 0)                                      # remembered as posted from then on

    def test_grave_already_on_feed_is_marked_visited(self):
        tmp = tempfile.mkdtemp(); threads = []
        sites = Throwback.SITES[:1]
        n = g.throwback(sites, tmp, ask=lambda p: Throwback.ELEGY, poster=lambda posts: threads.append(posts) or ["at://x"],
                        lookup=lambda s: ("u", ["Get a free home page today"], ""), cards=FAKE_CARD, now=NOW, seen_live=lambda key: True)
        self.assertEqual((n, len(threads)), (0, 0))
        self.assertEqual(g.load_state(tmp, "throwback.json", []), ["GeoCities"])


class Ofrenda(unittest.TestCase):
    SAT = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        rows = [
            {"when": "2026-10-28T01:28:00Z", "kind": "haunting", "service": "Cloudflare", "id": "Cloudflare:k1", "title": "Durable Objects",
             "since": "2026-10-28T00:17:00Z", "uri": "at://x/1"},
            {"when": "2026-10-28T01:29:00Z", "kind": "resurrection", "service": "Cloudflare", "id": "Cloudflare:k1", "uri": "at://x/2"},
            {"when": "2026-10-28T01:29:00Z", "kind": "haunting", "service": "DigitalOcean", "id": "DigitalOcean:d1", "title": "Kubernetes",
             "since": "2026-10-27T22:14:00Z", "uri": "at://x/3"},
            {"when": "2026-10-20T01:00:00Z", "kind": "haunting", "service": "GitHub", "id": "GitHub:old", "title": "Old",
             "since": "2026-10-20T00:00:00Z", "uri": "at://x/4"},                                   # last week: not on this altar
            {"when": "2026-10-29T01:00:00Z", "kind": "haunting", "service": "Slack", "id": "Slack:s1", "title": "Dry", "since": "2026-10-29T00:00:00Z"},  # never posted
        ]
        for r in rows:
            g._log(self.tmp, r)
        self.threads = []

    def poster(self, posts):
        self.threads.append(posts)
        return ["at://x/ofrenda"]

    def test_week_of_the_fallen(self):
        week, items, total = g.altar(g.fallen(self.tmp, self.SAT), self.SAT)
        self.assertEqual(week, "Oct 25 to 31")
        self.assertEqual([n for n, _ in items], ["DigitalOcean", "Cloudflare"])              # longest first; GitHub and Slack left out
        self.assertEqual(dict(items)["Cloudflare"], {"n": 1, "minutes": 72, "open": False})
        self.assertTrue(dict(items)["DigitalOcean"]["open"])
        self.assertEqual(g.ofrenda_text(week, items, total),
                         "🕯️ Ofrenda, week of Oct 25 to 31: 2 services fell. DigitalOcean 3 days (still haunted), Cloudflare 1 h 12 min. "
                         "3 days in the underworld in all. 👻")
        self.assertEqual(g.ofrenda_text("Nov 1 to 7", [], 0), "🕯️ Ofrenda, week of Nov 1 to 7: nobody fell. The candles burn for an empty altar. 👻")
        many = [(f"Service{n}", {"n": 1, "minutes": 60 + n, "open": False}) for n in range(40)]
        self.assertLessEqual(len(g.ofrenda_text("Nov 1 to 7", many, 3000)), 300)
        self.assertIn(" more.", g.ofrenda_text("Nov 1 to 7", many, 3000))

    def test_posts_once_with_a_card(self):
        n = g.ofrenda(self.tmp, poster=self.poster, cards=FAKE_CARD, now=self.SAT)
        self.assertEqual(n, 1)
        post = self.threads[0][0]
        self.assertTrue(post["text"].startswith("🕯️ Ofrenda, week of Oct 25 to 31: 2 services fell."))
        self.assertEqual(post["images"][0]["alt"], "card: Ofrenda")
        self.assertEqual(g.posts(self.tmp)[-1]["kind"], "ofrenda")
        self.assertEqual(g.ofrenda(self.tmp, poster=self.poster, cards=FAKE_CARD, now=self.SAT, seen_live=lambda k: True), 0)
        self.assertEqual(len(self.threads), 1)
        self.assertIn(g.fingerprint(g.ofrenda_key("Oct 25 to 31")), g.fingerprint(post["text"]))   # the live-feed check can find it


class Throwback(unittest.TestCase):
    SITES = [{"name": "GeoCities", "url": "http://www.geocities.com/", "when": "20090601", "born": 1994, "died": 2009, "note": "n"},
             {"name": "Vine", "url": "https://vine.co/", "when": "20160901", "born": 2013, "died": 2017, "note": "n"},
             {"name": "Orkut", "url": "http://www.orkut.com/", "when": "20120601", "born": 2004, "died": 2014, "note": "n"},
             {"name": "Napster", "url": "http://www.napster.com/", "when": "20000601", "born": 1999, "died": 2001, "note": "n"}]
    ELEGY = {"poem": "Neighbourhoods of blinking text,\nwe never knew what came next.", "quote": "Get a free home page today"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.threads = []

    def poster(self, posts):
        self.threads.append(posts)
        return [f"at://x/{len(self.threads)}-{n}" for n in range(len(posts))]

    def lookup(self, site):
        if site["name"] == "Vine":
            raise LookupError("no snapshot")
        return (f"https://web.archive.org/web/20090601/{site['url']}",
                ["Welcome to the neighbourhood.", "Get a free home page today and join millions of members."], "Last seen June 1, 2009")

    def test_wayback_lookup_builds_raw_url_and_paragraphs(self):
        calls = []

        def fetch_json(url):
            calls.append(url)
            return {"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20071202031442",
                                                       "url": "http://web.archive.org/web/20071202031442/http://www.geocities.com/"}}}

        def fetch_html(url):
            calls.append(url)
            return "<html><body><p>Get a free home page today and join millions of members on GeoCities.</p><p>short</p></body></html>"
        url, paragraphs, seen = g.wayback(self.SITES[0], fetch_json, fetch_html)
        self.assertEqual(url, "https://web.archive.org/web/20071202031442/http://www.geocities.com/")
        self.assertEqual(calls[1], "https://web.archive.org/web/20071202031442id_/http://www.geocities.com/")
        self.assertIn("url=http%3A%2F%2Fwww.geocities.com%2F&timestamp=20090601", calls[0])
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(seen, "Last seen December 2, 2007")

    def test_newsy_quote_falls_back_to_the_name(self):
        bad = {"poem": "Neighbourhoods of text,\nwe never knew what came next.", "quote": "Investigators said Mr. Doe was charged."}
        lookup = lambda site: ("https://web.archive.org/web/1/x", ["Investigators said Mr. Doe was charged.", "Get a free home page today"], "")
        g.throwback(self.SITES[:1], self.tmp, ask=lambda p: bad, poster=self.poster, lookup=lookup, cards=FAKE_CARD, now=NOW)
        self.assertIn("“GeoCities”", self.threads[0][-2]["text"])
        self.assertFalse(g.posts(self.tmp)[0]["verified"])

    def test_three_graves_then_the_next_three(self):
        n = g.throwback(self.SITES, self.tmp, ask=lambda prompt: self.ELEGY, poster=self.poster, lookup=self.lookup,
                        cards=FAKE_CARD, now=NOW)
        self.assertEqual(n, 3)                                               # Vine had no snapshot: skipped, not counted
        self.assertEqual(g.load_state(self.tmp, "throwback.json", []), ["GeoCities", "Orkut", "Napster"])
        first = self.threads[0][0]
        self.assertTrue(first["text"].startswith("🕯️ Throwback Thursday: in memoriam GeoCities (1994 to 2009)\n\n"))
        self.assertIn("“Get a free home page today”", first["text"])
        self.assertTrue(first["text"].endswith("Source: web.archive.org"))
        self.assertEqual(first["facets"][0]["features"][0]["uri"], "https://web.archive.org/web/20090601/http://www.geocities.com/")
        self.assertEqual(first["images"][0]["alt"], "card: In memoriam: GeoCities")
        self.assertTrue(g.posts(self.tmp)[0]["verified"])
        self.assertEqual(g.throwback(self.SITES, self.tmp, ask=lambda p: self.ELEGY, poster=self.poster, lookup=self.lookup,
                                     cards=FAKE_CARD, now=NOW), 0)          # every grave visited
        self.assertIn("3 graves visited", g.graveyard(self.tmp))

    def test_the_old_home_page_closes_each_grave(self):
        shots = []
        shooter = lambda url, name: shots.append(url) or {"data": b"jpeg", "mime": "image/jpeg", "alt": "x"}
        g.throwback(self.SITES, self.tmp, ask=lambda p: self.ELEGY, poster=self.poster, lookup=self.lookup,
                    cards=FAKE_CARD, now=NOW, count=1, shooter=shooter)
        last = self.threads[0][-1]
        self.assertEqual(shots[0], "https://web.archive.org/web/20090601id_/http://www.geocities.com/")   # no Wayback toolbar
        self.assertTrue(last["text"].startswith("GeoCities as it looked"))
        self.assertTrue(last["text"].endswith("Source: web.archive.org"))
        self.assertEqual(last["facets"][0]["features"][0]["uri"], "https://web.archive.org/web/20090601/http://www.geocities.com/")
        self.assertEqual(len(last["images"]), 1)
        self.assertIn("GeoCities home page", last["images"][0]["alt"])

    def test_dry_run_visits_but_remembers_nothing(self):
        n = g.throwback(self.SITES, self.tmp, ask=lambda p: self.ELEGY, poster=self.poster, lookup=self.lookup,
                        cards=FAKE_CARD, now=NOW, dry_run=True, count=1)
        self.assertEqual(n, 1)
        self.assertFalse((Path(self.tmp) / "ghost").exists())


class Rehearsal(unittest.TestCase):
    def test_rehearse_uses_history_and_posts_nothing(self):
        history = {"incidents": [dict(STATUSPAGE["incidents"][1], status="resolved", shortlink="https://stspg.io/old1",
                                      incident_updates=[{"body": "This incident has been resolved.", "created_at": "2026-10-20T10:00:00Z"}],
                                      created_at="2026-10-20T09:00:00Z")]}
        urls = []

        def fetch(site):
            urls.append(site["url"])
            return history if site["url"].endswith("incidents.json") else {"incidents": []}
        lines = []
        n = g.rehearse([GITHUB, SLACK, GOOGLE], ask=lambda i, f: dict(POEM, quote="This incident has been resolved."),
                       fetch=fetch, cards=FAKE_CARD, out=lines.append)
        self.assertEqual(n, 1)                                                  # only Statuspage feeds have a history file
        self.assertEqual(urls, ["https://www.githubstatus.com/api/v2/incidents.json"])
        self.assertIn("quote verified", lines[0])
        self.assertIn("“This incident has been resolved.”", lines[1])
        self.assertIn("down since Oct 20, 2026, 09:00 UTC", lines[1])


class Bluesky(unittest.TestCase):
    def test_link_facet_byte_offsets(self):
        text = "👻 An ode to Zoom\n\nSource: status.zoom.us"
        f = g.link_facet(text, "status.zoom.us", "https://status.zoom.us/x")
        b = text.encode("utf-8")
        self.assertEqual(b[f[0]["index"]["byteStart"]:f[0]["index"]["byteEnd"]].decode(), "status.zoom.us")
        self.assertEqual(g.link_facet(text, "missing", "u"), [])


if __name__ == "__main__":
    unittest.main()
