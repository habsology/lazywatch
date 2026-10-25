# lazywatch

Watch the public rules pages you care about without reading them. Once a day a cron job fetches each page. If nothing changed, the model is never called. If something changed, a local open-weight model (Gemma 3 4B through Ollama) summarises only the changed paragraphs, and the summary is kept only if its quote appears word for word in the new page text. Otherwise you get the raw change, clearly marked unverified. Once a week you read one short markdown digest.

Nothing leaves your machine except the page fetches.

## Run it

```
ollama pull gemma3:4b
copy sites.example.json sites.json        # then edit the URLs
python lazywatch.py check                 # run daily; first run only stores snapshots
python lazywatch.py digest --days 7 > digest.md
python -m unittest                        # 20 tests, no model, no accounts needed
```

Python 3.9+, standard library only. Windows Task Scheduler or cron can run `check` daily.

## Durable version (Temporal)

`lazywatch_temporal.py` runs the same check as a Temporal workflow: one activity per site with its own retry policy (5 attempts, 10 s backoff doubling); a page that is dead after 5 attempts is reported, and the other pages still run, so a slow page or a model that is still loading is retried on its own, and a crash halfway through a run resumes with the finished sites kept. Everything stays on your machine with the free local dev server.

```
pip install temporalio
temporal server start-dev              # https://docs.temporal.io/cli, open source, no account
python lazywatch_temporal.py worker    # terminal 1, keep running
python lazywatch_temporal.py once      # terminal 2: run a check now
python lazywatch_temporal.py schedule  # or create the daily 06:00 schedule
```

The dev server UI at http://localhost:8233 shows every run, retry and failure.

## JavaScript pages (Browserbase)

Some rules pages render their text in the browser, so a plain fetch sees almost nothing. Mark those `"render": true` in `sites.json` and lazywatch opens them in a real browser on Browserbase. `"render": "auto"` tries the plain download first and switches to the browser only if the page refuses it (a 404 is not a refusal: a dead page stays dead instead of snapshotting the error page); the choice is remembered so later checks compare like with like. If a page's fetch path ever changes, its first snapshot on the new path replaces the old one and no change is reported, because a plain fetch and a browser see different text. Set `BROWSERBASE_API_KEY` (and `BROWSERBASE_PROJECT_ID` if your dashboard shows one). The free plan is enough for a few pages a day.

```
python lazywatch_browser.py compare https://www.canada.ca/en/news.html
```

prints how many paragraphs the plain fetch saw against the real browser. On our first run, canada.ca refused the plain download and the browser read 190 paragraphs.

## Memory (Tiger Data)

With `TIGER_DATABASE_URL` set to a Tiger Cloud Postgres service, every check and every change is stored. Changes get an embedding from a local open-weight model (`ollama pull embeddinggemma`), so you can ask whether a rule has changed before and get both keyword and meaning matches, fused by reciprocal rank:

```
python lazywatch_store.py init
python lazywatch_store.py stats
python lazywatch_store.py search "licence fee"
```

Without the variable set, lazywatch keeps working from the local `state/` folder.

## Limits

- It compares page text. A page that renders its rules with JavaScript looks empty to the plain fetch; mark it `"render": true` so it goes through the real browser. `python lazywatch.py peek` shows what each snapshot holds, so you can tell a real page from a "please enable JavaScript" shell.
- Atom and RSS feeds work as pages: each entry's title and summary become one paragraph each. Feeds must stay on the plain fetch (no `render` key), since a browser would show its XML viewer instead.
- A 4B model can misread a change. The verbatim-quote check catches invented text, not a wrong interpretation, so read the quote.
- Be polite: one fetch per page per day, and respect each site's terms.

MIT licence.
