"""Durable lazywatch: the daily check runs inside a Temporal workflow.

Each site check is an activity with its own retry policy, so a page that times
out or a model that is still loading is retried on its own, and a crash
mid-run resumes with the sites already checked kept.

    temporal server start-dev            # local dev server, no account
    python lazywatch_temporal.py worker  # keep running
    python lazywatch_temporal.py once    # run a check now (optional: a sites file)
    python lazywatch_temporal.py schedule  # create the daily 06:00 schedule once
"""
import asyncio
import contextvars
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import (Client, Schedule, ScheduleActionStartWorkflow,
                               ScheduleCalendarSpec, ScheduleSpec)
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import lazywatch

TASK_QUEUE = "lazywatch"
STATE = "state"


@activity.defn
def check_site(site: dict) -> str:
    """Runs lazywatch.check_site with a heartbeat every 10 s, so a dead worker is noticed in 30 s."""
    done = threading.Event()

    def beat():
        while not done.wait(10):
            activity.heartbeat(site["name"])

    threading.Thread(target=contextvars.copy_context().run, args=(beat,), daemon=True).start()
    try:
        return lazywatch.check_site(site, STATE)
    finally:
        done.set()


@workflow.defn
class DailyCheck:
    @workflow.run
    async def run(self, sites: list) -> list:
        results = []
        for site in sites:
            try:
                status = await workflow.execute_activity(
                    check_site, site,
                    start_to_close_timeout=timedelta(minutes=6),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10),
                                             backoff_coefficient=2.0, maximum_attempts=5),
                )
            except ActivityError:  # one dead page must not stop the other checks
                status = "failed after 5 attempts"
            results.append([site["name"], status])
        return results


async def run_worker():
    client = await Client.connect("localhost:7233")
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[DailyCheck],
                    activities=[check_site], activity_executor=ThreadPoolExecutor(2))
    print("worker up on", TASK_QUEUE)
    await worker.run()


async def create_schedule(config="sites.json"):
    client = await Client.connect("localhost:7233")
    sites = json.loads(Path(config).read_text())
    await client.create_schedule(
        "lazywatch-daily",
        Schedule(action=ScheduleActionStartWorkflow(DailyCheck.run, sites, id="lazywatch-daily-run",
                                                    task_queue=TASK_QUEUE),
                 spec=ScheduleSpec(calendars=[ScheduleCalendarSpec(hour=6)])),
    )
    print("schedule created: every day at 06:00, see http://localhost:8233")


async def run_once(config="sites.json"):
    client = await Client.connect("localhost:7233")
    sites = json.loads(Path(config).read_text())
    run_id = "lazywatch-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    handle = await client.start_workflow(DailyCheck.run, sites, id=run_id, task_queue=TASK_QUEUE)
    print(f"workflow {run_id}  http://localhost:8233/namespaces/default/workflows/{run_id}", flush=True)
    try:
        results = await asyncio.wait_for(handle.result(), timeout=900)
    except asyncio.TimeoutError:
        print("gave up after 15 minutes; is a worker running? (python lazywatch_temporal.py worker)")
        return
    for name, status in results:
        print(f"{name}: {status}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "worker"
    extra = sys.argv[2:3]  # optional sites file for schedule / once
    asyncio.run({"worker": run_worker, "schedule": create_schedule, "once": run_once}[cmd](*extra))
    # The client has printed everything it will print; leave without waiting on interpreter teardown
    # (on Windows a "once" run was seen to print its results and then never exit).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
