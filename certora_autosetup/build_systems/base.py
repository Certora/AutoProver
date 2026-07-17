#!/usr/bin/env python3
"""
Base configuration class for all build systems.

Provides common fields and helper methods shared by Foundry, Hardhat, and other build systems.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, List

from certora_autosetup.utils.config_manager import convert_solc_version_to_certora_format
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


@dataclass
class BuildSystemConfig(ABC):
    """
    Abstract base configuration for all build systems.

    This class defines the common interface and shared fields that all build system
    configurations (Foundry, Hardhat, etc.) must implement.
    """

    # Common compiler settings (shared by all build systems)
    solc_version: Optional[str] = None
    optimizer: Optional[bool] = None
    optimizer_runs: int = 200
    via_ir: Optional[bool] = None
    # Declared EVM target (foundry `evm_version`, hardhat `settings.evmVersion`);
    # None means each solc's own default.
    evm_version: Optional[str] = None

    # Common source configuration
    src: Optional[str] = None

    @abstractmethod
    def to_certora_dict(
        self,
        convert_solc_to_certora_format: bool = True,
        include_packages: bool = True
    ) -> Dict[str, Any]:
        """
        Convert config to Certora-compatible dictionary format.

        This method encapsulates the conversion logic, eliminating the need
        for orchestrator to know about build system specifics.

        Args:
            convert_solc_to_certora_format: Whether to convert "0.8.19" to "solc8.19" format
            include_packages: Whether to include packages/remappings

        Returns:
            Dictionary with Certora config format:
            {
                "solc": "solc8.24",  # or "0.8.24" if convert_solc=False
                "solc_optimize": 200,
                "solc_via_ir": true,
                "solc_evm_version": "paris",  # only when the project declares one
                "packages": [...]
            }
        """
        pass

    @abstractmethod
    def get_artifact_directory(self) -> str:
        """
        Get the artifact output directory for this build system.

        Returns:
            Directory path: "out" for Foundry, "artifacts" for Hardhat
        """
        pass

    def apply_per_contract_settings(
        self,
        contracts: List[ContractHandle],
        config_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply build-system-specific per-contract compiler settings to config_dict.

        Default implementation is a no-op so callers can invoke this
        unconditionally — Foundry overrides it to apply its
        `compilation_restrictions`.
        TODO: add it for HardHat override block (or check if needed at all)

        Returns the (possibly mutated) config_dict for caller convenience.
        """
        return config_dict

    def __post_init__(self):
        """Initialize defaults for common fields."""
        if self.optimizer is None:
            self.optimizer = False
        if self.via_ir is None:
            self.via_ir = False

    def _apply_common_solc_settings(
        self,
        convert_solc_to_certora_format: bool = True
    ) -> Dict[str, Any]:
        """
        Apply common settings (solc, optimizer, via_ir).

        Args:
            convert_solc_to_certora_format: Whether to convert solc version format

        Returns:
            Dictionary with common settings applied
        """
        result = {}

        # Apply Solidity compiler version
        if self.solc_version:
            if convert_solc_to_certora_format:
                result["solc"] = convert_solc_version_to_certora_format(self.solc_version)
            else:
                result["solc"] = self.solc_version

        # Deliberately ignore the build system's own optimizer setting by default, even
        # when the project enables it. That setting tunes the project's normal build and
        # has nothing to do with what Certora's own compilation of the same sources needs;
        # inheriting it can break the prover (e.g. a huge optimizer_runs) or is simply
        # unnecessary. compilation_workarounds.py's escalation chain
        # (yul_exception_add_optimizer, stack_too_deep_via_ir) already re-adds solc_optimize
        # if compilation genuinely requires it, so we start unoptimized and let a real
        # failure bring it back. This applies to every build system (Foundry, Hardhat, ...).
        # Foundry's explicit per-contract compilation_restrictions path is separate and still
        # emits solc_optimize / solc_optimize_map — that is a deliberate signal from the project.
        if self.optimizer:
            logger.log(
                f"Ignoring {type(self).__name__}'s optimizer setting by default; compilation "
                "workarounds will re-enable it if compilation requires it",
                "INFO",
                type(self).__name__,
            )

        # Apply via_ir
        if self.via_ir:
            result["solc_via_ir"] = True

        # Honor the project's declared EVM target. Unlike the optimizer setting
        # above, this is not a tuning knob: solc's default EVM version can be
        # NEWER than the declared one (e.g. shanghai/PUSH0 vs a pinned "paris"),
        # producing bytecode for the wrong chain and different codegen (which can
        # even fail where the declared target compiles), and no reactive
        # workaround could guess the intended version. If the declared version is
        # rejected by the solc in use, the invalid_evm_version workaround drops it.
        if self.evm_version:
            result["solc_evm_version"] = self.evm_version

        return result

    def _relativize_packages(
        self,
        packages: List[str],
        project_root: Optional[Path] = None
    ) -> List[str]:
        """
        Convert absolute package paths to relative paths.

        Args:
            packages: List of packages in format "name=path"
            project_root: Project root path (defaults to cwd)

        Returns:
            List of packages with relative paths
        """
        if project_root is None:
            project_root = Path.cwd()

        result = []
        for package in packages:
            if "=" in package:
                name, path = package.split("=", 1)
                try:
                    relative_path = Path(path).relative_to(project_root)
                    result.append(f"{name}={relative_path}")
                except ValueError:
                    # Path is outside project root, use absolute path as fallback
                    result.append(package)
            else:
                result.append(package)

        return result
