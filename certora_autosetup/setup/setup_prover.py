"""
Setup Prover Module - Handles all setup-related operations for the Certora PreAudit Orchestrator.

This module contains all the setup phase functionality including:
- Setup summaries generation
- Compilation analysis
- ERC-7201 detection
- Generator execution (Generic rules, External call checker, Privileged operations)
- Summary compilation testing
"""

import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from certora_autosetup.setup.setup_summaries import SummarySetup

from certora_autosetup.build_systems.base import BuildSystemConfig
from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.parsers.foundry import FoundryContractExtractor
from certora_autosetup.utils.contract_utils import parse_contract_files
from certora_autosetup.setup.auto_munges import detect_and_apply_code_access_patches
from certora_autosetup.setup.signature_manager import SignatureManager
from certora_autosetup.setup.signature_types import ContractInfo
from certora_autosetup.setup.solidity_utils import extract_definitions_from_solidity
from packaging.version import Version
from certora_autosetup.utils.config_manager import convert_solc_version_to_certora_format
from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils.file_utils import atomic_write_json_fsspec
from certora_autosetup.utils.llm_util import ledger_component
from certora_autosetup.utils.constants import (
    DEFAULT_SOLC_VERSION,
    DIR_CERTORA_INTERNAL,
    FILE_BUILD_ASTS,
    SolcConvention,
    SUMMARIES_SUBDIR,
)
from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.paths import (
    internal_compilation_conf,
    internal_compilation_dummy_spec,
    user_erc7201_spec_path,
)
from certora_autosetup.utils.solc_version_resolver import VIA_IR_MIN_VERSION
from certora_autosetup.utils.types import ContractHandle, ContractKind, TypeParseMode, parse_type_descriptor

class CompilationAnalysisError(Exception):
    """Raised when compilation analysis fails."""


class SummarySetupError(Exception):
    """Raised when setup summaries generation fails."""


# Add the script directory to path for imports
scripts_dir_path = Path(__file__).parent.resolve()
sys.path.insert(0, str(scripts_dir_path))



class SetupProver:
    """Class to handle setup operations for Certora Prover."""

    def __init__(
        self,
        log,
        certora_dir,
        script_dir,
        additional_contracts,
        extra_args,
        skip_llm,
        force_llm_regenerate,
        stop_after_summaries,
        scope,
        verbose=0,
        certora_run_command="certoraRun",
        contract_names=None,
        get_build_system_config_dict=None,
        solc_default_version=DEFAULT_SOLC_VERSION,
    ):
        """Initialize SetupProver with required dependencies."""
        self.log = log
        self.certora_dir = certora_dir
        self.script_dir = script_dir
        self.additional_contracts = additional_contracts
        self.extra_args = extra_args
        self.skip_llm = skip_llm
        self.force_llm_regenerate = force_llm_regenerate
        self.stop_after_summaries = stop_after_summaries
        self.verbose = verbose
        self.certora_run_command = certora_run_command
        self.contract_names = contract_names or []
        self.get_build_system_config_dict = get_build_system_config_dict or (lambda: {})
        self.solc_default_version = solc_default_version
        self.scope = scope
        self.build_system: Optional[BuildSystem] = None
        self.build_system_config: Optional[BuildSystemConfig] = None

        # Track compilation configuration updates
        self.compilation_config_updates: Dict[str, Any] = {}
        self.import_patcher_applied: bool = False
        self.erc7201_namespaces_found: bool = False
        self._remappings_workaround_applied: bool = False
        self._build_dir: Path | None = None
        # SummarySetup is constructed during run_setup_summaries and kept around so that
        # call resolution can reuse it (its _methods_per_contract / _cvl_functions
        # accumulators must persist across upfront and lazy LLM phases).
        self.summary_setup: Optional["SummarySetup"] = None

    def run_setup_erc7201_patch(self) -> bool:
        """Run ERC-7201 annotation patching to add missing annotations before detection."""
        self.log("🔧 Running ERC-7201 annotation patching...")

        try:
            from setup.setup_erc7201_patch import run_erc7201_patch  # type: ignore[import-not-found]

            _ = run_erc7201_patch(
                log_func=self.log,
                skip_llm=self.skip_llm,
                verbose=self.verbose > 0,
            )
            return True

        except Exception as e:
            self.log(f"❌ Error running ERC-7201 annotation patching: {e}", "WARNING")
            return True  # Don't fail the orchestration

    def run_setup_erc7201(self) -> bool:
        """Run setup_erc7201.py to detect and configure ERC-7201 storage patterns."""
        self.log("🔍 Running ERC-7201 storage pattern detection...")

        try:
            # Import and call the run function directly
            from setup.setup_erc7201 import run  # type: ignore[import-not-found]

            # Get verbose setting from orchestrator if available
            verbose = getattr(self, "verbose", False)

            spec_output = str(user_erc7201_spec_path(self.certora_dir.parent).relative_to(self.certora_dir.parent))
            result, namespaces_found = run(
                directory=".",
                spec_output=spec_output,
                verbose=verbose,
                no_config_update=False,
                summary_only=False,
            )
            self.erc7201_namespaces_found = namespaces_found

            if result == 0:
                self.log("✅ ERC-7201 detection completed successfully")
                return True
            else:
                self.log("⚠️ ERC-7201 detection completed with warnings", "WARNING")
                return True  # Don't fail the orchestration

        except Exception as e:
            self.log(f"❌ Error running ERC-7201 detection: {e}", "WARNING")
            return True  # Don't fail the orchestration

    def _precompute_compiler_settings(
        self, contracts: List[ContractHandle], config_dict: Dict
    ) -> Dict:
        """Precompute compiler_map and solc_via_ir_map from Foundry build artifacts.

        Reads each artifact's metadata.compiler.version to determine the actual solc version
        used during Foundry build. Sets up:
        - compiler_map: maps contracts to their required solc version (Certora format)
        - solc_via_ir_map: disables viaIR for contracts compiled with solc < 0.7.5
        """

        # Fall back to detection when used standalone (no orchestrator to populate self.build_system).
        if self.build_system is None:
            self.build_system = BuildSystemDetector.detect(Path.cwd())
        if self.build_system != BuildSystem.FOUNDRY:
            return config_dict

        try:
            extractor = FoundryContractExtractor(Path.cwd())
            source_map = extractor.build_source_path_to_contracts_map()
        except Exception:
            return config_dict

        if not source_map:
            return config_dict

        # Build contract_name -> compiler_version lookup from artifacts
        contract_versions: Dict[str, str] = {}
        for handle in contracts:
            normalized = str(Path(handle.source_file))
            for src_path, entries in source_map.items():
                if str(Path(src_path)) == normalized:
                    for name, version in entries:
                        if name == handle.contract_name and version:
                            contract_versions[handle.contract_name] = version
                    break

        if not contract_versions:
            return config_dict

        # Determine default version from build system config
        default_solc = config_dict.get("solc", "")
        default_raw = ""
        if default_solc.startswith("solc"):
            default_raw = "0." + default_solc[4:]

        # Precompute compiler_map for contracts with different versions
        compiler_map: Dict[str, str] = {}
        for contract_name, version in contract_versions.items():
            if version and version != default_raw:
                certora_ver = convert_solc_version_to_certora_format(version)
                compiler_map[contract_name] = certora_ver

        if compiler_map:
            # Initialize all contracts to default, then override
            full_map = {c.contract_name: default_solc for c in contracts if default_solc}
            full_map.update(compiler_map)
            # Fill in contracts not found in build artifacts (e.g., generated mocks).
            # Uses the shared ConfigManager helper so the pragma-resolution recipe
            # lives in one place (also used by sync_compiler_maps_with_files).
            # Convention is hardcoded CERTORA here — convention plumbing in
            # setup_prover is out of scope; the existing Certora-only behaviour
            # is preserved.
            for c in contracts:
                if c.contract_name not in full_map:
                    formatted = ConfigManager.extract_solc_version_from_pragma(
                        c, Path.cwd(),
                        preferred_version=default_raw or None,
                        convention=SolcConvention.CERTORA,
                    )
                    if formatted:
                        full_map[c.contract_name] = formatted
                        self.log(f"  {c.contract_name} -> {formatted} (from pragma)")
            config_dict["compiler_map"] = full_map
            self.log(
                f"Precomputed compiler_map from build artifacts: "
                f"{len(compiler_map)} contract(s) differ from default {default_solc}"
            )
            for name, ver in compiler_map.items():
                self.log(f"  {name} -> {ver}")

        # Precompute solc_via_ir_map: disable viaIR for contracts with solc < 0.7.5
        if config_dict.get("solc_via_ir"):
            via_ir_disabled: list[str] = []
            for contract_name, version in contract_versions.items():
                try:
                    if Version(version) < VIA_IR_MIN_VERSION:
                        via_ir_disabled.append(contract_name)
                except Exception:
                    pass

            if via_ir_disabled:
                config_dict.pop("solc_via_ir", None)
                via_ir_map: Dict[str, bool] = {c.contract_name: True for c in contracts}
                for name in via_ir_disabled:
                    via_ir_map[name] = False
                config_dict["solc_via_ir_map"] = via_ir_map
                self.log(
                    f"Precomputed solc_via_ir_map: disabled viaIR for {len(via_ir_disabled)} contract(s) "
                    f"with solc < {VIA_IR_MIN_VERSION}: {via_ir_disabled}"
                )

        # Apply restrictions of compiler for the files - e.g. from the compilation_restrictions section of foundry.toml
        if self.build_system_config is not None:
            config_dict = self.build_system_config.apply_per_contract_settings(
                contracts, config_dict
            )

        return config_dict

    def run_compilation_analysis(
        self, contracts: List[ContractHandle], main_contract: str
    ) -> Tuple[bool, Dict[str, Any], bool, List[ContractHandle]]:
        """Run compilation-only step to extract method information from contracts.

        Returns:
            Tuple[bool, Dict[str, Any], bool, List[ContractHandle]]:
            (success, updated_config_dict, import_patcher_applied, surviving_contracts)
            - success: True if compilation succeeded
            - updated_config_dict: Configuration dictionary with all updates applied during compilation
            - import_patcher_applied: True if import patcher was successfully applied
            - surviving_contracts: ``contracts`` after dedup and any workaround-driven renames.
        """
        self.log("=== COMPILATION ANALYSIS PHASE ===")
        self.log("Running compilation analysis to extract method information...")

        # Track configuration updates and import patcher application
        import_patcher_applied = False
        updated_config_dict = self.get_build_system_config_dict().copy()
        # Surviving scene, needs to be defined here for the case that exception fires
        surviving_contracts: List[ContractHandle] = list(contracts)

        try:
            project_root = self.certora_dir.parent
            # Always create a dummy spec for compilation to avoid path issues
            dummy_spec_path = internal_compilation_dummy_spec(project_root)
            dummy_spec_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dummy_spec_path, "w") as f:
                f.write("")  # Empty spec file
            spec_to_use = str(dummy_spec_path.relative_to(project_root))

            # Create a minimal config file for compilation
            config_file = internal_compilation_conf(project_root)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            contract_strings = [c.to_config_str() for c in contracts]
            all_files_raw = contract_strings + self.additional_contracts

            # Deduplicate files by contract name (keep shortest path) BEFORE building maps
            all_files = self._deduplicate_contract_files(all_files_raw)

            # Derive which contracts survived deduplication
            surviving_handles = parse_contract_files(all_files)
            surviving_names = {h.contract_name for h in surviving_handles}
            surviving_contracts = [c for c in contracts if c.contract_name in surviving_names]

            # If the verified contract (i.e., main contract) is not among surviving names,
            # fail gracefully here rather than failing later via certoraRun.
            if main_contract not in surviving_names:
                raise CompilationAnalysisError(
                    f"Main contract '{main_contract}' is not among the files in the prover scene: "
                    f"{sorted(surviving_names) or '(none)'}. "
                    f"The active build system did not compile it — check the build config "
                    f"or override with --build-system"
                )

            # Precompute compiler_map and solc_via_ir_map from build artifacts (survivors only)
            updated_config_dict = self._precompute_compiler_settings(surviving_contracts, updated_config_dict)

            compilation_config = {
                "files": all_files,
                "verify": f"{main_contract}:{spec_to_use}",
                "msg": f"Compilation analysis for {main_contract}",
                "assert_autofinder_success": True
            }

            # Merge build system configuration (Foundry, Hardhat, etc.)
            if updated_config_dict:
                compilation_config.update(updated_config_dict)
                self.log("Merged build system config into compilation config")

            with open(config_file, "w") as f:
                json.dump(compilation_config, f, indent=2)

            # Build command using config file - similar to warmup
            cmd = [self.certora_run_command, str(config_file)]

            # Add --compilation_steps_only flag
            cmd.append("--compilation_steps_only")

            # Add --dump-asts flag to generate AST files
            cmd.append("--dump_asts")

            # Add custom message if not already in extra_args
            has_msg = any(arg == "--msg" for arg in self.extra_args)
            if not has_msg:
                msg = f'"{main_contract} compilation analysis"'
                cmd.extend(["--msg", msg])

            # Add ALL extra arguments - this ensures --solc-via-ir and other compilation flags are included
            cmd.extend(self.extra_args)

            # Run the compilation command with workarounds
            self.log("Running: Compilation analysis")
            self.log(f"Full command: {' '.join(cmd)}")

            success, output, updated_config_dict = self._run_compilation_with_workarounds(
                cmd, config_file, compilation_config, surviving_contracts, updated_config_dict
            )

            if not success:
                self.log("Compilation analysis failed - attempting import patch fix", "WARNING")
                # Log the failure output from first attempt
                self.log("Output from first compilation attempt:", "WARNING")
                self.log(output, "WARNING")

                # Try to apply import patch and retry
                if self._run_import_patch():
                    self.log("Import patch applied successfully, retrying compilation...")
                    import_patcher_applied = True
                    success, output, updated_config_dict = self._run_compilation_with_workarounds(
                        cmd, config_file, compilation_config, surviving_contracts, updated_config_dict
                    )

                    if not success:
                        self.log("Compilation analysis failed even after import patch", "ERROR")
                        self.log("Output from second compilation attempt (after import patch):", "ERROR")
                        self.log(output, "ERROR")
                        self.log("Reverting import patch as it was not useful...", "WARNING")
                        self._revert_import_patch()
                        import_patcher_applied = False
                        raise CompilationAnalysisError(
                            "Compilation analysis failed even after import patch"
                        )
                else:
                    self.log("Import patch failed", "ERROR")
                    raise CompilationAnalysisError(
                        "Compilation analysis failed and import patch could not be applied"
                    )

            self.log("✓ Compilation analysis completed successfully")

            # Post-process the .certora_build.json file
            if not self.process_certora_build_json():
                self.log("Warning: Failed to process .certora_build.json", "WARNING")
                return False, updated_config_dict, import_patcher_applied, surviving_contracts

            return True, updated_config_dict, import_patcher_applied, surviving_contracts

        except Exception as e:
            self.log(f"✗ Compilation analysis failed with exception: {e}", "ERROR")
            return False, updated_config_dict, import_patcher_applied, surviving_contracts

    def _run_import_patch(self, project_dir: str = ".") -> bool:
        """Create and apply import patches to convert relative imports to absolute imports.

        Args:
            project_dir: Project directory to process (default: current directory)

        Returns:
            True if successful, False otherwise
        """
        from certora_autosetup.setup.solidity_import_patch import apply_patch, create_patch

        try:
            self.log("Creating import patch...")
            create_patch(project_dir)
            self.log("Applying import patch...")
            apply_patch()
            self.log("✓ Import patch completed successfully")
            return True
        except Exception as e:
            self.log(f"Error running import patch: {e}", "ERROR")
            return False

    def _revert_import_patch(self) -> bool:
        """Revert previously applied import patches.

        Returns:
            True if successful, False otherwise
        """
        from certora_autosetup.setup.solidity_import_patch import revert_patch

        try:
            self.log("Reverting import patch...")
            revert_patch()
            self.log("✓ Import patch reverted successfully")
            return True
        except Exception as e:
            self.log(f"Error reverting import patch: {e}", "ERROR")
            return False

    def _deduplicate_contract_files(self, file_strings: List[str]) -> List[str]:
        """Deduplicate contract files by contract name, keeping shortest relative path.

        When multiple files declare the same contract name, keeps only the file with
        the shortest relative path to cwd. This resolves certoraRun errors about
        duplicate contract declarations.

        Args:
            file_strings: List of file specifications in format "PATH" or "PATH:CONTRACT_NAME"

        Returns:
            Deduplicated list of file specifications

        Example:
            Input: [
                "contracts/helpers/Token.sol",
                "src/contracts/helpers/Token.sol",
                "src/Utils.sol:UtilityContract"
            ]
            Output: [
                "contracts/helpers/Token.sol",  # Shorter path wins
                "src/Utils.sol:UtilityContract"
            ]
        """
        from pathlib import Path

        # Parse all file strings to ContractHandles (includes validation)
        contract_handles = parse_contract_files(file_strings)
        parsed_files = []

        for handle in contract_handles:
            # Normalize path to relative from cwd
            try:
                path_obj = Path(handle.source_file)
                if path_obj.is_absolute():
                    # Convert absolute to relative
                    rel_path = path_obj.relative_to(Path.cwd())
                else:
                    rel_path = path_obj

                # Check for parent directory references (../)
                if '..' in str(rel_path):
                    self.log(
                        f"Warning: File path contains parent directory reference: {rel_path}. "
                        f"This may indicate incorrect working directory.",
                        "WARNING"
                    )

                parsed_files.append((str(rel_path), handle.contract_name))
            except ValueError:
                # Path is outside cwd, keep as is
                parsed_files.append((handle.source_file, handle.contract_name))

        # Group by contract name
        contract_to_paths: dict[str, list[str]] = {}
        for path, contract_name in parsed_files:
            if contract_name not in contract_to_paths:
                contract_to_paths[contract_name] = []
            contract_to_paths[contract_name].append(path)

        # For each contract, keep the shortest path
        deduplicated_tuples = []
        for contract_name, paths in contract_to_paths.items():
            if len(paths) > 1:
                # Multiple files for same contract - pick shortest path
                # Sort by path length (shortest first)
                paths.sort(key=lambda p: len(p))
                shortest_path = paths[0]

                # Log the deduplication
                self.log(
                    f"Deduplicated contract '{contract_name}': selected '{shortest_path}' "
                    f"(shortest of {len(paths)} paths: {', '.join(paths)})",
                    "INFO"
                )

                deduplicated_tuples.append((shortest_path, contract_name))
            else:
                # Only one file for this contract
                deduplicated_tuples.append((paths[0], contract_name))

        # Convert tuples back to file strings
        result = []
        for path, contract_name in deduplicated_tuples:
            # Check if contract name matches the basename
            if Path(path).stem == contract_name:
                # Simple format: just the path
                result.append(path)
            else:
                # Explicit format: PATH:CONTRACT_NAME
                result.append(f"{path}:{contract_name}")

        return result

    def _run_compilation_with_workarounds(
        self,
        cmd: List[str],
        config_file: Path,
        compilation_config: Dict,
        contracts: List[ContractHandle],
        updated_config_dict: Dict,
    ) -> Tuple[bool, str, Dict]:
        """Run compilation with automatic workarounds for common errors.

        Delegates to CompilationWorkaroundManager which handles all workaround
        detection and application.

        Args:
            cmd: Command to execute
            config_file: Path to config file (to rewrite on updates)
            compilation_config: Full compilation config (will be updated)
            contracts: List of contracts for path mapping. The missing-library harness
                renames a consumer handle to its wrapper in place in this list.
            updated_config_dict: Config dict to track updates

        Returns:
            Tuple of (success, output, updated_config_dict)
        """
        from certora_autosetup.utils.compilation_workarounds import CompilationWorkaroundManager

        workaround_manager = CompilationWorkaroundManager(
            project_root=Path.cwd(),
            solc_default_version=self.solc_default_version,
            verbose=self.verbose,
        )

        return workaround_manager.run_compilation_with_workarounds(
            cmd, config_file, compilation_config, contracts, updated_config_dict
        )

    def _byte_offset_to_line(self, file_path: str, source_bytes: dict) -> int:
        """Convert a byte offset in a source file to a 1-based line number."""
        if not file_path or "begin" not in source_bytes:
            return 0
        # Cache file contents to avoid re-reading for every method in the same file
        if not hasattr(self, "_source_file_cache"):
            self._source_file_cache: dict[str, str] = {}
        if file_path not in self._source_file_cache:
            try:
                full_path = Path(file_path)
                if not full_path.is_absolute():
                    full_path = Path.cwd() / full_path
                self._source_file_cache[file_path] = full_path.read_text(errors="replace")
            except Exception:
                return 0
        content = self._source_file_cache[file_path]
        offset = source_bytes["begin"]
        if offset > len(content):
            return 0
        return content[:offset].count("\n") + 1

    def _process_method_info(
        self,
        method: Dict,
        methods_set: set,
        all_methods: List,
        method_counts: Dict,
        originating_contract: str,
        is_internal: bool = False,
    ) -> None:
        """Process a single method and add it to the collections if not already seen."""
        # Build fullSignature from fullArgs
        full_args = method.get("fullArgs", [])
        signature_types = []
        locations = []

        for arg in full_args:
            type_desc = arg.get("typeDesc", {})
            arg_type = parse_type_descriptor(type_desc, TypeParseMode.QUALIFIED, method.get("contractName", ""))

            # Crash if arg_type is empty - this indicates a serious problem
            if not arg_type:
                raise Exception(
                    f"FATAL: Failed to convert typeDesc to signature type for argument in method {method.get('name', 'UNKNOWN')} "
                    f"in contract {method.get('contractName', 'UNKNOWN')}. "
                    f"typeDesc: {type_desc}, arg: {arg}"
                )

            signature_types.append(arg_type)
            locations.append(arg.get("location", ""))

        # Extract parameter names from method (paramNames is a separate field in the build JSON)
        param_names = method.get("paramNames", [])

        # Build return type signature from returns (same structure as fullArgs)
        returns_raw = method.get("returns", [])
        return_types = []
        return_locations = []
        for ret in returns_raw:
            type_desc = ret.get("typeDesc", {})
            ret_type = parse_type_descriptor(type_desc, TypeParseMode.QUALIFIED, method.get("contractName", ""))
            if not ret_type:
                raise Exception(
                    f"FATAL: Failed to convert typeDesc to signature type for return value in method "
                    f"{method.get('name', 'UNKNOWN')} in contract {method.get('contractName', 'UNKNOWN')}. "
                    f"typeDesc: {type_desc}, ret: {ret}"
                )
            return_types.append(ret_type)
            return_locations.append(ret.get("location", ""))

        # Extract only the fields we care about
        method_info = {
            "contractName": method.get("contractName", ""),
            # The contract whose *definition* (body) of this function is in scene — which, for an
            # inherited method, differs from ``contractName`` (the deriving contract), and for an
            # overridden method is the overriding contract, not the base that declared it. Resolved
            # from the source file, so it is '' when the file declares multiple concrete contracts,
            # including an inherited base that shares its file with another contract; such a method
            # then matches curated summaries by ``contractName`` only. Disambiguating that case needs
            # the per-function defining contract, best emitted upstream by certoraBuild.py from the
            # function node's ``certora_contract_name``, which would also remove this file heuristic.
            "definingContract": self._sole_contract_declared_in(method.get("originalFile", "")),
            "originatingContract": originating_contract,
            "isLibrary": method.get("isLibrary", False),
            "name": method.get("name", ""),
            "nonpayable": method.get("nonpayable", False)
            if not is_internal
            else method.get("stateMutability", "nonpayable") == "nonpayable",
            "stateMutability": method.get("stateMutability", "")
            if not is_internal
            else method.get("stateMutability", "nonpayable"),
            "visibility": method.get("visibility", "")
            if not is_internal
            else method.get("visibility", "internal"),
            "fullSignature": signature_types,
            "paramNames": param_names,
            "location": locations,
            "returns": return_types,
            "returnLocations": return_locations,
            "originalFile": method.get("originalFile", ""),
            "sourceLine": self._byte_offset_to_line(
                method.get("originalFile", ""), method.get("sourceBytes", {})
            ),
        }

        # Create a signature for deduplication (include full signature)
        method_signature = (
            method_info["contractName"],
            method_info["name"],
            method_info["stateMutability"],
            method_info["visibility"],
            tuple(method_info["fullSignature"]),
        )

        # Only add if not already seen
        if method_signature not in methods_set:
            methods_set.add(method_signature)
            all_methods.append(method_info)

    def _build_declared_contracts_by_file(self) -> Dict[str, Set[str]]:
        """Map each source file (relative path) to the concrete contracts/libraries it declares.

        Built from the ``ContractDefinition`` nodes in ``.asts.json``. Contracts and libraries
        (including ``abstract contract``) are collected; interfaces are excluded. The set dedups
        a declaration the AST repeats across compilation units.
        """
        contracts_by_file: Dict[str, Set[str]] = {}
        ast_path = self._build_dir / FILE_BUILD_ASTS if self._build_dir else None
        if not ast_path or not ast_path.exists():
            return {}
        with open(ast_path, "r", encoding="utf-8") as f:
            asts = json.load(f)
        for abs_path_dict in asts.values():
            for abs_path, nodes in abs_path_dict.items():
                rel = self.scope.get_relative_path(Path(abs_path))
                for node in nodes.values():
                    if (
                        node.get("nodeType") == "ContractDefinition"
                        and node.get("contractKind") != "interface"
                        and node.get("name")
                    ):
                        contracts_by_file.setdefault(rel, set()).add(node["name"])
        return contracts_by_file

    def _sole_contract_declared_in(self, original_file: str) -> str:
        """The single concrete contract/library declared in ``original_file``, or '' when the file
        is empty/unknown, declares no concrete contract, or declares more than one.
        """
        if not original_file:
            return ""
        rel = self.scope.get_relative_path(Path(original_file))
        contracts = self._declared_contracts_by_file.get(rel, set())
        return next(iter(contracts)) if len(contracts) == 1 else ""

    def generate_all_methods_json(self, build_data: Dict) -> None:
        """Extract and write all methods information to all_methods.json."""
        # Extract all methods from all contracts
        # Use a set to avoid duplicates during collection, then convert to list
        self._declared_contracts_by_file = self._build_declared_contracts_by_file()
        methods_set: set = set()
        all_methods: list = []
        method_counts: dict = {}  # For calculating overload counts

        # Iterate through all objects in the build data
        for key, obj in build_data.items():
            if isinstance(obj, dict) and "contracts" in obj:
                # Each contract object has a 'contracts' array with actual contract data
                for contract in obj.get("contracts", []):
                    # Get the originating contract name (the main compilation unit)
                    originating_contract = contract.get("name", "")

                    # Process regular methods
                    if isinstance(contract, dict) and "allMethods" in contract:
                        for method in contract["allMethods"]:
                            self._process_method_info(
                                method,
                                methods_set,
                                all_methods,
                                method_counts,
                                originating_contract,
                                is_internal=False,
                            )

                    # Process internal functions
                    if isinstance(contract, dict) and "internalFunctions" in contract:
                        # Internal functions are stored with IDs as keys
                        for func_id, func_data in contract["internalFunctions"].items():
                            if "method" in func_data:
                                method = func_data["method"]
                                self._process_method_info(
                                    method,
                                    methods_set,
                                    all_methods,
                                    method_counts,
                                    originating_contract,
                                    is_internal=True,
                                )

        # Write the processed data to all_methods.json
        output_path = Path(".certora_internal/all_methods.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            json.dump(all_methods, f, indent=2)

        # Also persist to the cache prefix for debuggability (visible on S3 in SaaS).
        # Compute-only intermediate: NOT part of cache validation/restore.
        atomic_write_json_fsspec(cache_path(DIR_CERTORA_INTERNAL, "all_methods.json"), all_methods)

        self.log(f"✓ Processed {len(all_methods)} methods to {output_path}")

    def _qualify_from_canonical_id(
        self, type_info: Dict, type_name: str, contract: Dict
    ) -> str:
        """Derive a qualified name from canonicalId, matching _qualify_user_defined_type logic.

        canonicalId format: "contracts/path/File.sol|ContractName.TypeName"
        - If right side of | contains a dot, it's already qualified.
        - Otherwise, qualify with the contract's name.
        Falls back to contract.get('name') if canonicalId is unavailable.
        """
        canonical_id = type_info.get("canonicalId", "")
        self.log(f"Qualifying type '{type_name}' with canonicalId: '{canonical_id}' and contract name: '{contract.get('name', 'UnknownContract')}'", "DEBUG")
        if "|" in canonical_id:
            right_side = canonical_id.split("|", 1)[1]
            if "." in right_side:
                return right_side
            # Not already qualified — use enclosing contract name - this should probably be unreachable
            fallback = contract.get("name", "UnknownContract")
            return f"{fallback}.{right_side}"
        # No canonicalId — use enclosing contract name - this should probably be unreachable
        fallback = contract.get("name", "UnknownContract")
        return f"{fallback}.{type_name}"

    def generate_all_user_defined_types_json(self, build_data: Dict) -> int:
        """Extract and write all user-defined types information to all_user_defined_types.json."""
        # Extract user-defined types from solidityTypes sections
        all_user_defined_types = []

        # Process user-defined types from each contract
        for key, obj in build_data.items():
            if isinstance(obj, dict) and "contracts" in obj:
                # Each contract object has a 'contracts' array with actual contract data
                for contract in obj.get("contracts", []):
                    if isinstance(contract, dict) and "solidityTypes" in contract:
                        for type_info in contract.get("solidityTypes", []):
                            if isinstance(type_info, dict):
                                type_name = None
                                qualified_name = None
                                base_type = None

                                # Handle UserDefinedValueType
                                if type_info.get("type") == "UserDefinedValueType":
                                    type_name = type_info.get("valueTypeName")
                                    containing_contract = type_info.get(
                                        "containingContract"
                                    )

                                    if containing_contract and type_name:
                                        qualified_name = (
                                            f"{containing_contract}.{type_name}"
                                        )
                                    elif type_name:
                                        # Use canonicalId to match _qualify_user_defined_type logic
                                        qualified_name = self._qualify_from_canonical_id(
                                            type_info, str(type_name), contract
                                        )

                                    # Get the base type
                                    value_type = type_info.get(
                                        "valueTypeAliasedName", {}
                                    )
                                    if value_type.get("type") == "Primitive":
                                        base_type = value_type.get("primitiveName")

                                # Handle UserDefinedStruct
                                elif type_info.get("type") == "UserDefinedStruct":
                                    type_name = type_info.get("structName")
                                    containing_contract = type_info.get(
                                        "containingContract"
                                    )

                                    if containing_contract and type_name:
                                        qualified_name = (
                                            f"{containing_contract}.{type_name}"
                                        )
                                    elif type_name:
                                        # Use canonicalId to match _qualify_user_defined_type logic
                                        qualified_name = self._qualify_from_canonical_id(
                                            type_info, str(type_name), contract
                                        )

                                    base_type = "struct"
                                    # Extract struct members
                                    struct_members = type_info.get("structMembers", [])

                                # Handle UserDefinedEnum
                                elif type_info.get("type") == "UserDefinedEnum":
                                    type_name = type_info.get("enumName")
                                    containing_contract = type_info.get(
                                        "containingContract"
                                    )

                                    if containing_contract and type_name:
                                        qualified_name = (
                                            f"{containing_contract}.{type_name}"
                                        )
                                    elif type_name:
                                        # Use canonicalId to match _qualify_user_defined_type logic
                                        qualified_name = self._qualify_from_canonical_id(
                                            type_info, str(type_name), contract
                                        )

                                    base_type = "uint8"
                                    # Extract enum members
                                    enum_members = type_info.get("enumMembers", [])

                                # Add to collection if we found a valid type
                                if type_name and qualified_name:
                                    user_type_info = {
                                        "typeName": type_name,
                                        "qualifiedName": qualified_name,
                                        "baseType": base_type,
                                        "typeCategory": type_info.get("type"),
                                        "containingContract": type_info.get(
                                            "containingContract"
                                        ),
                                        "main_contract": contract.get("name"),
                                    }

                                    # Add enum members for UserDefinedEnum
                                    if type_info.get("type") == "UserDefinedEnum":
                                        user_type_info["enumMembers"] = enum_members
                                    # Add struct members for UserDefinedStruct
                                    if type_info.get("type") == "UserDefinedStruct":
                                        user_type_info["structMembers"] = struct_members
                                    all_user_defined_types.append(user_type_info)

        # Write user-defined types to JSON file
        types_output_path = Path(".certora_internal/all_user_defined_types.json")
        types_output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(types_output_path, "w") as f:
            json.dump(all_user_defined_types, f, indent=2)

        # Also persist to the cache prefix for debuggability (visible on S3 in SaaS).
        # Compute-only intermediate: NOT part of cache validation/restore.
        atomic_write_json_fsspec(
            cache_path(DIR_CERTORA_INTERNAL, "all_user_defined_types.json"), all_user_defined_types
        )

        return len(all_user_defined_types)

    def generate_all_sources_json(self, build_data: Dict) -> None:
        """Extract and write all source file paths with their definitions to all_sources.json.

        The output structure is:
        {
            "path/to/file.sol": {
                "contracts": ["ContractA", "ContractB"],
                "libraries": ["LibraryX"]
            },
            ...
        }
        """
        # Collect all source file paths from srclist entries
        all_source_paths = set()

        # Iterate through all objects in the build data
        for key, obj in build_data.items():
            if isinstance(obj, dict) and "srclist" in obj:
                srclist = obj["srclist"]
                if isinstance(srclist, dict):
                    # srclist is a mapping from string to path - collect all paths
                    for source_key, source_path in srclist.items():
                        if isinstance(source_path, str):
                            all_source_paths.add(source_path)

        # Build dict mapping path -> {contracts: [...], libraries: [...]}
        all_sources_dict = {}

        for source_path in sorted(all_source_paths):
            try:
                # Extract contracts and libraries from the source file
                contracts = extract_definitions_from_solidity(source_path, definition_type='contract')
                libraries = extract_definitions_from_solidity(source_path, definition_type='library')

                all_sources_dict[source_path] = {
                    "contracts": contracts,
                    "libraries": libraries
                }
            except FileNotFoundError:
                self.log(f"Warning: Source file not found: {source_path}", "WARNING")
                # Store empty lists for missing files
                all_sources_dict[source_path] = {
                    "contracts": [],
                    "libraries": []
                }
            except Exception as e:
                self.log(f"Warning: Could not parse {source_path}: {e}", "WARNING")
                # Store empty lists for unparseable files
                all_sources_dict[source_path] = {
                    "contracts": [],
                    "libraries": []
                }

        # Write source files to JSON file. Keep the local write (same-run readers,
        # incl. PreAudit's non-fsspec s3_sync read a local path) AND persist a copy
        # to the cache prefix so a later cache hit can restore it locally — a hit
        # skips compute, so this is the only way the srclist survives cross-run.
        sources_output_path = Path(".certora_internal/all_sources.json")
        sources_output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(sources_output_path, "w") as f:
            json.dump(all_sources_dict, f, indent=2)

        atomic_write_json_fsspec(self.all_sources_cache_path(), all_sources_dict)

        # Count total definitions
        total_contracts = sum(len(defs["contracts"]) for defs in all_sources_dict.values())
        total_libraries = sum(len(defs["libraries"]) for defs in all_sources_dict.values())

        self.log(
            f"✓ Processed {len(all_sources_dict)} source files to {sources_output_path} "
            f"({total_contracts} contracts, {total_libraries} libraries)"
        )

    def generate_bytes_mappings_json(self, build_data: Dict) -> None:
        """Extract and write contracts with bytes mapping fields to bytes_mappings.json.

        The output structure is a list of objects:
        [
            {
                "contract_name": "MyContract",
                "source_file": "src/MyContract.sol",
                "bytes_mapping_fields": ["field1", "field2"]
            },
            ...
        ]
        """
        bytes_mappings_list = []

        # Iterate through all contracts in the build data
        for contract_data in build_data.values():
            for contract in contract_data.get('contracts', []):
                contract_name = contract.get('name')
                source_file = contract.get('file')

                if not contract_name or not source_file:
                    continue

                storage_layout = contract.get('storageLayout', {})
                bytes_mapping_fields = []

                # Check each storage field
                for storage_item in storage_layout.get('storage', []):
                    descriptor = storage_item.get('descriptor', {})

                    # Check if this is a mapping with bytes key
                    if descriptor.get('type') == 'Mapping':
                        key_type = descriptor.get('mappingKeyType', {})
                        if key_type.get('type') == 'PackedBytes':
                            field_name = storage_item.get('label', '')
                            if field_name:
                                bytes_mapping_fields.append(field_name)

                # Add to list if we found any bytes mapping fields
                if bytes_mapping_fields:
                    bytes_mappings_list.append({
                        "contract_name": contract_name,
                        "source_file": source_file,
                        "bytes_mapping_fields": bytes_mapping_fields
                    })

        # Write through fsspec so it lands on the cache prefix (S3 in SaaS, local in CLI)
        # and the autosetup cache-hit path can read it back. The sole same-run reader
        # (Autosetup._load_bytes_mappings) also reads via fsspec, so no local write is
        # needed — unlike all_sources, which a non-fsspec PreAudit consumer reads locally.
        output_path = self.bytes_mappings_cache_path()
        atomic_write_json_fsspec(output_path, bytes_mappings_list)

        if bytes_mappings_list:
            self.log(f"✓ Found {len(bytes_mappings_list)} contract(s) with bytes mappings, written to {output_path}")
        else:
            self.log(f"✓ No contracts with bytes mappings found")

    def generate_signature_database_json(self, build_json_path: Path) -> None:
        """Generate signature database JSON from the Certora build JSON."""
        self.log("Extracting function signatures and creating signature database...")

        try:
            # Initialize signature manager
            project_root = Path.cwd()
            signature_manager = SignatureManager(project_root)

            # Extract signatures from build JSON
            signatures = signature_manager.extract_signatures_from_build(
                build_json_path
            )

            # Create contract info objects from build data
            contract_infos = self._extract_contract_infos_from_build(build_json_path)

            # Find the latest AST file
            ast_file_path = self.getASTPath()

            # Extract inheritance info and abstract contracts from AST if available
            inheritance_info, abstract_contracts = self._extract_inheritance_and_abstract_from_ast(ast_file_path)
            if inheritance_info:
                self._merge_inheritance_info(contract_infos, inheritance_info)

            self.log(f"Detected abstract contracts: {abstract_contracts}")
            # Populate signature database
            self.log("🔄 Populating signature database with contract_infos and signatures...")
            signature_manager.populate_signature_database(contract_infos, signatures, abstract_contracts)

            # Dump signature database to JSON (same as autosetup)
            dump_path = signature_manager.dump_signature_database()
            self.log(f"✓ Generated signature database: {dump_path}")

        except Exception as e:
            self.log(f"Failed to generate signature database: {e}", "ERROR")
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            raise

    def _extract_inheritance_and_abstract_from_ast(self, ast_file_path: Optional[Path]) -> tuple[Dict[str, List[str]], set]:
        """Extract inheritance information and abstract contracts from AST dumps.

        Args:
            ast_file_path: Path to the .asts.json file, or None if not available

        Returns:
            Tuple of (inheritance_info, abstract_contracts) where:
            - inheritance_info: Dict mapping contract_name -> list of parent contracts
            - abstract_contracts: Set of contract names that are abstract or interfaces
        """
        inheritance_info = {}
        abstract_contracts = set()

        if not ast_file_path:
            self.log("No AST file provided - inheritance info will be missing", "WARNING")
            return inheritance_info, abstract_contracts

        try:
            with open(ast_file_path, "r", encoding="utf-8") as f:
                asts = json.load(f)

            self.log(f"Extracting inheritance info from {ast_file_path}")

            # Extract inheritance info from AST structure
            # Build ID to contract name mapping once
            id_to_name = {}
            for file_path, abs_path_dict in asts.items():
                for abs_path, nodes in abs_path_dict.items():
                    for node_id, node in nodes.items():
                        if node.get("nodeType") == "ContractDefinition":
                            contract_id = node.get("id")
                            contract_name = node.get("name")
                            if contract_id and contract_name:
                                id_to_name[contract_id] = contract_name

            # Now process contracts and resolve inheritance using the pre-built mapping
            for file_path, abs_path_dict in asts.items():
                for abs_path, nodes in abs_path_dict.items():
                    for node_id, node in nodes.items():
                        # Look for contract definitions to get linearizedBaseContracts
                        if node.get("nodeType") == "ContractDefinition":
                            contract_name = node.get("name")
                            if contract_name:
                                # Check if abstract or interface
                                is_abstract = node.get("abstract", False)
                                contract_kind = node.get("contractKind", "contract")

                                if is_abstract or contract_kind == "interface":
                                    abstract_contracts.add(contract_name)
                                    self.log(f"Identified {'abstract' if is_abstract else 'interface'}: {contract_name}", "DEBUG")

                                # Get linearized base contracts (includes self + all inherited contracts)
                                linearized = node.get("linearizedBaseContracts", [])
                                if len(linearized) > 1:  # More than just self
                                    # Convert IDs to contract names using pre-built mapping
                                    base_contracts = [id_to_name[contract_id] for contract_id in linearized[1:] if contract_id in id_to_name]
                                    if base_contracts:
                                        inheritance_info[contract_name] = base_contracts

            self.log(f"Extracted inheritance for {len(inheritance_info)} contracts", "DEBUG")
            self.log(f"Found {len(abstract_contracts)} abstract/interface contracts to skip", "INFO")
            return inheritance_info, abstract_contracts

        except Exception as e:
            self.log(f"Failed to extract inheritance from AST: {e}", "WARNING")
            return inheritance_info, abstract_contracts

    def _merge_inheritance_info(self, contract_infos: List[ContractInfo], inheritance_info: Dict[str, List[str]]) -> None:
        """Merge inheritance information into ContractInfo objects in place."""
        for contract_info in contract_infos:
            if contract_info.name in inheritance_info:
                contract_info.inherits_from = (contract_info.inherits_from or []) + inheritance_info[contract_info.name]

    def _extract_contract_infos_from_build(
        self, build_json_path: Path
    ) -> List[ContractInfo]:
        """Extract contract information from build JSON to create ContractInfo objects."""
        contract_infos = []

        try:
            with open(build_json_path, "r") as f:
                build_data = json.load(f)

            seen_contracts = set()

            # Discover contracts and create ContractInfo objects
            for contract_key, contract_data in build_data.items():
                if (
                    not isinstance(contract_data, dict)
                    or "contracts" not in contract_data
                ):
                    continue

                for contract in contract_data.get("contracts", []):
                    if not isinstance(contract, dict):
                        continue

                    methods = contract.get("methods", [])
                    if not methods:
                        continue

                    contract_name = contract.get("name", "Unknown")

                    # Skip if already processed
                    if contract_name in seen_contracts:
                        continue
                    seen_contracts.add(contract_name)

                    # Get source file directly from contract object (canonical source)
                    source_file_str = contract.get("original_file") or contract.get("file")

                    # Fall back to method inspection only if contract-level fields are missing
                    if not source_file_str:
                        fallback_source = None
                        for method in methods:
                            original_file = method.get("originalFile")
                            if original_file:
                                fallback_source = original_file

                            # Prefer file that matches contract name (where contract is actually defined)
                            if original_file.endswith(f"/{contract_name}.sol") or original_file.endswith(f"\\{contract_name}.sol"):
                                source_file_str = original_file
                                break
                        if not source_file_str and fallback_source:
                            source_file_str = fallback_source

                    # Final fallback
                    if not source_file_str:
                        source_file_str = "unknown.sol"

                    # Determine contract kind (basic heuristic)
                    is_library = any(
                        method.get("isLibrary", False) for method in methods
                    )
                    kind = ContractKind.LIBRARY if is_library else ContractKind.CONTRACT

                    # Extract constructor params
                    ctor_params = None
                    for method in methods:
                        if method.get("name", "") == "constructor":
                            params = []
                            for arg, param_name in zip(
                                method.get("fullArgs", []), method.get("paramNames", [])
                            ):
                                type_desc = arg.get("typeDesc", {})
                                sol_type = parse_type_descriptor(type_desc, TypeParseMode.QUALIFIED)
                                location = arg.get("location", "")
                                if location in ("memory", "calldata", "storage"):
                                    sol_type = f"{sol_type} {location}"
                                params.append((sol_type, param_name))
                            if params:
                                ctor_params = params
                            break

                    # Create contract info with inheritance
                    contract_info = ContractInfo(
                        name=contract_name,
                        kind=kind,
                        source_file=Path(source_file_str),
                        inherits_from=[],  # added later via _extract_inheritance_from_ast()
                        artifact_path=build_json_path,
                        constructor_params=ctor_params,
                    )

                    contract_infos.append(contract_info)

            return contract_infos

        except Exception as e:
            self.log(f"Error extracting contract infos: {e}", "ERROR")
            return []

    def generate_ast_graph(self, ast_path: Path) -> None:
        """
        Generate a parent graph from the AST for efficient node parent lookups.

        Creates a mapping from node_id to parent_node_id for all nodes in the AST.
        This allows O(1) lookup of a node's parent, which is useful for filtering
        operations like detecting chained member accesses.

        Args:
            ast_path: Path to the .asts.json file

        Output:
            Writes to .certora_internal/.ast_graph.json with structure:
            {
                "relative_path": {
                    "absolute_path": {
                        "node_id": "parent_node_id"
                    }
                }
            }
        """
        self.log("Building AST parent graph...")

        try:
            with open(ast_path, 'r') as f:
                asts_data = json.load(f)

            # Build parent graph: node_id -> parent_node_id
            parent_graph = {}

            # Structure: dict[relative_path: dict[absolute_path: dict[node_id: node_data]]]
            for relative_path, path_data in asts_data.items():
                parent_graph[relative_path] = {}

                for absolute_path, nodes in path_data.items():
                    parent_graph[relative_path][absolute_path] = {}

                    # For each node, find all child node IDs and map them to this parent
                    for node_id, node in nodes.items():
                        if not isinstance(node, dict):
                            continue

                        # Find all child node IDs referenced in this node
                        child_ids = self._extract_child_node_ids(node)
                        for child_id in child_ids:
                            parent_graph[relative_path][absolute_path][str(child_id)] = str(node_id)

            # Write parent graph to JSON
            graph_path = self.getASTParentGraphPath()
            with open(graph_path, 'w') as f:
                json.dump(parent_graph, f, indent=2)

            self.log(f"✓ AST parent graph saved to {graph_path}")

        except Exception as e:
            self.log(f"Warning: Failed to generate AST parent graph: {e}", "WARNING")
            self.log(f"Traceback: {traceback.format_exc()}", "WARNING")

    def _extract_child_node_ids(self, node: Any) -> List[int]:
        """
        Extract all child node IDs from an AST node.

        Args:
            node: AST node (dict or other type)

        Returns:
            List of child node IDs
        """
        child_ids = []

        if isinstance(node, dict):
            for key, value in node.items():
                # Look for 'id' fields in nested structures
                if isinstance(value, dict) and 'id' in value:
                    child_ids.append(value['id'])
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and 'id' in item:
                            child_ids.append(item['id'])

        return child_ids

    def getASTPath(self) -> Path:
        return Path(".certora_internal/all_asts.json")

    def getBytesMappingsPath(self) -> Path:
        return Path(".certora_internal/bytes_mappings.json")

    def getAllSourcesPath(self) -> Path:
        return Path(".certora_internal/all_sources.json")

    @staticmethod
    def bytes_mappings_cache_path() -> str:
        """Canonical fsspec cache path for bytes_mappings.json.

        Single source of truth shared by the writer (``generate_bytes_mappings_json``)
        and the cache-hit reader (``Autosetup._load_bytes_mappings``). Resolves to a
        local path in CLI mode and to ``s3://…`` under the SaaS cache prefix, so the
        file persisted on a first run is found when the autosetup cache hits."""
        return cache_path(DIR_CERTORA_INTERNAL, "bytes_mappings.json")

    @staticmethod
    def all_sources_cache_path() -> str:
        """Canonical fsspec cache path for all_sources.json (the compiler srclist).

        Persisted here on compute so a cache hit can restore it to local disk for
        the downstream non-fsspec readers (PreAudit's s3_sync source upload + report
        viewer), which a hit would otherwise leave without sources."""
        return cache_path(DIR_CERTORA_INTERNAL, "all_sources.json")

    def getASTParentGraphPath(self) -> Path:
        return Path(".certora_internal/all_ast_parent_graph.json")

    def process_certora_build_json(self) -> bool:
        """Process .certora_build.json to extract method information."""
        build_json_path = Path(".certora_internal/latest/.certora_build.json")
        self._build_dir = build_json_path.parent

        self.log(f"Processing build json: {build_json_path.resolve()}")

        if not build_json_path.exists():
            self.log(f"Build JSON not found at: {build_json_path}", "ERROR")
            return False

        try:
            with open(build_json_path, "r") as f:
                build_data = json.load(f)

            # Generate all_methods.json
            self.generate_all_methods_json(build_data)

            # Generate all_user_defined_types.json
            self.generate_all_user_defined_types_json(build_data)

            # Generate all_sources.json
            self.generate_all_sources_json(build_data)

            # Copy .asts.json from build directory to .certora_internal
            asts_source = self._build_dir / FILE_BUILD_ASTS
            asts_target = self.getASTPath()
            if not asts_source.exists():
                raise CompilationAnalysisError(
                    f".asts.json not found at {asts_source}. "
                    f"The Certora build did not produce the expected AST file."
                )
            try:
                shutil.copy2(asts_source, asts_target)
                self.log(f"Copied AST file from {asts_source} to {asts_target}")
            except Exception as e:
                raise CompilationAnalysisError(
                    f"Failed to copy .asts.json from {asts_source} to {asts_target}: {e}"
                )

            self.generate_ast_graph(asts_target)

            # Generate signature database (uses the ast file copied before)
            self.generate_signature_database_json(build_json_path)

            # Generate bytes mappings JSON
            self.generate_bytes_mappings_json(build_data)

            return True

        except Exception as e:
            self.log(f"Error processing .certora_build.json: {e}", "ERROR")
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return False

    def run_setup_summaries(self, contract_files: List[str], main_contract: str) -> bool:
        """
        Run setup_summaries to detect and configure library summaries. On success,
        the constructed ``SummarySetup`` is stored on ``self.summary_setup`` so
        that call resolution can reuse its ``analyze_contract`` entry point and
        dedup state for the lazy-LLM-on-add path.
        """
        self.log("Running setup_summaries to configure library summaries...")

        try:
            from setup.setup_summaries import SummarySetup  # type: ignore[import-not-found]

            inheritance_graph = self.scope.signature_database.build_inheritance_graph()
            setup = SummarySetup(verbose=max(1, self.verbose), inheritance_graph=inheritance_graph)

            with ledger_component("summaries"):
                success = setup.run(
                    main_contract=main_contract,
                    contract_files=contract_files,
                    additional_contracts=self.additional_contracts,
                    include_test_files=False,
                    include_dependencies=True,
                    enable_llm=not self.skip_llm,
                    custom_recipe=None,
                )

            if success:
                self.log("✓ Setup summaries completed successfully")
                self.summary_setup = setup
                return True
            self.log("✗ Setup summaries failed", "ERROR")
            return False

        except Exception as e:
            self.log(f"✗ Setup summaries failed with exception: {e}", "ERROR")
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return False

    def setup_prover(
        self, contract_handles: List[ContractHandle], main_contract_handle: ContractHandle
    ) -> Tuple[Path, Dict[str, Any], bool, List[ContractHandle]]:
        """Run compilation analysis and setup summaries for the prover.

        Returns:
            Tuple[Path, Dict[str, Any], bool, List[ContractHandle]]:
            - summary_spec_path: Path to the main contract's base summary aggregator spec.
            - updated_config_dict: Configuration dictionary with all updates applied during compilation.
            - import_patcher_applied: True if import patcher was successfully applied.
            - surviving_contracts: scene after dedup + workarounds, for the caller to propagate.
        """
        main_contract_name = main_contract_handle.contract_name

        # Run compilation-only step to extract method information
        # We run the compilation analysis for all the contracts (i.e. for
        # contract_files, not just for main_contract_files) because we need to
        # have all the contracts in the signature database (which is then for
        # instance consumed by the dispatcher call resolution logic).
        success, updated_config_dict, import_patcher_applied, surviving_contracts = self.run_compilation_analysis(
            contract_handles, main_contract_name
        )
        if not success:
            raise CompilationAnalysisError("Compilation analysis failed")

        # Store the configuration updates
        self.compilation_config_updates = updated_config_dict
        self.import_patcher_applied = import_patcher_applied

        # Detect and apply code access patches
        self.code_access_patches_applied = detect_and_apply_code_access_patches(
            self.log, self.getASTPath(), self.getASTParentGraphPath(), self.scope
        )

        # Run setup_summaries to detect and configure library summaries.
        # We pass all contract files (not just the main contract's file) because the LLM
        # recipe analysis scans only the files passed directly. If contract A imports and
        # uses B, and B uses mulDiv from PRBMath, scanning just A would miss the mulDiv
        # call from B. Including all contract files ensures we catch transitive usage.
        success_summaries = self.run_setup_summaries(
            [ch.source_file for ch in surviving_contracts], main_contract_name
        )
        if not success_summaries:
            raise SummarySetupError("Setup summaries generation failed")

        # Stop here if requested for debugging summaries
        if self.stop_after_summaries:
            self.log(
                "🛑 Stopping after summaries generation as requested (--stop-after-summaries)"
            )
            self.log("✅ Function summaries generation completed successfully")
            self.log(f"📁 Generated summaries are available in: {self.certora_dir}")

            all_methods = self.certora_dir / "all_methods.json"
            if all_methods.exists():
                self.log(f"📄 all_methods.json: {all_methods}")

            all_types = self.certora_dir / "all_user_defined_types.json"
            if all_types.exists():
                self.log(f"📄 all_user_defined_types.json: {all_types}")

            # Exit cleanly - summaries generation completed successfully
            sys.exit(0)

        # Run ERC-7201 annotation patching (adds missing annotations to source files)
        self.log("=== ERC-7201 ANNOTATION PATCHING PHASE ===")
        self.run_setup_erc7201_patch()

        # Run setup_erc7201.py
        self.log("=== ERC-7201 DETECTION PHASE ===")
        self.run_setup_erc7201()  # todo handle this within summaries.spec

        # Propagate ERC-7201 result into the config dict so that configs created
        # later (e.g. base-{Contract}.conf) also get the flag.
        if self.erc7201_namespaces_found:
            updated_config_dict["storage_extension_annotation"] = True

        aggregator_path = (
            self.certora_dir / SUMMARIES_SUBDIR / f"{main_contract_name}_base_summaries.spec"
        )
        return aggregator_path, self.compilation_config_updates, self.import_patcher_applied, surviving_contracts
