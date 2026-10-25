import json
import os
import tempfile
import unittest
from pathlib import Path

import lazywatch as lw

V1 = "<html><body><nav>Menu menu menu menu menu menu</nav><p>Short-term rentals need a licence renewed every year before March.</p><p>Fines for operating without a licence start at 500 dollars per night.</p></body></html>"
V2 = V1.replace("start at 500 dollars", "start at 900 dollars")
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en-ca">
<title>Montréal - Weather - Environment Canada</title>
<updated>2026-10-24T18:00:00Z</updated>
<entry>
<title>No watches or warnings in effect, Montréal</title>
<updated>2026-10-24T18:00:00Z</updated>
<summary type="html">No watches or warnings in effect.</summary>
</entry>
<entry>
<title>Current Conditions: Partly Cloudy, 8.2&#xB0;C</title>
<updated>2026-10-24T18:00:00Z</updated>
<summary type="html">&lt;b&gt;Observed at:&lt;/b&gt; Montréal Int'l Airport 2:00 PM EDT Saturday 24 October 2026 &lt;br/&gt;
&lt;b&gt;Temperature:&lt;/b&gt; 8.2&amp;deg;C &lt;br/&gt;</summary>
</entry>
</feed>"""


class Test(unittest.TestCase):
    def setUp(self):
        for v in ("TIGER_DATABASE_URL", "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"):
            os.environ.pop(v, None)  # tests never touch real services
        self.d = Path(tempfile.mkdtemp())
        (self.d / "sites.json").write_text(json.dumps([{"name": "Demo", "url": "http://x/rules"}]))
        self.calls = 0

    def run_check(self, html, reply):
        def ask(name, added, removed):
            self.calls += 1
            return reply
        return lw.check(self.d / "sites.json", self.d / "state", ask=ask, fetcher=lambda u: html)

    def test_nav_ignored(self):
        self.assertEqual(len(lw.paragraphs(V1)), 2)

    def test_unchanged_never_calls_model(self):
        self.run_check(V1, {})
        res = self.run_check(V1, {})
        self.assertEqual(self.calls, 0)
        self.assertIn("model not called", res[0][1])

    def test_verified_change(self):
        self.run_check(V1, {})
        good = {"summary": "Fine went up.", "quote": "Fines for operating without a licence start at 900 dollars per night."}
        res = self.run_check(V2, good)
        self.assertEqual(self.calls, 1)
        self.assertIn("verified", res[0][1])
        self.assertIn("900 dollars", lw.digest(self.d / "state"))

    def test_invented_quote_is_flagged(self):
        self.run_check(V1, {})
        res = self.run_check(V2, {"summary": "Fine doubled.", "quote": "Fines are now 2000 dollars."})
        self.assertIn("UNVERIFIED", res[0][1])
        self.assertIn("NOT verified", lw.digest(self.d / "state"))

    def test_fetch_error_propagates_for_retry(self):
        def boom(u):
            raise OSError("timeout")
        with self.assertRaises(OSError):
            lw.check_site({"name": "Demo", "url": "http://x"}, self.d / "state", ask=lambda *a: {}, fetcher=boom)

    def test_temporal_module_loads(self):
        try:
            import lazywatch_temporal as lt
        except ImportError:
            self.skipTest("temporalio not installed")
        self.assertTrue(hasattr(lt.DailyCheck, "run"))

    def test_checks_logged_and_effort_line(self):
        self.run_check(V1, {})
        self.run_check(V1, {})
        self.assertIn("2 page checks", lw.effort_line(self.d / "state"))
        self.assertIn("woken 0 times", lw.effort_line(self.d / "state"))

    def test_plain_fetcher_by_default(self):
        self.assertIs(lw.pick_fetcher({"name": "x", "url": "http://x"}), lw.fetch)

    def test_auto_render_falls_back_once_and_sticks(self):
        import sys, types
        calls = []
        fake = types.ModuleType("lazywatch_browser")
        fake.fetch_rendered = lambda url: calls.append(url) or "<p>" + "rendered text long enough to count" + "</p>"
        sys.modules["lazywatch_browser"] = fake
        self.addCleanup(sys.modules.pop, "lazywatch_browser", None)
        state = self.d / "state"
        state.mkdir()
        site = {"name": "blocked", "url": "http://blocked.invalid/", "render": "auto"}

        def refuse(url):
            raise ConnectionError("closed")

        original = lw.fetch
        lw.fetch = refuse
        self.addCleanup(setattr, lw, "fetch", original)
        self.assertTrue(lw.check_site(site, state, ask=None).startswith("first snapshot"))
        self.assertEqual(len(calls), 1)
        self.assertIs(lw.pick_fetcher(site, state), fake.fetch_rendered)  # remembered

    def test_auto_render_lets_a_dead_page_fail(self):
        import urllib.error
        state = self.d / "state"
        state.mkdir()
        site = {"name": "gone", "url": "http://gone.invalid/", "render": "auto"}

        def not_found(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        original = lw.fetch
        lw.fetch = not_found
        self.addCleanup(setattr, lw, "fetch", original)
        with self.assertRaises(urllib.error.HTTPError):  # Temporal retries, then reports it; no browser
            lw.check_site(site, state, ask=None)
        self.assertFalse((state / (lw._key(site) + ".browser")).exists())

    def test_fetch_path_change_starts_the_page_over(self):
        self.run_check(V1, {})
        state = self.d / "state"
        site = {"name": "Demo", "url": "http://x/rules"}
        (state / (lw._key(site) + ".browser")).write_text("switched")  # now on the real browser
        status = lw.check_site(site, state, ask=lambda *a: self.fail("model must not run"), fetcher=lambda u: V2)
        self.assertIn("fetch path changed", status)
        self.assertTrue(status.startswith("first snapshot"))
        self.assertIn("unchanged", lw.check_site(site, state, ask=None, fetcher=lambda u: V2))

    def test_legacy_list_snapshot_still_compares(self):
        state = self.d / "state"
        state.mkdir()
        site = {"name": "Demo", "url": "http://x/rules"}
        (state / (lw._key(site) + ".json")).write_text(json.dumps(lw.paragraphs(V1)))
        self.assertIn("unchanged", lw.check_site(site, state, ask=None, fetcher=lambda u: V1))

    def test_auto_render_keeps_plain_when_it_works(self):
        state = self.d / "state"
        state.mkdir()
        site = {"name": "open", "url": "http://open.invalid/", "render": "auto"}
        (state / (lw._key(site) + ".json")).write_text("[]")
        self.assertIs(lw.pick_fetcher(site, state), lw.fetch)

    def test_store_skipped_without_env(self):
        lw._store("record_check", {"name": "x", "url": "http://x"}, "unchanged")  # must not raise

    def test_rrf_fuses_ranks(self):
        try:
            import lazywatch_store as ls
        except ImportError:
            self.skipTest("psycopg/pgvector not installed")
        self.assertEqual(ls.rrf([1, 2, 3], [3, 1, 4])[:2], [1, 3])
        self.assertIn("Fine", ls.change_body({"summary": "Fine went up.", "added": ["Fines start at 900."]}))

    def test_browser_session_kwargs(self):
        try:
            import lazywatch_browser as lb
        except ImportError:
            self.skipTest("browserbase/playwright not installed")
        self.assertEqual(lb._session_kwargs({}), {})
        self.assertEqual(lb._session_kwargs({"BROWSERBASE_PROJECT_ID": "p1"}), {"project_id": "p1"})

    def test_peek_lists_each_page(self):
        self.run_check(V1, {})
        text = lw.peek(self.d / "sites.json", self.d / "state")
        self.assertIn("## Demo", text)
        self.assertIn("plain fetch, 2 paragraphs", text)
        self.assertIn("900 dollars", text.replace("500 dollars", "900 dollars"))

    def test_empty_digest(self):
        self.assertIn("Nothing changed", lw.digest(self.d / "state"))

    def test_atom_feed_entries_become_paragraphs(self):
        got = lw.paragraphs(ATOM)
        self.assertEqual(got[0], "Montréal - Weather - Environment Canada")
        self.assertIn("No watches or warnings in effect, Montréal", got)
        self.assertIn("Current Conditions: Partly Cloudy, 8.2°C", got)
        self.assertIn("Observed at: Montréal Int'l Airport 2:00 PM EDT Saturday 24 October 2026 Temperature: 8.2°C", got)
        self.assertFalse(any("<b>" in p or "2026-10-24T18" in p for p in got), got)
        hourly = ATOM.replace("Partly Cloudy, 8.2", "Mostly Cloudy, 9.1")
        added, removed = lw.changed_paragraphs(got, lw.paragraphs(hourly))
        self.assertEqual((added, removed), (["Current Conditions: Mostly Cloudy, 9.1°C"], ["Current Conditions: Partly Cloudy, 8.2°C"]))

    def test_rss_items_and_broken_xml(self):
        rss = "<rss version=\"2.0\"><channel><title>Montreal weather alerts feed title</title><item><title>Rainfall warning in effect for the island</title><description>&lt;p&gt;Up to 40 mm of rain is expected tonight.&lt;/p&gt;</description></item></channel></rss>"
        self.assertEqual(lw.paragraphs(rss), ["Montreal weather alerts feed title", "Rainfall warning in effect for the island", "Up to 40 mm of rain is expected tonight."])
        broken = "<?xml version=\"1.0\"?><feed><entry><title>An entry that never closes properly in this feed"
        self.assertEqual(lw.paragraphs(broken), ["An entry that never closes properly in this feed"])  # falls back to the HTML path


if __name__ == "__main__":
    unittest.main()
