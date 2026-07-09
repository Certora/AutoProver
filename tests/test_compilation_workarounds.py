"""Unit tests for CompilationWorkaroundManager detectors."""

from pathlib import Path

import pytest

from certora_autosetup.utils.compilation_workarounds import CompilationWorkaroundManager


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
