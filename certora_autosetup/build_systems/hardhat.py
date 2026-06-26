#!/usr/bin/env python3
"""
Hardhat Manager - Manages Hardhat project configuration, compilation, and artifacts.

Parallel to FoundryManager but adapted for Hardhat's JavaScript/TypeScript config structure.
"""

import json
import subprocess
import sys
from typing import Dict, Any, List, Optional
from pathlib import Path
from dataclasses import dataclass

from certora_autosetup.build_systems.base import BuildSystemConfig
from certora_autosetup.build_systems.manager import BuildSystemManager


@dataclass
class HardhatConfig(BuildSystemConfig):
    """Parsed Hardhat configuration with resolved settings."""

    # Hardhat-specific fields (common fields inherited from BuildSystemConfig)
    artifacts: Optional[str] = None  # Default: "artifacts"
    cache: Optional[str] = None  # Default: "cache"

    # Paths from config
    paths: Optional[Dict[str, str]] = None

    # Configuration type
    config_type: str = "javascript"  # "javascript" or "typescript"

    def __post_init__(self):
        """Initialize default values for mutable fields."""
        # Call parent class initialization for common fields
        super().__post_init__()

        # Initialize Hardhat-specific defaults
        if self.src is None:
            self.src = "contracts"
        if self.artifacts is None:
            self.artifacts = "artifacts"
        if self.cache is None:
            self.cache = "cache"
        if self.paths is None:
            self.paths = {}

    def to_certora_dict(
        self,
        convert_solc_to_certora_format: bool = True,
        include_packages: bool = True
    ) -> Dict[str, Any]:
        """
        Convert Hardhat config to Certora format.

        Args:
            convert_solc_to_certora_format: Whether to convert "0.8.19" to "solc8.19" format
            include_packages: Whether to include packages/remappings (ignored for Hardhat)

        Returns:
            Dictionary with Certora config format
        """
        # Apply common settings (solc, optimizer, via_ir) using base class helper
        # Hardhat doesn't have packages, so include_packages is ignored
        return self._apply_common_solc_settings(convert_solc_to_certora_format)

    def get_artifact_directory(self) -> str:
        """Return Hardhat artifact directory."""
        return self.artifacts or "artifacts"


class HardhatManager(BuildSystemManager):
    """
    Hardhat project manager with support for configuration, compilation, and artifacts.

    Parallel to FoundryManager but adapted for Hardhat's JavaScript/TypeScript ecosystem.
    """

    def __init__(self, project_root: Path, scope):
        """
        Initialize Hardhat manager.

        Args:
            project_root: Root directory of the project
            scope: Centralized scope for consistent filtering
        """
        super().__init__(project_root, scope, "HardhatManager")

    def get_config_filenames(self) -> List[str]:
        """Return list of config filenames to search for."""
        return ["hardhat.config.js", "hardhat.config.ts"]

    def parse_config(self, config_file: Path, profile: str | None = None) -> HardhatConfig:
        """
        Parse hardhat.config.{js,ts} file using a Node.js extraction script.

        Creates a temporary script that loads the Hardhat config and outputs
        the relevant settings as JSON.

        Args:
            config_file: Path to hardhat.config.js or hardhat.config.ts

        Returns:
            HardhatConfig with parsed settings
        """
        # Determine config type (before the try so it's available in the except handler)
        config_type = "typescript" if config_file.name.endswith(".ts") else "javascript"
        try:
            self.log(f"Parsing Hardhat config from {config_file}")

            # Try to extract config using a Node.js script
            config_data = self._extract_config_via_node(config_file)
            if config_data:
                return self._extract_config_from_json(config_data, config_type)

            # If extraction fails, use defaults
            return self._get_default_config(config_type)

        except Exception as e:
            self.log(f"Failed to parse Hardhat config: {e}", "WARNING")
            return self._get_default_config(config_type)

    def _extract_config_via_node(self, config_file: Path) -> Optional[Dict[str, Any]]:
        """
        Extract Hardhat config using Node.js extraction script with Hardhat HRE.

        Uses the Hardhat Runtime Environment (HRE) API to load the resolved configuration.
        The script must run from the project directory to access the hardhat.config.js.

        Args:
            config_file: Path to hardhat.config.js or hardhat.config.ts

        Returns:
            Dict with config data, or None if extraction fails
        """
        try:
            # Path to the extraction script (same directory as this file)
            extractor_script = Path(__file__).parent / "hardhat_config_extractor.js"

            if not extractor_script.exists():
                self.log(f"Config extractor script not found: {extractor_script}", "WARNING")
                return None

            # Run the extraction script from the directory containing the config file
            # The script uses require("hardhat") which needs to be run from the directory
            # where hardhat.config.js is located (to find node_modules/hardhat)
            result = subprocess.run(
                ["node", str(extractor_script)],
                cwd=config_file.parent,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and result.stdout.strip():
                config_data = json.loads(result.stdout.strip())
                if config_data:  # Has actual data
                    return config_data
                else:  # Empty object {}
                    self.log("Config extraction returned empty object - may indicate extraction failure", "WARNING")
            elif result.returncode != 0:
                self.log(f"Config extraction failed with exit code {result.returncode}", "WARNING")
            else:
                self.log("Config extraction produced no output", "WARNING")

            if result.stderr:
                self.log(f"Config extraction stderr: {result.stderr.strip()}", "DEBUG")

            return None

        except FileNotFoundError:
            self.log("node command not found. Hardhat requires Node.js.", "WARNING")
            return None
        except subprocess.TimeoutExpired:
            self.log("Config extraction timed out", "WARNING")
            return None
        except json.JSONDecodeError as e:
            self.log(f"Failed to parse extracted config JSON: {e}", "WARNING")
            return None
        except Exception as e:
            self.log(f"Error extracting config via Node.js: {e}", "WARNING")
            return None

    def _process_path(self, path_value: str, default: str, path_type: str) -> str:
        """
        Process a path from Hardhat config (handle absolute/relative, validate existence).

        Args:
            path_value: Path from config (can be absolute or relative)
            default: Default value if processing fails
            path_type: Description for logging (e.g., "sources", "artifacts")

        Returns:
            Processed path (relative to project_root if originally absolute)
        """
        if not path_value:
            return default

        path_obj = Path(path_value)

        # Convert absolute paths to relative (relative to project_root)
        if path_obj.is_absolute():
            try:
                # Make relative to project_root
                relative_path = path_obj.relative_to(self.project_root)
                processed = str(relative_path)

                # Validate the path exists
                if not (self.project_root / relative_path).exists():
                    self.log(
                        f"Hardhat config {path_type} path does not exist: {path_value}",
                        "WARNING"
                    )

                return processed
            except ValueError:
                # Path is outside project_root
                self.log(
                    f"Hardhat config {path_type} path is outside project root: {path_value}",
                    "WARNING"
                )
                return default
        else:
            # Relative path - validate it exists
            if not (self.project_root / path_obj).exists():
                self.log(
                    f"Hardhat config {path_type} path does not exist: {path_value}",
                    "WARNING"
                )
            return str(path_obj)

    def _extract_config_from_json(self, config_data: Dict[str, Any], config_type: str) -> HardhatConfig:
        """
        Extract HardhatConfig from Hardhat's JSON config output.

        Args:
            config_data: Parsed JSON from 'npx hardhat config --json'
            config_type: "javascript" or "typescript"

        Returns:
            HardhatConfig with extracted settings
        """
        # Extract solidity settings
        solidity = config_data.get("solidity", {})

        # Solidity can be a string (version) or object (settings)
        if isinstance(solidity, str):
            solc_version = solidity
            optimizer = False
            optimizer_runs = 200
            via_ir = False
        elif isinstance(solidity, dict):
            # Hardhat config structure: {"compilers": [{"version": "0.8.28", "settings": {...}}], ...}
            # Or simple format: {"version": "0.8.28", "settings": {...}}

            # Check for compilers array (standard Hardhat structure)
            if "compilers" in solidity and isinstance(solidity["compilers"], list) and len(solidity["compilers"]) > 0:
                # Check if there are multiple different compiler versions
                compiler_versions = [c.get("version") for c in solidity["compilers"] if c.get("version")]
                unique_versions = set(compiler_versions)

                if len(unique_versions) > 1:
                    # Multiple different versions - don't set a global solc_version
                    # The workaround system will handle this with compiler_map
                    solc_version = None
                    self.log(f"Multiple compiler versions detected: {unique_versions}. Using compiler_map.", "INFO")
                else:
                    # Single version (or all same version) - use it as global
                    solc_version = compiler_versions[0] if compiler_versions else None

                # Use first compiler for settings
                compiler = solidity["compilers"][0]
                settings = compiler.get("settings", {})
            else:
                # Simple format
                solc_version = solidity.get("version")
                settings = solidity.get("settings", {})

            optimizer_settings = settings.get("optimizer", {})
            optimizer = optimizer_settings.get("enabled", False)
            optimizer_runs = optimizer_settings.get("runs", 200)
            via_ir = settings.get("viaIR", False)
        else:
            solc_version = None
            optimizer = False
            optimizer_runs = 200
            via_ir = False

        # Extract and process paths
        paths = config_data.get("paths", {})
        src = self._process_path(
            paths.get("sources", "contracts"),
            "contracts",
            "sources"
        )
        artifacts = self._process_path(
            paths.get("artifacts", "artifacts"),
            "artifacts",
            "artifacts"
        )
        cache = self._process_path(
            paths.get("cache", "cache"),
            "cache",
            "cache"
        )

        self.log(f"Extracted Hardhat config: solc={solc_version}, optimizer={optimizer}")

        return HardhatConfig(
            solc_version=solc_version,
            optimizer=optimizer,
            optimizer_runs=optimizer_runs,
            via_ir=via_ir,
            src=src,
            artifacts=artifacts,
            cache=cache,
            paths=paths,
            config_type=config_type
        )

    def _get_default_config(self, config_type: str = "javascript") -> HardhatConfig:
        """
        Get default Hardhat configuration when parsing fails.

        Args:
            config_type: "javascript" or "typescript"

        Returns:
            HardhatConfig with default settings
        """
        self.log("Using default Hardhat configuration", "INFO")
        return HardhatConfig(
            solc_version=None,  # Let Certora auto-detect
            optimizer=False,
            optimizer_runs=200,
            via_ir=False,
            src="contracts",
            artifacts="artifacts",
            cache="cache",
            config_type=config_type
        )

    def get_default_artifact_dir(self) -> str:
        """Return default Hardhat artifact directory."""
        return "artifacts"

    def get_build_command(self, profile: Optional[str] = None) -> str:
        """Return Hardhat build command."""
        return "npx hardhat compile"

    def filter_artifacts(self, artifacts_dir: Path) -> List[Path]:
        """
        Filter Hardhat artifacts - only contracts/, exclude .dbg.json and build-info/.

        Filters out:
        - .dbg.json files (Hardhat debug metadata)
        - build-info/ directory files
        - Only includes artifacts/contracts/**/*.json

        Args:
            artifacts_dir: Path to artifacts directory

        Returns:
            List of artifact file paths
        """
        # Only look in artifacts/contracts/ subdirectory
        contracts_dir = artifacts_dir / "contracts"
        if not contracts_dir.exists():
            return []

        return self._walk_and_filter_artifacts(
            contracts_dir,
            skip_dirs={"build-info"},
            file_filter=lambda f: f.endswith(".json") and not f.endswith(".dbg.json")
        )

