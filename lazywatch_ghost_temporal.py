"""Durable Downtime Ghost: one haunting pass as a Temporal workflow, one activity per status page.

Each status page is its own activity with its own retry policy, so a feed that times out or a model
that is still loading is retried on its own, and a worker that dies mid-haunting is replaced: the
workflow resumes on the next worker at the page it was on, and the pages already done are not read
twice. Without BLUESKY_APP_PASSWORD every activity is a dry run that prints what it would post.
GHOST_PAUSE=5 makes each page linger five seconds, long enough to kill the worker mid-haunting on purpose.

    temporal server start-dev                   # local dev server, no account; UI at http://localhost:8233
    python lazywatch_ghost_temporal.py worker   # keep running (Ctrl+C mid-run to watch the ghost come back)
    python lazywatch_ghost_temporal.py once     # one pass over statuspages.json (optional: another file)
    python lazywatch_ghost_temporal.py schedule # create the hourly schedule once
"""
import asyncio
import contextvars
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import (Client, Schedule, ScheduleActionStartWorkflow,
                               ScheduleIntervalSpec, ScheduleSpec)
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import lazywatch_ghost

TASK_QUEUE = "downtime-ghost"
STATE = "state"
OUT = "out"


@activity.defn
def haunt_page(site: dict) -> dict:
    """One status page: read the feed, write verse for a new incident, post it. Heartbeats every 10 s,
    so a dead worker is noticed within 30 s and the page is retried on the next one."""
    done = threading.Event()

    def beat():
        while not done.wait(10):
            activity.heartbeat(site["name"])

    threading.Thread(target=contextvars.copy_context().run, args=(beat,), daemon=True).start()
    try:
        if os.environ.get("GHOST_PAUSE"):            # demo aid: linger on each page, to watch a killed worker's retry in the UI
            time.sleep(float(os.environ["GHOST_PAUSE"]))
        dry = not os.environ.get("BLUESKY_APP_PASSWORD")
        r = lazywatch_ghost.haunt([site], STATE, dry_run=dry, out_dir=OUT)
        return {"name": site["name"], "open": r["open"], "posted": r["posted"], "failed": bool(r["failed"]), "dry": dry}
    finally:
        done.set()


@workflow.defn
class Haunting:
    @workflow.run
    async def run(self, sites: list) -> list:
        results = []
        for site in sites:                      # one page at a time, so a killed worker resumes at the page it was on
            try:
                r = await workflow.execute_activity(
                    haunt_page, site,
                    start_to_close_timeout=timedelta(minutes=8),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10),
                                             backoff_coefficient=2.0, maximum_attempts=5),
                )
                status = (f"{r['open']} open, {r['posted']} " + ("would post" if r["dry"] else "posted")
                          + (", feed failed" if r["failed"] else ""))
            except ActivityError:               # one dead feed must not stop the other pages
                status = "failed after 5 attempts"
            results.append([site["name"], status])
        return results


async def run_worker():
    client = await Client.connect("localhost:7233")
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[Haunting],
                    activities=[haunt_page], activity_executor=ThreadPoolExecutor(2))
    print("worker up on", TASK_QUEUE)
    await worker.run()


async def create_schedule(config="statuspages.json"):
    client = await Client.connect("localhost:7233")
    sites = json.loads(Path(config).read_text())
    await client.create_schedule(
        "downtime-ghost-hourly",
        Schedule(action=ScheduleActionStartWorkflow(Haunting.run, sites, id="downtime-ghost-hourly-run",
                                                    task_queue=TASK_QUEUE),
                 spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=1))])),
    )
    print("schedule created: every hour, see http://localhost:8233")


async def run_once(config="statuspages.json"):
    client = await Client.connect("localhost:7233")
    sites = json.loads(Path(config).read_text())
    run_id = "downtime-ghost-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    handle = await client.start_workflow(Haunting.run, sites, id=run_id, task_queue=TASK_QUEUE)
    print(f"workflow {run_id}  http://localhost:8233/namespaces/default/workflows/{run_id}", flush=True)
    try:
        results = await asyncio.wait_for(handle.result(), timeout=1800)
    except asyncio.TimeoutError:
        print("gave up after 30 minutes; is a worker running? (python lazywatch_ghost_temporal.py worker)")
        return
    for name, status in results:
        print(f"{name}: {status}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "worker"
    extra = sys.argv[2:3]
    asyncio.run({"worker": run_worker, "schedule": create_schedule, "once": run_once}[cmd](*extra))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)      # a "once" run on Windows was seen to print its results and then never exit
