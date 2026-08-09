#!/usr/bin/env python3
"""
Build System Detector - Auto-detects build system (Foundry, Hardhat, etc.)

Provides unified interface for working with different build systems.
"""

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Optional, Type

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.build_systems.hardhat import HardhatManager
from certora_autosetup.build_systems.manager import BuildSystemManager
from certora_autosetup.build_systems.truffle import TruffleManager
from certora_autosetup.parsers.base import ContractExtractor
from certora_autosetup.parsers.foundry import FoundryContractExtractor
from certora_autosetup.parsers.hardhat import HardhatContractExtractor
from certora_autosetup.parsers.truffle import TruffleContractExtractor



class BuildSystem(Enum):
    """Supported build systems."""
    FOUNDRY = "foundry"
    HARDHAT = "hardhat"
    TRUFFLE = "truffle"
    UNKNOWN = "unknown"


class BuildSystemDetector:
    """
    Detects and provides unified interface for build systems.
    """

    @staticmethod
    def detect(project_root: Path) -> BuildSystem:
        """
        Auto-detect build system from project structure.

        Detection logic (in order of precedence):
        1. Check for foundry.toml → FOUNDRY
        2. Check for hardhat.config.js or hardhat.config.ts → HARDHAT
        3. Check for truffle-config.js or truffle.js → TRUFFLE
        4. Check for package.json with a hardhat/truffle dependency → HARDHAT / TRUFFLE
        5. Check for artifact directory structures → FOUNDRY, HARDHAT or TRUFFLE
        6. Return UNKNOWN if none found

        Truffle ranks below the other two throughout: a repo migrating off Truffle keeps its
        stale truffle-config.js next to the foundry.toml/hardhat.config that now drives it.

        Args:
            project_root: Root directory of the project

        Returns:
            BuildSystem enum value
        """
        # Import logger here to avoid circular dependency
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from certora_autosetup.utils.logger import logger

        # 1. Config file detection (most reliable)
        foundry_toml = project_root / "foundry.toml"
        hardhat_config_js = project_root / "hardhat.config.js"
        hardhat_config_ts = project_root / "hardhat.config.ts"

        foundry_present = foundry_toml.exists()
        hardhat_present = hardhat_config_js.exists() or hardhat_config_ts.exists()

        # Handle case where both are present
        if foundry_present and hardhat_present:
            logger.log(
                "Both Foundry and Hardhat detected, defaulting to Foundry",
                "WARNING",
                "BuildSystemDetector"
            )
            logger.log(
                "Use --build-system hardhat to override",
                "INFO",
                "BuildSystemDetector"
            )
            return BuildSystem.FOUNDRY

        if foundry_present:
            return BuildSystem.FOUNDRY

        if hardhat_present:
            return BuildSystem.HARDHAT

        truffle_config_js = project_root / "truffle-config.js"
        truffle_config_legacy = project_root / "truffle.js"
        if truffle_config_js.exists() or truffle_config_legacy.exists():
            return BuildSystem.TRUFFLE

        # 2. Package.json detection
        package_json = project_root / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    data = json.load(f)
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    if "hardhat" in deps:
                        logger.log(
                            "Detected Hardhat from package.json dependencies",
                            "INFO",
                            "BuildSystemDetector"
                        )
                        return BuildSystem.HARDHAT
                    if "truffle" in deps:
                        logger.log(
                            "Detected Truffle from package.json dependencies",
                            "INFO",
                            "BuildSystemDetector"
                        )
                        return BuildSystem.TRUFFLE
            except Exception as e:
                logger.log(
                    f"Failed to parse package.json: {e}",
                    "WARNING",
                    "BuildSystemDetector"
                )

        # 3. Artifact directory detection (less reliable, but helpful)
        out_dir = project_root / "out"
        artifacts_dir = project_root / "artifacts"

        # Check for Foundry structure
        if out_dir.exists() and out_dir.is_dir():
            # Look for typical Foundry structure: out/*.sol/*.json
            sol_dirs = [d for d in out_dir.iterdir() if d.is_dir() and d.name.endswith(".sol")]
            if sol_dirs:
                logger.log(
                    "Detected Foundry from out/ directory structure",
                    "INFO",
                    "BuildSystemDetector"
                )
                return BuildSystem.FOUNDRY

        # Check for Hardhat structure
        if artifacts_dir.exists() and artifacts_dir.is_dir():
            # Look for Hardhat-specific structure
            contracts_dir = artifacts_dir / "contracts"
            build_info_dir = artifacts_dir / "build-info"
            if contracts_dir.exists() or build_info_dir.exists():
                logger.log(
                    "Detected Hardhat from artifacts/ directory structure",
                    "INFO",
                    "BuildSystemDetector"
                )
                return BuildSystem.HARDHAT

        # Check for Truffle structure: build/contracts/<ContractName>.json
        truffle_build_dir = project_root / "build" / "contracts"
        if truffle_build_dir.is_dir():
            logger.log(
                "Detected Truffle from build/contracts/ directory structure",
                "INFO",
                "BuildSystemDetector"
            )
            return BuildSystem.TRUFFLE

        return BuildSystem.UNKNOWN

    @staticmethod
    def resolve(project_root: Path, requested: Optional[str]) -> BuildSystem:
        """
        Return the explicit build system if the user supplied one, otherwise auto-detect.

        Centralizes the "explicit override or auto-detect" logic so call sites cannot
        accidentally drop the user's --build-system choice and fall back to detection
        (which warns and defaults to Foundry when both build systems are present).
        """
        if requested is None or requested == "auto":
            return BuildSystemDetector.detect(project_root)
        return BuildSystem(requested.lower())

    @staticmethod
    def get_contract_extractor(
        build_system: BuildSystem, project_root: Path, profile: Optional[str] = None
    ) -> ContractExtractor:
        """
        Get an instantiated contract extractor for the detected build system.

        Args:
            build_system: Detected build system (from detect())
            project_root: Project root directory
            profile: Build system profile to use (e.g. Foundry profile name)

        Returns:
            ContractExtractor instance (Foundry/Hardhat/Truffle contract extractor)

        Raises:
            ValueError: If build system is not supported
        """
        if build_system == BuildSystem.FOUNDRY:
            return FoundryContractExtractor(project_root, profile=profile)
        elif build_system == BuildSystem.HARDHAT:
            return HardhatContractExtractor(project_root)
        elif build_system == BuildSystem.TRUFFLE:
            return TruffleContractExtractor(project_root)
        else:
            raise ValueError(f"Unsupported build system: {build_system}")

    @staticmethod
    def get_manager_class(build_system: BuildSystem) -> Type[BuildSystemManager]:
        """
        Get the appropriate manager class (FoundryManager, HardhatManager or TruffleManager).

        Args:
            build_system: The build system to get manager for

        Returns:
            Manager class implementing BuildSystemManager protocol

        Raises:
            ValueError: If build system is unsupported
        """
        if build_system == BuildSystem.FOUNDRY:
            return FoundryManager
        elif build_system == BuildSystem.HARDHAT:
            return HardhatManager
        elif build_system == BuildSystem.TRUFFLE:
            return TruffleManager
        else:
            raise ValueError(f"Unsupported build system: {build_system}")
