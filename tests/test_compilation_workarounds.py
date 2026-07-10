"""Unit tests for CompilationWorkaroundManager detectors and retry-loop guards."""

import subprocess
from pathlib import Path

import pytest

from certora_autosetup.utils.compilation_workarounds import CompilationWorkaroundManager
from certora_autosetup.utils.types import ContractHandle


# Verbatim solc output captured from a real AutoProver run (tokemak-v2-core-fv):
# solc hard-wraps its diagnostics, so "Stack too deep" is split across a newline
# ("...Stack too\ndeep."). This is the case that must be detected so the via-ir /
# optimizer workarounds fire.
WRAPPED_YUL_STACK_TOO_DEEP = (
    "Compiling certora/harnesses/LMPStrategyInstance1.sol...\n"
    "solc8.17 had an error:\n"
    "YulException: Variable param_0 is 2 slot(s) too deep inside the stack. Stack too\n"
    "deep. Try compiling with `--via-ir` (cli) or the equivalent `viaIR: true` \n"
    "(standard JSON) while enabling the optimizer. Otherwise, try removing local \n"
    "variables.\n"
)

SINGLE_LINE_YUL_STACK_TOO_DEEP = (
    "solc8.17 had an error:\n"
    "YulException: Variable x is 2 slot(s) too deep. Stack too deep. Try --via-ir.\n"
)

UNRELATED_OUTPUT = (
    "Compiling certora/harnesses/Foo.sol...\n"
    "Warning: Unused local variable.\n"
    "Compilation successful.\n"
)


@pytest.fixture
def manager(tmp_path: Path) -> CompilationWorkaroundManager:
    return CompilationWorkaroundManager(project_root=tmp_path)


def test_detects_wrapped_yul_stack_too_deep(manager: CompilationWorkaroundManager) -> None:
    # Regression: before the DOTALL/\s+ fix the wrapped phrase was missed, so
    # yul_exception_add_optimizer never fired and the run died as "no applicable
    # workaround".
    assert manager._detect_yul_exception_stack_too_deep(WRAPPED_YUL_STACK_TOO_DEEP) is True


def test_detects_single_line_yul_stack_too_deep(manager: CompilationWorkaroundManager) -> None:
    assert manager._detect_yul_exception_stack_too_deep(SINGLE_LINE_YUL_STACK_TOO_DEEP) is True


def test_ignores_unrelated_output(manager: CompilationWorkaroundManager) -> None:
    assert manager._detect_yul_exception_stack_too_deep(UNRELATED_OUTPUT) is False


# =============================================================================
# Retry-loop no-progress guards
# =============================================================================
#
# Each scenario below is a compilation that fails the same way on every retry.
# Before the guards, a workaround whose detect_fn kept matching the (unchanged)
# output was re-applied as a no-op until max_retries — observed in the wild as
# hundreds of consecutive `unnamed_return_warning` applications on a run whose
# real error was something else entirely. The assertions pin the exact number
# of certoraRun invocations, so any reintroduced no-op iteration fails the test.


class _SequencedRun:
    """subprocess.run stand-in: fails with each queued output in turn, then
    succeeds once the queue is exhausted. Queue more copies than the loop can
    consume to model a compilation that never gets fixed."""

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, cmd, **kwargs):
        self.calls += 1
        if not self.outputs:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=self.outputs.pop(0), stderr="")


def _run_loop(manager, monkeypatch, tmp_path, outputs, contracts):
    fake_run = _SequencedRun(outputs)
    monkeypatch.setattr(
        "certora_autosetup.utils.compilation_workarounds.subprocess.run", fake_run
    )
    compilation_config = {"files": [f"{c.source_file}:{c.contract_name}" for c in contracts]}
    success, _, updated = manager.run_compilation_with_workarounds(
        cmd=["certoraRun", "test.conf"],
        config_file=tmp_path / "test.conf",
        compilation_config=compilation_config,
        contracts=contracts,
        updated_config_dict={},
    )
    return success, updated, compilation_config, fake_run


def _run_loop_with_output(manager, monkeypatch, tmp_path, output, contracts):
    # 10 copies >> what the guarded loop can consume: these tests model runs
    # that never compile, and assert how quickly the loop gives up.
    return _run_loop(manager, monkeypatch, tmp_path, [output] * 10, contracts)


UNNAMED_RETURN_WARNING_OUTPUT = (
    "Compiling contracts/Foo.sol...\n"
    "Warning: Unnamed return variable can remain unassigned. Add an explicit return.\n"
    "Error: something else is failing this run\n"
)

PERSISTENT_STACK_TOO_DEEP_OUTPUT = (
    "Compiling contracts/Foo.sol...\n"
    "solc8.17 had an error:\n"
    "CompilerError: Stack too deep. Try compiling with --via-ir.\n"
)

STACK_TOO_DEEP_BAR_OUTPUT = (
    "Compiling contracts/Bar.sol...\n"
    "solc8.17 had an error:\n"
    "CompilerError: Stack too deep. Try compiling with --via-ir.\n"
)

MISSING_LIB_UNKNOWN_CONSUMER_OUTPUT = (
    "Compiling contracts/Unknown.sol...\n"
    "Failed to find a dependency library while building the constructor bytecode of Bar.\n"
    "Failed to find a contract named MathLib in file contracts/MathLib.sol.\n"
)


def test_unnamed_return_warning_fires_once(manager, monkeypatch, tmp_path) -> None:
    # The warning text persists in the output after ignore_solidity_warnings is
    # set (the flag only stops it from failing the run), so detect_fn must check
    # the live conf or the workaround re-fires on every retry.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, _, config, fake_run = _run_loop_with_output(
        manager, monkeypatch, tmp_path, UNNAMED_RETURN_WARNING_OUTPUT, contracts
    )
    assert success is False
    assert config["ignore_solidity_warnings"] is True
    # Run 1: warning workaround applies. Run 2: it no longer detects; the
    # relpaths catch-all applies. Run 3: nothing applies -> loop exits.
    assert fake_run.calls == 3


def test_repeated_detect_result_disables_workaround(manager, monkeypatch, tmp_path) -> None:
    # via-ir gets applied for Foo once; when the identical stack-too-deep hit
    # comes back on the next retry, re-applying is a no-op and the workaround
    # must be disabled so lower-priority ones get a shot at the real error.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, updated, _, fake_run = _run_loop_with_output(
        manager, monkeypatch, tmp_path, PERSISTENT_STACK_TOO_DEEP_OUTPUT, contracts
    )
    assert success is False
    # The single application is preserved (uniform one-contract map collapses
    # back to the scalar on exit).
    assert updated["solc_via_ir"] is True
    # Run 1: via-ir applies. Run 2: same detect result -> disabled; the
    # relpaths catch-all applies. Run 3: nothing applies -> loop exits.
    assert fake_run.calls == 3


def test_different_detect_results_keep_workaround_enabled(manager, monkeypatch, tmp_path) -> None:
    # The repeat guard must key on the detect RESULT, not on the workaround
    # having fired before: stack-too-deep surfacing for Bar after Foo was
    # fixed needs its own via-ir application.
    contracts = [
        ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol"),
        ContractHandle(contract_name="Bar", source_file="contracts/Bar.sol"),
    ]
    success, updated, _, fake_run = _run_loop(
        manager,
        monkeypatch,
        tmp_path,
        [PERSISTENT_STACK_TOO_DEEP_OUTPUT, STACK_TOO_DEEP_BAR_OUTPUT],
        contracts,
    )
    assert success is True
    assert fake_run.calls == 3
    # Both contracts got via-ir, so the uniform map collapsed to the scalar on
    # exit. A guard that disabled the workaround after its first application
    # would leave Bar's entry False and the map uncollapsed.
    assert updated.get("solc_via_ir") is True
    assert "solc_via_ir_map" not in updated


def test_noop_apply_disables_workaround(manager, monkeypatch, tmp_path) -> None:
    # The missing-library consumer isn't in the scene, so apply bails out
    # leaving conf and command untouched; the no-progress guard must disable
    # the workaround instead of re-applying the same no-op on every retry.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, _, _, fake_run = _run_loop_with_output(
        manager, monkeypatch, tmp_path, MISSING_LIB_UNKNOWN_CONSUMER_OUTPUT, contracts
    )
    assert success is False
    # Run 1: missing-library apply no-ops -> disabled in the same pass; the
    # relpaths catch-all applies. Run 2: nothing applies -> loop exits.
    assert fake_run.calls == 2
