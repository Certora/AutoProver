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


# Verbatim-shaped certoraRun output for the "Source ... not found" ParserError. solc
# hard-wraps the diagnostic, so the two markers ('ParserError: Source "' and
# "File not found") land on separate lines and defeat a raw substring check. These are
# the ion-protocol / angstrom cases that must be detected so the source-not-found
# packages workaround fires.

# ion-protocol: wraps between `Source` and the opening quote.
ION_WRAPPED_SOURCE_NOT_FOUND = (
    "Compiling 41 files with Solc 0.8.21\n"
    "ParserError: Source\n"
    '"@openzeppelin/contracts/token/ERC20/IERC20.sol" not found: File not found.\n'
)

# angstrom: wraps `File` / `not found`.
ANGSTROM_WRAPPED_SOURCE_NOT_FOUND = (
    'ParserError: Source "solady/utils/FixedPointMathLib.sol" not found: File\n'
    "not found.\n"
)

SINGLE_LINE_SOURCE_NOT_FOUND = 'ParserError: Source "src/Foo.sol" not found: File not found.\n'


def test_detects_ion_wrapped_source_not_found(manager: CompilationWorkaroundManager) -> None:
    # Regression: the raw `'ParserError: Source "' in output` check fails here because
    # the output has a newline where the literal has a space.
    assert manager._has_source_not_found(ION_WRAPPED_SOURCE_NOT_FOUND) is True


def test_detects_angstrom_wrapped_source_not_found(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(ANGSTROM_WRAPPED_SOURCE_NOT_FOUND) is True


def test_detects_single_line_source_not_found(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(SINGLE_LINE_SOURCE_NOT_FOUND) is True


def test_ignores_unrelated_source_not_found(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(UNRELATED_OUTPUT) is False
