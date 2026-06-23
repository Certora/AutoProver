"""
Autosetup — run the setup phase of the PreAudit pipeline.

This class owns the autosetup portion of the orchestration:
- Build system detection and configuration
- Compilation analysis via SetupProver
- Signature database loading
- Bytes mappings loading
- Sanity spec generation
- Base config creation and warmup
- Test run spec creation

"""

import asyncio
import functools
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from certora_autosetup.autosetup.types import AutosetupConfig, AutosetupResult
from certora_autosetup.build_systems.base import BuildSystemConfig
from certora_autosetup.build_systems.manager import BuildSystemManager
from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.parsers.method_parser import MethodParser
from certora_autosetup.setup.call_resolution import CallResolutionPhase
from certora_autosetup.setup.sanity import SanityFailureResult
from certora_autosetup.setup.setup_prover import CompilationAnalysisError, SummarySetupError
from certora_autosetup.setup.signature_manager import SignatureManager
from certora_autosetup.setup.solidity_utils import (
    build_library_name_index,
    find_all_library_files_and_names,
    find_libraries_used_by,
)
from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils import logger
from certora_autosetup.utils.constants import (
    CERTORA_REPORTS_DIR,
    DIR_CERTORA_INTERNAL,
    FILE_AUTOSETUP_RESULT,
)
from certora_autosetup.utils.constants import DIR_INTERNAL_CONFS
from certora_autosetup.utils.paths import (
    internal_test_compilation_conf,
    internal_warmup_spec,
    user_conf_path,
    user_sanity_spec_path,
)
from certora_autosetup.utils.contract_utils import split_contract_spec
from certora_autosetup.utils.enhanced_config_manager import ConfigManager, FileContent, ProverJobSpec
from certora_autosetup.utils.llm_util import get_ledger_rows
from certora_autosetup.utils.scope import Scope
from certora_autosetup.utils.types import ContractHandle, ContractInfo

COMPONENT = "Autosetup"


class Autosetup:
    """Execute the autosetup phase and return an AutosetupResult.

    This class is fully standalone — it owns all setup state and logic.
    """

    TEST_RUN_PROVER_ARGS = {
        "verifyCache": "",
        "verifyTACDumps": "",
        "testMode": "",
        "checkRuleDigest": "",
        "callTraceHardFail": "on",
    }

    def __init__(
        self,
        config: AutosetupConfig,
        setup_prover,
        prover_runner,
        config_manager: ConfigManager,
        scope: Scope,
        signature_manager: SignatureManager,
        rule_generator,
        contract_handles: List[ContractHandle],
    ):
        """Initialize with config and injected dependencies.

        Args:
            config: AutosetupConfig with all configuration values.
            setup_prover: SetupProver instance for compilation analysis.
            prover_runner: ProverRunner instance (cloud or local).
            config_manager: ConfigManager for creating/updating configs.
            scope: Scope instance for signature database management.
            signature_manager: SignatureManager for loading signature databases.
            rule_generator: RuleGenerator for sanity spec generation.
            contract_handles: List of all detected contract handles.
        """
        self.config = config
        self.setup_prover = setup_prover
        self.prover_runner = prover_runner
        self.config_manager = config_manager
        self.scope = scope
        self.signature_manager = signature_manager
        self.rule_generator = rule_generator
        self.contract_handles = contract_handles

        # Mutable state populated during run()
        self.build_system: Optional[BuildSystem] = None
        self.build_system_config: Optional[BuildSystemConfig] = None
        self.compilation_config_updates: Dict[str, Any] = {}
        self.import_patcher_applied: bool = False
        self._sanity_advanced_analysis: Dict[str, Dict[str, SanityFailureResult]] = {}
        self._test_run_specs: List[ProverJobSpec[Any]] = []
        self.bytes_mappings: List[tuple[ContractHandle, List[str]]] = []
        # Set during run()
        self.main_contract_handle: Optional[ContractHandle] = None

    def log(self, message: str, level: str = "INFO"):
        logger.log(message, level, COMPONENT)

    @functools.cached_property
    def _library_files(self) -> Dict[str, List[str]]:
        """Map of library source-file paths to the library names defined inside them.

        Stable for the lifetime of this Autosetup run; used by scene reduction
        and call resolution to add only the libraries actually called from each
        compilation unit, instead of every library file in the project.
        """
        return find_all_library_files_and_names(
            include_test_files=False, include_dependencies=True, log_func=self.log
        )

    @functools.cached_property
    def _library_name_to_file(self) -> Dict[str, str]:
        """Inverted ``library_name → defining_file`` map, deduped first-definition-wins.

        Computed once per run (which means the "library defined in multiple files"
        warnings are emitted exactly once per duplicate name, not once per contract
        × per library-resolution call). Consumed by ``find_libraries_used_by``.
        """
        return build_library_name_index(self._library_files)

    @functools.cached_property
    def _all_methods(self) -> List[Dict[str, Any]]:
        """Parsed ``.certora_internal/all_methods.json``.

        Precondition: ``setup_prover.setup_prover()`` must have completed (it's the step
        that produces ``all_methods.json``). Accessing this property before that point is
        a programming error — the file's absence means our compilation analysis hasn't
        run, so scene reduction / call resolution have nothing valid to reason about.
        """
        methods_file = Path(".certora_internal/all_methods.json")
        if not methods_file.exists():
            raise RuntimeError(
                f"Required {methods_file} is missing. "
                "Autosetup._all_methods accessed before setup_prover ran — fix the "
                "ordering: compilation analysis must precede scene reduction / call resolution."
            )
        return MethodParser(str(methods_file)).get_all_methods()

    def libraries_for_contracts(self, contract_names: List[str]) -> List[ContractHandle]:
        """Return library ContractHandles that (a) are referenced from any of the given
        contracts' compilation units and (b) have at least one summary attached
        (curated or LLM-generated).

        Adding libraries to the prover's scene is only useful if *something* in the
        spec actually mentions them. Pulling in every referenced library would bloat
        the scene with libraries whose code is inlined and never named anywhere CVL
        cares about. Deduped across the input list on the full ContractHandle so two
        libraries defined in the same .sol file each get an entry (their config
        strings differ: ``Foo.sol:LibA`` vs ``Foo.sol:LibB``).
        """
        summarized = self._summarized_library_names
        seen: set[ContractHandle] = set()
        result: List[ContractHandle] = []
        for name in contract_names:
            for handle in find_libraries_used_by(name, self._library_name_to_file, self._all_methods):
                if handle.contract_name not in summarized:
                    continue
                if handle in seen:
                    continue
                seen.add(handle)
                result.append(handle)
        return result

    @functools.cached_property
    def _library_names(self) -> set[str]:
        """Set of all Solidity ``library`` names defined in the project (incl. deps).
        Used to exclude libraries from analyses that only apply to regular contracts
        (e.g. proxy detection — libraries are inlined and can't be proxies)."""
        names: set[str] = set()
        for libs in self._library_files.values():
            names.update(libs)
        return names

    def is_library(self, contract_name: str) -> bool:
        return contract_name in self._library_names

    @property
    def _summarized_library_names(self) -> set[str]:
        """Library names that have a summary attached — curated (matched in
        ``function_summaries.json``) or LLM-generated. Recomputed on each access since
        ``_methods_per_contract`` grows during call resolution. Empty if setup_prover
        hasn't run yet."""
        setup = self.setup_prover.summary_setup
        if setup is None:
            return set()
        # Curated: matched_functions are keys into function_summaries.json, each of
        # which may declare a ``library_names`` list.
        curated = {
            name
            for key in setup.matched_functions
            for name in setup.function_summaries[key].get("library_names", [])
        }
        # LLM-generated: any contract that landed in _methods_per_contract whose
        # name we also know to be a library.
        llm = set(setup._methods_per_contract.keys()) & self._library_names
        return curated | llm

    def _build_job_msg(self, contract_name: str, conf_file: Path) -> str:
        """Build the msg string for a prover job."""
        return ProverJobSpec.build_job_msg(self.config.orchestration_timestamp, contract_name, conf_file)

    def run(self, main_contract_handle: ContractHandle, skip_warmup: bool = False) -> AutosetupResult:
        """Execute the full autosetup pipeline.

        Args:
            main_contract_handle: The main contract to verify.
            skip_warmup: If True, skip the warmup phase.

        Returns:
            AutosetupResult with all setup artifacts and metadata.

        Raises:
            CompilationAnalysisError: If compilation analysis fails.
            SummarySetupError: If summary setup fails.
        """
        self.main_contract_handle = main_contract_handle

        # Step 1: Setup build system config (must run before cache check so solc version is in cache key)
        self.setup_build_system_config()
        self.setup_prover.build_system = self.build_system
        self.setup_prover.build_system_config = self.build_system_config

        # Check full-pipeline cache before running
        result_path = Path(DIR_CERTORA_INTERNAL) / FILE_AUTOSETUP_RESULT
        cached_result = self._check_cache(result_path)
        if cached_result is not None and not self.config.composer_output:
            self.log("Source files unchanged - using cached autosetup result")
            # Still need to load signature database and bytes mappings from cached artifacts
            self._load_signature_database_to_scope()
            self._load_bytes_mappings()
            # A hit skips compute, so all_sources.json was not regenerated this run.
            # Restore it to local disk for the downstream non-fsspec readers (PreAudit's
            # s3_sync source upload + report viewer).
            self._restore_all_sources_to_local()
            return cached_result

        # Step 2: Run setup_prover (compilation analysis, LLM summarization, sig DB)
        summary_spec_path, updated_config_dict, import_patcher_applied, self.contract_handles = (
            self.setup_prover.setup_prover(self.contract_handles, main_contract_handle)
        )
        main_contract_name = main_contract_handle.contract_name

        # Step 3: Load signature database
        self._load_signature_database_to_scope()

        # Step 4: Store compilation config updates
        self.compilation_config_updates = updated_config_dict
        self.config_manager.reference_compiler_maps = updated_config_dict
        self.import_patcher_applied = import_patcher_applied

        # Step 5: Generate the user-facing sanity spec at certora/specs/sanity-{C}.spec.
        # It directly imports the summary aggregator, call resolution, and (when present) erc7201.
        # Must run after ERC-7201 detection (in setup_prover) so the erc7201 import is included.
        self.rule_generator.generate_sanity_specs({main_contract_name: summary_spec_path})

        # Step 6: Load bytes mappings
        self._load_bytes_mappings()

        # Step 7: Base config creation and warmup. The base conf lives at
        # certora/confs/{C}.conf and accumulates all setup mutations in place.
        # The warmup runs against a separate copy under .certora_internal/.
        effective_skip_warmup = skip_warmup or (self.config.composer_output is not None)
        warmup_success = self.generate_base_and_run_warmup(effective_skip_warmup)
        if not warmup_success:
            raise RuntimeError("Cache warmup failed - stopping orchestration")

        user_conf = self.base_config_path(main_contract_name)

        # Build the result. ``summary_specs`` is kept as a single-entry dict for
        # back-compat with downstream consumers (e.g. PreAudit's workflow indexes by
        # contract name).
        sig_db_path = self.signature_manager.get_signature_db_path()
        asts_path = self.setup_prover.getASTPath()
        bytes_path = self.setup_prover.getBytesMappingsPath()

        # sig DB + bytes_mappings are persisted via fsspec (S3 in SaaS), so probe
        # existence through get_fs() — a local Path.exists() would be False in the
        # cloud container and wrongly record None, breaking the next run's cache
        # validation. ASTs stay local (compute-only; excluded from cache validation).
        fs = get_fs()
        sig_db_exists = fs.exists(self.signature_manager.signature_db_cache_path())
        bytes_exists = fs.exists(self.setup_prover.bytes_mappings_cache_path())
        all_sources_exists = fs.exists(self.setup_prover.all_sources_cache_path())

        result = AutosetupResult(
            base_configs={main_contract_name: user_conf},
            summary_specs={main_contract_name: summary_spec_path},
            signature_database_path=sig_db_path if sig_db_exists else None,
            asts_path=asts_path if asts_path.exists() else None,
            bytes_mappings_path=bytes_path if bytes_exists else None,
            all_sources_path=self.setup_prover.getAllSourcesPath() if all_sources_exists else None,
            import_patcher_applied=import_patcher_applied,
            compilation_config_updates=updated_config_dict,
            sanity_analysis=dict(self._sanity_advanced_analysis),
            bytes_mappings=list(self.bytes_mappings),
            test_run_specs=list(self._test_run_specs),
            build_system_config_dict=self.get_build_system_config_dict_with_updates(),
            orchestration_timestamp=self.config.orchestration_timestamp,
            # Every LLM call this process made (summaries, proxy detection,
            # ERC-7201, call resolution), captured at the transport-layer ledger.
            llm_usage=get_ledger_rows(),
        )

        # Handle composer_output mode
        if self.config.composer_output:
            result.composer_output = {
                "contract_to_summary": {main_contract_name: str(summary_spec_path)},
                "contract_to_config": {main_contract_name: str(user_conf)},
            }

        # Persist the result for caching
        result.save(result_path, self.config.project_root)
        self._save_cache(result)
        self.log(f"AutosetupResult saved to {result_path}")

        return result

    # -------------------------------------------------------------------------
    # Build system configuration
    # -------------------------------------------------------------------------

    def setup_build_system_config(self):
        """
        Auto-detect and setup build system configuration (Foundry or Hardhat).

        This method:
        1. Auto-detects build system or uses explicit build system from self.config.requested_build_system
        2. Creates appropriate manager (FoundryManager or HardhatManager)
        3. Calls auto_detect_config() to find and parse config file
        4. Stores the config in self.build_system_config (polymorphic)
        """
        try:
            # Auto-detect or use explicit build system from init parameter
            detected = BuildSystemDetector.resolve(Path.cwd(), self.config.requested_build_system)
            if self.config.requested_build_system is None or self.config.requested_build_system == 'auto':
                self.log(f"Auto-detected build system: {detected.value}")
            else:
                self.log(f"Using explicit build system: {detected.value}")

            if detected == BuildSystem.UNKNOWN:
                self.log("No build system detected, continuing without build config", "WARNING")
                self.build_system = BuildSystem.UNKNOWN
                self.build_system_config = None
                return

            self.build_system = detected

            # Create a minimal scope that accepts all files
            class MinimalScope:
                def is_file_in_scope(self, file_path):
                    return True

            project_root = Path.cwd()
            scope = MinimalScope()

            # Get appropriate manager class and create instance
            ManagerClass = BuildSystemDetector.get_manager_class(detected)
            manager: BuildSystemManager = ManagerClass(project_root, scope)  # type: ignore

            # Auto-detect and parse config (polymorphic - returns FoundryConfig or HardhatConfig)
            self.log(f"Auto-detecting {detected.value} configuration...")
            self.build_system_config = manager.auto_detect_config(profile=self.config.requested_profile)

            # Log common info
            self.log(f"Solc version: {self.build_system_config.solc_version}")
            self.log(
                f"Optimizer: {self.build_system_config.optimizer} (runs: {self.build_system_config.optimizer_runs})"
            )
            if self.build_system_config.via_ir:
                self.log("Via IR: enabled")

        except Exception as e:
            self.log(f"Failed to setup build system configuration: {e}", "WARNING")
            self.log("Continuing without build system configuration", "WARNING")
            self.build_system = BuildSystem.UNKNOWN if 'BuildSystem' in dir() else None
            self.build_system_config = None

    def get_build_system_config_dict(self) -> Dict[str, Any]:
        """
        Get Certora-compatible configuration dictionary from detected build system.

        Returns:
            Dictionary with Certora config settings derived from Foundry or Hardhat config,
            or empty dict if no build system config is available.
        """
        if self.build_system_config:
            # Use polymorphic to_certora_dict() - eliminates branching!
            return self.build_system_config.to_certora_dict(
                convert_solc_to_certora_format=True,
                include_packages=self.config.include_foundry_packages
            )

        return {}

    def get_build_system_config_dict_with_updates(self) -> Dict[str, Any]:
        """
        Get Certora-compatible configuration dictionary from build system config,
        merged with any updates from compilation analysis.

        Returns:
            Dictionary with Certora config settings, including any updates
            applied during compilation (e.g., from import patcher or other
            compilation techniques).
        """
        # Start with base build system config
        config = self.get_build_system_config_dict()

        # Merge in compilation updates (which already started from build system config)
        # This ensures any modifications made during compilation are preserved
        if self.compilation_config_updates:
            config.update(self.compilation_config_updates)

        # solc_via_ir and solc_via_ir_map cannot coexist; the map supersedes the global flag
        if "solc_via_ir_map" in config:
            config.pop("solc_via_ir", None)

        # Same for solc_optimize and solc_optimize_map
        if "solc_optimize_map" in config:
            config.pop("solc_optimize", None)

        # Filter per-contract maps to only include contracts in self.contract_handles
        contract_names = {ch.contract_name for ch in self.contract_handles}
        for map_key in ("compiler_map", "solc_via_ir_map", "solc_evm_version_map", "solc_optimize_map"):
            if map_key in config:
                config[map_key] = {k: v for k, v in config[map_key].items() if k in contract_names}

        return config

    # -------------------------------------------------------------------------
    # Signature database and bytes mappings
    # -------------------------------------------------------------------------

    def _load_signature_database_to_scope(self):
        """Universal function to load signature database from JSON to scope."""
        # Use the fsspec cache path (matches dump_signature_database's writer) and
        # check existence via get_fs() so the cache-hit path finds the DB that was
        # persisted to S3 in SaaS mode — not just a local copy that never exists there.
        signature_db_path = self.signature_manager.signature_db_cache_path()
        if get_fs().exists(signature_db_path):
            self.signature_manager.load_from_json(signature_db_path)
            loaded_database = self.signature_manager.get_signature_database()

            # Extend existing signature database instead of replacing it
            for contract_name, contract_info in loaded_database.get_all_contracts().items():
                self.scope.signature_database.add_contract(contract_info)

            for selector, signature in loaded_database.get_all_signatures().items():
                implementing_contracts = loaded_database.get_implementing_contracts(selector)
                for contract in implementing_contracts:
                    self.scope.signature_database.add_signature(signature, contract)

            self.log(f"✓ Extended signature database with data from: {signature_db_path}", level="DEBUG")
        elif self.config.verbose >= 2:
            self.log("ℹ️ No signature database found, keeping existing database")

    def _load_bytes_mappings(self) -> None:
        """Load bytes mappings from the persistent JSON file generated during compilation analysis."""
        # fsspec cache path (matches generate_bytes_mappings_json's writer) so the
        # cache-hit path reads the copy persisted to S3 in SaaS mode, not a local
        # file that only exists in the run that produced it.
        bytes_mappings_file = self.setup_prover.bytes_mappings_cache_path()
        fs = get_fs()

        if not fs.exists(bytes_mappings_file):
            self.log("bytes_mappings.json not found - no bytes mappings detected", "DEBUG")
            self.bytes_mappings = []
            return

        try:
            with fs.open(bytes_mappings_file, 'r') as f:
                data = json.load(f)

            # Convert JSON format to ContractHandle tuples
            self.bytes_mappings = []
            for entry in data:
                contract_handle = ContractHandle(
                    contract_name=entry["contract_name"],
                    source_file=entry["source_file"]
                )
                self.bytes_mappings.append((contract_handle, entry["bytes_mapping_fields"]))

            if self.bytes_mappings:
                self.log(f"Loaded {len(self.bytes_mappings)} contract(s) with bytes mappings from cache")

        except Exception as e:
            self.log(f"Warning: Failed to load bytes_mappings.json: {e}", "WARNING")
            self.bytes_mappings = []

    def _restore_all_sources_to_local(self) -> None:
        """On a cache hit, copy all_sources.json from the cache prefix to local disk.

        Compute (which writes it locally) is skipped on a hit, so without this the
        downstream non-fsspec readers — PreAudit's s3_sync source upload + report
        viewer — would find no srclist. Write to the local path they read from.
        """
        fs = get_fs()
        src = self.setup_prover.all_sources_cache_path()
        dst = self.config.project_root / self.setup_prover.getAllSourcesPath()
        if dst.exists() or not fs.exists(src):
            return
        try:
            with fs.open(src, "r") as f:
                data = f.read()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(data)
            self.log(f"✓ Restored all_sources.json to {dst} from cache", level="DEBUG")
        except Exception as e:
            self.log(f"Warning: Failed to restore all_sources.json from cache: {e}", "WARNING")

    # -------------------------------------------------------------------------
    # Dummy ERC20 generation
    # -------------------------------------------------------------------------

    def generate_dummy_erc20_files(self):
        """Add the base DummyERC20Impl contract to the scene.

        The linker's wildcard harness mechanism will create numbered instances
        (DummyERC20Impl_1, DummyERC20Impl_2) automatically when wildcard storage
        paths resolve to DummyERC20Impl.
        """
        if not self.config.dummy_erc20:
            return

        # Create mocks subdirectory
        mocks_dir = self.config.certora_dir / "mocks"
        mocks_dir.mkdir(exist_ok=True)

        # Copy DummyERC20Impl.sol to mocks directory
        impl_source = self.config.script_dir / "setup" / "mocks" / "DummyERC20Impl.sol"
        if not impl_source.exists():
            raise FileNotFoundError(f"DummyERC20Impl.sol not found at {impl_source}")

        impl_dest = mocks_dir / "DummyERC20Impl.sol"
        shutil.copy2(impl_source, impl_dest)
        self.contract_handles.append(ContractHandle.from_filepath(str(impl_dest)))
        self.log("Copied DummyERC20Impl.sol to mocks/")

        # Add DummyERC20Impl to signature database
        impl_contract_info = ContractInfo(
            name="DummyERC20Impl",
            source_file=impl_dest,
        )
        self.scope.add_contract(impl_contract_info)
        self.log("Added DummyERC20Impl to signature database")

    # -------------------------------------------------------------------------
    # Config creation and warmup
    # -------------------------------------------------------------------------

    def base_config_path(self, main_contract: str) -> Path:
        """The single user-facing conf at certora/confs/{C}.conf.

        Mutated in place throughout setup — typechecker fixes, call-resolution
        linker entries, and sanity-tuning values are all desirable user-visible
        state. The warmup job runs against a copy in .certora_internal/confs/
        so the user-facing conf's verify field never points at the warmup spec.
        """
        return user_conf_path(self.config.project_root, main_contract)

    def create_warmup_spec(self, contract_name: str) -> Path:
        """Create a minimal warmup spec for a contract.

        Copies certora/trivial.spec and injects an import of the user-facing sanity spec,
        which transitively pulls in the summary aggregator, call resolution, and erc7201 (when present).
        Warming up against the same specs the user will run gives a correct cache.
        """
        trivial_spec = Path(__file__).parent.parent / "certora" / "trivial.spec"
        warmup_spec = internal_warmup_spec(self.config.project_root, contract_name)
        warmup_spec.parent.mkdir(parents=True, exist_ok=True)
        sanity_spec = user_sanity_spec_path(self.config.project_root, contract_name)

        return self.rule_generator.create_spec_with_summary_import(
            trivial_spec, warmup_spec, sanity_spec
        )

    def create_base_config(
        self, main_contract: str, spec_path: Path
    ) -> FileContent:
        """Create base configuration if it doesn't exist."""
        conf_path = self.base_config_path(main_contract)
        conf_path.parent.mkdir(parents=True, exist_ok=True)

        base_prover_args = {
            "quiet": "",  # Add quiet flag for base
        }

        # Prepare base-specific properties
        base_properties = {
            "wait_for_results": "none",
        }

        # Stamp run_source if the invoker set one (e.g. PreAudit passes "STATIC_ANALYZER").
        # No-op if caller left it as None, preserving current behavior for direct CLI use.
        if self.config.run_source:
            base_properties["run_source"] = self.config.run_source

        # Merge build system configuration with compilation updates
        foundry_dict = self.get_build_system_config_dict_with_updates()
        if foundry_dict:
            base_properties.update(foundry_dict)
            self.log(
                f"Merged build system config into base config: {list(foundry_dict.keys())}"
            )

        # Use ConfigManager to create the configuration with all settings at once
        final_config = self.config_manager.create_config(
            main_contract,
            self.contract_handles,
            self.config.additional_contracts,
            spec_path,
            conf_path=conf_path,
            additional_args=base_prover_args,
            properties=base_properties,
        )

        self.log(f"Created base config using ConfigManager: {final_config.path}")
        return final_config

    def test_summary_compilation(
        self, main_contract: str
    ) -> bool:
        """Test that generated summaries compile correctly using ConfigManager.

        This runs a quick compilation check on just one contract to validate
        that llm_summaries.spec and other generated summaries have correct syntax.
        """
        suffix = main_contract
        sanity_spec_path = user_sanity_spec_path(self.config.project_root, suffix)

        # Prepare test configuration settings
        test_config_path = internal_test_compilation_conf(self.config.project_root, suffix)
        test_config_path.parent.mkdir(parents=True, exist_ok=True)

        # Prepare compilation-specific properties
        compilation_properties = {
            "build_cache": True,
            "compilation_steps_only": True,  # Just test compilation, don't run verification
            "msg": f"{main_contract} summary compilation test",
        }

        # Merge build system configuration with compilation updates
        build_system_dict = self.get_build_system_config_dict_with_updates()
        if build_system_dict:
            compilation_properties.update(build_system_dict)

        # Use ConfigManager to create test config with all settings at once
        final_config = self.config_manager.create_config(
            main_contract,
            self.contract_handles,
            [],  # TODO: should we include also self.config.additional_contracts?
            sanity_spec_path,
            conf_path=test_config_path,
            properties=compilation_properties,
        )
        self.log(f"Running compilation test on {main_contract}...")

        # Build command for typechecker error handling
        cmd = [self.config.certora_run_command, str(final_config.path)]
        cmd.extend(self.config.extra_args)

        # Use the proper typechecker error handling loop
        success = self.handle_typechecker_errors(
            cmd, f"Compilation test on {main_contract}"
        )

        if success:
            self.log("✅ Summary compilation test passed")
            return True
        else:
            self.log("Summary compilation test failed", "ERROR")
            return False

    def generate_base_and_run_warmup(
        self,
        skip_warmup: bool = False,
    ) -> bool:
        """Run sanity warmup to prime the cache.

        Returns:
            True if warmup succeeded (or was skipped), False otherwise.
        """
        self.log("=== CACHE WARMUP PHASE ===")

        assert self.main_contract_handle is not None, "main_contract_handle must be set before calling generate_base_and_run_warmup"
        contract_handle = self.main_contract_handle
        contract_name = contract_handle.contract_name

        # Test compilation of summaries before running sanity
        self.log("Testing summary compilation...")

        compilation_failed = False
        if not self.test_summary_compilation(contract_name):
            self.log(
                f"Summary compilation test failed for {contract_name} - please check llm_summaries.spec for syntax errors",
                "ERROR",
            )
            compilation_failed = True

        if compilation_failed:
            return False

        try:
            self.log(
                f"🔀 Running warmup for {contract_name}"
            )

            # Create ProverJobSpec objects for warmup using ProverRunner pattern
            self.log("🚀 Preparing sanity job...")

            # Track whether preparation fails (typechecker failure)
            prep_failed = False

            # Capture self for use in async closures
            autosetup = self

            async def prepare_contract_warmup() -> Optional[ProverJobSpec[Any]]:
                """Prepare warmup job spec for the main contract. Returns None if preparation fails."""
                # Use the user-facing sanity spec for typechecker fixes and loop-iter/hashing detection
                sanity_spec = user_sanity_spec_path(autosetup.config.project_root, contract_name)
                enhanced_config = autosetup.create_base_config(contract_name, sanity_spec)

                # If we will run call resolution, we have to remove the extra files from the .conf before running typechecker.
                # We explicitly add the library FILES that main + additional_contracts use, because the Certora prover's
                # scene = files listed in the conf. solc inlines library imports during compilation, but the prover
                # treats unlisted contracts as out-of-scene — so curated CVL specs that reference
                # ``function Math.mulDiv(...)`` won't typecheck unless ``Math`` (the library, by name) is in ``files``.
                # Crucially: an entry like ``Foo.sol`` puts only the contract named ``Foo`` in scene. If the file also
                # defines ``library FooHelpers``, that needs its own ``Foo.sol:FooHelpers`` entry — sharing a source
                # file with an already-listed contract is NOT enough. So we dedupe library candidates against the full
                # (source_file, contract_name) pair already in scene, not against the source_file alone.
                if not autosetup.config.skip_call_resolution and not autosetup.config.no_strip_contracts:
                    parsed_additional = [
                        split_contract_spec(ac) for ac in autosetup.config.additional_contracts
                    ]
                    in_scene_units = [contract_name] + [name for _, name in parsed_additional]
                    in_scene_handles: set[tuple[str, str]] = {
                        (contract_handle.source_file, contract_handle.contract_name),
                        *((path, name) for path, name in parsed_additional),
                    }
                    extra_libs = [
                        h.to_config_str()
                        for h in autosetup.libraries_for_contracts(in_scene_units)
                        if (h.source_file, h.contract_name) not in in_scene_handles
                    ]
                    files_to_include = (
                        [contract_handle.to_config_str()]
                        + autosetup.config.additional_contracts
                        + extra_libs
                    )
                    props = {"files": files_to_include}
                    autosetup.log(
                        f"Updating config {enhanced_config.path.name} to keep contract {contract_name}, "
                        f"--additional-contracts, and {len(extra_libs)} library file(s) used by them"
                    )
                    autosetup.config_manager.update_config_with_properties(enhanced_config.path, props)

                # Fix the base config with typechecker
                typechecker_success = await asyncio.to_thread(
                    autosetup._fix_config_with_typechecker, enhanced_config.path, f"warmup {contract_name}"
                )
                if not typechecker_success:
                    autosetup.log(
                        f"✗ Skipping warmup for {contract_name} due to typechecker failure",
                        "ERROR",
                    )
                    nonlocal prep_failed
                    prep_failed = True
                    return None

                # Call resolution runs proxy detection internally; --skip-call-resolution skips both.
                if not autosetup.config.skip_call_resolution:
                    await autosetup.run_single_contract_call_resolution(enhanced_config, contract_handle)

                # Detect and set suitable sanity flags (loop iter, hashing bounds)
                if not autosetup.config.skip_sanity_setup:
                    contract_advanced = await autosetup.set_sanity_options(enhanced_config.path, contract_name)
                    autosetup._sanity_advanced_analysis.update(contract_advanced)

                # Create test run config BEFORE building the warmup config, because the test flags
                # need actual sanity rules to exercise. Lands in .certora_internal/confs/ so the
                # user-facing certora/confs/ stays a single-file directory.
                internal_confs_dir = autosetup.config.project_root / DIR_INTERNAL_CONFS
                test_config = autosetup.config_manager.create_copy_with_prover_args(
                    enhanced_config.path,
                    autosetup.TEST_RUN_PROVER_ARGS,
                    "_test_run",
                    target_dir=internal_confs_dir,
                )
                autosetup._test_run_specs.append(ProverJobSpec(
                    config_file=test_config,
                    contract_name=contract_name,
                    phase=f"Sanity Test Run - {contract_name}",
                    extra_args=autosetup.config.extra_args,
                    msg=autosetup._build_job_msg(contract_name, test_config.path),
                ))

                if skip_warmup:
                    return None

                # Build the warmup conf as a copy in .certora_internal/confs/ with verify
                # pointing at the trivial warmup spec. The user-facing conf at
                # certora/confs/{C}.conf is never mutated to point at the warmup spec.
                warmup_spec = autosetup.create_warmup_spec(contract_name)
                warmup_config = autosetup.config_manager.create_copy_with_prover_args(
                    enhanced_config.path, {}, "_warmup", target_dir=internal_confs_dir
                )
                autosetup.config_manager.update_config_spec(warmup_config.path, warmup_spec)

                return ProverJobSpec(
                    config_file=warmup_config,
                    contract_name=contract_name,
                    phase="Sanity Warmup - warmup",
                    extra_args=autosetup.config.extra_args,
                    msg=autosetup._build_job_msg(contract_name, warmup_config.path),
                )

            async def prepare_and_run_warmup():
                job_spec = await prepare_contract_warmup()

                if skip_warmup or job_spec is None:
                    return None

                # Submit and wait for the job
                results = await autosetup.prover_runner.submit_and_wait_for_jobs([job_spec])
                return results[0] if results else None

            warmup_result = asyncio.run(prepare_and_run_warmup())

            if skip_warmup:
                if prep_failed:
                    return False
                return True

            # Check result
            if warmup_result is None:
                return False

            if warmup_result.success:
                self.log(
                    f"✓ Completed: {warmup_result.job_handle.phase} for {warmup_result.job_handle.job_id.split('/')[-1]}"
                )
                if warmup_result.job_handle.job_id.startswith("http"):
                    self.log(f"📎 Job URL: {warmup_result.job_handle.job_id}")
                self.log("✅ Sanity job completed")
                return True
            else:
                self.log(f"✗ Failed: {warmup_result.job_handle.phase}", "ERROR")
                self.log(
                    "❌ Sanity warmup job failed - please check the build status job URL above for details",
                    "ERROR",
                )
                return False

        finally:
            # Keep warmup config file for future adjustments
            pass

    # -------------------------------------------------------------------------
    # Call resolution
    # -------------------------------------------------------------------------

    async def run_single_contract_call_resolution(
        self, enhanced_config: FileContent, contract_handle: ContractHandle
    ) -> bool:
        """Run call resolution (which internally drives proxy detection on
        newly-introduced contracts) for a single contract using its enhanced config."""
        try:
            call_resolution_phase = CallResolutionPhase(
                scope=self.scope,
                prover_runner=self.prover_runner,
                config_manager=self.config_manager,
                config_file=enhanced_config.path,
                reports_dir=self.config.reports_dir,
                extra_args=self.config.extra_args,
                max_prover_invocations=10,
                verbose=bool(self.config.verbose),
                skip_proxy_detection=self.config.skip_proxy_detection,
                skip_harnessing=self.config.skip_harnessing,
                summary_setup=self.setup_prover.summary_setup,
                library_resolver=self.libraries_for_contracts,
                is_library=self.is_library,
            )

            self.log(f"🔗 Running call resolution for {contract_handle.contract_name}")
            await call_resolution_phase.execute(max_iterations=10)
            return True

        except Exception as e:
            self.log(f"⚠️ Call resolution failed for {contract_handle.contract_name}: {str(e)}", "WARNING")
            return False

    # -------------------------------------------------------------------------
    # Sanity options
    # -------------------------------------------------------------------------

    async def set_sanity_options(
        self, conf: Path, contract_name: Optional[str] = None
    ) -> Dict[str, Dict[str, SanityFailureResult]]:
        """
        Set sanity options for a configuration file using SanityPhase.

        Args:
            conf: Path to the configuration file to optimize
            contract_name: Optional contract name. If not provided, will be extracted from config file

        Returns:
            Structured sanity failure analysis: contract -> method -> SanityFailureResult
        """
        # Import sanity phase
        from certora_autosetup.setup.sanity import SanityPhase

        try:
            self.log(f"Starting sanity optimization for config: {conf}")

            # Extract contract name from config file if not provided
            if contract_name is None:
                contract_name = self.config_manager.extract_contract_name_from_config(conf)
                self.log(f"Extracted contract name from config: {contract_name}")

            # Create SanityPhase instance
            sanity_phase = SanityPhase(
                contract_name=contract_name,
                config_file=conf,
                prover_runner=self.prover_runner,
                config_manager=self.config_manager,
                orchestration_timestamp=self.config.orchestration_timestamp,
                extra_args=self.config.extra_args,
                skip_hashing_bound_detection=self.config.skip_hashing_bound_detection,
                min_loop_iter=self.config.min_loop_iter,
                max_loop_iter=self.config.max_loop_iter,
                skip_coverage_analysis=self.config.skip_sanity_coverage_analysis,
            )

            # Execute sanity phase
            advanced_analysis = await sanity_phase.execute()

            self.log(f"✅ Sanity optimization completed for {contract_name}")
            return advanced_analysis

        except Exception as e:
            self.log(f"❌ Error during sanity optimization for {conf}: {e}", "ERROR")
            raise

    # -------------------------------------------------------------------------
    # Git config helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def ensure_git_config_files() -> None:
        """Ensure .gitattributes and .gitignore have recommended Certora settings."""
        project_dir = Path.cwd()

        # Define required lines for each file
        gitattributes_lines = [
            "*.spec linguist-language=Solidity",
            "*.conf linguist-detectable",
            "*.conf linguist-language=JSON5",
        ]

        gitignore_lines = [
            "**/.certora_internal",
            f"**/{CERTORA_REPORTS_DIR}",
        ]

        # Helper function to ensure file has required lines
        def ensure_file_has_lines(file_path: Path, required_lines: list[str], file_description: str) -> None:
            try:
                # Read existing content if file exists
                if file_path.exists():
                    with open(file_path, "r") as f:
                        existing_content = f.read()
                    existing_lines = existing_content.splitlines()
                else:
                    existing_content = ""
                    existing_lines = []

                # Find missing lines
                missing_lines = [
                    line for line in required_lines
                    if line not in existing_lines
                ]

                if missing_lines:
                    # Determine if this is a new file before writing
                    is_new_file = not existing_lines

                    # Append missing lines
                    with open(file_path, "a") as f:
                        # Add newline before if file exists and doesn't end with newline
                        if existing_content and not existing_content.endswith('\n'):
                            f.write('\n')

                        # Add a comment header if this is a new section
                        if file_path.name == ".gitattributes" and existing_lines:
                            f.write('\n# Certora linguist settings\n')
                        elif file_path.name == ".gitignore" and existing_lines:
                            f.write('\n# Certora directories\n')

                        # Write missing lines
                        for line in missing_lines:
                            f.write(f'{line}\n')

                    if is_new_file:
                        logger.log(f"Created {file_path.name} with Certora {file_description} settings", "INFO", COMPONENT)
                    else:
                        logger.log(f"Updated {file_path.name} with missing Certora {file_description} settings", "INFO", COMPONENT)

            except (PermissionError, OSError) as e:
                logger.log(f"Warning: Could not update {file_path.name}: {e}", "WARNING", COMPONENT)

        # Ensure .gitattributes
        gitattributes_path = project_dir / ".gitattributes"
        ensure_file_has_lines(gitattributes_path, gitattributes_lines, "linguist")

        # Ensure .gitignore
        gitignore_path = project_dir / ".gitignore"
        ensure_file_has_lines(gitignore_path, gitignore_lines, "ignore")

    # -------------------------------------------------------------------------
    # Spec file utilities
    # -------------------------------------------------------------------------

    def _find_all_spec_files(self, spec_filename: str) -> List[Path]:
        """Find all instances of a spec file in the certora/ directory.

        Args:
            spec_filename: The name of the spec file (can include partial path)

        Returns:
            List of Path objects for all found instances
        """

        found_files: list = []
        spec_name = Path(spec_filename).name  # Get just the filename

        # Search only in the certora directory
        search_dir = self.config.certora_dir  # This is Path.cwd() / 'certora'

        if not search_dir.exists():
            return found_files

        # Recursively search for the spec file
        for root, dirs, files in os.walk(search_dir):
            # Skip .certora_internal directories (build artifacts)
            dirs[:] = [d for d in dirs if d != ".certora_internal"]

            root_path = Path(root)
            if spec_name in files:
                spec_path = root_path / spec_name
                found_files.append(spec_path)

        return found_files

    def _resolve_spec_file(self, spec_file: str) -> Path:
        """Resolve a spec file path ensuring it exists uniquely in the certora/ directory.

        Args:
            spec_file: The spec file path/name from the error message

        Returns:
            The resolved Path to the unique spec file

        Raises:
            Exception: If the spec file is not found or multiple instances exist
        """
        # Find all instances of this spec file
        found_files = self._find_all_spec_files(spec_file)

        if len(found_files) == 0:
            raise Exception(f"Spec file not found in certora/ directory: {spec_file}")

        if len(found_files) > 1:
            # Fatal error - duplicate spec files
            error_msg = f"FATAL ERROR: Multiple instances of spec file '{Path(spec_file).name}' found in certora/ directory:\n"
            for i, path in enumerate(found_files, 1):
                error_msg += f"  {i}. {path}\n"
            error_msg += (
                "\nEach spec file name must be unique within the certora/ directory."
            )
            error_msg += "\nPlease rename duplicate files to have unique names."
            self.log(error_msg, "ERROR")
            raise Exception(error_msg)

        # Exactly one file found
        return found_files[0]

    # -------------------------------------------------------------------------
    # Typechecker
    # -------------------------------------------------------------------------

    def handle_typechecker_errors(
        self, cmd: List[str], description: str, max_retries: int = 10
    ) -> bool:
        """Handle typechecker errors using the new copy-based approach.

        Args:
            cmd: The certoraRun command to execute
            description: Description of the operation
            max_retries: Maximum number of retry attempts

        Returns:
            Tuple of (success, job_url)
        """
        from typechecker_loop import TypecheckerLoop  # type: ignore[import-not-found]

        self.log(f"Starting typechecker loop for: {description}")

        # Create TypecheckerLoop instance
        typechecker = TypecheckerLoop(
            certora_dir=self.config.certora_dir,
            verbose=bool(self.config.verbose),
            keep_intermediate_files=self.config.keep_intermediate_typechecker_files
        )

        # Run the typechecker loop to fix any typechecker errors
        success, final_cmd = typechecker.run_typechecker_loop(cmd, max_retries)

        if success:
            self.log("✓ Typechecker loop completed successfully")
            return True
        else:
            self.log("✗ Typechecker loop failed", "ERROR")
            return False

    def _fix_config_with_typechecker(
        self, config_file: Path, description: str = ""
    ) -> bool:
        """Fix a single config file with typechecker and update it in place.

        Args:
            config_file: Path to the config file to fix
            description: Description for logging (e.g., "warmup HelloToken")

        Returns:
            bool: True if successful, False if failed
        """
        from typechecker_loop import TypecheckerLoop  # type: ignore[import-not-found]

        try:
            # Build command for typechecker
            cmd = [self.config.certora_run_command, str(config_file)] + self.config.extra_args

            # Create TypecheckerLoop instance
            typechecker = TypecheckerLoop(
                certora_dir=self.config.certora_dir,
                verbose=False,  # Use False to avoid excessive logging
                keep_intermediate_files=self.config.keep_intermediate_typechecker_files
            )

            # Run the typechecker loop to fix any typechecker errors
            desc_text = f" for {description}" if description else ""
            self.log(f"🔧 Running typechecker fixes{desc_text}: {config_file.name}")
            success, final_cmd = typechecker.run_typechecker_loop(cmd, max_rounds=10)

            if success:
                # Extract the fixed config file from final_cmd
                if len(final_cmd) > 1 and final_cmd[1] != str(config_file):
                    fixed_config_file = Path(final_cmd[1])
                    # Copy the fixed config back to the original location
                    shutil.copy2(fixed_config_file, config_file)
                    self.log(f"✓ Config fixed{desc_text}: {config_file.name}")
                else:
                    self.log(
                        f"✓ Config OK{desc_text}: {config_file.name} (no fixes needed)"
                    )
                return True
            else:
                self.log(
                    f"✗ Failed to fix config{desc_text}: {config_file.name}", "ERROR"
                )
                return False

        except Exception as e:
            self.log(
                f"✗ Error fixing config{desc_text} {config_file.name}: {e}", "ERROR"
            )
            return False


    # -------------------------------------------------------------------------
    # Caching helpers
    # -------------------------------------------------------------------------

    def _get_source_files(self) -> list[Path]:
        """Collect all source files whose content determines the autosetup cache key."""
        from certora_autosetup.setup.solidity_utils import find_all_solidity_files
        return [Path(f) for f in find_all_solidity_files()]

    def _get_cache_extra_parts(self) -> list[str]:
        """Extra key parts for the autosetup cache (compiler settings, args, etc.)."""
        parts = [
            f"extra_args:{','.join(sorted(self.config.extra_args))}",
            f"additional_contracts:{','.join(sorted(self.config.additional_contracts))}",
        ]
        if self.main_contract_handle:
            parts.append(f"main_contract:{self.main_contract_handle.contract_name}")
        if self.build_system_config:
            parts.append(f"solc:{self.build_system_config.solc_version}")
        return parts

    def _check_cache(self, result_path: Path) -> AutosetupResult | None:
        """Check if autosetup can be skipped based on source file content hashes.

        Returns cached AutosetupResult if cache is valid, None otherwise.
        """
        from certora_autosetup.cache.content_cache import ContentCache
        try:
            cache = ContentCache("autosetup")
            source_files = self._get_source_files()
            extra = self._get_cache_extra_parts()
            cache_key = cache.compute_cache_key(source_files, extra)

            cached_data = cache.get(cache_key)
            if cached_data is not None:
                cached_result = AutosetupResult.from_json(cached_data, self.config.project_root)
                # Validate via get_fs() so files persisted to the SaaS cache prefix
                # (S3) are found — a local Path.exists() is always False there since
                # .certora_internal/ is never hydrated to local disk. In CLI mode the
                # fsspec root is the project root, so this is equivalent to .exists().
                fs = get_fs()

                def _is_missing(p: Path) -> bool:
                    try:
                        rel = p.relative_to(self.config.project_root)
                    except ValueError:
                        return not p.exists()  # path outside project_root (to_json kept it absolute)
                    return not fs.exists(cache_path(*rel.parts))

                missing = [p for p in cached_result.all_referenced_paths() if p and _is_missing(p)]
                if missing:
                    self.log(f"Cache invalid: {len(missing)} referenced file(s) missing, re-running", "WARNING")
                    for p in missing:
                        self.log(f"  Missing: {p}", "DEBUG")
                    return None
                cached_result.save(result_path, self.config.project_root)
                return cached_result
        except Exception as e:
            self.log(f"Autosetup cache check failed (will re-run): {e}", "DEBUG")
        return None

    def _save_cache(self, result: AutosetupResult) -> None:
        """Save autosetup cache entry after successful pipeline run."""
        from certora_autosetup.cache.content_cache import ContentCache
        try:
            cache = ContentCache("autosetup")
            source_files = self._get_source_files()
            extra = self._get_cache_extra_parts()
            cache_key = cache.compute_cache_key(source_files, extra)
            cache.put(cache_key, result.to_json(self.config.project_root))
        except Exception as e:
            self.log(f"Failed to save autosetup cache: {e}", "WARNING")
