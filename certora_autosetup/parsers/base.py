#!/usr/bin/env python3
"""
Abstract base class for contract extractors.

This module provides the ContractExtractor ABC that extracts common logic from
build-system-specific contract extraction implementations (Foundry, Hardhat).

Leverages BuildSystemManager and BuildSystemConfig for build system details,
requiring only build-system-specific artifact parsing logic.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Set

from certora_autosetup.setup.solidity_utils import find_all_solidity_files
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


class ContractExtractor(ABC):
    """
    Abstract base class for extracting contract information from build artifacts.

    Leverages BuildSystemManager and BuildSystemConfig for build system details.
    Concrete extractors only need to implement build-system-specific artifact parsing.
    """

    def __init__(self, project_root: Path, manager_class, profile: Optional[str] = None):
        """
        Initialize contract extractor.

        Args:
            project_root: Root directory of the project
            manager_class: BuildSystemManager class (FoundryManager or HardhatManager)
            profile: Build system profile to use (e.g. Foundry profile name)
        """
        self.project_root = project_root
        self.profile = profile

        # Create minimal scope for manager
        class MinimalScope:
            def is_file_in_scope(self, file_path):
                return True

        self.manager = manager_class(project_root, MinimalScope())

    def log(self, message: str, level: str = "INFO"):
        """Log message using component name from manager."""
        logger.log(message, level, self.manager.component)

    def project_source_files(self) -> Set[str]:
        """Normalized set of project-local Solidity source paths (excludes
        dependencies, tests, scripts, and certora/ directories)."""
        return {
            str(Path(p)) for p in find_all_solidity_files(
                include_test_files=False,
                include_dependencies=False,
                verbose=False,
            )
        }

    @abstractmethod
    def extract_logic_contracts_impl(self, artifacts_dir: Path) -> List[ContractHandle]:
        """
        Build-system-specific implementation of contract extraction.

        Reads artifact JSON files and returns one ContractHandle per (source_file,
        contract_name) pair that has non-empty bytecode. source_file is the relative
        source path (build-system convention; typically project-relative POSIX).

        Args:
            artifacts_dir: Path to artifacts directory

        Returns:
            List of ContractHandle, one per unique (source_file, contract_name).
        """
        pass

    def extract_logic_contracts(self) -> List[ContractHandle]:
        """Extract logic contracts from build artifacts with config fallback."""
        # Get default artifact directory from manager
        default_artifact_dir = self.project_root / self.manager.get_default_artifact_dir()

        artifacts_dir = default_artifact_dir

        # Config file fallback: use manager's auto_detect_config()
        if not artifacts_dir.exists():
            artifacts_dir = self._try_read_artifact_dir_from_config()
            if not artifacts_dir or not artifacts_dir.exists():
                raise Exception(
                    f"{self.manager.component} artifacts directory '{artifacts_dir}' does not exist. "
                    f"Please run '{self.manager.get_build_command(profile=self.profile)}' first."
                )

        if not artifacts_dir.is_dir():
            raise Exception(f"'{artifacts_dir}' exists but is not a directory")

        # Delegate to build-system-specific implementation
        return self.extract_logic_contracts_impl(artifacts_dir)

    def _try_read_artifact_dir_from_config(self) -> Optional[Path]:
        """
        Try to read artifact directory from config using manager's auto_detect_config().

        Returns artifact directory Path or None if reading failed.
        """
        try:
            # Use manager's auto_detect_config() which handles finding and parsing config
            config = self.manager.auto_detect_config(profile=self.profile)

            # Use BuildSystemConfig's get_artifact_directory() method
            artifact_dir_str = config.get_artifact_directory()

            if artifact_dir_str:
                artifact_dir = Path(artifact_dir_str)
                self.log(f"Using artifact directory from config: {artifact_dir}")
                return self.project_root / artifact_dir

        except Exception as e:
            self.log(f"Failed to read artifact directory from config: {e}", "WARNING")

        return None

    def extract_logic_contracts_and_files(self) -> List[ContractHandle]:
        """Return one ``ContractHandle`` per (source_file, contract_name) for each
        concrete contract with non-empty bytecode found in the project's source
        tree (interfaces, abstract contracts, libraries, dependencies, and
        test/script files are excluded).
        """
        handles = self.extract_logic_contracts()

        if not handles:
            raise Exception(
                f"No project-local logic contracts found in {self.manager.component} build output"
            )

        contract_handles = sorted(
            handles, key=lambda h: (h.source_file, h.contract_name)
        )
        distinct_files = {h.source_file for h in contract_handles}
        self.log(
            f"Found {len(contract_handles)} logic contracts in {len(distinct_files)} files "
            f"from {self.manager.component}"
        )

        self.log(f"Matched {len(contract_handles)} Solidity files to logic contracts")
        self.log(
            f"Contract handles: {[f'{ch.contract_name}@{ch.source_file}' for ch in contract_handles]}"
        )

        return contract_handles
