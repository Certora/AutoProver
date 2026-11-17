"""CloudProverRunner resubmits after a transient server error, and only then."""

import pytest

from certora_autosetup.utils import cloud_runner as CR
from certora_autosetup.utils.cloud_runner import CloudProverRunner, _is_transient_submission_error

JOB_URL = "https://vaas-dev.certora.com/output/22224/0f62e8b047ff4cc8899c52ad3630fcad"
RACE = (
    "Connecting to server...\nERROR: Error Status:: 400\nERROR: Server reported an error: "
    "(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) "
    "<class 'asyncpg.exceptions.UniqueViolationError'>: duplicate key value violates unique "
    'constraint "uq_contract_name"\nCertora Prover run failed due to a job submission error\n'
)
COMPILE_ERROR = "ERROR: CompilerError: Stack too deep\n"


@pytest.mark.parametrize(("output", "transient"), [
    (RACE, True),
    ("ERROR: Error Status:: 503\n", True),
    (COMPILE_ERROR, False),
    ("ERROR: Error Status:: 400\nERROR: invalid key\n", False),
])
def test_only_server_failures_a_resubmission_can_clear_are_transient(output: str, transient: bool) -> None:
    assert _is_transient_submission_error(output) is transient


def _runner(outputs: list[str]) -> tuple[CloudProverRunner, list[list[str]]]:
    runner = object.__new__(CloudProverRunner)
    runner.component = "CloudRunner"
    calls: list[list[str]] = []
    queue = list(outputs)

    async def once(cmd: list[str]) -> tuple[str, int]:
        calls.append(cmd)
        return queue.pop(0), 1

    runner._run_certora_once = once  # type: ignore[method-assign]
    return runner, calls


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(_: float) -> None:
        return None

    monkeypatch.setattr(CR.asyncio, "sleep", sleep)


@pytest.mark.asyncio
async def test_a_submission_lost_to_a_server_race_is_resubmitted() -> None:
    runner, calls = _runner([RACE, f"Job submitted to server\n{JOB_URL}\n"])
    output, _ = await runner._run_certora_with_retry(["certoraRun", "SyncOp.conf"], "SyncOp")
    assert len(calls) == 2 and JOB_URL in output


@pytest.mark.asyncio
async def test_a_genuine_failure_is_not_resubmitted() -> None:
    runner, calls = _runner([COMPILE_ERROR])
    output, _ = await runner._run_certora_with_retry(["certoraRun", "Bad.conf"], "Bad")
    assert len(calls) == 1 and output == COMPILE_ERROR


@pytest.mark.asyncio
async def test_resubmission_stops_after_the_last_attempt() -> None:
    runner, calls = _runner([RACE] * CR.SUBMISSION_ATTEMPTS)
    output, _ = await runner._run_certora_with_retry(["certoraRun", "SyncOp.conf"], "SyncOp")
    assert len(calls) == CR.SUBMISSION_ATTEMPTS and output == RACE
