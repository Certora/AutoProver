"""
Centralized scope filtering for autosetup workflows.

This module provides a unified way to determine which files and directories
should be included in autosetup processing, with consistent exclusion rules
applied throughout the codebase.
"""

import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Set

from certora_autosetup.setup.solidity_utils import DEPENDENCIES

from .logger import logger
from .types import ContractInfo
from ..setup.signature_types import SignatureDatabase


class Scope:
    """
    Centralized scope for tracking contracts, signatures, and compilation status.

    This class manages the complete project scope including:
    - Which files/directories are in scope for processing
    - All contracts and their inheritance relationships
    - Function signatures and which contracts implement them
    - Compilation status and requirements
    """

    def __init__(
        self,
        project_root: Path,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ):
        """
        Initialize the scope.

        Args:
            project_root: Root directory of the project
            include_patterns: List of file/folder/glob patterns to include (None means include all)
            exclude_patterns: List of file/folder/glob patterns to exclude
        """
        self.project_root = Path(project_root)

        # Store patterns for filtering
        self.include_patterns = include_patterns or []
        self.exclude_patterns = exclude_patterns or []

        # Directories that should always be excluded from processing
        self.excluded_directories = set(DEPENDENCIES) | {
            # Build outputs and non-source directories
            "out",
            "cache",
            "target",
            "artifacts",
            "typechain",
            "build",
            # Certora internal directories (always excluded except for result parsing)
            ".certora_internal",
            # Note: "lib" is NOT excluded as it contains dependencies needed for linking/dispatching
        }

        # File patterns to exclude
        self.excluded_file_patterns = {"example", "demo"}

        # Cached results
        self._scoped_files: Optional[Set[Path]] = None
        self._scoped_directories: Optional[Set[Path]] = None

        # Enhanced contract and signature tracking.
        self.signature_database = SignatureDatabase(project_root=self.project_root)

    def get_scoped_solidity_files(self, skip_test_files: bool = True) -> Set[Path]:
        """
        Get all Solidity files that are in scope for processing.

        Args:
            skip_test_files: Whether to skip test files

        Returns:
            Set of Path objects for valid Solidity files
        """
        if self._scoped_files is None:
            self._scoped_files = self._collect_scoped_files(skip_test_files)

        return self._scoped_files.copy()

    def is_file_in_scope(self, file_path: Path, skip_test_files: bool = True) -> bool:
        """
        Check if a specific file is in scope for processing.

        Args:
            file_path: Path to check
            skip_test_files: Whether to skip test files (default True)

        Returns:
            True if the file should be processed, False otherwise
        """
        return not self._should_exclude_file(file_path, skip_test_files)

    def is_directory_in_scope(self, directory_path: Path) -> bool:
        """
        Check if a specific directory is in scope for processing.

        Args:
            directory_path: Directory path to check

        Returns:
            True if the directory should be processed, False otherwise
        """
        return not self._should_exclude_directory(directory_path)

    def _matches_patterns(self, file_path: Path, patterns: List[str]) -> bool:
        """
        Check if a file path matches any of the given patterns.

        Supports:
        - File paths: exact matches (e.g., "src/main/Contract.sol")
        - Folder paths: matches all files in folder (e.g., "src/main/")
        - Glob patterns: fnmatch patterns (e.g., "src/**/*.sol", "*/test/*")

        Args:
            file_path: Path to check
            patterns: List of patterns to match against

        Returns:
            True if the file matches any pattern, False otherwise
        """
        if not patterns:
            return False

        # Convert to relative path for pattern matching
        try:
            relative_path = file_path.relative_to(self.project_root)
            relative_str = str(relative_path)
        except ValueError:
            # File is not under project root, use absolute path
            relative_str = str(file_path)

        for pattern in patterns:
            # Convert pattern to use forward slashes for consistency
            pattern = pattern.replace("\\", "/")
            relative_str_normalized = relative_str.replace("\\", "/")

            # Check if pattern is a directory path (ends with /)
            if pattern.endswith("/"):
                # Match all files under this directory
                if relative_str_normalized.startswith(pattern) or fnmatch.fnmatch(
                    relative_str_normalized, pattern + "*"
                ):
                    return True
            # Check if pattern is an exact file path
            elif "/" in pattern and not any(
                char in pattern for char in ["*", "?", "["]
            ):
                # Exact file path match
                if relative_str_normalized == pattern:
                    return True
            else:
                # Glob pattern match
                if fnmatch.fnmatch(relative_str_normalized, pattern):
                    return True

        return False

    def _is_included_by_patterns(self, file_path: Path) -> bool:
        """
        Check if a file should be included based on include/exclude patterns.

        Logic:
        - If include_patterns is empty or not specified: include all files (except excluded)
        - If include_patterns is specified: only include files that match at least one include pattern
        - Always exclude files that match any exclude pattern

        Args:
            file_path: Path to check

        Returns:
            True if the file should be included, False otherwise
        """
        # Check exclude patterns first (they take priority)
        if self._matches_patterns(file_path, self.exclude_patterns):
            return False

        # If no include patterns specified, include everything (that's not excluded)
        if not self.include_patterns:
            return True

        # If include patterns specified, file must match at least one
        return self._matches_patterns(file_path, self.include_patterns)

    def find_scoped_file(self, filename: str) -> Optional[Path]:
        """
        Find a specific filename within the scoped files.
        Useful for finding contract source files while avoiding duplicates
        from excluded directories.

        Args:
            filename: Name of file to find (e.g., "PremiumERC20.sol")

        Returns:
            Path to the file if found in scope, None otherwise
        """
        for scoped_file in self.get_scoped_solidity_files():
            if scoped_file.name == filename:
                return scoped_file

        return None

    def get_relative_path(self, file_path: Path) -> str:
        """
        Get the relative path from project root for a file.

        Args:
            file_path: Absolute or relative path

        Returns:
            Relative path string from project root
        """
        try:
            if file_path.is_absolute():
                return str(file_path.relative_to(self.project_root))
            else:
                return str(file_path)
        except ValueError:
            # File is not relative to project root
            return str(file_path)

    def _collect_scoped_files(self, skip_test_files: bool) -> Set[Path]:
        """
        Collect all Solidity files that are in scope for processing.

        Args:
            skip_test_files: Whether to skip test files

        Returns:
            Set of Path objects for valid Solidity files
        """
        scoped_files = set()

        try:
            all_sol_files = list(self.project_root.rglob("*.sol"))
            logger.debug(
                f"Found {len(all_sol_files)} total .sol files in {self.project_root}"
            )
            for sol_file in all_sol_files:
                if self._should_exclude_file(sol_file, skip_test_files):
                    continue
                scoped_files.add(sol_file)

            logger.debug(
                f"Final result: {len(scoped_files)} Solidity files in scope"
            )

        except Exception as e:
            logger.error(f"Error collecting scoped files: {e}")

        return scoped_files

    def _should_exclude_file(
        self, file_path: Path, skip_test_files: bool = True
    ) -> bool:
        """
        Determine if a file should be excluded from processing.

        Args:
            file_path: Path to the file
            skip_test_files: Whether to skip test files

        Returns:
            True if the file should be excluded
        """
        # Check if file is in an excluded directory
        if self._should_exclude_directory(file_path):
            return True

        # Always skip mock, example, and demo files
        if not self._is_included_by_patterns(file_path):
            return True

        # Skip test files if requested
        if skip_test_files:
            if "test" in file_path.name.lower():
                return True

            # Check for test directories
            for part in file_path.parts:
                if part.lower() in {"test", "tests", "testing"}:
                    return True

        return False

    def _should_exclude_directory(self, path: Path) -> bool:
        """
        Determine if a path is within an excluded directory.

        Args:
            path: Path to check (file or directory)

        Returns:
            True if the path should be excluded
        """
        path_str = str(path)

        # Check for .certora_internal directories
        if ".certora_internal" in path_str:
            return True

        # Check for emv-* directories (Certora prover outputs)
        for part in path.parts:
            if part.startswith("emv-"):
                return True

        # Check fixed excluded directories
        for part in path.parts:
            if part in self.excluded_directories:
                return True

        return False

    def clear_cache(self):
        """Clear cached results to force re-collection."""
        self._scoped_files = None
        self._scoped_directories = None
        self.signature_database = SignatureDatabase(project_root=self.project_root)

    def add_contract(self, contract_info: ContractInfo) -> None:
        """
        Add a single contract information to the scope.

        Args:
            contract_info: Contract information
        """
        self.signature_database.add_contract(contract_info)
        logger.debug(
            f"Added {contract_info.kind.value} {contract_info.name} to scope"
        )
        # Add function signatures if available
        if contract_info.function_signatures:
            for signature in contract_info.function_signatures.values():
                self.signature_database.add_signature(signature, contract_info.name)

    def add_contracts(self, contract_infos: List[ContractInfo]) -> None:
        """
        Add contract information to the scope and extract their signatures.

        Args:
            contract_infos: List of contract information
        """
        for contract_info in contract_infos:
            # Add contract metadata
            self.add_contract(contract_info)

        compilable_count = sum(1 for c in contract_infos if c.is_compilable)
        signature_count = len(self.signature_database.get_all_signatures())

        logger.info(
            f"Added {len(contract_infos)} contracts to scope "
            f"({compilable_count} compilable, {signature_count} total signatures)"
        )

    def add_signatures(self, signatures_data: Dict) -> None:
        """
        Add signatures to the database.

        Args:
            signatures_data: Signature data (selector -> signature info with contract)
        """
        from .types import FunctionSignature

        for selector, sig_data in signatures_data.items():
            # Handle both dict and FunctionSignature objects
            if isinstance(sig_data, dict):
                # Convert dict to FunctionSignature
                signature = FunctionSignature(
                    signature=sig_data.get("signature", ""),
                    selector=sig_data.get("selector", selector),
                    is_view=sig_data.get("is_view", False),
                    is_pure=sig_data.get("is_pure", False),
                    internal_type_signature=sig_data.get("internal_type_signature"),
                    internal_type_selector=sig_data.get("internal_type_selector"),
                    dispatcher_entry_name=sig_data.get("dispatcher_entry_name"),
                )
                contract_name = sig_data.get("contract", "Unknown")
            else:
                # Should be a FunctionSignature object - verify and use directly
                if isinstance(sig_data, FunctionSignature):
                    signature = sig_data
                    contract_name = getattr(sig_data, "contract", "Unknown")
                else:
                    # Unexpected type - crash with clear error
                    error_msg = f"Expected dict or FunctionSignature, got {type(sig_data).__name__}: {sig_data}"
                    logger.error(error_msg)
                    raise TypeError(error_msg)

            self.signature_database.add_signature(signature, contract_name)
        logger.info(f"Added {len(signatures_data)} signatures to scope")

    def get_implementing_contracts(self, signature: str) -> List[str]:
        """
        Get all contracts that implement a given function signature.

        Args:
            signature: Function signature like "transfer(address,uint256)"

        Returns:
            List of contract names that implement this signature (including inheritance)
        """
        return self.signature_database.get_implementing_contracts_by_signature(
            signature
        )

    def get_implementing_contracts_by_selector(self, selector: str) -> List[str]:
        """
        Get all contracts that implement a given function selector.

        Args:
            selector: Function selector like "0xa9059cbb"

        Returns:
            List of contract names that implement this selector (including inheritance)
        """
        return self.signature_database.get_implementing_contracts(selector)

    def get_source_files_for_contracts(self, contract_names: List[str]) -> List[Path]:
        """
        Get source file paths for a list of contract names.

        Args:
            contract_names: List of contract names

        Returns:
            List of source file paths (relative to project root)
        """
        source_files = []
        for contract_name in contract_names:
            source_file = self.signature_database.get_source_file_for_contract(
                contract_name
            )
            if source_file:
                # Make relative to project root
                try:
                    rel_path = source_file.relative_to(self.project_root)
                    source_files.append(rel_path)
                except ValueError:
                    # Path is outside project root, use as-is
                    source_files.append(source_file)

        return source_files

    def check_all_contracts_compilable(self) -> Dict[str, Optional[str]]:
        """
        Check compilation status of all contracts in scope.

        Returns:
            Dict mapping contract_name -> compilation_error (None if compilable)
        """
        compilation_status = {}
        for (
            contract_name,
            contract_info,
        ) in self.signature_database.get_all_contracts().items():
            compilation_status[contract_name] = contract_info.compilation_error

        return compilation_status

    def get_available_contracts(self) -> Dict[str, ContractInfo]:
        """
        Get all contracts available in the signature database.

        Returns:
            Dict mapping contract names to ContractInfo objects
        """
        contracts = self.signature_database.get_all_contracts()
        logger.debug(f"Available contracts in scope: {list(contracts.keys())}")

        # If no contracts in database, provide debug info about scoped files
        if not contracts:
            scoped_files = self.get_scoped_solidity_files(skip_test_files=False)
            logger.debug(f"No contracts in signature database. Scoped files: {len(scoped_files)}")
            for i, file_path in enumerate(list(scoped_files)[:5]):  # Show first 5 files
                logger.debug(f"  Scoped file {i+1}: {file_path}")
            if len(scoped_files) > 5:
                logger.debug(f"  ... and {len(scoped_files) - 5} more")

        return contracts


def create_scope(project_root: Path) -> Scope:
    """
    Factory function to create a Scope instance.

    Args:
        project_root: Root directory of the project

    Returns:
        Scope instance
    """
    return Scope(project_root)
