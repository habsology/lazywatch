"""Fetch a rules page through a real browser on Browserbase.

Many municipal and platform pages render their text with JavaScript, so a plain
HTTP fetch sees a shell. Mark such a site "render": true in sites.json and
lazywatch opens it in a Browserbase session instead. Needs BROWSERBASE_API_KEY
(and optionally BROWSERBASE_PROJECT_ID) in the environment.

    python lazywatch_browser.py compare URL   # paragraphs seen plain vs rendered
"""
import os
import sys

from browserbase import Browserbase
from playwright.sync_api import sync_playwright

import lazywatch


def _session_kwargs(env=os.environ):
    pid = env.get("BROWSERBASE_PROJECT_ID")
    return {"project_id": pid} if pid else {}


def fetch_rendered(url, settle_ms=3000):
    bb = Browserbase(api_key=os.environ["BROWSERBASE_API_KEY"])
    session = bb.sessions.create(**_session_kwargs())
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(session.connect_url)
        try:
            page = browser.contexts[0].pages[0]
            # "domcontentloaded" plus a settle pause: busy pages never reach "networkidle".
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(settle_ms)
            return page.content()
        finally:
            browser.close()


def screenshot(url, settle_ms=3000, width=1200, height=800):
    """A JPEG of the page as a reader sees it (the ghost's link-card thumbnail)."""
    bb = Browserbase(api_key=os.environ["BROWSERBASE_API_KEY"])
    session = bb.sessions.create(**_session_kwargs())
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(session.connect_url)
        try:
            page = browser.contexts[0].pages[0]
            page.set_viewport_size({"width": width, "height": height})
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if "web.archive.org/" in url:              # archived images come one by one from the archive: give them time
                try:
                    page.wait_for_load_state("load", timeout=30_000)
                except Exception:
                    pass
                settle_ms = max(settle_ms, 5000)
            page.wait_for_timeout(settle_ms)
            return page.screenshot(type="jpeg", quality=80)
        finally:
            browser.close()


def _count(fetcher, url):
    try:
        return str(len(lazywatch.paragraphs(fetcher(url)))) + " paragraphs"
    except Exception as exc:
        return f"failed ({exc.__class__.__name__}: {str(exc)[:80]})"


def compare(url):
    return f"{url}\n  plain fetch: {_count(lazywatch.fetch, url)}\n  real browser: {_count(fetch_rendered, url)}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "compare":
        print(compare(sys.argv[2]))
    else:
        print(__doc__)
