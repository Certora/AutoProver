#!/usr/bin/env python3
"""
Configuration Manager - Handles .conf file creation, updates, and dependency tracking.

Based on Brain's configuration management approach but enhanced for better
dependency tracking and content-based caching support.
"""


import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generic, List, Optional, TypeVar

from packaging.version import Version

from certora_autosetup.parsers.spec_imports import parse_imports_from_spec
from certora_autosetup.utils.config_manager import certora_format_to_raw_version
from certora_autosetup.utils.constants import DEFAULT_SOLC_VERSION, SolcConvention
from certora_autosetup.utils.contract_utils import parse_contract_files
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.solc_version_resolver import (
    parse_pragma_constraint,
    read_pragma_from_source_file,
    resolve_pragma_to_version,
)

# Note: CompilationWorkaroundManager is imported lazily in the two methods that
# need it, to avoid a circular import (compilation_workarounds imports
# ConfigManager at top level).
from certora_autosetup.utils.types import ContractHandle

try:
    import json5  # type: ignore[import-untyped]
except ImportError:
    # Fallback to regular json if json5 is not available
    import json as json5  # type: ignore[no-redef]


@dataclass
class FileContent:
    """Represents file with path and content hash."""

    path: Path
    content_hash: str

    @classmethod
    def from_file(cls, path: Path) -> "FileContent":
        """Create FileContent from file on disk."""
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        content = path.read_text()
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return cls(path=path, content_hash=content_hash)

    def __str__(self) -> str:
        return f"{self.path}#{self.content_hash}"


ContextT = TypeVar('ContextT')


@dataclass
class ProverJobSpec(Generic[ContextT]):
    """Complete specification for a prover job."""

    contract_name: str
    phase: str
    config_file: FileContent
    extra_args: Optional[List[str]] = None
    context: Optional[ContextT] = None
    msg: Optional[str] = None  # Message for --msg argument

    def get_cache_key(self, config_manager: "ConfigManager") -> str:
        """
        Generate cache key from config + all referenced files + extra_args.

        This ensures that any change to the config or its dependencies
        results in a new cache key and a fresh job submission.
        """
        referenced_files = config_manager.get_referenced_files_with_hashes(self.config_file.path)
        components = [
            self.contract_name,
            str(self.config_file),  # config_path#content_hash
            *[str(f) for f in referenced_files],  # All referenced files with hashes
        ]

        # Include extra_args in the cache key if present
        if self.extra_args:
            extra_args_str = " ".join(self.extra_args)
            components.append(f"extra_args:{extra_args_str}")

        joined_components = "|".join(components)
        cache_key = hashlib.sha256(joined_components.encode()).hexdigest()

        return cache_key

    @staticmethod
    def build_job_msg(orchestration_timestamp: str, contract_name: str, conf_file: Path) -> str:
        """Build the msg string for a prover job.

        Format: "Certora <timestamp> <ContractName>: <conf_name>"

        Args:
            orchestration_timestamp: Timestamp string from orchestration start
            contract_name: Name of the contract being verified
            conf_file: Path to the configuration file

        Returns:
            Formatted message string or None if no timestamp
        """
        return f"Certora {orchestration_timestamp} {contract_name}: {conf_file.stem}"


class ConfigManager:
    """
    Configuration manager that handles .conf file creation, updates, and dependency tracking.
    """

    DEFAULT_CONF_TEMPLATE = {"assert_autofinder_success": True, "files": []}

    def __init__(
        self,
        project_root: Path,
        convert_solc_to_certora_format: bool = True,
        global_timeout: int = 1200,
    ):
        """
        Initialize configuration manager.

        Args:
            project_root: Root directory of the project
            convert_solc_to_certora_format: Whether to convert solc version from foundry format to Certora format
            global_timeout: Global timeout in seconds for prover execution
        """
        self.project_root = project_root
        self.convert_solc_to_certora_format = convert_solc_to_certora_format
        self.global_timeout = global_timeout
        self.component = "ConfigManager"
        self.reference_compiler_maps: Dict[str, Any] = {}

    def log(self, message: str, level: str = "INFO"):
        """Log message using centralized logger."""
        logger.log(message, level, self.component)

    def _normalize_path(self, path: Path, context: str = "File") -> Path:
        """
        Normalize a file path relative to project root with fallback to absolute paths.

        Args:
            path: Path to normalize
            context: Description for logging (e.g., "File", "Spec file")

        Returns:
            Normalized path (relative if possible, otherwise original)
        """
        # If already relative and exists under project_root, use as-is
        if not path.is_absolute() and (self.project_root / path).exists():
            return path

        # Try to make it relative to project_root
        try:
            return path.relative_to(self.project_root)
        except ValueError:
            # Could not make relative - use as-is, warn appropriately
            if path.is_absolute():
                self.log(
                    f"{context} {path} is outside project root {self.project_root}, using absolute path",
                    "WARNING"
                )
            else:
                self.log(
                    f"{context} {path} does not exist, using as-is, likely an error",
                    "WARNING"
                )
            return path

    def normalize_paths(self, handles: List[ContractHandle]) -> List[ContractHandle]:
        """
        Normalize file paths relative to project root with fallback to absolute paths.

        Args:
            handles: List of contract handles with file paths to normalize

        Returns:
            List of contract handles with normalized path strings
        """
        normalized_handles = []
        for handle in handles:
            source_path = Path(handle.source_file)
            normalized_path = self._normalize_path(source_path, context="File")
            if str(normalized_path) != handle.source_file:
                normalized_handles.append(ContractHandle(
                    contract_name=handle.contract_name,
                    source_file=str(normalized_path)
                ))
            else:
                normalized_handles.append(handle)
        return normalized_handles

    def create_config(
        self,
        contract_name: str,
        contract_handles: List[ContractHandle],
        additional_files: List[str],
        spec_file: Path,
        conf_path: Optional[Path] = None,
        additional_args: Optional[Dict[str, str]] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> FileContent:
        """
        Create initial configuration file from template.

        Args:
            contract_name: Name of the contract
            contract_handles: List of ContractHandle objects
            additional_files: List of additional file paths
            spec_file: Path to the CVL specification file
            conf_path: Path to the to-be-created .conf file
            additional_args: Optional dict of additional prover arguments
            properties: Optional dict of additional config properties (including build system settings)

        Returns:
            FileContent representing the created configuration
        """
        # Create config from template
        conf_template = copy.deepcopy(self.DEFAULT_CONF_TEMPLATE)
        conf_template["global_timeout"] = str(self.global_timeout)

        # Add contract files (use normalized paths) TODO: what about the additional files? we expect them to come already normalized now
        normalized = self.normalize_paths(contract_handles)
        conf_template["files"] = [c.to_config_str() for c in normalized] + additional_files

        # Set verification target (use relative path for spec file)
        normalized_spec = self._normalize_path(spec_file, context="Spec file")
        conf_template["verify"] = f"{contract_name}:{normalized_spec}"

        conf_template["parametric_contracts"] = contract_name

        # Apply additional properties if provided
        if properties:
            conf_template.update(properties)

        # Apply additional prover args if provided
        if additional_args:
            existing_args_raw = conf_template.get("prover_args", [])
            existing_args = existing_args_raw if isinstance(existing_args_raw, list) else []
            parsed_args = self._parse_prover_args(existing_args)
            for arg_key, arg_value in additional_args.items():
                normalized_key = arg_key.lstrip("-")
                parsed_args[normalized_key] = arg_value
            conf_template["prover_args"] = self._build_prover_args_list(parsed_args)

        # Write configuration file
        if not conf_path:
            raise ValueError("conf_path is required; ConfigManager no longer defaults a conf output path")
        with conf_path.open("w") as f:
            json.dump(conf_template, f, indent=4, sort_keys=True)

        self.log(f"Created configuration at {conf_path}")
        return FileContent.from_file(conf_path)

    @staticmethod
    def extract_contract_and_spec_from_config(
        config_file: Path, project_root: Path
    ) -> Optional[tuple[str, Path]]:
        """
        Extract contract name and spec file path from an existing .conf file.

        Args:
            config_file: Path to the .conf file
            project_root: Project root directory for resolving relative paths

        Returns:
            Tuple of (contract_name, spec_file_path) if found, None otherwise
        """
        try:
            if not config_file.exists():
                return None

            with config_file.open("r") as f:
                config_data = json5.load(f)

            # Extract from verify field (format: "ContractName:spec/path")
            verify = config_data.get("verify", "")
            if ":" in verify:
                contract_name, spec_path = verify.split(":", 1)

                # Convert to Path object
                spec_path = Path(spec_path)

                # If relative path, resolve against project root
                if not spec_path.is_absolute():
                    spec_path = project_root / spec_path

                return (contract_name, spec_path)

            return None

        except Exception as e:
            logger.warning(f"Failed to extract contract and spec from {config_file}: {e}")
            return None

    def update_config_spec(self, config_path: Path, new_spec: Path) -> None:
        """Update the spec file path in a config's verify field.

        Args:
            config_path: Path to the config file to update
            new_spec: Path to the new spec file
        """
        with open(config_path, "r") as f:
            config = json5.load(f)

        verify = config.get("verify", "")
        if ":" in verify:
            contract_name = verify.split(":", 1)[0]
            config["verify"] = f"{contract_name}:{new_spec}"
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4)
            self.log(f"Updated config to use spec: {new_spec.name}")

    def add_files_to_config(
        self,
        config_file: Path,
        new_contract_files: List[ContractHandle] | None = None,
    ) -> FileContent:
        """
        Update configuration with new contract files.

        Args:
            config_file: Path to configuration file to update
            new_contract_files: Optional list of ContractHandle objects to add to config

        Returns:
            Updated FileContent
        """
        # Load existing configuration
        with config_file.open("r") as f:
            conf_object = json5.load(f)

        files_added = []

        # Add new contract files
        if new_contract_files:
            orig_files = conf_object.get("files", [])

            # Normalize all paths to relative paths for comparison
            normalized_orig = set()
            for existing_file in orig_files:
                existing_path = Path(existing_file)
                if existing_path.is_absolute():
                    try:
                        relative_path = existing_path.relative_to(self.project_root)
                        normalized_orig.add(str(relative_path))
                    except ValueError:
                        normalized_orig.add(str(existing_path))
                else:
                    normalized_orig.add(str(existing_path))

            contracts_added: List[ContractHandle] = []
            for contract_file in new_contract_files:
                # ContractHandle already has the right format (includes :ContractName if needed)
                file_str = contract_file.to_config_str()

                # Only add if not already present
                if file_str not in normalized_orig:
                    orig_files.append(file_str)
                    normalized_orig.add(file_str)
                    files_added.append(file_str)
                    contracts_added.append(contract_file)
                    self.log(
                        f"Added contract file to {config_file.name}: {file_str}"
                    )

            conf_object["files"] = orig_files

        # Handle compiler_map and solc_via_ir_map for newly added contracts
        compiler_maps_updated = False
        if new_contract_files and files_added:
            compiler_maps_updated = self._update_compiler_maps_for_new_contracts(
                conf_object, contracts_added
            )

        # Write updated configuration only if something was actually changed
        if len(files_added) > 0 or compiler_maps_updated:
            with config_file.open("w") as f:
                json.dump(conf_object, f, indent=4, sort_keys=True)

        return FileContent.from_file(config_file)

    def _update_compiler_maps_for_new_contracts(
        self,
        conf_object: Dict[str, Any],
        contracts_added: List[ContractHandle],
    ) -> bool:
        """
        Update compiler_map and solc_via_ir_map when new contracts are added.

        Delegates to CompilationWorkaroundManager which handles:
        - Parsing pragma from source files
        - Creating/updating compiler_map entries
        - Creating/updating solc_via_ir_map entries

        Args:
            conf_object: The config dict to modify in-place
            contracts_added: ContractHandle objects that were actually added (not duplicates)

        Returns:
            True if any compiler map was modified
        """
        if not contracts_added:
            return False

        modified = False
        ref_maps = self.reference_compiler_maps or None
        for contract in contracts_added:
            modified |= self.update_compiler_map_for_contract(conf_object, contract, ref_maps)
            modified |= self.update_via_ir_map_for_contract(conf_object, contract, ref_maps)

        return modified

    def update_config_with_prover_args(
        self,
        config_file: Path,
        additional_args: Dict[str, str] | None = None,
        remove_args: List[str] | None = None,
        _logger_context: Optional[str] = None,
    ) -> FileContent:
        """
        Update configuration with additional or removed prover arguments.

        Args:
            config_file: Path to configuration file to update
            additional_args: Dict mapping argument names to values (empty string for flags)
            remove_args: List of argument names to remove (e.g., ["split", "timeout"])
            _logger_context: Optional context for logging (e.g., contract name) (currently unused)

        Returns:
            Updated FileContent
        """
        # Load existing configuration
        with config_file.open("r") as f:
            conf_object = json5.load(f)

        # Parse existing prover arguments into a more manageable format
        existing_args = conf_object.get("prover_args", [])
        parsed_args = self._parse_prover_args(existing_args)

        # Remove specified arguments
        if remove_args:
            for arg_key in remove_args:
                # Normalize the key (remove leading dashes if any)
                normalized_key = arg_key.lstrip("-")
                if normalized_key in parsed_args:
                    del parsed_args[normalized_key]

        # Add/update new arguments
        if additional_args:
            for arg_key, arg_value in additional_args.items():
                normalized_key = arg_key.lstrip("-")
                parsed_args[normalized_key] = arg_value

        # Convert back to list format
        updated_args = self._build_prover_args_list(parsed_args)
        conf_object["prover_args"] = updated_args

        # Write updated configuration
        with config_file.open("w") as f:
            json.dump(conf_object, f, indent=4, sort_keys=True)

        # Log what actually happened
        if additional_args and remove_args:
            self.log(
                f"Updated configuration {config_file.name}: added {additional_args}, removed {remove_args}",
                "DEBUG"
            )
        elif additional_args:
            self.log(
                f"Updated configuration {config_file.name} with prover args: {additional_args}",
                "DEBUG"
            )
        elif remove_args:
            self.log(
                f"Updated configuration {config_file.name}: removed prover args {remove_args}",
                "DEBUG"
            )
        else:
            self.log(
                f"Configuration {config_file.name} updated with no changes",
                "DEBUG"
            )
        return FileContent.from_file(config_file)

    def _parse_prover_args(self, args: List[str]) -> Dict[str, str]:
        """
        Parse prover arguments list into key-value pairs by joining and splitting on "-".

        Handles both formats:
        ["-split", "false", "-s", "z3"] and ["-split false", "-s z3"]
        -> {"split": "false", "s": "z3"}

        Args:
            args: List of prover arguments in various formats

        Returns:
            Dictionary mapping argument keys to their values (empty string for flags)
        """
        if not args:
            return {}

        # Join all arguments into a single string
        joined_args = " ".join(args)

        # Split on "-" to get individual argument chunks
        # Filter out empty strings from the split
        arg_chunks = [
            chunk.strip() for chunk in joined_args.split("-") if chunk.strip()
        ]

        parsed = {}

        for chunk in arg_chunks:
            if " " in chunk:
                # This chunk has a value: "split false" or "timeout 3600"
                parts = chunk.split(" ", 1)  # Split on first space only
                key = parts[0].strip()
                value = parts[1].strip()
                parsed[key] = value
            else:
                # This is a flag without value: "optimize"
                key = chunk.strip()
                parsed[key] = ""

        return parsed

    def _build_prover_args_list(self, parsed_args: Dict[str, str]) -> List[str]:
        """
        Build prover arguments list from key-value pairs.

        Examples:
        {"split": "false", "s": "z3", "optimize": ""}
        -> ["-split false", "-s z3", "-optimize"]

        Args:
            parsed_args: Dictionary of argument key-value pairs

        Returns:
            List of prover arguments in consistent format
        """
        result = []

        for key, value in parsed_args.items():
            if value:
                # Key with value: combine into single string
                result.append(f"-{key} {value}")
            else:
                # Flag without value
                result.append(f"-{key}")

        return result

    def get_referenced_files_with_hashes(self, config_file: Path) -> List[FileContent]:
        """
        Parse configuration and return all referenced files with their content hashes.

        This is crucial for content-based caching - any change to referenced files
        should result in a different cache key.
        # TODO: the hash is not computed from recursively included files
        # (e.g. imports within .sol files) and hence can be wrong. Perhaps we might reuse
        # the build_cache functionality of the prover.

        Args:
            config_file: Path to configuration file to analyze

        Returns:
            List of FileContent objects for all referenced files
        """

        referenced_files = []
        config = self._load_config(config_file=config_file)
        if config is None:
            return []
        files: List[Path] = [Path(contractHandle.source_file) for contractHandle in self.get_referenced_contracts(config)]

        # Extract contract files
        for file_path in files:
            try:
                if file_path.exists():
                    referenced_files.append(FileContent.from_file(file_path))
                else:
                    self.log(f"Referenced file not found: {file_path}", "WARNING")
            except Exception as e:
                self.log(
                    f"Error processing referenced file {file_path}: {e}",
                    "ERROR"
                )

        # Extract specification file and its imports from verify field
        if "verify" in config:
            try:
                verify_parts = config["verify"].split(":")
                if len(verify_parts) >= 2:
                    spec_path = Path(verify_parts[1])
                    if not spec_path.is_absolute():
                        spec_path = self.project_root / spec_path

                    if spec_path.exists():
                        referenced_files.append(FileContent.from_file(spec_path))

                        # Include imported spec files in the cache key
                        for imported_spec in parse_imports_from_spec(spec_path):
                            if imported_spec.exists():
                                referenced_files.append(FileContent.from_file(imported_spec))
                    else:
                        self.log(f"Spec file not found: {spec_path}", "WARNING")
            except Exception as e:
                self.log(f"Error processing verify field: {e}", "ERROR")

        return referenced_files

    def get_referenced_contracts(self, config_file: Path | dict) -> List[ContractHandle]:
        """
        Extract contract handles from .sol file paths referenced in configuration.

        Args:
            config_file: Path to configuration file or already-parsed config dict

        Returns:
            List of ContractHandles extracted from .sol file paths
        """
        # Handle both Path and dict inputs
        config = self._load_config(config_file) if isinstance(config_file, Path) else config_file
        if config is None:
            return []

        return parse_contract_files(config.get("files", []), project_root=self.project_root, strict=False)

    def _load_config(self, config_file: Path) -> Optional[Dict[str, Any]]:
        try:
            with config_file.open("r") as f:
                return json5.load(f)
        except Exception as e:
            self.log(f"Failed to parse config file {config_file}: {e}")
            return None

    def format_solc(self, solc_version: str) -> str:
        """
        Convert solc version to appropriate format based on convert_solc_to_certora_format flag.

        Args:
            solc_version: Version in any format (e.g., "0.8.19", "solc-0.8.26")

        Returns:
            Version in appropriate format (e.g., "solc8.19" for Certora, "0.8.19" for foundry)
        """
        if self.convert_solc_to_certora_format:
            # Convert from foundry format "0.8.19" to Certora format "solc8.19"
            return ConfigManager.convert_solc_version_to_certora_format(solc_version)
        else:
            # Use foundry format as-is
            return solc_version

    @staticmethod
    def convert_solc_version_to_certora_format(foundry_version: str) -> str:
        """
        Convert solc version from foundry format to Certora format.

        Args:
            foundry_version: Version in foundry format (e.g., "0.8.19", "solc-0.8.26")

        Returns:
            Version in Certora format (e.g., "solc8.19")
        """
        # Remove any leading 'v' if present
        version = foundry_version.lstrip("v")

        # Handle "solc-0.8.26" format - extract the version part
        if version.startswith("solc-"):
            version = version[5:]  # Remove "solc-" prefix

        # Convert "0.8.19" to "solc8.19"
        if version.startswith("0."):
            # Remove the '0.' prefix and add 'solc' prefix
            return f"solc{version[2:]}"
        else:
            # If it doesn't start with '0.', just add 'solc' prefix
            return f"solc{version}"

    @staticmethod
    def format_solc_version(version: str, convention: SolcConvention) -> str:
        """Format a semantic solc version string per the project's convention.

        Single source of truth for the Certora vs solc-select naming choice; the
        instance-level wrapper on CompilationWorkaroundManager delegates here.

        Examples:
          "0.8.34" + CERTORA      -> "solc8.34"
          "0.8.34" + SOLC_SELECT  -> "solc-0.8.34"
        """
        v = version.lstrip("v")
        if convention == SolcConvention.SOLC_SELECT:
            if not v.startswith("0."):
                v = f"0.{v}"
            return f"solc-{v}"
        return ConfigManager.convert_solc_version_to_certora_format(v)

    @staticmethod
    def extract_solc_version_from_pragma(
        handle: ContractHandle,
        project_root: Path,
        preferred_version: Optional[str] = None,
        convention: SolcConvention = SolcConvention.CERTORA,
        pragma_spec: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve ``handle``'s pragma to a concrete solc version, formatted per
        ``convention``.
        Callers can provide ``pragma_spec`` if it was already parsed for
        ``handle``. Otherwise it is read via ``read_pragma_from_source_file``.

        Returns None if the source file is missing, the pragma is absent, or
        the pragma can't be resolved to a concrete version.
        """
        if pragma_spec is None:
            pragma_spec = read_pragma_from_source_file(Path(handle.source_file), project_root)
        if not pragma_spec:
            return None

        raw = resolve_pragma_to_version(pragma_spec, preferred_version=preferred_version)
        if not raw:
            return None
        return ConfigManager.format_solc_version(raw, convention)

    # =========================================================================
    # compiler_map / solc_via_ir_map reconciliation
    #
    # These were previously methods on CompilationWorkaroundManager but live
    # here now: they mutate the conf object (ConfigManager's job) and don't
    # need any of the workaround/retry state. Moving them here also breaks
    # the circular import between enhanced_config_manager and
    # compilation_workarounds.
    # =========================================================================

    def _solc_convention(self) -> SolcConvention:
        """Convention this manager emits: derived from the boolean
        convert_solc_to_certora_format flag for symmetry with the rest of
        the autosetup codebase."""
        return SolcConvention.CERTORA if self.convert_solc_to_certora_format else SolcConvention.SOLC_SELECT

    def _key_matches_any_contract(self, key: str, contracts: List[ContractHandle]) -> bool:
        """True if any contract in `contracts` matches the compiler_map key
        (per the prover's matching rules — see ContractHandle.matches_map_key)."""
        return any(c.matches_map_key(key) for c in contracts)

    def _project_preferred_raw_version(
        self,
        conf_object: Dict[str, Any],
        pragma_spec: Optional[str] = None,
    ) -> Optional[str]:
        """Project's preferred *raw* solc version (e.g. "0.8.35") for biasing
        pragma resolution. Accepts both Certora-style ("solc8.35") and
        solc-select-style ("solc-0.8.35") conf values.

        Order:
            1. conf["solc"] — project-wide override.
            2. When ``pragma_spec`` is supplied: the LOWEST version already in
               ``compiler_map`` that satisfies the pragma. This keeps a file
               with a permissive pragma (e.g. ``^0.8.28``) from being resolved
               to a NEWER version than its sibling files that have exact
               pragmas — which would otherwise cause solc to compile the file
               with a newer compiler and then reject one of its imports.
            3. Mode of existing compiler_map values — preserved fallback for
               callers that don't pass a pragma.
            4. None.
        """
        solc = conf_object.get("solc")
        if isinstance(solc, str) and solc:
            raw = certora_format_to_raw_version(solc)
            if raw:
                return raw
        existing = [
            v for v in conf_object.get("compiler_map", {}).values()
            if isinstance(v, str) and v
        ]
        if not existing:
            return None

        # TODO: consider replacing this heuristic with a transitive-import walk.
        if pragma_spec:
            constraint = parse_pragma_constraint(pragma_spec)
            if constraint is not None:
                satisfying_raw: List[str] = []
                for v in existing:
                    # Confs are autosetup-generated, so every compiler_map value
                    # converts to a parseable raw version.
                    raw = certora_format_to_raw_version(v)
                    assert raw is not None, f"compiler_map value {v!r} is not a recognizable solc version"
                    if Version(raw) in constraint:
                        satisfying_raw.append(raw)
                if satisfying_raw:
                    return min(satisfying_raw, key=Version)

        most_common = Counter(existing).most_common(1)[0][0]
        return certora_format_to_raw_version(most_common)

    def _resolve_default_solc(self, conf_object: Dict[str, Any]) -> str:
        """Project-wide formatted-solc fallback when a file has no parseable pragma.

        Order: conf["solc"] -> most common existing compiler_map version
        -> DEFAULT_SOLC_VERSION.
        """
        solc = conf_object.get("solc")
        if isinstance(solc, str) and solc:
            return solc
        existing = [
            v for v in conf_object.get("compiler_map", {}).values()
            if isinstance(v, str) and v
        ]
        if existing:
            from collections import Counter
            return Counter(existing).most_common(1)[0][0]
        return DEFAULT_SOLC_VERSION

    def _resolve_solc_for_handle(
        self,
        handle: ContractHandle,
        conf_object: Dict[str, Any],
        convention: Optional[SolcConvention] = None,
    ) -> str:
        """Resolve the solc version for a contract being added to compiler_map.

        Pragma-first: the file's own `pragma solidity` directive is authoritative.
        If pragma resolves, the result is formatted in `convention` (or this
        manager's convention when None) and biased toward
        `_project_preferred_raw_version(conf_object, pragma_spec=...)`.
        If no pragma, fall back to `_resolve_default_solc`.

        `convention` is exposed so callers with their own convention state
        (e.g. CompilationWorkaroundManager.solc_convention) can override the
        per-manager default without having to instantiate a new ConfigManager.
        """
        effective_convention = convention if convention is not None else self._solc_convention()

        pragma_spec = read_pragma_from_source_file(Path(handle.source_file), self.project_root)
        preferred_raw = self._project_preferred_raw_version(conf_object, pragma_spec=pragma_spec)
        formatted = ConfigManager.extract_solc_version_from_pragma(
            handle, self.project_root,
            preferred_version=preferred_raw,
            convention=effective_convention,
            pragma_spec=pragma_spec,
        )

        if formatted:
            return formatted
        return self._resolve_default_solc(conf_object)

    def _create_compiler_map_from_files(
        self,
        files: List[str],
        default_version: str,
        additional_entries: Dict[str, str],
    ) -> Dict[str, str]:
        """Create compiler_map from files list with default version, plus additional entries."""
        contracts = parse_contract_files(files, project_root=self.project_root, strict=False)
        compiler_map = {c.contract_name: default_version for c in contracts}
        compiler_map.update(additional_entries)
        return compiler_map

    def _create_via_ir_map_from_files(
        self,
        files: List[str],
        additional_entries: Dict[str, bool],
    ) -> Dict[str, bool]:
        """Create solc_via_ir_map from files list (all True), plus additional entries."""
        contracts = parse_contract_files(files, project_root=self.project_root, strict=False)
        via_ir_map: Dict[str, bool] = {c.contract_name: True for c in contracts}
        via_ir_map.update(additional_entries)
        return via_ir_map

    def update_compiler_map_for_contract(
        self,
        conf_object: Dict[str, Any],
        contract: ContractHandle,
        reference_maps: Optional[Dict[str, Any]] = None,
        convention: Optional[SolcConvention] = None,
    ) -> bool:
        """Add a compiler_map entry for a newly-added contract.

        - Prefers `reference_maps["compiler_map"][contract_name]` when supplied
          (from upstream compilation analysis).
        - Otherwise falls back to pragma resolution, then the project default.

        `convention` is forwarded to the pragma resolver so callers (e.g.
        CompilationWorkaroundManager) can pass their own solc_convention.
        Defaults to this manager's convention.

        Modifies `conf_object` in place. Returns True iff anything changed.
        """
        contract_name = contract.contract_name

        ref_compiler_map = (reference_maps or {}).get("compiler_map", {})
        if contract_name in ref_compiler_map:
            version_for_contract = ref_compiler_map[contract_name]
        else:
            # Route through _resolve_solc_for_handle so we pick up the
            # lowest-satisfying-in-conf bias — without this, a permissive pragma
            # like ^0.8.28 silently jumps to DEFAULT_SOLC_VERSION (0.8.34) even
            # when sibling files are already pinned to 0.8.28.
            version_for_contract = self._resolve_solc_for_handle(
                contract, conf_object, convention=convention,
            )

        modified = False
        if "compiler_map" in conf_object:
            if contract_name not in conf_object["compiler_map"]:
                conf_object["compiler_map"][contract_name] = version_for_contract
                self.log(f"Added {contract_name} to compiler_map with {version_for_contract}")
                modified = True
        else:
            # No compiler_map yet. If the new contract's version diverges from the
            # global "solc" (or "solc" — the prover's solc-select default — when
            # absent), materialise a per-contract map.
            global_solc = conf_object.get("solc", "solc")
            if version_for_contract != global_solc:
                conf_object["compiler_map"] = self._create_compiler_map_from_files(
                    conf_object.get("files", []), global_solc, {contract_name: version_for_contract}
                )
                conf_object.pop("solc", None)  # mutually exclusive with compiler_map
                self.log(
                    f"Created compiler_map with {len(conf_object['compiler_map'])} entries "
                    f"(removed global solc)"
                )
                modified = True

        return modified

    def update_via_ir_map_for_contract(
        self,
        conf_object: Dict[str, Any],
        contract: ContractHandle,
        reference_maps: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Add a solc_via_ir_map entry for a newly-added contract. Mirrors
        `update_compiler_map_for_contract` for the via-ir map."""
        contract_name = contract.contract_name
        modified = False

        if "solc_via_ir_map" in conf_object:
            if contract_name not in conf_object["solc_via_ir_map"]:
                ref_via_ir_map = (reference_maps or {}).get("solc_via_ir_map", {})
                via_ir_value = ref_via_ir_map.get(contract_name, True)
                conf_object["solc_via_ir_map"][contract_name] = via_ir_value
                self.log(f"Added {contract_name} to solc_via_ir_map (value={via_ir_value})")
                modified = True
        elif conf_object.get("solc_via_ir"):
            # Global via-ir is set but map doesn't exist - convert to map.
            conf_object["solc_via_ir_map"] = self._create_via_ir_map_from_files(
                conf_object.get("files", []), {contract_name: True}
            )
            del conf_object["solc_via_ir"]
            self.log(
                f"Converted solc_via_ir to solc_via_ir_map with "
                f"{len(conf_object['solc_via_ir_map'])} entries"
            )
            modified = True

        return modified

    def sync_compiler_maps_with_files(
        self,
        conf_object: Dict[str, Any],
        convention: Optional[SolcConvention] = None,
    ) -> bool:
        """Reconcile compiler_map, solc_via_ir_map, and solc_evm_version_map with `files`.

        Invariant: when `compiler_map` is present, every contract in `files`
        has a matching entry. Stale entries (no longer in `files`) get trimmed;
        missing entries (libraries newly injected into `files`) get filled in
        via pragma-first resolution.

        `convention` is forwarded to the pragma resolver so callers can pass
        their own solc_convention (e.g. CompilationWorkaroundManager passing
        `self.solc_convention`). Defaults to this manager's convention.

        Without this, certoraRun rejects the conf with
        "files are not matched in compiler_map" when files outgrows compiler_map.

        Returns True iff anything changed.
        """
        files = conf_object.get("files", [])
        contracts_in_files = parse_contract_files(files, project_root=self.project_root, strict=False)

        modified = False

        if "compiler_map" in conf_object:
            original_len = len(conf_object["compiler_map"])
            # Trim: keep keys that match at least one contract in files.
            conf_object["compiler_map"] = {
                key: version
                for key, version in conf_object["compiler_map"].items()
                if self._key_matches_any_contract(key, contracts_in_files)
            }
            if len(conf_object["compiler_map"]) != original_len:
                self.log(
                    f"Trimmed compiler_map from {original_len} to "
                    f"{len(conf_object['compiler_map'])} entries"
                )
                modified = True
            # Extend: for every contract in files not yet covered, add an entry
            # via pragma-first resolution.
            added = 0
            for handle in contracts_in_files:
                if not any(handle.matches_map_key(key) for key in conf_object["compiler_map"]):
                    conf_object["compiler_map"][handle.contract_name] = self._resolve_solc_for_handle(
                        handle, conf_object, convention=convention,
                    )
                    added += 1
            if added > 0:
                self.log(
                    f"Filled {added} missing compiler_map entry/entries "
                    f"(pragma-first, falls back to project default) "
                    f"so files and compiler_map stay consistent"
                )
                modified = True

        if "solc_via_ir_map" in conf_object:
            original_len = len(conf_object["solc_via_ir_map"])
            conf_object["solc_via_ir_map"] = {
                key: value
                for key, value in conf_object["solc_via_ir_map"].items()
                if self._key_matches_any_contract(key, contracts_in_files)
            }
            if len(conf_object["solc_via_ir_map"]) != original_len:
                self.log(
                    f"Trimmed solc_via_ir_map from {original_len} "
                    f"to {len(conf_object['solc_via_ir_map'])} entries"
                )
                modified = True

        if "solc_evm_version_map" in conf_object:
            original_len = len(conf_object["solc_evm_version_map"])
            conf_object["solc_evm_version_map"] = {
                key: value
                for key, value in conf_object["solc_evm_version_map"].items()
                if self._key_matches_any_contract(key, contracts_in_files)
            }
            if len(conf_object["solc_evm_version_map"]) != original_len:
                self.log(
                    f"Trimmed solc_evm_version_map from {original_len}"
                    f" to {len(conf_object['solc_evm_version_map'])} entries"
                )
                modified = True

        return modified

    def create_copy_with_prover_args(
        self,
        config_file: Path,
        additional_args: Dict[str, str],
        suffix: str,
        target_dir: Optional[Path] = None,
    ) -> FileContent:
        """
        Create a copy of configuration with additional prover arguments.

        Args:
            config_file: Original configuration file
            additional_args: Dict mapping argument names to values (empty string for flags)
            suffix: Suffix to add to filename (e.g., "_loop_3")
            target_dir: Optional directory to write the copy into. Defaults to the
                source conf's directory. Use this to keep transient copies out of
                the user-facing certora/confs/ tree.

        Returns:
            FileContent for the new configuration copy
        """
        # Create new filename with suffix
        original_stem = config_file.stem
        new_filename = f"{original_stem}{suffix}.conf"
        parent_dir = target_dir if target_dir is not None else config_file.parent
        parent_dir.mkdir(parents=True, exist_ok=True)
        new_config_path = parent_dir / new_filename

        # Copy the original file
        import shutil

        shutil.copy2(config_file, new_config_path)

        # Use existing method to update prover args
        updated_config = self.update_config_with_prover_args(
            new_config_path, additional_args=additional_args
        )

        self.log(
            f"Created config copy {new_filename} with args: {additional_args}"
        )
        return updated_config

    def create_copy_with_config_properties(
        self,
        config_file: Path,
        properties: Dict[str, Any],
        suffix: str,
        target_dir: Optional[Path] = None,
    ) -> FileContent:
        """
        Create a copy of configuration with additional top-level config properties.

        Useful for sanity phase where we set loop_iter, hashing_length_bound, etc.
        as top-level properties rather than command line arguments.

        Args:
            config_file: Original configuration file
            properties: Dictionary of top-level properties to set
            suffix: Suffix to add to filename (e.g., "_loop_3")
            target_dir: Optional directory to write the copy into. Defaults to the
                source conf's directory. Use this to keep transient copies out of
                the user-facing certora/confs/ tree.

        Returns:
            FileContent for the new configuration copy
        """
        # Create new filename with suffix
        original_stem = config_file.stem
        new_filename = f"{original_stem}{suffix}.conf"
        parent_dir = target_dir if target_dir is not None else config_file.parent
        parent_dir.mkdir(parents=True, exist_ok=True)
        new_config_path = parent_dir / new_filename

        # Copy the original file
        import shutil

        shutil.copy2(config_file, new_config_path)

        # Use existing method to update properties
        updated_config = self.update_config_with_properties(new_config_path, properties)

        return updated_config

    def update_config_with_properties(
        self, config_file: Path, properties: Dict[str, Any]
    ) -> FileContent:
        """
        Update an existing configuration file with top-level config properties.

        When "files" property is updated, reconciles compiler_map / solc_via_ir_map /
        solc_evm_version_map with the new files list — both trimming stale entries AND
        filling in defaults for any contract appearing in `files` without coverage.
        Keeps the conf internally consistent so certoraRun doesn't reject it with
        "files are not matched in compiler_map".

        Args:
            config_file: Configuration file to update
            properties: Dictionary of top-level properties to set

        Returns:
            FileContent for the updated configuration
        """
        # Load existing configuration
        with config_file.open("r") as f:
            conf_object = json5.load(f)

        # Update properties at top level of config
        for key, value in properties.items():
            conf_object[key] = value

        # If "files" property was updated, sync compiler maps to remove stale entries
        if "files" in properties:
            self.sync_compiler_maps_with_files(conf_object)

        # Write updated configuration back to same file
        with config_file.open("w") as f:
            json.dump(conf_object, f, indent=4, sort_keys=True)

        self.log(
            f"Updated config {config_file.name} with properties: {list(properties.keys())}"
        )
        return FileContent.from_file(config_file)

    def parse_pragma_solidity(self, solidity_file: Path) -> Optional[str]:
        """
        Parse pragma solidity statement from a Solidity file and return required version.

        Args:
            solidity_file: Path to Solidity source file

        Returns:
            Required solidity version string (e.g., "0.8.26", "^0.8.0") or None if not found
        """
        import re

        try:
            with solidity_file.open("r", encoding="utf-8") as f:
                content = f.read()

            # Match pragma solidity statements - handle various formats:
            # pragma solidity 0.8.26;
            # pragma solidity ^0.8.0;
            # pragma solidity >=0.8.0;
            # pragma solidity >=0.8.0 <0.9.0;
            pattern = r"pragma\s+solidity\s+([^;]+);"
            match = re.search(pattern, content, re.IGNORECASE)

            if match:
                version_spec = match.group(1).strip()
                self.log(
                    f"Found pragma solidity {version_spec} in {solidity_file}",
                    "DEBUG"
                )
                return version_spec

            self.log(f"No pragma solidity found in {solidity_file}", "DEBUG")
            return None

        except Exception as e:
            self.log(f"Failed to parse pragma from {solidity_file}: {e}", "WARNING")
            return None

    def resolve_solc_version_for_pragma(
        self, pragma_spec: str, preferred_version: Optional[str] = None
    ) -> Optional[str]:
        """
        Resolve a specific solc version that satisfies a pragma specification.

        Args:
            pragma_spec: Pragma version specification (e.g., "0.8.26", "^0.8.0", ">=0.8.0 <0.8.6")
            preferred_version: Project-wide solc

        Returns:
            Specific solc version in conf format (e.g., "solc-0.8.26") or None
        """
        version = resolve_pragma_to_version(pragma_spec, preferred_version=preferred_version)
        if version:
            return f"solc-{version}"
        return None

    def generate_compiler_map_for_contracts(
        self, contracts_and_files: List[tuple]
    ) -> Dict[str, str]:
        """
        Generate compiler_map for contracts with different version requirements.

        Args:
            contracts_and_files: List of (contract_name, file_path) tuples

        Returns:
            Dictionary mapping contract names to solc versions in conf format
        """
        compiler_map = {}

        for contract_name, file_path in contracts_and_files:
            pragma_spec = self.parse_pragma_solidity(file_path)

            if pragma_spec:
                solc_version = self.resolve_solc_version_for_pragma(pragma_spec)

                if solc_version:
                    formatted_solc = self.format_solc(solc_version)
                    compiler_map[contract_name] = formatted_solc
                    self.log(
                        f"Mapped {contract_name} -> {formatted_solc} (pragma: {pragma_spec})",
                        "DEBUG"
                    )
                else:
                    self.log(
                        f"Could not resolve solc version for {contract_name} with pragma {pragma_spec}",
                        "WARNING"
                    )
            else:
                self.log(
                    f"No pragma found in {file_path} for {contract_name}, will use default solc version",
                    "DEBUG"
                )

        self.log(f"Generated compiler_map with {len(compiler_map)} entries")
        return compiler_map

    def extract_contract_name_from_config(self, config_file: Path) -> str:
        """
        Extract contract name from a configuration file.

        Args:
            config_file: Path to configuration file

        Returns:
            Contract name extracted from the config
        """
        try:
            with config_file.open("r") as f:
                config = json5.load(f)

            # Try to extract from verify field first
            verify = config.get("verify", "")
            if ":" in verify:
                contract_name = verify.split(":", 1)[0]
                return contract_name

            # Fallback to parametric_contracts if available
            parametric = config.get("parametric_contracts", "")
            if isinstance(parametric, list) and parametric:
                return parametric[0]
            elif isinstance(parametric, str) and parametric:
                return parametric

            # Final fallback to filename
            return config_file.stem

        except Exception as e:
            self.log(
                f"Failed to extract contract name from {config_file}: {e}",
                "WARNING"
            )
            return config_file.stem

    def read_file_content(self, file_path: Path) -> FileContent:
        """
        Read file and return as FileContent.

        Args:
            file_path: Path to file to read

        Returns:
            FileContent object
        """
        return FileContent.from_file(file_path)

    @staticmethod
    def print_cache_status(project_root: Path) -> None:
        """
        Print cache status information to console.

        Args:
            project_root: Root directory of the project
        """
        from .prover_runner import ProverRunner

        print("=== CACHE STATUS ===")

        try:
            cache_status = ProverRunner.get_cache_status(project_root)

            if cache_status["cache_exists"]:
                print(f"Cache directory: {cache_status['cache_dir']}")
                print(f"Total entries: {cache_status['total_entries']}")
                print(f"Total size: {cache_status['total_size_bytes']:,} bytes")

                if cache_status["entries"]:
                    print("\nCached jobs:")
                    for entry in cache_status["entries"][:10]:  # Show first 10 entries
                        cached_at = entry.get("cached_at")
                        if cached_at:
                            import time

                            cached_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(cached_at)
                            )
                        else:
                            cached_time = "Unknown"

                        status_symbol = "✅" if entry["success"] else "❌"
                        print(
                            f"  {status_symbol} {entry['contract_name']}:{entry['phase']} [{entry['runner_type']}] - {cached_time}"
                        )

                    if cache_status["total_entries"] > 10:
                        print(
                            f"  ... and {cache_status['total_entries'] - 10} more entries"
                        )
                else:
                    print("No cached entries found.")
            else:
                print("No cache directory found.")

        except Exception as e:
            print(f"Error getting cache status: {e}")

    @staticmethod
    def clear_cache_and_print(project_root: Path) -> None:
        """
        Clear cache and print results to console.

        Args:
            project_root: Root directory of the project
        """
        from .prover_runner import ProverRunner

        print("=== CLEARING CACHE ===")

        try:
            clear_result = ProverRunner.clear_cache(project_root)

            if clear_result["cache_existed"]:
                print(f"✅ {clear_result['message']}")
                if clear_result["files_removed"] > 0:
                    size_mb = clear_result["bytes_freed"] / (1024 * 1024)
                    print(f"   Freed {size_mb:.1f} MB of disk space")
            else:
                print("ℹ️  No cache directory found - nothing to clear")

        except Exception as e:
            print(f"❌ Error clearing cache: {e}")
