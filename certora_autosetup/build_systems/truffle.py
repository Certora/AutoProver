#!/usr/bin/env python3
"""
Truffle Manager - Manages Truffle project configuration and artifacts.

Parallel to FoundryManager/HardhatManager. Truffle predates both and is still what many
0.4.x/0.5.x projects ship: `truffle-config.js`, or `truffle.js` on Truffle v4. Both names
are recognised, and the compiler settings and `@scope/pkg/...` package roots are read from
whichever is present.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from packaging.version import InvalidVersion, Version

from certora_autosetup.build_systems.base import BuildSystemConfig
from certora_autosetup.build_systems.manager import BuildSystemManager
from certora_autosetup.utils.remappings import build_packages_from_remapping_sources


@dataclass
class TruffleConfig(BuildSystemConfig):
    """Parsed Truffle configuration with resolved settings."""

    # Truffle-specific fields (common fields inherited from BuildSystemConfig)
    contracts_build_directory: Optional[str] = None

    # node_modules-derived remappings; see to_certora_dict()
    packages: Optional[List[str]] = None

    def __post_init__(self):
        """Initialize default values for mutable fields."""
        super().__post_init__()

        if self.src is None:
            self.src = "contracts"
        if self.contracts_build_directory is None:
            self.contracts_build_directory = "build/contracts"
        if self.packages is None:
            self.packages = []

    def to_certora_dict(
        self,
        convert_solc_to_certora_format: bool = True,
        include_packages: bool = True
    ) -> Dict[str, Any]:
        """
        Convert Truffle config to Certora format.

        Args:
            convert_solc_to_certora_format: Whether to convert "0.8.19" to "solc8.19" format
            include_packages: Whether to include packages/remappings

        Returns:
            Dictionary with Certora config format
        """
        result = self._apply_common_solc_settings(convert_solc_to_certora_format)

        # Truffle resolves bare imports (`@scope/pkg/contracts/Foo.sol`) through node_modules
        # with no remapping file anywhere in the project, so the packages list built from
        # package.json is the only thing that lets solc find those sources.
        if include_packages and self.packages:
            result["packages"] = self._relativize_packages(self.packages)

        return result

    def get_artifact_directory(self) -> str:
        """Return Truffle artifact directory."""
        return self.contracts_build_directory or "build/contracts"


class TruffleManager(BuildSystemManager):
    """Truffle project manager: config parsing and artifact discovery."""

    def __init__(self, project_root: Path, scope):
        """
        Initialize Truffle manager.

        Args:
            project_root: Root directory of the project
            scope: Centralized scope for consistent filtering
        """
        super().__init__(project_root, scope, "TruffleManager")

    def get_config_filenames(self) -> List[str]:
        """Return list of config filenames to search for."""
        return ["truffle-config.js", "truffle.js"]

    def get_default_artifact_dir(self) -> str:
        """Return default artifact directory name."""
        return "build/contracts"

    def get_build_command(self, profile: str | None = None) -> str:
        """Return the build command for this build system."""
        return "npx truffle compile"

    def filter_artifacts(self, artifacts_dir: Path) -> List[Path]:
        """Return Truffle's artifact JSONs — one flat `<ContractName>.json` per contract."""
        return self._walk_and_filter_artifacts(
            base_dir=artifacts_dir,
            skip_dirs=set(),
            file_filter=lambda filename: filename.endswith(".json"),
        )

    def parse_config(self, config_file: Path, profile: str | None = None) -> TruffleConfig:
        """
        Parse truffle-config.js / truffle.js via a Node.js extraction script.

        Args:
            config_file: Path to truffle-config.js or truffle.js
            profile: Unused (Truffle has no profile concept); kept for interface parity

        Returns:
            TruffleConfig with parsed settings
        """
        try:
            self.log(f"Parsing Truffle config from {config_file}")
            config_data = self._extract_config_via_node(config_file)
            config = self._extract_config_from_json(config_data or {}, config_file.parent)
        except Exception as e:
            self.log(f"Failed to parse Truffle config: {e}", "WARNING")
            config = TruffleConfig()

        # Independent of whether the config itself evaluated: the packages list comes from
        # package.json/node_modules, which is what Truffle's own resolver uses.
        packages = build_packages_from_remapping_sources(base_dir=config_file.parent, log_fn=self.log)
        if packages:
            config.packages = packages

        self.log(
            f"Parsed truffle config: solc={config.solc_version}, optimizer={config.optimizer}, "
            f"packages={len(config.packages or [])}"
        )
        return config

    def _extract_config_via_node(self, config_file: Path) -> Optional[Dict[str, Any]]:
        """
        Evaluate the config with Node.js and return its compiler-relevant fields.

        Returns:
            Dict with config data, or None if extraction failed
        """
        extractor_script = Path(__file__).parent / "truffle_config_extractor.js"

        if not extractor_script.exists():
            self.log(f"Config extractor script not found: {extractor_script}", "WARNING")
            return None

        try:
            # Run from the config's own directory so a config that requires project
            # dependencies (`require('<shared-truffle-config-pkg>')`) resolves them.
            result = subprocess.run(
                ["node", str(extractor_script), str(config_file)],
                cwd=config_file.parent,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            self.log("node command not found; cannot read Truffle config", "WARNING")
            return None
        except subprocess.TimeoutExpired:
            self.log("Config extraction timed out", "WARNING")
            return None

        if result.stderr:
            self.log(f"Config extraction stderr: {result.stderr.strip()}", "DEBUG")

        if result.returncode != 0:
            self.log(f"Config extraction failed with exit code {result.returncode}", "WARNING")
            return None

        if not result.stdout.strip():
            self.log("Config extraction produced no output", "WARNING")
            return None

        try:
            config_data = json.loads(result.stdout.strip())
        except json.JSONDecodeError as e:
            self.log(f"Failed to parse extracted config JSON: {e}", "WARNING")
            return None

        if config_data.get("error"):
            self.log(
                f"Could not evaluate {config_file.name} ({config_data['error']}); using Truffle defaults",
                "WARNING",
            )
            return None

        return config_data

    def _extract_config_from_json(self, config_data: Dict[str, Any], config_dir: Path) -> TruffleConfig:
        """
        Build a TruffleConfig from the extractor's JSON output.

        Args:
            config_data: Parsed JSON from truffle_config_extractor.js
            config_dir: Directory holding the config file (paths are relative to it)

        Returns:
            TruffleConfig with extracted settings
        """
        solc_block = (config_data.get("compilers") or {}).get("solc") or {}
        settings = solc_block.get("settings") or {}

        # Truffle v4 has no `compilers` block: its settings live under a top-level `solc`
        # key and carry no version at all (the compiler was bundled with truffle).
        optimizer_settings = settings.get("optimizer") or (config_data.get("solc") or {}).get("optimizer") or {}

        return TruffleConfig(
            solc_version=self._parse_solc_version(solc_block.get("version")),
            optimizer=bool(optimizer_settings.get("enabled", False)),
            optimizer_runs=int(optimizer_settings.get("runs", 200)),
            src=self._relative_to_config(config_data.get("contracts_directory"), config_dir, "contracts"),
            contracts_build_directory=self._relative_to_config(
                config_data.get("contracts_build_directory"), config_dir, "build/contracts"
            ),
        )

    def _parse_solc_version(self, raw_version: Any) -> Optional[str]:
        """Return an exact solc version to pin, or None to resolve from source pragmas.

        Truffle's `compilers.solc.version` also accepts non-pins — `"pragma"` (defer to the
        sources), `"native"` (whatever solc is on PATH), and docker/path specs. None of those
        name a version we can put in a conf, so they resolve from pragmas like an absent value.
        """
        if not raw_version or not isinstance(raw_version, str):
            return None

        version = raw_version.strip()
        try:
            parsed = Version(version)
        except InvalidVersion:
            self.log(f"Truffle solc version '{version}' is not an exact version; resolving from pragmas", "INFO")
            return None

        if len(parsed.release) != 3:
            self.log(f"Truffle solc version '{version}' is not a full x.y.z pin; resolving from pragmas", "INFO")
            return None

        return version

    def _relative_to_config(self, raw_path: Any, config_dir: Path, default: str) -> str:
        """Normalize a Truffle directory setting to a path relative to the config dir."""
        if not raw_path or not isinstance(raw_path, str):
            return default

        path = Path(raw_path)
        if not path.is_absolute():
            # Truffle configs habitually write './contracts'; Path normalizes that away.
            return str(path)

        try:
            return str(path.resolve().relative_to(config_dir.resolve()))
        except ValueError:
            self.log(f"Truffle path '{raw_path}' is outside the project; using '{default}'", "WARNING")
            return default
