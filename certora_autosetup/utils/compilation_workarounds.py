"""
Compilation workarounds for handling common Solidity compilation issues.

These workarounds can be applied:
1. During initial compilation analysis (via SetupProver)
2. When adding files during linking/dispatching (via ConfigManager)
3. When generating mock/harness contracts (missing-library harness — see
   ``_apply_missing_library_harness_to_config``)
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from certora_autosetup.utils.constants import DEFAULT_SOLC_VERSION, SolcConvention
from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.library_harness import (
    LibrarySpec,
    build_consumer_harness_source,
    pragma_for_solc,
)
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.paths import user_harness_path, user_harnesses_dir
from certora_autosetup.utils.remappings import build_packages_from_remapping_sources
from certora_autosetup.utils.solc_version_resolver import (
    extract_pragma_spec,
    resolve_pragma_to_version,
)
from certora_autosetup.utils.types import ContractHandle


class AbstractMainContractError(Exception):
    """Raised when the main (verify-target) contract compiled to no bytecode — it is
    abstract (or lacks a constructor) and therefore cannot be verified."""


def _normalize_ws(text: str) -> str:
    """Collapse every run of whitespace (including solc's hard-wrap newlines) to a
    single space, so multi-substring detectors survive line wrapping.

    solc wraps its diagnostics at a fixed width, splitting phrases across newlines
    (e.g. ``ParserError: Source\\n"..."`` or ``File\\nnot found``); a raw substring
    check then misses the error. Same failure mode as the Yul stack-too-deep wrap.
    """
    return " ".join(text.split())


def _path_from_compiling_line(line: str) -> Optional[str]:
    """Return ``<path>`` from a ``Compiling <path>...`` prover progress line, or
    None if ``line`` isn't one.

    Several detectors recover the file the prover was working on by scanning for
    these progress lines. This is the single home for that one stdout-format
    dependency, so the prefix/suffix stripping isn't open-coded at each call site.
    """
    prefix, suffix = "Compiling ", "..."
    if not (line.startswith(prefix) and line.endswith(suffix)):
        return None
    return line.removeprefix(prefix).removesuffix(suffix)


def _find_compiling_path_before(lines: List[str], idx: int, max_lookback: Optional[int] = None) -> Optional[str]:
    """Walk backward from ``lines[idx]`` to the nearest preceding plain
    ``Compiling <path>...`` line and return its path, or None if there is none.

    The relevant Compiling line is the one nearest *above* the error — multiple
    files compile per run, so the globally last one would be wrong. Skips the
    autofinder variant ``Compiling <path> to expose internal function
    information...``: its "path" embeds the suffix and won't match the contracts
    list, and the plain form for the same file appears one line earlier.

    ``max_lookback`` bounds the number of preceding lines examined (None = all
    the way back to the start).
    """
    stop = -1 if max_lookback is None else max(idx - max_lookback - 1, -1)
    for j in range(idx - 1, stop, -1):
        line = lines[j]
        if "to expose internal function information" in line:
            continue
        path = _path_from_compiling_line(line)
        if path is not None:
            return path
    return None


@dataclass
class CompilationWorkaround:
    """Represents a compilation workaround that can be applied to fix errors."""

    name: str
    detect_fn: Callable[[str], Any]  # Takes compilation output, returns detection result or None
    apply_fn: Callable[[Any, Dict, Dict, Path, List[ContractHandle]], Dict]
    enabled: bool = True
    # The workaround invalidates the whole output it fired on (e.g. the error
    # is a cached artifact): evaluated BEFORE all other workarounds regardless
    # of list position; when it applies, the pass ends immediately and
    # recompiles instead of letting anything act on garbage output.
    exclusive: bool = False
    # Catch-all: only tried when no other workaround applied in the pass.
    last_resort: bool = False


class CompilationWorkaroundManager:
    """Manages compilation workarounds for config files."""

    def __init__(
        self,
        project_root: Path,
        solc_default_version: str = DEFAULT_SOLC_VERSION,
        verbose: int = 0,
        solc_convention: SolcConvention = SolcConvention.CERTORA,
    ):
        self.project_root = project_root
        self.solc_convention = solc_convention
        self.verbose = verbose
        self._remappings_workaround_applied = False
        # (consumer, lib) pairs already covered by a generated harness in this run.
        # Used as a loop guard — if the prover still reports the same pair after we
        # wrapped the consumer, the workaround stops firing to avoid spinning.
        self._harnessed_libs: Set[Tuple[str, str]] = set()
        # consumer -> [(lib_name, lib_path), ...] in insertion order. A second
        # firing for the same consumer (different missing library) regenerates the
        # consumer's harness covering every library it has needed so far.
        self._harnesses_by_consumer: Dict[str, List[Tuple[str, str]]] = {}
        # Convert default version to the detected convention
        if solc_convention == SolcConvention.SOLC_SELECT and solc_default_version.startswith("solc") \
                and not solc_default_version.startswith("solc-"):
            # "solc8.34" -> "solc-0.8.34"
            self.solc_default_version = f"solc-0.{solc_default_version[4:]}"
        else:
            self.solc_default_version = solc_default_version

    def format_solc_version(self, version: str) -> str:
        """Format a semantic version string for this manager's solc convention.

        Thin wrapper around the static ``ConfigManager.format_solc_version`` so
        the formatting logic lives in exactly one place.
        """
        return ConfigManager.format_solc_version(version, self.solc_convention)

    # =========================================================================
    # Per-flag map lifecycle: seed → (workarounds update) → normalize
    # =========================================================================

    # Conf keys that move together when promoting/collapsing a scalar <-> map.

    def _seed_compile_maps(self, config: Dict, contracts: List[ContractHandle]) -> None:
        """Promote scalar ``solc``/``solc_via_ir`` into fully-populated maps.
        """
        if "compiler_map" not in config:
            default = config.pop("solc", None) or self.solc_default_version
            config["compiler_map"] = {c.contract_name: default for c in contracts}
        if "solc_via_ir_map" not in config:
            via_ir = config.pop("solc_via_ir", False)
            config["solc_via_ir_map"] = {c.contract_name: via_ir for c in contracts}

    def _normalize_compile_maps(self, config: Dict) -> None:
        """Collapse a uniform compiler_map / solc_via_ir_map back to its scalar if all values are same.
        """
        cmap = config.get("compiler_map")
        if isinstance(cmap, dict) and cmap and len(set(cmap.values())) == 1:
            config["solc"] = next(iter(cmap.values()))
            del config["compiler_map"]

        vmap = config.get("solc_via_ir_map")
        if isinstance(vmap, dict) and vmap and len(set(vmap.values())) == 1:
            value = next(iter(vmap.values()))
            del config["solc_via_ir_map"]
            if value:  # False is the prover default — no scalar needed.
                config["solc_via_ir"] = value

    def _mirror_compile_flags(self, src: Dict, dst: Dict) -> None:
        """Copy the compile-flag keys from ``src`` onto ``dst`` (dropping any the
        src no longer has), so the disk conf and the returned dict stay in sync
        after seeding/normalizing."""
        compile_flag_keys = ("solc", "compiler_map", "solc_via_ir", "solc_via_ir_map")
        for key in compile_flag_keys:
            dst.pop(key, None)
            if key in src:
                dst[key] = src[key]

    def _finalize_compile_maps(self, compilation_config: Dict, updated_config_dict: Dict, config_file: Path) -> None:
        """Collapse the seeded maps back to scalars if needed, sync the returned dict, and
        persist."""
        self._normalize_compile_maps(compilation_config)
        self._mirror_compile_flags(compilation_config, updated_config_dict)
        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

    def log(self, message: str, level: str = "INFO") -> None:
        """Log a message."""
        if level == "ERROR":
            logger.error(message)
        elif level == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)

    def _detect_abstract_main_contract(self, output: str, compilation_config: Dict) -> Optional[str]:
        """Return the main (verify-target) contract name if the compiler reported it has
        no bytecode — i.e. it is abstract (or lacks a constructor) and cannot be verified.

        The prover prints ``Contract <name> has no bytecode`` for any contract in the
        input files (harnesses/siblings included), so match the reported names against
        the verify target specifically rather than assuming the first one is the main.
        """
        main_contract = ConfigManager.extract_main_contract_from_config(compilation_config)
        if main_contract is None:
            return None
        no_bytecode = {m.group(1) for m in re.finditer(r"Contract (\S+) has no bytecode", output)}
        return main_contract if main_contract in no_bytecode else None

    # =========================================================================
    # Main entry point for running compilation with workarounds
    # =========================================================================

    def run_compilation_with_workarounds(
        self,
        cmd: List[str],
        config_file: Path,
        compilation_config: Dict,
        contracts: List[ContractHandle],
        updated_config_dict: Dict,
    ) -> Tuple[bool, str, Dict]:
        """Run compilation with automatic workarounds for common errors.

        Each failed compilation gets one pass in which EVERY applicable
        workaround is applied to that output (priority order = application
        order within the pass) before the single recompile; a pass that leaves
        the conf and command unchanged ends the loop, since recompiling would
        reproduce the identical failure.

        Workaround priority order:
        1. Solc not found fallback (versioned binary missing, fall back to plain solc)
        2. Remappings conflict (package.json vs remappings.txt duplicate keys)
        3. Source not found (add packages from `forge remappings`, foundry.toml, remappings.txt, package.json)
        4. Compiler version mismatch (blocks all compilation)
        5. Stack-too-deep errors (via-ir workaround)
        6. Feature only available on the via-ir pipeline (enable via-ir out of necessity)
        7. Unsupported solc version for via-ir (disable via-ir for old compiler versions)
        8. Cancun opcode errors (mcopy/tload/tstore — set EVM version to cancun)
        9. Unnamed return variable warning (ignore_solidity_warnings)
        10. YulException stack-too-deep with via-ir (try adding optimizer first)
        11. YulException stack-too-deep persists (stop asserting autofinder success)
        12. Missing dependency library (generate harness with dummy usage so solc emits the lib)
        13. Catch-all: use_relpaths_for_solc_json (last resort before import-patch fallback)

        Args:
            cmd: Command to execute
            config_file: Path to config file (to rewrite on updates)
            compilation_config: Full compilation config (will be updated)
            contracts: List of contracts for path mapping
            updated_config_dict: Config dict to track updates

        Returns:
            Tuple of (success, output, updated_config_dict)
        """
        # Check if global solc_via_ir is already enabled
        global_via_ir_enabled = updated_config_dict.get("solc_via_ir", False)
        solc_already_set = "solc" in updated_config_dict

        # Names of the workarounds applied in the current pass over a failed
        # output. Cleared at the top of each pass; detect lambdas below may
        # consult it to avoid escalating past a step that was applied on this
        # same (stale) output.
        applied_this_pass: Set[str] = set()

        # Initialize workarounds list
        workarounds = [
            CompilationWorkaround(
                name="cached_autofinder_failure",
                detect_fn=lambda output: (
                    "detected"
                    if "Failed to create autofinders, failing" in output
                    and (compilation_config.get("build_cache", False) or "--build_cache" in cmd)
                    else None
                ),
                apply_fn=self._apply_disable_build_cache,
                enabled=True,
                # The cached error hides the real one, so the rest of this
                # output is untrustworthy — recompile before applying anything else.
                exclusive=True,
            ),
            CompilationWorkaround(
                name="solc_not_found_fallback",
                detect_fn=lambda output: self._detect_solc_not_found(output),
                apply_fn=self._apply_solc_fallback_workaround,
                enabled=solc_already_set and updated_config_dict.get("solc") != "solc",
            ),
            CompilationWorkaround(
                name="remappings_conflict",
                detect_fn=lambda output: (
                    "detected"
                    if self._has_remappings_conflict(output) and not self._remappings_workaround_applied
                    else None
                ),
                apply_fn=self._apply_remappings_conflict_workaround,
                enabled=True,
            ),
            CompilationWorkaround(
                name="source_not_found_packages",
                detect_fn=lambda output: (
                    "detected"
                    if self._has_source_not_found(output)
                    and not self._remappings_workaround_applied
                    else None
                ),
                apply_fn=self._apply_source_not_found_packages_workaround,
                enabled=True,
            ),
            CompilationWorkaround(
                name="compiler_version_mismatch",
                detect_fn=lambda output: self._detect_compiler_version_mismatch(output, contracts),
                apply_fn=self._apply_compiler_version_workaround_to_config,
                # Enabled even when a global solc is configured: the detector
                # only fires when that compiler provably cannot parse a file
                # (hard ParserError), and _seed_compile_maps has already
                # promoted the scalar into compiler_map, so overriding one
                # contract's entry from its pragma is always safe.
                enabled=True,
            ),
            CompilationWorkaround(
                name="stack_too_deep_via_ir",
                detect_fn=lambda output: self._detect_stack_too_deep_errors(output, contracts),
                apply_fn=self._apply_via_ir_workaround_to_config,
                enabled=not global_via_ir_enabled,
            ),
            CompilationWorkaround(
                name="via_ir_required_feature",
                detect_fn=lambda output: self._detect_via_ir_required(output, contracts),
                apply_fn=self._apply_via_ir_workaround_to_config,
                enabled=not global_via_ir_enabled,
            ),
            CompilationWorkaround(
                name="unsupported_solc_via_ir",
                detect_fn=lambda output: self._detect_unsupported_solc_via_ir(output, contracts),
                apply_fn=self._apply_disable_via_ir_workaround_to_config,
                enabled=global_via_ir_enabled,
            ),
            CompilationWorkaround(
                name="cancun_opcode_evm_version",
                detect_fn=lambda output: self._detect_cancun_opcode_errors(output, contracts),
                apply_fn=self._apply_evm_version_cancun_workaround_to_config,
                enabled=True,
            ),
            CompilationWorkaround(
                name="unnamed_return_warning",
                detect_fn=lambda output: (
                    "detected"
                    if self._has_unnamed_return_warning(output)
                    and "ignore_solidity_warnings" not in compilation_config
                    else None
                ),
                apply_fn=self._apply_ignore_solidity_warnings_workaround,
                enabled=True,
            ),
            CompilationWorkaround(
                name="yul_exception_add_optimizer",
                detect_fn=lambda output: (
                    "detected"
                    if self._detect_yul_exception_stack_too_deep(output)
                    and "solc_optimize" not in compilation_config
                    else None
                ),
                apply_fn=self._apply_optimizer_for_via_ir,
                enabled=True,
            ),
            CompilationWorkaround(
                name="yul_exception_stack_too_deep",
                # This is the escalation step after yul_exception_add_optimizer:
                # it must only fire on output produced AFTER the optimizer was
                # tried, not in the same pass that just added it (the live
                # "solc_optimize in config" check would otherwise see the value
                # the previous workaround set seconds ago and stop asserting
                # autofinder success without ever testing the optimizer).
                detect_fn=lambda output: (
                    "detected"
                    if self._detect_yul_exception_stack_too_deep(output)
                    and compilation_config.get("assert_autofinder_success", False)
                    and "solc_optimize" in compilation_config
                    and "yul_exception_add_optimizer" not in applied_this_pass
                    else None
                ),
                apply_fn=self._apply_yul_exception_workaround,
                enabled=True,
            ),
            # Must stay ordered after every workaround that writes per-contract
            # maps (compiler_map, solc_via_ir_map, ...): its apply renames the
            # consumer's entries in those maps to the harness name, so map
            # writes for the consumer must happen before it in the pass.
            CompilationWorkaround(
                name="missing_library_harness",
                detect_fn=lambda output: self._detect_missing_library(output, contracts),
                apply_fn=self._apply_missing_library_harness_to_config,
                enabled=True,
            ),
            # Catch-all: final attempt before setup_prover falls back to the
            # import-patch pass.
            CompilationWorkaround(
                name="use_relpaths_for_solc_json",
                detect_fn=lambda output: (
                    "detected"
                    if not compilation_config.get("use_relpaths_for_solc_json", False)
                    else None
                ),
                apply_fn=self._apply_use_relpaths_workaround,
                enabled=True,
                last_resort=True,
            ),
        ]

        # Calculate max retries based on enabled workarounds
        enabled_workarounds = [w for w in workarounds if w.enabled]
        max_retries = len(contracts) * len(enabled_workarounds)
        retry_count = 0

        # Seed compiler_map / solc_via_ir_map up front so the workarounds below
        # can update per-contract entries unconditionally; _finalize_compile_maps
        # collapses them back to scalars on every exit (see helpers above).
        self._seed_compile_maps(compilation_config, contracts)
        self._mirror_compile_flags(compilation_config, updated_config_dict)
        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        output = ""
        while retry_count <= max_retries:
            # Run compilation
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            output = result.stdout + result.stderr

            if result.returncode == 0:
                self._finalize_compile_maps(compilation_config, updated_config_dict, config_file)
                return True, output, updated_config_dict

            # Log compilation output for debugging
            if self.verbose >= 2:
                self.log("Compilation output (stdout + stderr):")
                self.log(output)

            # Terminal, non-recoverable case: the main (verify-target) contract has no
            # bytecode (abstract / no constructor). No workaround can make an abstract
            # contract concrete, so detect it as soon as compilation fails rather than
            # after exhausting every workaround (the catch-all relpaths workaround would
            # otherwise fire first and delay this).
            abstract_main_contract = self._detect_abstract_main_contract(output, compilation_config)
            if abstract_main_contract is not None:
                self._finalize_compile_maps(compilation_config, updated_config_dict, config_file)
                raise AbstractMainContractError(
                    f"Main contract '{abstract_main_contract}' compiled to no bytecode: it is abstract "
                    f"(or is missing a constructor), so it is not deployable and cannot be "
                    f"verified. Re-run with a concrete implementation as the main contract."
                )

            # One pass over the failed output: apply EVERY applicable workaround
            # before recompiling — one full certoraRun per pass is expensive, so
            # a pass fixes as much of this output as it can. detect_fns run
            # sequentially after earlier applies in the same pass, so detects
            # gated on conf/manager state (e.g. _remappings_workaround_applied)
            # see the pass's own effects.
            applied_this_pass.clear()
            state_before = self._retry_state(cmd, compilation_config, updated_config_dict)

            def try_workaround(workaround: CompilationWorkaround) -> bool:
                """Detect and, on a hit, apply — returns whether it applied."""
                nonlocal updated_config_dict
                detect_result = workaround.detect_fn(output)
                if detect_result is None:
                    return False
                self.log(f"Applying {workaround.name} workaround")
                updated_config_dict = workaround.apply_fn(
                    detect_result,
                    updated_config_dict,
                    compilation_config,
                    config_file,
                    contracts,
                )
                # If we disabled build_cache in config, also remove from CLI command
                if workaround.name == "cached_autofinder_failure" and "--build_cache" in cmd:
                    cmd.remove("--build_cache")
                applied_this_pass.add(workaround.name)
                return True

            # Exclusive workarounds go first, regardless of list position: they
            # invalidate the whole output, so when one applies the pass ends
            # there and nothing else may act on it.
            for workaround in workarounds:
                if workaround.exclusive and workaround.enabled and try_workaround(workaround):
                    break
            if not applied_this_pass:
                for workaround in workarounds:
                    if not workaround.enabled or workaround.exclusive:
                        continue
                    if workaround.last_resort and applied_this_pass:
                        continue
                    try_workaround(workaround)

            # If no workaround applies, exit immediately (guardrail against infinite loop)
            if not applied_this_pass:
                if retry_count == 0:
                    # First attempt failed - log output for debugging
                    self.log("Compilation failed. Output:", "WARNING")
                    self.log(output, "WARNING")
                else:
                    self.log("Compilation failed with no applicable workaround", "ERROR")
                self._finalize_compile_maps(compilation_config, updated_config_dict, config_file)
                return False, output, updated_config_dict

            # If the whole pass changed nothing, recompiling would reproduce the
            # identical failure — stop here instead of burning another certoraRun.
            if self._retry_state(cmd, compilation_config, updated_config_dict) == state_before:
                self.log(
                    f"Workarounds applied ({', '.join(sorted(applied_this_pass))}) but the conf "
                    f"and command are unchanged — retrying would fail identically, giving up",
                    "ERROR",
                )
                self._finalize_compile_maps(compilation_config, updated_config_dict, config_file)
                return False, output, updated_config_dict

            retry_count += 1
            conf_contents = json.dumps(compilation_config, indent=2)
            self.log(
                f"Retrying compilation after {', '.join(sorted(applied_this_pass))} "
                f"fix(es) (attempt {retry_count}/{max_retries})\n"
                f"  Command: {' '.join(cmd)}\n"
                f"  Config ({config_file}):\n{conf_contents}"
            )

        # Max retries exceeded
        self.log(f"Max retries ({max_retries}) exceeded for workarounds", "ERROR")
        self.log("Final compilation output:", "ERROR")
        self.log(output, "ERROR")
        self._finalize_compile_maps(compilation_config, updated_config_dict, config_file)
        return False, output, updated_config_dict

    @staticmethod
    def _retry_state(cmd: List[str], compilation_config: Dict, updated_config_dict: Dict) -> str:
        """Serialized snapshot of everything a workaround can change to make the
        next compilation retry behave differently: the command line and both
        config dicts. Used by the no-progress check in
        ``run_compilation_with_workarounds`` — a pass that leaves this snapshot
        identical was a no-op, so retrying would reproduce the same failure
        verbatim.

        Invariant on apply_fns: any application that makes real progress MUST
        change the command or one of the two conf dicts. Progress expressed
        only through side channels (files written to disk, the contracts list,
        manager attributes) is invisible here and would be misread as no-op.
        """
        return json.dumps([cmd, compilation_config, updated_config_dict], sort_keys=True, default=str)

    # =========================================================================
    # Detection methods
    # =========================================================================

    def _detect_stack_too_deep_errors(
        self, output: str, contracts: List[ContractHandle]
    ) -> Optional[str]:
        """Detect stack-too-deep error and return affected contract name."""
        lines = output.split("\n")

        for i in range(len(lines)):
            line = lines[i]

            # Pattern 1: Look for "Compiling <PATH> to expose internal function information"
            if "Compiling" in line and "to expose internal function information" in line:
                parts = line.split("Compiling", 1)
                if len(parts) > 1:
                    path_part = parts[1].split("to expose internal function information", 1)[0].strip()

                    if i + 2 < len(lines):
                        if "Encountered an exception generating autofinder" in lines[i + 1]:
                            if lines[i + 2].startswith("CompilerError: Stack too deep"):
                                contract_name = self._get_contract_name_from_path(path_part, contracts)
                                if contract_name:
                                    self.log(f"Detected stack-too-deep error for {contract_name} (path: {path_part})")
                                    return contract_name
                                else:
                                    self.log(f"Warning: Could not map path '{path_part}' to contract name", "WARNING")

            # Pattern 2: Look for "Compiling <PATH>..." (generic compilation)
            else:
                path_part = _path_from_compiling_line(line)
                if path_part is None or i + 1 >= len(lines) or lines[i + 1].startswith("Compiling "):
                    continue

                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].startswith("Compiling "):
                        break
                    if "had an error:" in lines[j]:
                        for k in range(j + 1, min(j + 3, len(lines))):
                            if lines[k].startswith("CompilerError: Stack too deep"):
                                contract_name = self._get_contract_name_from_path(path_part, contracts)
                                if contract_name:
                                    self.log(f"Detected stack-too-deep error for {contract_name} (path: {path_part})")
                                    return contract_name
                                else:
                                    self.log(f"Warning: Could not map path '{path_part}' to contract name", "WARNING")
                        break

        return None

    def _detect_cancun_opcode_errors(self, output: str, contracts: List[ContractHandle]) -> Optional[str]:
        """Detect DeclarationError for Cancun opcodes (mcopy, tload, tstore) and return affected contract name."""
        if (
            'DeclarationError: Function "mcopy" not found' not in output
            and 'DeclarationError: Function "tload" not found' not in output
            and 'DeclarationError: Function "tstore" not found' not in output
        ):
            return None

        cancun_errors = (
            'DeclarationError: Function "mcopy" not found',
            'DeclarationError: Function "tload" not found',
            'DeclarationError: Function "tstore" not found',
        )
        lines = output.split("\n")
        for i in range(len(lines)):
            line = lines[i]
            path_part = _path_from_compiling_line(line)
            if path_part is None:
                continue
            for j in range(i + 1, min(i + 5, len(lines))):
                if lines[j].startswith("Compiling "):
                    break
                if "had an error:" in lines[j]:
                    for k in range(j + 1, min(j + 10, len(lines))):
                        if any(err in lines[k] for err in cancun_errors):
                            contract_name = self._get_contract_name_from_path(path_part, contracts)
                            if contract_name:
                                self.log(f"Detected Cancun opcode error for {contract_name} (path: {path_part})")
                                return contract_name
                    break
        return None

    def _detect_via_ir_required(
        self, output: str, contracts: List[ContractHandle]
    ) -> Optional[str]:
        """Detect a solc feature that exists only on the via-ir pipeline and return
        the affected contract name.

        Contracts start on plain settings and gain via-ir strictly out of
        necessity; this is the necessity signal for non-stack reasons, e.g.
        "UnimplementedFeatureError: Require with a custom error is only
        available using the via-ir pipeline." Matching is whitespace-normalized
        per compiled unit, since solc hard-wraps the phrase.
        """
        marker = "only available using the via-ir pipeline"
        current_path: Optional[str] = None
        segment: List[str] = []

        def segment_hit() -> Optional[str]:
            if current_path and marker in _normalize_ws("\n".join(segment)):
                return self._get_contract_name_from_path(current_path, contracts)
            return None

        for line in output.split("\n"):
            path = _path_from_compiling_line(line)
            if path is not None and "to expose internal function information" not in line:
                hit = segment_hit()
                if hit:
                    self.log(f"Detected via-ir-only feature for {hit} (path: {current_path})")
                    return hit
                current_path = path
                segment = []
            else:
                segment.append(line)
        hit = segment_hit()
        if hit:
            self.log(f"Detected via-ir-only feature for {hit} (path: {current_path})")
        return hit

    def _detect_yul_exception_stack_too_deep(self, output: str) -> bool:
        """Detect YulException with stack too deep error.

        solc hard-wraps its diagnostic text at a fixed width, so the phrase
        "Stack too deep" is frequently split across a newline (e.g. the real
        error reads "...Stack too\ndeep."). Match across arbitrary whitespace
        (DOTALL + ``\\s+`` between words) so the wrap does not hide the error;
        otherwise the via-ir / optimizer workarounds never trigger.

        Some solc emissions never contain the "Stack too deep" phrase at all —
        e.g. "YulException: Variable _7 is 1 too deep in the stack [ ... ]
        memoryguard was present." — so also match the "too deep in(side) the
        stack" wording (the semantics, not one spelling).

        Autofinder-generation failures ARE matched on purpose: they cost the
        file its internal summaries, and this ladder (optimizer, then relaxing
        the autofinder assertion) is the reaction to exactly that.
        """
        pattern = r"YulException:.*?(?:Stack\s+too\s+deep|too\s+deep\s+in(?:side)?\s+the\s+stack)"
        return bool(re.search(pattern, output, re.IGNORECASE | re.DOTALL))

    def _detect_compiler_version_mismatch(
        self, output: str, contracts: List[ContractHandle]
    ) -> Optional[Tuple[str, str]]:
        """Detect compiler version mismatch error and extract contract name and required version.

        solc hard-wraps its diagnostic text at a fixed width, so the marker
        phrase is frequently split across newlines (e.g. "ParserError: Source
        \\nfile requires different compiler version"). Match it with ``\\s+``
        between words over the whole output, then map each match back to its
        line index for the path/pragma context extraction.
        """
        lines = output.split("\n")

        marker = re.compile(
            r"ParserError:\s+Source\s+file\s+requires\s+different\s+compiler\s+version"
        )
        for match in marker.finditer(output):
            # Line index of the match start; the error may span several lines,
            # so context searches below start from where the marker begins.
            i = output.count("\n", 0, match.start())

            # Try to find file_path from preceding "Compiling ..." line
            file_path = _find_compiling_path_before(lines, i, max_lookback=15)

            # Fallback: Extract file_path from arrow line if not found above
            if not file_path:
                for j in range(i + 1, min(i + 10, len(lines))):
                    if "-->" in lines[j]:
                        path_parts = []
                        arrow_line = lines[j].split("-->", 1)
                        if len(arrow_line) > 1:
                            path_parts.append(arrow_line[1].strip())

                        for k in range(j + 1, min(j + 5, len(lines))):
                            stripped = lines[k].strip()
                            if not stripped or stripped == "|":
                                break
                            path_parts.append(stripped)
                            if re.search(r":\d+:\d+:\s*$", stripped):
                                break

                        full_path = "".join(path_parts)
                        path_match = re.search(r"^(.+?):\d+:\d+:\s*$", full_path)
                        if path_match:
                            file_path = path_match.group(1).strip()
                            break

            if not file_path:
                continue

            # Extract pragma specification from subsequent lines
            for k in range(i + 1, min(i + 10, len(lines))):
                pragma_spec = extract_pragma_spec(lines[k])
                if pragma_spec:
                    version = resolve_pragma_to_version(pragma_spec)
                    if not version:
                        self.log(f"Could not resolve pragma '{pragma_spec}' to concrete version", "WARNING")
                        return None

                    contract_name = self._get_contract_name_from_path(file_path, contracts)
                    if contract_name:
                        self.log(f"Detected compiler version mismatch for {contract_name}: requires {version}")
                        return (contract_name, version)
                    else:
                        self.log(f"Warning: Could not map path '{file_path}' to contract name", "WARNING")
                        return None

        return None

    def _detect_solc_not_found(self, output: str) -> Optional[str]:
        """Detect a missing solc binary and return its name.
        """
        match = re.search(
            r"attribute/flag 'compiler_map': Solidity executable (solc\S+) not found in path",
            output,
        )
        if match:
            self.log(f"Detected missing solc binary: {match.group(1)}", "WARNING")
            return match.group(1)
        return None

    def _detect_unsupported_solc_via_ir(self, output: str, contracts: List[ContractHandle]) -> Optional[str]:
        """Detect 'Unsupported solc version ... for solc_via_ir' and return the affected contract name."""
        lines = output.split("\n")
        for i in range(len(lines)):
            # Normalize intra-line whitespace so the two markers still match if solc
            # padded/wrapped within the line; the line index is kept for the lookback.
            norm_line = _normalize_ws(lines[i])
            if "Unsupported solc version" in norm_line and "solc_via_ir" in norm_line:
                file_path = _find_compiling_path_before(lines, i, max_lookback=15)
                if file_path is not None:
                    contract_name = self._get_contract_name_from_path(file_path, contracts)
                    if contract_name:
                        self.log(f"Detected unsupported solc version for via-ir: {contract_name}")
                        return contract_name
                return None
        return None

    def _has_remappings_conflict(self, output: str) -> bool:
        return "package.json and remappings.txt include duplicated keys in" in output

    def _has_source_not_found(self, output: str) -> bool:
        # solc hard-wraps this diagnostic, splitting the two markers across newlines
        # (ion-protocol wraps between `Source` and the quote; angstrom wraps
        # `File\nnot found`), so normalize whitespace before the substring check.
        normalized = _normalize_ws(output)
        return 'ParserError: Source "' in normalized and "File not found" in normalized

    def _has_unnamed_return_warning(self, output: str) -> bool:
        return "Unnamed return variable can remain unassigned" in output

    # Two-line signature emitted by the Certora wrapper when a library used by a
    # contract's constructor is not in the compile scope. Matching both lines makes
    # the detector specific enough that no other prover error collides. (Both regexes
    # search the whole output, so a marker split across a solc wrap could be missed —
    # unlike _has_source_not_found it isn't normalized here because the \w+/\S+ captures
    # rely on the raw token boundaries.)

    def _detect_missing_library(
        self, output: str, contracts: List[ContractHandle]
    ) -> Optional[Tuple[str, str, str]]:
        """Detect "Failed to find a dependency library" + "Failed to find a contract named ..."
        and return (consumer, lib_name, lib_path), or None. Covers case in which we need to
        create a harness of `consumer` contract with dummy use of the library `lib_name`
        to produce library bytecode.
        Returns None if ``(consumer, lib_name)`` is already in ``self._harnessed_libs``.
        """
        _missing_lib_consumer_re = re.compile(
            r"Failed to find a dependency library while building the constructor bytecode of (\w+)\."
        )
        consumer_match = _missing_lib_consumer_re.search(output)
        _missing_lib_target_re = re.compile(
            r"Failed to find a contract named (\w+) in file (\S+\.sol)\."
        )
        target_match = _missing_lib_target_re.search(output)
        if not (consumer_match and target_match):
            return None

        # Walk backwards from the "Failed to find a dependency library" line to the
        # nearest preceding "Compiling <path>..." line — that path is the file the
        # prover was working on when linking failed.
        lines = output.split("\n")
        for idx, line in enumerate(lines):
            if "Failed to find a dependency library while building" in line:
                header_line_idx = idx
                break
        else:
            # consumer_match matched the longer string that contains this exact
            # substring, so it must appear in `lines`. Reaching here is impossible.
            assert False, "dependency-library header matched but absent line-by-line"

        compiling_path = _find_compiling_path_before(lines, header_line_idx)
        # A missing-library error is always preceded by a plain
        # ``Compiling <path>...`` line for the file under compilation.
        assert compiling_path is not None, \
            "missing-library error not preceded by a 'Compiling <path>...' line"

        # Consumer is contract for which compilation faile, i.e. contract in path from
        # ``Compiling <path>`` log message.
        # Ancestor is contract containing actually the import of library.
        # Consumer can be also the Ancestor or it inherits the ancestor.
        ancestor = consumer_match.group(1)
        mapped = self._get_contract_name_from_path(compiling_path, contracts)
        if mapped:
            consumer = mapped
        else:
            # Path present but not in the scene (a file we don't track) — fall
            # back to the ancestor the error names.
            consumer = ancestor
            self.log(
                f"Missing-library error: could not map '{compiling_path}' to a "
                f"contract in the scene; falling back to ancestor '{ancestor}'",
                "WARNING",
            )

        lib_name = target_match.group(1)
        lib_path = target_match.group(2)
        if (consumer, lib_name) in self._harnessed_libs:
            self.log(
                f"Missing-library error still references ({consumer}, {lib_name}) after "
                f"harnessing — giving up to break retry loop",
                "WARNING",
            )
            return None
        return consumer, lib_name, lib_path

    # =========================================================================
    # Apply workaround methods (full versions that write to config file)
    # =========================================================================

    def _apply_compiler_version_workaround_to_config(
        self,
        detect_result: Tuple[str, str],
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Apply compiler version workaround to config files for a contract with version mismatch."""
        contract_name, version_string = detect_result

        self.log(f"Applying compiler version workaround for contract: {contract_name}")
        self.apply_compiler_version_workaround(contract_name, version_string, updated_config_dict)
        compilation_config.update(updated_config_dict)

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_via_ir_workaround_to_config(
        self,
        contract_name: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Apply via-ir workaround to config files for a contract with stack-too-deep error."""
        self.log(f"Applying via-ir workaround for contract: {contract_name}")
        self._apply_via_ir_workaround(contract_name, updated_config_dict)
        compilation_config.update(updated_config_dict)

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_evm_version_cancun_workaround_to_config(
        self,
        contract_name: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        contracts: List[ContractHandle],
    ) -> Dict:
        """Apply EVM version cancun workaround to config for a contract with Cancun opcode errors."""
        self.log(f"Applying EVM version cancun workaround for contract: {contract_name}")
        if "solc_evm_version_map" not in updated_config_dict:
            updated_config_dict["solc_evm_version_map"] = {}
        updated_config_dict["solc_evm_version_map"][contract_name] = "cancun"
        self.log(f"Adding EVM version cancun workaround for contract: {contract_name}")
        compilation_config.update(updated_config_dict)

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_disable_via_ir_workaround_to_config(
        self,
        contract_name: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        contracts: List[ContractHandle],
    ) -> Dict:
        """Disable via-ir for a contract with an old Solidity version that doesn't support it."""
        self.log(f"Disabling via-ir for contract: {contract_name} (unsupported solc version)")

        updated_config_dict["solc_via_ir_map"][contract_name] = False

        compilation_config.update(updated_config_dict)
        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)
        return updated_config_dict

    def _apply_optimizer_for_via_ir(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Apply optimizer alongside via-ir to resolve YulException stack-too-deep."""
        self.log("Detected YulException stack-too-deep with via-ir — adding solc_optimize 200", "WARNING")

        compilation_config["solc_optimize"] = "200"
        updated_config_dict["solc_optimize"] = "200"

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_yul_exception_workaround(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Last resort: stop asserting autofinder success.

        Autofinders are still generated per file — files whose instrumentation
        compiles keep their internal summaries, and files where it fails fall
        back to the un-instrumented source. Compile settings are untouched:
        contracts gain via-ir/optimizer strictly out of compilation necessity,
        so removing them here would reintroduce the errors they fix.
        """
        self.log(
            "YulException persists with via-ir + optimizer — no longer asserting "
            "autofinder success (failing files fall back un-instrumented)",
            "WARNING",
        )

        compilation_config["assert_autofinder_success"] = False
        updated_config_dict["assert_autofinder_success"] = False

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_disable_build_cache(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Disable build_cache to get the real error instead of a cached autofinder failure."""
        self.log("Autofinder failure from build cache — disabling build_cache to get the real error", "WARNING")

        compilation_config["build_cache"] = False
        updated_config_dict["build_cache"] = False

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_solc_fallback_workaround(
        self,
        failed_solc: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Fall back from a missing versioned solc binary.

        Checks if plain 'solc' provides the version we need; if not, uses the
        default versioned binary (convention-aware, e.g. solc8.34 or solc-0.8.34).
        """
        fallback = self._pick_solc_fallback()
        self.log(f"Falling back from '{failed_solc}' to '{fallback}'", "WARNING")

        # solc is seeded into compiler_map up front (see _seed_compile_maps), so
        # rewrite the bad binary there — every entry pinned to it — rather than
        # setting the scalar solc, which can't coexist with compiler_map. A
        # uniform map collapses back to scalar solc in _normalize_compile_maps.
        # compilation_config shares this map object with updated_config_dict,
        # so the in-place rewrite is visible in the disk write below too — no mirroring needed.
        cmap = updated_config_dict.get("compiler_map")
        assert isinstance(cmap, dict), "compiler_map is seeded before any workaround runs"
        for name, version in cmap.items():
            if version == failed_solc:
                cmap[name] = fallback

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _pick_solc_fallback(self) -> str:
        """Choose the best solc fallback: plain 'solc' if it matches the desired version, else the default."""
        desired = self._extract_version_from_solc_name(self.solc_default_version)
        if not desired:
            return "solc"

        plain_version = self._get_plain_solc_version()
        if plain_version and plain_version == desired:
            return "solc"

        if shutil.which(self.solc_default_version):
            return self.solc_default_version

        return "solc"

    @staticmethod
    def _extract_version_from_solc_name(solc_name: str) -> Optional[str]:
        """Extract semantic version from a solc binary name.

        'solc8.34' -> '0.8.34', 'solc-0.8.34' -> '0.8.34', 'solc' -> None
        """
        if solc_name.startswith("solc-"):
            return solc_name[5:]  # "solc-0.8.34" -> "0.8.34"
        if solc_name.startswith("solc") and len(solc_name) > 4 and solc_name[4].isdigit():
            return f"0.{solc_name[4:]}"  # "solc8.34" -> "0.8.34"
        return None

    @staticmethod
    def _get_plain_solc_version() -> Optional[str]:
        """Run 'solc --version' and return the version string, e.g. '0.8.33'."""
        try:
            result = subprocess.run(["solc", "--version"], capture_output=True, text=True, check=False, timeout=5)
            match = re.search(r"Version:\s*(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    def _apply_remappings_conflict_workaround(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Apply remappings conflict workaround by constructing explicit packages list."""
        self.log("Detected remappings conflict between package.json and remappings.txt", "WARNING")
        self.log("Building explicit packages list to resolve conflict...")

        packages = self._build_packages_from_remapping_sources()
        updated_config_dict["packages"] = packages
        compilation_config["packages"] = packages

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        self._remappings_workaround_applied = True
        return updated_config_dict

    def _apply_source_not_found_packages_workaround(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Apply source-not-found workaround by adding packages from project remapping sources."""
        self.log(
            "Source file not found — adding packages from `forge remappings`, "
            "foundry.toml, remappings.txt, and package.json",
            "WARNING",
        )

        packages = self._build_packages_from_remapping_sources()
        updated_config_dict["packages"] = packages
        compilation_config["packages"] = packages

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        self._remappings_workaround_applied = True
        return updated_config_dict

    def _apply_ignore_solidity_warnings_workaround(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Set ignore_solidity_warnings to suppress unnamed return variable warnings."""
        self.log("Setting ignore_solidity_warnings to suppress unnamed return warnings", "WARNING")
        updated_config_dict["ignore_solidity_warnings"] = True
        compilation_config["ignore_solidity_warnings"] = True

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_missing_library_harness_to_config(
        self,
        detect_result: Tuple[str, str, str],
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        contracts: List[ContractHandle],
    ) -> Dict:
        """Wrap the *consumer* in a harness that uses the missing library *lib_name*.
        The hanrness contains a dummy method calling a public
        external/public library function to force compilation of the library's deployable
        bytecode into the same compilation unit,
        """
        consumer, lib_name, lib_path = detect_result

        # Accumulate libs per consumer so multi-library cases regenerate the
        # harness with everything that's ever been missing.
        bucket = self._harnesses_by_consumer.setdefault(consumer, [])
        if (lib_name, lib_path) not in bucket:
            bucket.append((lib_name, lib_path))

        # Find the consumer in the ContractHandle list so we know its source file.
        consumer_handle = next(
            (h for h in contracts if h.contract_name == consumer), None
        )
        if consumer_handle is None:
            self.log(
                f"Consumer '{consumer}' not in contracts list — cannot generate "
                f"wrapping harness for missing library '{lib_name}'",
                "ERROR",
            )
            return updated_config_dict

        consumer_path = consumer_handle.source_file

        # Pin the harness to the consumer's compile mode so its bytecode matches
        # what the linker expects.
        compiler_map = compilation_config.get("compiler_map", {}) or {}
        target_solc = compiler_map.get(consumer, self.solc_default_version)

        harness_dir = user_harnesses_dir(self.project_root)
        harness_dir.mkdir(parents=True, exist_ok=True)
        harness_name = f"{consumer}Harness"
        harness_file = user_harness_path(self.project_root, harness_name)

        consumer_file_abs = self.project_root / consumer_path
        library_specs = [
            LibrarySpec(
                name=lib,
                source_text=(self.project_root / path).read_text(),
                file_path=self.project_root / path,
            )
            for lib, path in bucket
        ]
        content = build_consumer_harness_source(
            consumer_name=consumer,
            consumer_source_text=consumer_file_abs.read_text(),
            consumer_file_abs=consumer_file_abs,
            libraries=library_specs,
            harness_dir=harness_dir,
            harness_name=harness_name,
            pragma_line=pragma_for_solc(target_solc),
        )
        harness_file.write_text(content)
        self.log(
            f"Generated consumer harness {harness_name} wrapping {consumer} "
            f"({len(bucket)} library/libraries): {harness_file}"
        )

        rel_harness = str(harness_file.relative_to(self.project_root))
        harness_files_entry = f"{rel_harness}:{harness_name}"

        # Replace consumer's entry in `files` with the harness. Match both the bare
        # path and the explicit `path:Consumer` form to be defensive. If we've
        # already replaced it on a prior firing for this consumer, the entry is
        # already the harness form — leave it alone.
        files = compilation_config.get("files", []) or []
        new_files: List[str] = []
        for entry in files:
            if entry == consumer_path or entry == f"{consumer_path}:{consumer}":
                new_files.append(harness_files_entry)
            else:
                new_files.append(entry)
        if harness_files_entry not in new_files:
            new_files.append(harness_files_entry)
        compilation_config["files"] = new_files
        updated_config_dict["files"] = new_files

        for map_name in ("compiler_map", "solc_via_ir_map", "solc_optimize_map", "solc_evm_version_map"):
            existing_map = compilation_config.get(map_name)
            if not isinstance(existing_map, dict):
                continue
            if consumer in existing_map:
                existing_map[harness_name] = existing_map.pop(consumer)
            updated_config_dict[map_name] = existing_map

        # Replace the consumer's ContractHandle with the harness's so downstream
        # detectors see the new name. Edit in place to keep caller references valid.
        for i, h in enumerate(contracts):
            if h.contract_name == consumer:
                contracts[i] = ContractHandle(
                    contract_name=harness_name, source_file=rel_harness
                )

        self._harnessed_libs.add((consumer, lib_name))

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    def _apply_use_relpaths_workaround(
        self,
        _detect_result: str,
        updated_config_dict: Dict,
        compilation_config: Dict,
        config_file: Path,
        _contracts: List[ContractHandle],
    ) -> Dict:
        """Catch-all: enable use_relpaths_for_solc_json when a compile fails for a non-specific reason.

        Last workaround tried before setup_prover falls back to the import-patch pass.
        Works around solc autofinder / path-resolution edge cases that the pattern-specific
        workarounds don't recognize.
        """
        self.log(
            "Compile failed with no specific workaround match — enabling use_relpaths_for_solc_json",
            "WARNING",
        )
        updated_config_dict["use_relpaths_for_solc_json"] = True
        compilation_config["use_relpaths_for_solc_json"] = True

        with open(config_file, "w") as f:
            json.dump(compilation_config, f, indent=2)

        return updated_config_dict

    # =========================================================================
    # Core workaround logic (in-memory only, no file writes)
    # =========================================================================

    def apply_compiler_version_workaround(
        self,
        contract_name: str,
        version_string: str,
        conf_object: Dict[str, Any],
    ) -> bool:
        """
        Apply compiler version workaround for a contract that needs a specific compiler version.

        Args:
            contract_name: Contract name that needs specific compiler version
            version_string: Required version (e.g., "0.8.23")
            conf_object: Config dictionary to update (modified in place)

        Returns:
            True if compiler_map was modified
        """
        # compiler_map is seeded up front by _seed_compile_maps, so it always
        # exists here — just set the affected contract's entry.
        formatted_version = self.format_solc_version(version_string)

        # Set the affected contract to required version
        conf_object["compiler_map"][contract_name] = formatted_version
        self.log(f"Adding compiler version workaround for {contract_name}: {formatted_version}")

        return True

    def _apply_via_ir_workaround(self, contract_needing_via_ir: str, config_dict: Dict) -> Dict:
        """Add solc_via_ir_map entry for contract that needs via-ir compilation."""
        # solc_via_ir_map is seeded up front by _seed_compile_maps; just set the
        # contract that needs via-ir to True.
        config_dict["solc_via_ir_map"][contract_needing_via_ir] = True
        self.log(f"Adding via-ir workaround for contract: {contract_needing_via_ir}")

        return config_dict

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _get_contract_name_from_path(self, file_path: str, contracts: List[ContractHandle]) -> Optional[str]:
        """Map a file path from error message to contract name.

        TODO: This returns the first matching contract, but a .sol file can contain multiple
        contracts. Consider returning all contract names or using file paths as keys instead.
        """
        # Try direct string match first (fastest)
        for contract in contracts:
            if contract.source_file == file_path:
                return contract.contract_name

        # Try normalized path comparison
        try:
            normalized_path = str(Path(file_path).resolve())
            for contract in contracts:
                contract_path = str(Path(contract.source_file).resolve())
                if contract_path == normalized_path:
                    return contract.contract_name
        except Exception:
            pass

        return None

    def _build_packages_from_remapping_sources(self) -> List[str]:
        """Build a merged packages list from forge remappings, foundry.toml, remappings.txt, package.json.

        Priority on key conflict (highest wins, with a warning on path mismatch):
        1. `forge remappings` — recursively walks nested foundry.toml files (e.g. lib/*/foundry.toml)
            and emits paths relative to CWD; strictly stronger than parsing the top-level
            foundry.toml alone. Best-effort: skipped silently if forge is not installed or
            the command fails.
        2. foundry.toml — hand-curated source of truth for the build system
        3. remappings.txt — often partially auto-generated; may drift
        4. package.json — npm-style fallback
        """
        # The reactive path runs in the project CWD, so base_dir="." emits relative paths
        # unchanged and runs forge in CWD.
        return build_packages_from_remapping_sources(base_dir=Path("."), log_fn=self.log)
