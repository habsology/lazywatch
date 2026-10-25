"""Tiger Data memory for lazywatch: Postgres + pgvector on Tiger Cloud.

Every check and every change is stored. Changes get an embedding from a local
open-weight model (embeddinggemma via Ollama), so you can ask "has this rule
changed before?" and get both keyword and meaning matches, fused by rank.
Needs TIGER_DATABASE_URL in the environment.

    python lazywatch_store.py init
    python lazywatch_store.py backfill   # once: load earlier local checks into the database
    python lazywatch_store.py stats
    python lazywatch_store.py search "licence fee"
"""
import json
import os
import sys
import urllib.request

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "embeddinggemma"
DIM = 768

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS checks (
    id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
    site text NOT NULL, url text NOT NULL, status text NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
    id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
    site text NOT NULL, url text NOT NULL, summary text, quote text, verified boolean,
    body text NOT NULL, embedding vector({DIM}));
CREATE INDEX IF NOT EXISTS changes_body_fts ON changes USING gin (to_tsvector('english', body));
"""


def connect():
    conn = psycopg.connect(os.environ["TIGER_DATABASE_URL"], autocommit=True)
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")  # must exist before the type can be registered
        register_vector(conn)
    except Exception:
        conn.close()
        raise
    return conn


def embed(text):
    body = json.dumps({"model": EMBED_MODEL, "input": text[:4000]}).encode()
    req = urllib.request.Request(EMBED_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["embeddings"][0]


def init():
    with connect() as conn:
        conn.execute(SCHEMA)


def record_check(site, status):
    with connect() as conn:
        conn.execute("INSERT INTO checks (site, url, status) VALUES (%s, %s, %s)",
                     (site["name"], site["url"], status))


def change_body(entry):
    return "\n".join([entry.get("summary", "")] + entry.get("added", []) + entry.get("removed", []))


def record_change(entry):
    body = change_body(entry)
    try:
        vec = Vector(embed(body))
    except Exception as exc:  # keyword search still works without the vector
        print(f"embedding skipped: {exc.__class__.__name__}", file=sys.stderr)
        vec = None
    with connect() as conn:
        conn.execute(
            "INSERT INTO changes (site, url, summary, quote, verified, body, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (entry["site"], entry["url"], entry.get("summary"), entry.get("quote"),
             bool(entry.get("verified")), body, vec))


def rrf(*ranked_lists, k=60):
    """Reciprocal rank fusion: merge several ranked id lists into one score per id."""
    scores = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=scores.get, reverse=True)


def search(query, limit=5):
    """Hybrid search: full-text ranks and vector ranks, fused with RRF."""
    with connect() as conn:
        by_keyword = [r[0] for r in conn.execute(
            "SELECT id FROM changes WHERE to_tsvector('english', body) @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank(to_tsvector('english', body), plainto_tsquery('english', %s)) DESC LIMIT 20",
            (query, query))]
        try:
            qv = Vector(embed(query))
            by_meaning = [r[0] for r in conn.execute(
                "SELECT id FROM changes WHERE embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT 20", (qv,))]
        except Exception:
            by_meaning = []
        ids = rrf(by_keyword, by_meaning)[:limit]
        if not ids:
            return []
        rows = conn.execute("SELECT id, at, site, summary, quote, verified FROM changes WHERE id = ANY(%s)",
                            (ids,)).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r[0]])


def backfill(state="state"):
    """Load the local state/ logs into the database, for checks made before the database was ready."""
    from pathlib import Path
    checks = Path(state) / "checks.jsonl"
    changes = Path(state) / "changes.jsonl"
    rows = [json.loads(x) for x in checks.read_text().splitlines()] if checks.exists() else []
    with connect() as conn:
        conn.execute("DELETE FROM checks")
        for r in rows:
            conn.execute("INSERT INTO checks (at, site, url, status) VALUES (%s, %s, %s, %s)",
                         (r["when"], r["site"], r.get("url", ""), r["status"]))
        conn.execute("DELETE FROM changes")
    entries = [json.loads(x) for x in changes.read_text().splitlines()] if changes.exists() else []
    for e in entries:
        record_change(e)
    return f"{len(rows)} checks and {len(entries)} changes loaded from {state}/"


def stats(days=7):
    with connect() as conn:
        checks, woke = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE status LIKE 'changed%%') FROM checks "
            "WHERE at > now() - make_interval(days => %s)", (days,)).fetchone()
        sites = conn.execute("SELECT count(DISTINCT url) FROM checks").fetchone()[0]
    return f"{sites} pages, {checks} checks in {days} days, model woken {woke} times."


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "init":
        init()
        print("tables ready")
    elif cmd == "backfill":
        print(backfill())
    elif cmd == "stats":
        print(stats())
    elif cmd == "search" and len(sys.argv) > 2:
        for _id, at, site, summary, quote, verified in search(" ".join(sys.argv[2:])):
            print(f"{at:%Y-%m-%d} {site}: {summary} {'(verified)' if verified else '(unverified)'}\n  > {quote}")
    else:
        print(__doc__)
