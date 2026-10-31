"""
AutoSetup integration for spec generation.

Provides compilation analysis and summary generation for verifying specs
against real source code.
"""

import json
import logging
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from pydantic import BaseModel, Field
from pathlib import Path
from typing import TextIO, TypedDict, Literal, Annotated
from pydantic import Discriminator
import asyncio
from composer.prover.core import ProverOptions

from graphcore.utils import NormalizedTokenUsage
from composer.io.context import emit_custom_event
# Locators for autosetup's on-disk usage files (certora_autosetup owns that layout).
from certora_autosetup.utils.paths import (
    resolve_autosetup_llm_usage_file,
    resolve_autosetup_prover_usage_file,
)

_logger = logging.getLogger(__name__)

class SetupSuccess(BaseModel):
    """Result of running AutoSetup compilation analysis and summary generation."""
    prover_config: dict  # Contents of compilation_config.conf
    summaries_path: str  # Path to summaries-{Contract}.spec, if generated
    user_types: list[dict]

class SetupFailure(BaseModel):
    error: str
    stderr: str | None = Field(default=None)

type SetupResult = SetupSuccess | SetupFailure

class AutoSetupComplete(TypedDict):
    type: Literal["auto_setup_complete"]
    return_code: int

class AutoSetupStart(TypedDict):
    type: Literal["auto_setup_start"]

class AutoSetupStdout(TypedDict):
    type: Literal["auto_setup_output"]
    line: str

type AutoSetupEvents = Annotated[
    AutoSetupComplete | AutoSetupStart | AutoSetupStdout, Discriminator("type")
]


_LINE_SPLIT = re.compile(r"[\r\n]+")


async def _drain(
    stream: asyncio.StreamReader,
    sink: Callable[[str], object],
    log_level: int,
) -> None:
    """Drain a subprocess stream line by line into ``sink``.

    stdout and stderr are drained concurrently: if the child fills the OS pipe
    buffer on one stream while we block reading the other, it stalls on its write
    and we deadlock. Splitting on ``[\\r\\n]+`` also collapses the carriage-return
    progress redraws AutoSetup emits into discrete lines.
    """
    buf = ""
    while chunk := await stream.read(4096):
        buf += chunk.decode(errors="replace")
        parts = _LINE_SPLIT.split(buf)
        buf = parts.pop()
        for line in parts:
            if line:
                _logger.log(log_level, line)
                sink(line)
    if buf := buf.strip():
        _logger.log(log_level, buf)
        sink(buf)


def _autosetup_args(
    relative_path: str,
    main_contract: str,
    prover_opts: ProverOptions,
    extra_files: Sequence[str],
    *mode_args: str,
) -> list[str]:
    """The AutoSetup command line both entry points share; ``mode_args`` picks what it outputs."""
    main_contract_path = f"{relative_path}:{main_contract}"
    args = [
        sys.executable, "-m", "certora_autosetup.autosetup",
        *mode_args,
        "--no-strip-contracts",
        "--skip-harnessing",
        "--skip-test-run",
        "--run-source", "AUTO_PROVER",
        "--main-contract",
        main_contract_path,
        main_contract_path,
        *extra_files,
    ]
    if not prover_opts.cloud:
        args.append("--use-local-runner")
    return args


async def _run_autosetup_process(
    project_root: Path, args: list[str], log: TextIO | None = None,
) -> tuple[int, list[str]]:
    """Run AutoSetup with ``args`` in ``project_root``, streaming its stdout as
    ``auto_setup_*`` events (and to ``log`` when given, stderr too). Returns the exit code and
    the stderr lines."""

    def emitter(
        s: AutoSetupEvents
    ):
        emit_custom_event(s)

    def to_log(line: str) -> None:
        if log is not None:
            log.write(line + "\n")

    class CB():
        def log_start(self):
            emitter({
                "type": "auto_setup_start"
            })

        def log_stdout(self, line: str):
            to_log(line)
            emitter({
                "line": line,
                "type": "auto_setup_output"
            })

        def log_complete(self, returncode: int):
            emitter({
                "return_code": returncode,
                "type": "auto_setup_complete"
            })

    cb = CB()
    stderr_lines: list[str] = []

    def on_stderr(line: str) -> None:
        to_log(line)
        stderr_lines.append(line)

    _logger.debug("Starting AutoSetup process")
    cb.log_start()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=project_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None and proc.stderr is not None

    try:
        _logger.debug("Draining AutoSetup subprocess output")
        await asyncio.gather(
            _drain(proc.stdout, cb.log_stdout, logging.INFO),
            _drain(proc.stderr, on_stderr, logging.ERROR),
        )
    except BaseException:
        _logger.exception("Error while draining AutoSetup subprocess output")
        # A sink raising (or cancellation) would leave the child running with
        # its pipes half-drained; kill it so wait() can reap.
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        _logger.debug("AutoSetup process complete, waiting for exit")
        returncode = await proc.wait()
    cb.log_complete(returncode)
    return returncode, stderr_lines


async def run_autosetup(
    project_root: Path,
    relative_path: str,
    main_contract: str,
    prover_opts: ProverOptions,
    *extra_files: str
) -> SetupResult:
    """
    Run AutoSetup compilation analysis and summary generation.

    Args:
        project_root: Path to the Foundry project root
        relative_path: Relative path to the main contract file
        main_contract: Contract name, e.g. "Token"
        prover_opts: Prover options; the cloud flag selects local vs cloud AutoSetup runner

    Returns:
        SetupResult with compilation config and summaries path
    """
    certora_dir = project_root / "certora"
    with tempfile.NamedTemporaryFile("r") as f:
        # AutoSetup writes its composer-facing result (config + summary paths) to
        # this temp file via --composer-setup; we read it back once the process exits.
        returncode, stderr_lines = await _run_autosetup_process(
            project_root,
            _autosetup_args(
                relative_path, main_contract, prover_opts, extra_files, "--composer-setup", f.name,
            ),
        )
        if returncode != 0:
            return SetupFailure(
                error="AutoSetup failed",
                stderr="\n".join(stderr_lines),
            )

        data = json.load(f)

    # AutoSetup reports the generated summary spec relative to the project (under
    # certora/); validate it lands inside certora/ before handing it back.
    summary_path = Path(data["contract_to_summary"][main_contract])
    resolved_summary_path: Path
    if summary_path.is_absolute():
        if not summary_path.is_relative_to(certora_dir):
            return SetupFailure(error="Summary not in project relative path")
        else:
            resolved_summary_path = summary_path
    else:
        if summary_path.parts[0] != "certora":
            return SetupFailure(error="Summary not in certora/ folder")
        resolved_summary_path = project_root / summary_path
        if not resolved_summary_path.exists() or not resolved_summary_path.is_relative_to(certora_dir):
            return SetupFailure(error=f"Relative path {summary_path} doesn't exist in project certora/ folder")

    udts = json.loads((project_root / ".certora_internal" / "all_user_defined_types.json").read_text())

    return SetupSuccess(
        prover_config=json.loads((project_root / data["contract_to_config"][main_contract]).read_text()),
        summaries_path=str(resolved_summary_path.relative_to(certora_dir)),
        user_types=udts,
    )


class BuildSuccess(BaseModel):
    """What ``certora_autosetup --build-only`` wrote: the compilation settings under which the
    project's scene compiles (see ``certora_autosetup.autosetup.types.ProjectBuildResult``)."""
    compilation_config: dict
    solc_versions: list[str]
    import_patcher_applied: bool
    contracts: list[str]


async def run_autosetup_build(
    project_root: Path,
    relative_path: str,
    main_contract: str,
    prover_opts: ProverOptions,
    extra_files: Sequence[str],
    log_path: Path,
) -> BuildSuccess | SetupFailure:
    """Run only AutoSetup's build step (``--build-only``): build-system detection and the
    compilation analysis with its workarounds, stopping before summaries, sanity specs, base
    conf and warmup. The process output, stdout and stderr, goes to ``log_path``.

    Not cached: the import fixes the build may apply edit project sources, so a cache hit would
    leave the tree uncompilable."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("r") as f, log_path.open("w") as log:
        returncode, stderr_lines = await _run_autosetup_process(
            project_root,
            _autosetup_args(
                relative_path, main_contract, prover_opts, extra_files, "--build-only", f.name,
            ),
            log,
        )
        if returncode != 0:
            return SetupFailure(
                error=f"AutoSetup build failed (exit {returncode})",
                stderr="\n".join(stderr_lines),
            )
        return BuildSuccess.model_validate_json(f.read())


# --- AutoSetup LLM token-usage ingestion -----------------------------------
# AutoSetup runs as a separate process, so its LLM calls never pass through
# composer's model factory and are invisible to the UsageCallback. Its only
# trace is the llm_usage.json / prover_usage.json it writes to disk. certora_autosetup
# owns that on-disk layout and exposes resolve_autosetup_{llm,prover}_usage_file() to locate them.


def _to_token_usage(model: str, bucket: dict) -> NormalizedTokenUsage:
    """Build a graphcore ``NormalizedTokenUsage`` from one AutoSetup rollup bucket.
    The bucket keeps the raw Anthropic convention (input excludes the cache
    buckets) while the normalized form totals them, hence the sum. AutoSetup's
    ``calls`` count has no slot in ``TokenTotals`` and is dropped; it reports no
    thinking-token split, so ``thinking_tokens`` is 0."""
    cache_read = int(bucket.get("cache_read_input_tokens", 0))
    cache_write = int(bucket.get("cache_creation_input_tokens", 0))
    return {
        "model_name": model,
        "total_input_tokens": int(bucket.get("input_tokens", 0)) + cache_read + cache_write,
        "total_output_tokens": int(bucket.get("output_tokens", 0)),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "thinking_tokens": 0,
    }


def read_autosetup_usage(project_root: Path) -> list[NormalizedTokenUsage]:
    """Return AutoSetup's per-model token usage for the most recent run, one
    ``NormalizedTokenUsage`` per model — ready to feed straight into
    ``RunSummary.record_token_usage``.

    Returns ``[]`` on any failure (file absent — autosetup skipped, cache hit,
    crash, replayed snapshot, or an AutoSetup too old to emit it — or malformed
    JSON): missing external usage must never break the phase.
    """
    usage_file = resolve_autosetup_llm_usage_file(project_root)
    if usage_file is None:
        return []
    try:
        totals = json.loads(usage_file.read_text())["llm_usage_totals"]
        by_model = totals["by_model"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        _logger.debug(f"Could not read AutoSetup llm usage from {usage_file}: {e}")
        return []

    return [_to_token_usage(model, bucket) for model, bucket in by_model.items()]


def read_autosetup_prover_usage(project_root: Path) -> int | None:
    """Prover-reported runtime (milliseconds) AutoSetup's subprocess prover runs
    consumed on this run — ready to fold into the run's prover usage via
    ``RunSummary.record_prover_runtime``.

    AutoSetup runs the prover in a separate process, so its runs never reach composer's
    native ``run_prover`` capture; its only trace is the ``prover_usage.json`` it writes
    (resolved from the same timestamped reports dir as ``read_autosetup_usage``). The
    value already EXCLUDES cache hits — AutoSetup's ledger only counts freshly-executed
    jobs, which is what "minutes each job ran" means.

    Returns ``None`` on any failure (file absent — autosetup skipped, full cache hit,
    crash, replayed snapshot, or an AutoSetup too old to emit it — or malformed JSON):
    missing external usage must never break the phase."""
    usage_file = resolve_autosetup_prover_usage_file(project_root)
    if usage_file is None:
        return None
    try:
        return int(json.loads(usage_file.read_text())["ms"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        _logger.warning(f"Could not read AutoSetup prover usage from {usage_file}: {e}")
        return None
