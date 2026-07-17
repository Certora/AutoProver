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

# Verbatim solc 0.8.34 via-ir emission: no "Stack too deep" phrase anywhere —
# the error ends with "memoryguard was present." Missing this wording left the
# yul escalation ladder (optimizer, then teardown) untried on a real project.
MEMORYGUARD_YUL_STACK_TOO_DEEP = (
    "Compiling contracts/facets/IntentFacet.sol...\n"
    "\n"
    "solc8.34 had an error:\n"
    "YulException: Variable _7 is 1 too deep in the stack [ RET _7 expr_2992_address \n"
    "_6 var_maker var_salt var_expiry var_originalOrderAmount var_feeRecipient \n"
    "var_proratedBorrowFee var_fillAmount var_termRepoTokenFillAmount var_offerRate \n"
    "expr var_servicer_2952_address var_taker expr_2995_address \n"
    "expr_2995_functionSelector ]\n"
    "memoryguard was present.\n"
)

# Verbatim autofinder-generation failure. The prover falls back to the original
# file, silently losing that file's internal summaries — so the yul ladder MUST
# react to it (optimizer, then relaxing the autofinder assertion), while never touching the
# compile settings the project itself requires.
AUTOFINDER_YUL_STACK_TOO_DEEP = (
    "Compiling src/initializers/LockableUniswapV3Initializer.sol to expose internal function information and local variables...\n"
    "Encountered an exception generating autofinder .certora_internal/26_07_13/.certora_sources/src/initializers/LockableUniswapV3Initializer.sol (solc8.26 had an error:\n"
    "YulException: Variable _8 is 1 too deep in the stack [ expr_327_component_1 _8 RET _mpos var_asset var_numeraire expr_327_component var_totalTokensOnBondingCurve _2 _4 _1 _3 expr_1 expr_2 expr _1 expr_327_component_1 expr_327_component ]\n"
    "memoryguard was present.\n"
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


def test_detects_memoryguard_wording_without_stack_too_deep_phrase(
    manager: CompilationWorkaroundManager,
) -> None:
    assert manager._detect_yul_exception_stack_too_deep(MEMORYGUARD_YUL_STACK_TOO_DEEP) is True


def test_ignores_unrelated_output(manager: CompilationWorkaroundManager) -> None:
    assert manager._detect_yul_exception_stack_too_deep(UNRELATED_OUTPUT) is False


def test_detects_autofinder_yul_stack_too_deep(manager: CompilationWorkaroundManager) -> None:
    # Autofinder-generation failures silently drop the file's internal
    # summaries — the ladder must react to them like any other yul error.
    assert manager._detect_yul_exception_stack_too_deep(AUTOFINDER_YUL_STACK_TOO_DEEP) is True


# =============================================================================
# Retry-loop behavior: apply-all-per-pass + no-progress exit
# =============================================================================
#
# Each failed compilation gets one pass applying EVERY applicable workaround
# before the single recompile, and a pass that changes nothing ends the loop.
# Before that, one workaround was applied per recompile and a detect_fn that
# kept matching the (unchanged) output was re-applied as a no-op until
# max_retries — observed in the wild as hundreds of consecutive
# `unnamed_return_warning` applications on a run whose real error was something
# else entirely. The assertions pin the exact number of certoraRun invocations,
# so any reintroduced no-op recompile fails the test.


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


def _run_loop(manager, monkeypatch, tmp_path, outputs, contracts, extra_config=None):
    fake_run = _SequencedRun(outputs)
    monkeypatch.setattr(
        "certora_autosetup.utils.compilation_workarounds.subprocess.run", fake_run
    )
    compilation_config = {"files": [f"{c.source_file}:{c.contract_name}" for c in contracts]}
    compilation_config.update(extra_config or {})
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


def test_noop_pass_exits_without_recompile(manager, monkeypatch, tmp_path) -> None:
    # Run 1: via-ir applies for Foo. Run 2: the identical stack-too-deep hit
    # fires again, re-applying is a no-op; the catch-all is suppressed because
    # a specific workaround applied this pass, and the pass changed nothing ->
    # exit without recompiling.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, updated, _, fake_run = _run_loop_with_output(
        manager, monkeypatch, tmp_path, PERSISTENT_STACK_TOO_DEEP_OUTPUT, contracts
    )
    assert success is False
    # The first application is preserved (uniform one-contract map collapses
    # back to the scalar on exit).
    assert updated["solc_via_ir"] is True
    assert "use_relpaths_for_solc_json" not in updated
    assert fake_run.calls == 2


def test_different_detect_results_keep_workaround_enabled(manager, monkeypatch, tmp_path) -> None:
    # A workaround stays available for later passes: stack-too-deep surfacing
    # for Bar after Foo was fixed needs its own via-ir application on the
    # next pass.
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


def test_noop_apply_exits_without_recompile(manager, monkeypatch, tmp_path) -> None:
    # The missing-library consumer isn't in the scene, so apply bails out
    # leaving cmd and conf untouched; the pass applied only no-ops, so
    # recompiling would reproduce the identical failure -> exit immediately
    # after the single certoraRun.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, _, config, fake_run = _run_loop_with_output(
        manager, monkeypatch, tmp_path, MISSING_LIB_UNKNOWN_CONSUMER_OUTPUT, contracts
    )
    assert success is False
    assert "use_relpaths_for_solc_json" not in config
    assert fake_run.calls == 1


MULTI_ERROR_OUTPUT = (
    "Compiling contracts/Foo.sol...\n"
    "Warning: Unnamed return variable can remain unassigned. Add an explicit return.\n"
    "solc8.17 had an error:\n"
    "CompilerError: Stack too deep. Try compiling with --via-ir.\n"
)


def test_multiple_workarounds_apply_in_one_pass(manager, monkeypatch, tmp_path) -> None:
    # The redesign's signature: one failed output containing two independent
    # errors gets BOTH workarounds in a single pass and needs only one
    # recompile (the old one-per-recompile loop needed two).
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, updated, _, fake_run = _run_loop(
        manager, monkeypatch, tmp_path, [MULTI_ERROR_OUTPUT], contracts
    )
    assert success is True
    assert fake_run.calls == 2
    assert updated["solc_via_ir"] is True
    assert updated["ignore_solidity_warnings"] is True
    assert "use_relpaths_for_solc_json" not in updated


CACHED_AUTOFINDER_OUTPUT = (
    "Compiling contracts/Foo.sol...\n"
    "Warning: Unnamed return variable can remain unassigned. Add an explicit return.\n"
    "Failed to create autofinders, failing\n"
)


def test_cached_autofinder_failure_is_exclusive(manager, monkeypatch, tmp_path) -> None:
    # A cached autofinder failure hides the real error, so the whole output is
    # untrustworthy: the pass must apply ONLY the cache disable and recompile,
    # skipping the unnamed-return workaround even though its detect matches.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    fake_run = _SequencedRun([CACHED_AUTOFINDER_OUTPUT])
    monkeypatch.setattr(
        "certora_autosetup.utils.compilation_workarounds.subprocess.run", fake_run
    )
    cmd = ["certoraRun", "test.conf", "--build_cache"]
    success, _, updated = manager.run_compilation_with_workarounds(
        cmd=cmd,
        config_file=tmp_path / "test.conf",
        compilation_config={"files": ["contracts/Foo.sol:Foo"], "build_cache": True},
        contracts=contracts,
        updated_config_dict={},
    )
    assert success is True
    assert fake_run.calls == 2
    assert updated["build_cache"] is False
    assert "--build_cache" not in cmd
    assert "ignore_solidity_warnings" not in updated


def test_yul_ladder_escalates_across_passes(manager, monkeypatch, tmp_path) -> None:
    # The YulException escalation must span two recompiles: pass 1 only adds
    # the optimizer (trying to succeed WITH autofinders); only when the
    # exception SURVIVES that recompile does the last resort stop asserting
    # autofinder success — keeping the compile settings.
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    success, updated, config, fake_run = _run_loop(
        manager,
        monkeypatch,
        tmp_path,
        [SINGLE_LINE_YUL_STACK_TOO_DEEP, SINGLE_LINE_YUL_STACK_TOO_DEEP],
        contracts,
        extra_config={"assert_autofinder_success": True},
    )
    assert success is True
    assert fake_run.calls == 3
    assert config["solc_optimize"] == "200"
    assert updated["assert_autofinder_success"] is False


# Verbatim-shaped solc output for a feature that exists only on the via-ir
# pipeline (observed with `require(cond, CustomError())` on solc 0.8.26). The
# phrase is hard-wrapped by solc, so detection must be whitespace-normalized.
VIA_IR_REQUIRED_OUTPUT = (
    "Compiling src/Airlock.sol...\n"
    "solc8.26 had an error:\n"
    "UnimplementedFeatureError: Require with a custom error is only available using \n"
    "the via-ir pipeline.\n"
)


def test_detects_via_ir_required_feature(manager) -> None:
    contracts = [ContractHandle(contract_name="Airlock", source_file="src/Airlock.sol")]
    assert manager._detect_via_ir_required(VIA_IR_REQUIRED_OUTPUT, contracts) == "Airlock"
    assert manager._detect_via_ir_required(UNRELATED_OUTPUT, contracts) is None


def test_via_ir_added_out_of_necessity(manager, monkeypatch, tmp_path) -> None:
    # Contracts start on plain settings; a via-ir-only feature error is the
    # necessity signal that adds via-ir for the affected contract.
    contracts = [ContractHandle(contract_name="Airlock", source_file="src/Airlock.sol")]
    success, updated, _, fake_run = _run_loop(
        manager, monkeypatch, tmp_path, [VIA_IR_REQUIRED_OUTPUT], contracts
    )
    assert success is True
    assert fake_run.calls == 2
    assert updated["solc_via_ir"] is True


def test_yul_last_resort_keeps_compile_settings(manager, monkeypatch, tmp_path) -> None:
    # One output carries a plain stack-too-deep for Foo AND a YulException with
    # the optimizer already present (e.g. supplied by the project's foundry
    # config): the pass applies via-ir for Foo and relaxes the autofinder
    # assertion, but via-ir and the optimizer must survive — the source itself may not compile
    # without them (seen in the wild: "Require with a custom error is only
    # available using the via-ir pipeline").
    contracts = [ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol")]
    combined = PERSISTENT_STACK_TOO_DEEP_OUTPUT + SINGLE_LINE_YUL_STACK_TOO_DEEP
    success, updated, config, fake_run = _run_loop(
        manager,
        monkeypatch,
        tmp_path,
        [combined],
        contracts,
        extra_config={"assert_autofinder_success": True, "solc_optimize": "200"},
    )
    assert success is True
    assert fake_run.calls == 2
    assert updated["solc_via_ir"] is True
    assert config["assert_autofinder_success"] is False
    assert config["solc_optimize"] == "200"


def test_via_ir_after_yul_last_resort_stays_per_contract(manager, monkeypatch, tmp_path) -> None:
    # The last resort leaves the seeded via-ir map in place, so a later
    # per-contract via-ir fix stays per-contract (a partial map collapsing to
    # a global solc_via_ir=true would re-enable via-ir scene-wide).
    contracts = [
        ContractHandle(contract_name="Foo", source_file="contracts/Foo.sol"),
        ContractHandle(contract_name="Bar", source_file="contracts/Bar.sol"),
    ]
    success, updated, config, fake_run = _run_loop(
        manager,
        monkeypatch,
        tmp_path,
        [
            SINGLE_LINE_YUL_STACK_TOO_DEEP,   # pass 1: add optimizer
            SINGLE_LINE_YUL_STACK_TOO_DEEP,   # pass 2: last resort (assertion relaxed)
            PERSISTENT_STACK_TOO_DEEP_OUTPUT,  # pass 3: via-ir for Foo only
        ],
        contracts,
        extra_config={"assert_autofinder_success": True},
    )
    assert success is True
    assert fake_run.calls == 4
    assert updated["solc_via_ir_map"] == {"Foo": True, "Bar": False}
    assert "solc_via_ir" not in updated
    assert config["assert_autofinder_success"] is False
    assert config["solc_optimize"] == "200"

# Verbatim-shaped certoraRun output for the "Source ... not found" ParserError. solc
# hard-wraps the diagnostic, so the two markers ('ParserError: Source "' and
# "File not found") land on separate lines and defeat a raw substring check. Both wrap
# positions observed in real runs must be detected so the source-not-found packages
# workaround fires.

# Wrap between `Source` and the opening quote.
WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_QUOTE = (
    "Compiling 41 files with Solc 0.8.21\n"
    "ParserError: Source\n"
    '"@openzeppelin/contracts/token/ERC20/IERC20.sol" not found: File not found.\n'
)

# Wrap between `File` and `not found`.
WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_FILE = (
    'ParserError: Source "solady/utils/FixedPointMathLib.sol" not found: File\n'
    "not found.\n"
)

SINGLE_LINE_SOURCE_NOT_FOUND = 'ParserError: Source "src/Foo.sol" not found: File not found.\n'


def test_detects_source_not_found_split_at_quote(manager: CompilationWorkaroundManager) -> None:
    # Regression: the raw `'ParserError: Source "' in output` check fails here because
    # the output has a newline where the literal has a space.
    assert manager._has_source_not_found(WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_QUOTE) is True


def test_detects_source_not_found_split_at_file(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_FILE) is True


def test_detects_single_line_source_not_found(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(SINGLE_LINE_SOURCE_NOT_FOUND) is True


def test_ignores_unrelated_source_not_found(manager: CompilationWorkaroundManager) -> None:
    assert manager._has_source_not_found(UNRELATED_OUTPUT) is False


# =============================================================================
# compiler_version_mismatch: wrap-tolerant detection + enabled with global solc
# =============================================================================

# Verbatim certoraRun output from a real mass-test run: the whole
# scene was pinned to solc7.3 while every source is ^0.8.0, and solc wrapped the
# marker phrase ("ParserError: Source \nfile requires different compiler version"),
# defeating the old single-line substring check.
WRAPPED_COMPILER_VERSION_MISMATCH = (
    "Compiling certora/mocks/DummyERC20Impl.sol...\n"
    "\n"
    "solc7.3 had an error:\n"
    "/workspace/project/certora/mocks/DummyERC20Impl.sol:2:1: ParserError: Source \n"
    "file requires different compiler version (current compiler is \n"
    "0.7.3+commit.9bfce1f6.Linux.g++) - note that nightly builds are considered to be\n"
    "strictly less than the released version\n"
    "pragma solidity ^0.8.0;\n"
    "^---------------------^\n"
)

SINGLE_LINE_COMPILER_VERSION_MISMATCH = (
    "Compiling certora/mocks/DummyERC20Impl.sol...\n"
    "solc7.3 had an error:\n"
    "certora/mocks/DummyERC20Impl.sol:2:1: ParserError: Source file requires different compiler version (current compiler is 0.7.3+commit.9bfce1f6.Linux.g++)\n"
    "pragma solidity ^0.8.0;\n"
)

MISMATCH_CONTRACTS = [
    ContractHandle(contract_name="DummyERC20Impl", source_file="certora/mocks/DummyERC20Impl.sol"),
    ContractHandle(contract_name="Vault", source_file="contracts/Vault.sol"),
]


@pytest.fixture
def resolve_pragma_offline(monkeypatch):
    """resolve_pragma_to_version fetches soliditylang.org; pin it for tests."""
    monkeypatch.setattr(
        "certora_autosetup.utils.compilation_workarounds.resolve_pragma_to_version",
        lambda spec, **kwargs: "0.8.30",
    )


def test_detects_wrapped_compiler_version_mismatch(
    manager: CompilationWorkaroundManager, resolve_pragma_offline
) -> None:
    # Regression: the wrapped marker phrase was missed, so the scene-wide wrong
    # compiler pin was never repaired and the run died in compilation analysis.
    result = manager._detect_compiler_version_mismatch(
        WRAPPED_COMPILER_VERSION_MISMATCH, MISMATCH_CONTRACTS
    )
    assert result == ("DummyERC20Impl", "0.8.30")


def test_detects_single_line_compiler_version_mismatch(
    manager: CompilationWorkaroundManager, resolve_pragma_offline
) -> None:
    result = manager._detect_compiler_version_mismatch(
        SINGLE_LINE_COMPILER_VERSION_MISMATCH, MISMATCH_CONTRACTS
    )
    assert result == ("DummyERC20Impl", "0.8.30")


def test_ignores_unrelated_compiler_version_mismatch(
    manager: CompilationWorkaroundManager,
) -> None:
    assert manager._detect_compiler_version_mismatch(UNRELATED_OUTPUT, MISMATCH_CONTRACTS) is None


def test_compiler_mismatch_workaround_fires_with_global_solc(
    manager, monkeypatch, tmp_path, resolve_pragma_offline
) -> None:
    # Regression: `enabled=not solc_already_set` disabled the only recovery path
    # exactly when a build system pinned a wrong global solc. The workaround must
    # override the seeded compiler_map entry from the pragma.
    success, updated, compilation_config, fake_run = _run_loop(
        manager,
        monkeypatch,
        tmp_path,
        [WRAPPED_COMPILER_VERSION_MISMATCH],
        MISMATCH_CONTRACTS,
        extra_config={"solc": "solc7.3"},
    )
    assert success is True
    assert fake_run.calls == 2  # failing compile + one recompile after the fix
    assert compilation_config["compiler_map"]["DummyERC20Impl"] == "solc8.30"
    assert compilation_config["compiler_map"]["Vault"] == "solc7.3"
