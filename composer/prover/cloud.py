"""Cloud prover integration.

Polls a Certora cloud job to completion using the anonymousKey embedded in the
job URL (so waiting needs no credentials), then retrieves its results through
``prover_output_utility``.
"""

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse, parse_qs

import aiohttp
from prover_output_utility import ProverOutputAPI
from prover_output_utility.models import JobStatus, convert_job_status

logger = logging.getLogger("composer.spec")


class CloudJobError(RuntimeError):
    """Raised when a cloud prover job reaches a terminal status other than
    SUCCEEDED, does not reach a terminal status within the poll timeout, or
    finishes without an output archive. Carries the ``status`` and prover
    ``link``.
    """

    def __init__(self, status: JobStatus, link: str) -> None:
        super().__init__(f"Cloud job ended with status {status.value}")
        self.status = status
        self.link = link


# Terminal cloud job statuses (the job is no longer running). A status outside
# this set means the job is still in progress.
_TERMINAL_STATUSES = frozenset({
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELED,
    JobStatus.HALTED,
    JobStatus.SERVICE_UNAVAILABLE,
    JobStatus.UPLOAD_FAILED,
})

# Avoid requesting Brotli — aiohttp's brotli support is often broken/missing.
_NO_BROTLI_HEADERS = {"Accept-Encoding": "gzip, deflate"}


@dataclass
class CloudJob:
    """Parsed cloud prover job reference."""
    base_url: str       # e.g. "https://prover.certora.com"
    user_id: str
    job_id: str
    anonymous_key: str

    @property
    def job_data_url(self) -> str:
        return f"{self.base_url}/jobData/{self.user_id}/{self.job_id}?anonymousKey={self.anonymous_key}"


def parse_cloud_link(link: str) -> CloudJob:
    """Parse a CertoraRunResult.link URL into a CloudJob.

    Expected format:
        https://prover.certora.com/jobStatus/{user_id}/{job_id}?anonymousKey=...
    """
    parsed = urlparse(link)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    # Expect: ["jobStatus", user_id, job_id]
    if len(parts) < 3 or parts[0] != "jobStatus":
        raise ValueError(f"Unexpected cloud link format: {link}")

    user_id = parts[1]
    job_id = parts[2]

    qs = parse_qs(parsed.query)
    keys = qs.get("anonymousKey", [])
    if not keys:
        raise ValueError(f"No anonymousKey in cloud link: {link}")

    return CloudJob(
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        user_id=user_id,
        job_id=job_id,
        anonymous_key=keys[0],
    )


async def _poll_job_inner(
    job: CloudJob,
    *,
    interval: float,
    on_status: Callable[[str], Awaitable[None]] | None,
) -> dict:
    url = job.job_data_url

    async with aiohttp.ClientSession(headers=_NO_BROTLI_HEADERS) as session:
        while True:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                data = await resp.json()

            status = data.get("jobStatus", "UNKNOWN")

            if on_status is not None:
                await on_status(status)

            if convert_job_status(status) in _TERMINAL_STATUSES:
                return data

            await asyncio.sleep(interval)

async def poll_job(
    job: CloudJob,
    *,
    timeout: float,
    interval: float = 10.0,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Poll /jobData until the job reaches a terminal status.

    Returns the full jobData JSON dict.
    Raises TimeoutError if the job doesn't finish within `timeout` seconds.
    """
    return await asyncio.wait_for(_poll_job_inner(job, interval=interval, on_status=on_status), timeout=timeout)

def _job_runtime_ms(job_data: dict) -> int | None:
    """Prover execution time (ms) from the cloud job's ``startTime``→``finishTime`` —
    the post-dequeue run window.

    EXCLUDES queue wait: the job is created at ``postTime``, sits in the queue, then
    ``startTime`` marks when the prover actually began executing. Returns ``None``
    if either timestamp is absent or unparseable, so usage capture never breaks a run.
    """
    start, finish = job_data.get("startTime"), job_data.get("finishTime")
    if not start or not finish:
        return None
    try:
        return int((datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=1)
def _results_api() -> ProverOutputAPI:
    """Client for reading job results. Cached because constructing one logs in.

    ``enable_cache=False``: each job's documents are read once, and building
    POU's cache would mkdir ``<cwd>/.certora_internal/api_cache`` in whatever
    directory composer was invoked from.
    """
    return ProverOutputAPI(enable_cache=False)


@asynccontextmanager
async def cloud_results(
    run_result_link: str,
    *,
    poll_timeout: float,
    poll_callback: Callable[[str, str], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[Path, int | None]]:
    """Async context manager: poll cloud job, download results, yield (path, runtime_ms),
    clean up.

    Parses the cloud link, polls until completion, downloads and extracts the results
    archive, then yields ``(results_root, runtime_ms)`` where ``runtime_ms`` is the prover's
    queue-free execution time from the job's ``startTime``→``finishTime`` (``None`` if
    unavailable). The temporary directory is cleaned up on exit.
    """
    cloud_job = parse_cloud_link(run_result_link)

    logger.info("Cloud job submitted: %s/%s", cloud_job.user_id, cloud_job.job_id)

    async def on_status(status: str) -> None:
        logger.info("Cloud job %s status: %s", cloud_job.job_id[:8], status)
        if poll_callback:
            await poll_callback(status, f"Cloud job {cloud_job.job_id[:8]}: {status}")

    try:
        job_data = await poll_job(cloud_job, timeout=poll_timeout, on_status=on_status)
    except TimeoutError as exc:
        raise CloudJobError(JobStatus.UNKNOWN, run_result_link) from exc

    status = convert_job_status(job_data.get("jobStatus", "UNKNOWN"))
    if status is not JobStatus.SUCCEEDED:
        raise CloudJobError(status, run_result_link)

    runtime_ms = _job_runtime_ms(job_data)

    with tempfile.TemporaryDirectory(prefix="certora_cloud_") as tmp_dir:
        dest = Path(tmp_dir)
        # Fetch just the tree view and the compiled sources, which is everything
        # the results parse and the CEX analyzer read. The job's zipOutput archive
        # also carries ``outputs/`` and ``debugs/`` — the split dumps — which reach
        # tens of gigabytes on real jobs and used to exhaust the disk; across the
        # jobs measured here these two subtrees are ~3% of the archive. POU writes
        # them in the same layout the archive had, so the parse is unchanged.
        await asyncio.to_thread(
            _results_api().fetch_sources_and_treeview_files, cloud_job.job_id, dest
        )
        yield (dest, runtime_ms)
