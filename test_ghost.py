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


class Verse(unittest.TestCase):
    def setUp(self):
        self.inc = g.open_incidents(GITHUB, STATUSPAGE)[0]

    def test_quote_must_be_verbatim(self):
        self.assertEqual(g.verify_quote(POEM, self.inc), (POEM["quote"], True))
        made_up = {"quote": "Everything is on fire and the cause is unknown."}
        self.assertEqual(g.verify_quote(made_up, self.inc), ("Incident with Actions", False))
        self.assertEqual(g.verify_quote({"quote": "  “We have identified the cause and are working on a fix.” "}, self.inc)[1], True)
        self.assertTrue(g.verify_quote({"quote": "we have identified the cause and are working on a fix"}, self.inc)[1])  # case and full stop
        self.assertFalse(g.verify_quote({"quote": "the cause"}, self.inc)[1])                                        # too short to mean anything
        self.assertFalse(g.verify_quote({"quote": "We have identified the cause and are working on the fix."}, self.inc)[1])  # one word off

    def test_post_fits_bluesky_and_quotes_the_page(self):
        text = g.compose(self.inc, "ode", g.clean_poem(POEM), POEM["quote"])
        self.assertTrue(text.startswith("👻 An ode to GitHub, down since 14:02 UTC\n\n"))
        self.assertIn("“We have identified the cause and are working on a fix.”", text)
        self.assertLessEqual(len(text), 300)

    def test_long_verse_is_trimmed_not_rejected(self):
        poem = "\n".join(["a line that goes on and on and on and on and on and on, yes"] * 4)
        text = g.compose(self.inc, "ballad", poem, "x" * 120)
        self.assertLessEqual(len(text), 300)
        self.assertIn("“x", text)

    def test_tone_guard(self):
        self.assertTrue(g.too_mean("Their useless engineers broke it again"))
        self.assertFalse(g.too_mean("The runners rest, the queue grows long"))

    def test_form_is_stable_per_incident(self):
        self.assertEqual(g.pick_form("GitHub:abc1"), g.pick_form("GitHub:abc1"))
        self.assertIn(g.pick_form("GitHub:abc1"), dict(g.FORMS))

    def test_resurrection(self):
        text = g.resurrection_text("GitHub", "Incident with Actions", "2 h 14 min")
        self.assertEqual(text, "👻 GitHub has risen. “Incident with Actions” haunted the status page for 2 h 14 min. Lights back on, candles out. 🕯️")
        self.assertLessEqual(len(g.resurrection_text("GitHub", "t" * 400, "5 min")), 300)
        self.assertEqual(g._duration("2026-10-28T13:16:00Z", NOW), "2 h 14 min")
        self.assertEqual(g._duration("2026-10-26T15:30:00Z", NOW), "2 days")


class Haunting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posted = []

    def poster(self, text, incident=None, image=None):
        self.posted.append((text, incident["url"] if incident else None))
        return f"at://did:plc:ghost/app.bsky.feed.post/{len(self.posted)}"

    def run_pass(self, feeds, dry_run=False, now=NOW, ask=lambda inc, form: POEM):
        sites = [GITHUB, SLACK, GOOGLE]
        return g.haunt(sites, self.tmp, ask=ask, poster=self.poster, fetch=lambda s: feeds[s["name"]],
                       shooter=None, now=now, dry_run=dry_run)

    def test_new_incident_is_posted_once_then_resurrected(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": {"active_incidents": []}, "Google Cloud": []}
        r = self.run_pass(feeds)
        self.assertEqual((r["checked"], r["open"], r["posted"]), (3, 1, 1))
        self.assertEqual(self.posted[0][1], "https://stspg.io/abc1")
        self.assertEqual(self.run_pass(feeds)["posted"], 0)                 # still open: nothing new to say
        feeds["GitHub"] = {"incidents": []}
        self.assertEqual(self.run_pass(feeds)["posted"], 1)                 # cleared: the resurrection
        self.assertIn("GitHub has risen", self.posted[-1][0])
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
                    shooter=None, now=NOW)
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

    def test_graveyard(self):
        feeds = {"GitHub": STATUSPAGE, "Slack": SLACK_FEED, "Google Cloud": GOOGLE_FEED}
        self.run_pass(feeds)
        out = g.graveyard(self.tmp)
        self.assertIn("3 hauntings, 0 resurrections", out)
        self.assertIn("Still haunted: GitHub, Google Cloud, Slack", out)


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
                       fetch=fetch, out=lines.append)
        self.assertEqual(n, 1)                                                  # only Statuspage feeds have a history file
        self.assertEqual(urls, ["https://www.githubstatus.com/api/v2/incidents.json"])
        self.assertIn("quote verified", lines[0])
        self.assertIn("“This incident has been resolved.”", lines[1])


class Bluesky(unittest.TestCase):
    def test_link_card(self):
        inc = g.open_incidents(GITHUB, STATUSPAGE)[0]
        card = g.link_card(inc, thumb={"$type": "blob"})
        self.assertEqual(card["$type"], "app.bsky.embed.external")
        self.assertEqual(card["external"]["uri"], "https://stspg.io/abc1")
        self.assertEqual(card["external"]["title"], "Incident with Actions")
        self.assertEqual(card["external"]["thumb"], {"$type": "blob"})


if __name__ == "__main__":
    unittest.main()
