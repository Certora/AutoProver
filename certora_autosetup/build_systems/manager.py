#!/usr/bin/env python3
"""
Build System Manager - Abstract base class for build system managers.

Provides common functionality for config file discovery, auto-detection,
and artifact management. Concrete managers only implement build-system-specific
parsing and artifact filtering.
"""

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Set

from certora_autosetup.build_systems.base import BuildSystemConfig


class BuildSystemManager(ABC):
    """
    Abstract base class for build system managers.

    Provides common functionality for config file discovery, auto-detection,
    compilation, and artifact management. Concrete managers only implement
    build-system-specific parsing and command generation.
    """

    def __init__(self, project_root: Path, scope, component_name: str):
        """
        Initialize build system manager.

        Args:
            project_root: Root directory of the project
            scope: Centralized scope for consistent filtering
            component_name: Name for logging (e.g. "FoundryManager", "HardhatManager")
        """
        self.project_root = project_root
        self.scope = scope
        self.component = component_name

    def log(self, message: str, level: str = "INFO"):
        """Log message using centralized logger."""
        # Import here to avoid circular dependency
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from certora_autosetup.utils.logger import logger
        logger.log(message, level, self.component)

    @abstractmethod
    def get_config_filenames(self) -> List[str]:
        """
        Return list of config filenames to search for.

        Examples:
            - Foundry: ["foundry.toml"]
            - Hardhat: ["hardhat.config.js", "hardhat.config.ts"]

        Returns:
            List of config filenames
        """
        pass

    @abstractmethod
    def parse_config(self, config_file: Path, profile: str | None = None) -> BuildSystemConfig:
        """
        Parse a specific config file (build-system specific logic).

        Args:
            config_file: Path to config file
            profile: Optional build profile (used by Foundry)

        Returns:
            Parsed BuildSystemConfig (FoundryConfig, HardhatConfig, etc.)
        """
        pass

    @abstractmethod
    def get_default_artifact_dir(self) -> str:
        """
        Return default artifact directory name.

        Examples:
            - Foundry: "out"
            - Hardhat: "artifacts"

        Returns:
            Directory name (relative to project root)
        """
        pass

    @abstractmethod
    def get_build_command(self, profile: str | None = None) -> str:
        """
        Return the build command for this build system.

        Examples:
            - Foundry: "forge build"
            - Hardhat: "npx hardhat compile"

        Returns:
            Build command string
        """
        pass

    @abstractmethod
    def filter_artifacts(self, artifacts_dir: Path) -> List[Path]:
        """
        Filter artifacts based on build system conventions.

        Implemented using os.walk for explicit directory traversal with pruning.
        Each build system has different filtering logic:
            - Foundry: Include all .json files except build-info/
            - Hardhat: Include .json files in contracts/, exclude .dbg.json and build-info/

        Args:
            artifacts_dir: Path to artifacts directory

        Returns:
            List of artifact file paths
        """
        pass

    def find_config_file(self) -> Path | None:
        """
        Find the build system config file by searching upward from project root.

        Checks project_root first, then walks up to 3 parent levels.
        Returns the first config file found.
        """
        config_filenames = self.get_config_filenames()
        current_dir = self.project_root
        for _ in range(4):  # project_root + 3 parent levels
            for config_name in config_filenames:
                config_file = current_dir / config_name
                if config_file.exists():
                    self.log(f"Found config file: {config_file}")
                    return config_file
            current_dir = current_dir.parent
        return None

    def _walk_and_filter_artifacts(
        self,
        base_dir: Path,
        skip_dirs: Set[str],
        file_filter: Callable[[str], bool]
    ) -> List[Path]:
        """
        Common os.walk pattern for artifact filtering.

        Template helper method that traverses a directory tree and filters files
        based on provided criteria. Used by concrete managers in filter_artifacts().

        Args:
            base_dir: Base directory to start traversal
            skip_dirs: Set of directory names to skip during traversal
            file_filter: Callable that returns True if a filename should be included

        Returns:
            List of Path objects for files matching the filter
        """
        artifact_files = []
        for root, dirs, files in os.walk(base_dir):
            # Prune directories we don't want to traverse
            dirs[:] = [d for d in dirs if d not in skip_dirs]

            for filename in files:
                if file_filter(filename):
                    artifact_files.append(Path(root) / filename)

        return artifact_files

    def auto_detect_config(self, profile: str | None = None) -> BuildSystemConfig:
        """
        Find the config file and return parsed config.

        Args:
            profile: Optional build profile (used by Foundry)

        Returns:
            Parsed BuildSystemConfig

        Raises:
            Exception: If no config file is found
        """
        config_file = self.find_config_file()
        if config_file is None:
            raise Exception(f"No {self.component} config file found in project")
        self.log(f"Auto-detected config: {config_file}")
        return self.parse_config(config_file, profile=profile)

