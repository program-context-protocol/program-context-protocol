"""Temporal worker bootstrap. Connects to localhost:7233 by default,
matching `temporal server start-dev`'s own default address — that server is
a separate, explicit step you run yourself (this module never spawns it).
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from pcp.process.activities import build_module_activity
from pcp.process.workflows import BuildModuleWorkflow

TASK_QUEUE = "pcp-build"


async def run_worker(target_host: str = "localhost:7233") -> None:
    client = await Client.connect(target_host)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BuildModuleWorkflow],
        activities=[build_module_activity],
    )
    await worker.run()


def main(target_host: str = "localhost:7233") -> None:
    asyncio.run(run_worker(target_host))
