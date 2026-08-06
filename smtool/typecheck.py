"""CVL typecheckers for smtool.

Two levels:
- `typecheck_spec(spec_text)`: the standalone `EntryPointKt` jar on a single .spec string — FAST but
  SYNTAX-ONLY. It does NOT catch semantic CVL rules (e.g. "assigning to a CVL variable after it is
  accessed") and cannot resolve scene symbols. A cheap structural gate on the model spec.
- `typecheck_conf(conf)`: the FULL certoraRun typechecker via `--compilation_steps_only` — compiles the
  scene + typechecks every imported spec exactly as a real run does, LOCALLY (no cloud job). This is the
  one that catches the semantic errors WITH file:line, at a fraction of a cloud round's cost. Use it as
  the pre-cloud gate in the refine loop so typecheck failures come back to the agent fast.

  We run the plain `certoraRun <conf> --compilation_steps_only` command directly (NOT autosetup's
  CompilationWorkaroundManager): that machinery exists to make a *broken* project compile by mutating
  its solc/config, but smtool runs on an already-good scene and only ever mutates the CVL spec — so the
  only failures are CVL typecheck errors, which the workaround manager neither fixes nor should rewrite
  the trusted scene conf over. Failure signalling is verified in EVMVerifier/scripts/certoraRun.py:
  `run_typechecker` raises `CertoraUserInputError` on a nonzero typecheck, which propagates uncaught →
  the process exits nonzero. So `returncode == 0` iff compile+typecheck passed — no error-marker
  grepping. The typechecker prints its `Error in spec file (<spec>:<line>:<col>): <msg>` block at the
  END of the output, so on failure we hand the agent the tail verbatim.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from composer.certora_env import typechecker_jar


def typecheck_spec(spec_text: str) -> tuple[bool, str]:
    jar = str(typechecker_jar())
    with tempfile.NamedTemporaryFile("w", suffix=".spec", delete=False) as f:
        f.write(spec_text)
        f.flush()
        res = subprocess.run(
            ["java", "-classpath", jar, "EntryPointKt", f.name],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    ok = res.returncode == 0
    return ok, (res.stdout + res.stderr)


# Generic compiler PROGRESS/WARNING prefixes that dominate --compilation_steps_only output but say
# nothing about a CVL error. We drop these before returning the diagnostics — NOT an enumeration of
# error types (the exit code is the authoritative pass/fail), just noise removal so the actual
# `Error in spec file (...)` lines aren't buried under ~35 lines of auto-finder / storage-layout warnings.
_NOISE_PREFIXES = ("WARNING:", "Compiling ", "INFO:")

# certoraRun prints this header immediately before the actionable spec-error report; everything BEFORE
# it is compile/autofinder/warning noise — including the multi-line, non-fatal "Stack too deep" autofinder
# fallback, which the agent can neither fix nor should act on (it misleads toward --via-ir). Slice here.
_ERROR_SECTION_HEADER = "Please check the errors below"


def _clean_diagnostics(output: str, tail_lines: int) -> str:
    """Extract the typechecker's actionable error report (`Error in spec file (<spec>:<line>)` …).
    Prefer slicing from certoraRun's error-section header — that drops ALL the compile/autofinder noise
    before it (incl. the misleading stack-too-deep block). Fall back to dropping obvious progress/warning
    lines + tailing when the header isn't present (e.g. a genuine compile failure, no typecheck reached)."""
    lines = output.splitlines()
    for i in range(len(lines) - 1, -1, -1):        # last occurrence = the final error report
        if _ERROR_SECTION_HEADER in lines[i]:
            report = [ln.split("ERROR ALWAYS -", 1)[-1].strip() if "ERROR ALWAYS -" in ln else ln.strip()
                      for ln in lines[i + 1:] if ln.strip()]
            if report:
                return "\n".join(report[:tail_lines])
            break
    kept = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(_NOISE_PREFIXES)]
    return "\n".join(kept[-tail_lines:])


def typecheck_conf(conf_path: str | Path, sources_root: str | Path,
                   certora_run_path: str = "certoraRun", timeout: int = 900,
                   tail_lines: int = 25) -> tuple[bool, str]:
    """Run the FULL certoraRun typechecker on a conf (`--compilation_steps_only`, no cloud job) from
    `sources_root` (certoraRun's cwd, so relative conf paths resolve). Returns (ok, diagnostics): ok is
    the exit code (authoritative — see module docstring); diagnostics is the error report with the
    compiler warning/progress noise stripped (so the `Error in spec file (<spec>:<line>)` lines lead),
    empty when ok. Catches the semantic CVL errors the standalone jar misses (assign-after-access,
    type mismatch, unresolved symbol)."""
    conf_path = Path(conf_path).resolve()
    try:
        res = subprocess.run([certora_run_path, str(conf_path), "--compilation_steps_only"],
                             cwd=str(sources_root), text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"typecheck could not run: {type(e).__name__}: {e}"
    if res.returncode == 0:
        return True, ""
    return False, _clean_diagnostics(res.stdout or "", tail_lines)
