#!/usr/bin/env python3
"""
Build System Manager - Abstract base class for build system managers.

Provides common functionality for config file discovery, auto-detection,
and artifact management. Concrete managers only implement build-system-specific
parsing and artifact filtering.
"""

import json
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional, Set

from certora_autosetup.build_systems.base import BuildSystemConfig


class BuildSystemManager(ABC):
    """
    Abstract base class for build system managers.

    Provides common functionality for config file discovery, auto-detection,
    compilation, and artifact management. Concrete managers only implement
    build-system-specific parsing and command generation.
    """

    def __init__(self, project_root: Path, scope, component_name: str, run_root: Optional[Path] = None):
        """
        Initialize build system manager.

        Args:
            project_root: Directory the build config is anchored on — where config discovery
                starts and artifacts are read from. In a monorepo this is the sub-project that
                owns the main contract, not the run root.
            scope: Centralized scope for consistent filtering
            component_name: Name for logging (e.g. "FoundryManager", "HardhatManager")
            run_root: Directory certoraRun is invoked from. Remapping contexts are expressed
                against it and the hoisted-package walk is bounded by it. Defaults to
                project_root, which is correct whenever the build config sits at the run root.
        """
        self.project_root = project_root
        self.run_root = run_root or project_root
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

    @staticmethod
    @abstractmethod
    def holds_artifacts(artifacts_dir: Path) -> bool:
        """
        Whether *artifacts_dir* holds output written by this build system.

        Recognises the build system's own layout inside the directory, so a directory that
        merely exists under the expected name does not pass for a built project. That
        happens for real: a project whose configured output dir is nested (Foundry's
        ``out = "out/foundry"``) has a bare ``out/`` holding only subdirectories, and a
        project that shipped a second build config often has an empty artifact dir left by
        the tool that no longer runs.

        Args:
            artifacts_dir: Directory to inspect; need not exist

        Returns:
            True if the directory holds this build system's artifacts
        """
        pass

    @staticmethod
    @abstractmethod
    def recorded_source(artifact: dict) -> Optional[str]:
        """
        The source path *artifact* records having been compiled from, or None.

        Every build system stamps this into its artifacts, under its own key and in its own
        frame: some relative to the project that ran the build, some absolute. Callers get
        the value as written and decide what to do with it; ``artifacts_belong_to`` is the
        one that cares. None covers both a payload this build system did not write (a
        sidecar or a build-info file caught by the same directory walk) and one that records
        no source at all.

        Args:
            artifact: Parsed JSON of a single artifact file

        Returns:
            Source path as recorded, or None if this artifact records none
        """
        pass

    @classmethod
    def artifacts_belong_to(cls, config_dir: Path, artifacts_dir: Path, limit: int = 20) -> bool:
        """
        Whether the artifacts in *artifacts_dir* were written by the project at *config_dir*.

        Two configs can name the same physical artifact directory — a root ``foundry.toml``
        with ``out = 'pkg/out'`` next to ``pkg/foundry.toml`` with the default ``out`` — and
        only one of them ran. The artifacts settle it: the source path each one records
        resolves against the project that produced it, so if none of them lands inside
        *config_dir*, these are somebody else's artifacts and this is the wrong frame to
        read them in.

        An absolute recorded path is tested for containment; a relative one is resolved
        against *config_dir* and tested for existence. Sampling stops at the first artifact
        that answers, so the common case reads one file.

        Artifacts that record no source at all (older Foundry, metadata stripped) answer
        True: absence of evidence leaves the caller where it was.

        Args:
            config_dir: Candidate project directory
            artifacts_dir: Directory holding this build system's artifacts
            limit: How many artifacts to read before giving up on finding a recorded source

        Returns:
            True if these artifacts are this project's, or record nothing to judge by
        """
        read = 0
        for json_file in artifacts_dir.rglob("*.json"):
            if read >= limit:
                break
            try:
                with json_file.open() as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            source = cls.recorded_source(data)
            if source is None:
                continue
            read += 1
            candidate = Path(source)
            if candidate.is_absolute():
                if candidate.is_relative_to(config_dir):
                    return True
            elif (config_dir / candidate).exists():
                return True
        return read == 0

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

