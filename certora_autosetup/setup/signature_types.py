#!/usr/bin/env python3
"""
Shared types and data structures for PreAudit.

This module contains common data structures used across different components
of the PreAudit system, copied from autosetup to support signature database functionality.
"""

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..utils.solc_version_resolver import read_pragma_from_source_file

# Import all types from the canonical location
from ..utils.types import (
    ContractHandle,
    ContractInfo,
    FunctionSignature,
)


class InheritanceGraph:
    """Inheritance graph with ContractHandle-based lookups."""

    def __init__(self, graph: Dict[ContractHandle, Set[ContractHandle]] | None = None):
        self._graph: Dict[ContractHandle, Set[ContractHandle]] = graph or {}

    def find_handle_by_name(self, contract_name: str) -> ContractHandle | None:
        """Find a ContractHandle by contract name."""
        for handle in self._graph.keys():
            if handle.contract_name == contract_name:
                return handle
        return None

    def get_parents(self, handle: ContractHandle) -> Set[ContractHandle]:
        """Get parent handles for a contract."""
        return self._graph.get(handle, set())

    def keys(self):
        """Return the keys (ContractHandles) of the graph."""
        return self._graph.keys()

    def __contains__(self, handle: ContractHandle) -> bool:
        return handle in self._graph

    def __bool__(self) -> bool:
        return bool(self._graph)


class SignatureDatabase:
    """
    Database for function signatures with inheritance-aware lookups.

    Maps selectors to signatures and tracks which contracts implement each signature.
    """

    def __init__(self, project_root: Optional[Path] = None):
        # Map selector -> FunctionSignature (signature info without contract)
        self._signatures: Dict[str, FunctionSignature] = {}
        # Map selector -> Set[contract_name] (which contracts implement this signature)
        self._implementations: Dict[str, Set[str]] = {}
        # Map contract_name -> ContractInfo
        self._contracts: Dict[str, ContractInfo] = {}
        # Cache: contract_name -> set of all ancestors (inheritance chain)
        self._inheritance_cache: Dict[str, Set[str]] = {}
        # Used by ``get_solidity_version`` to resolve relative source-file paths.
        self._project_root: Optional[Path] = project_root

    def add_signature(self, signature: FunctionSignature, contract_name: str) -> None:
        """Add a function signature implementation for a specific contract."""
        self._signatures[signature.selector] = signature

        if signature.selector not in self._implementations:
            self._implementations[signature.selector] = set()
        self._implementations[signature.selector].add(contract_name)

    def add_contract(self, contract_info: ContractInfo) -> None:
        """Add contract information to the database."""
        self._contracts[contract_info.name] = contract_info
        # Clear inheritance cache when contracts are updated
        self._inheritance_cache.clear()

    def get_signature(self, selector: str) -> Optional[FunctionSignature]:
        """Get the function signature for a selector."""
        return self._signatures.get(selector)

    def get_signature_by_internal_selector(
        self, internal_selector: str
    ) -> Optional[FunctionSignature]:
        """
        Get the function signature by internal type selector.

        Args:
            internal_selector: Internal type selector like "0x3f2dd92c"

        Returns:
            FunctionSignature if found, None otherwise
        """
        for signature in self._signatures.values():
            if signature.internal_type_selector == internal_selector:
                return signature
        return None

    def resolve_selector(self, selector: str) -> Optional[FunctionSignature]:
        """
        Resolve a selector by trying canonical lookup first, then by internal/non-canonical
        selector. Functions that take custom types (enums, structs, user-defined value types)
        have a canonical sighash (e.g. against ``uint8``) and a separate non-canonical one
        (against the named type); the prover may emit either.
        """
        return self.get_signature(selector) or self.get_signature_by_internal_selector(selector)

    def get_implementing_contracts(self, selector: str) -> List[str]:
        """
        Get all contracts that implement a given selector.

        Args:
            selector: Function selector like "0xa9059cbb"

        Returns:
            List of contract names that implement this selector (including through inheritance)
        """
        direct_implementations = self._implementations.get(selector, set())
        all_implementations = set(direct_implementations)

        # Add contracts that inherit the signature
        for contract_name in self._contracts:
            for impl_contract in direct_implementations:
                if self._inherits_from(contract_name, impl_contract):
                    all_implementations.add(contract_name)

        return list(all_implementations)

    def get_verifiable_implementing_contracts(self, selector: str) -> List[str]:
        """
        Get verifiable contracts that implement a given selector (excludes abstract contracts and interfaces).

        Args:
            selector: Function selector like "0xa9059cbb"

        Returns:
            List of verifiable contract names that implement this selector
        """
        all_implementations = self.get_implementing_contracts(selector)
        verifiable_implementations = []

        for contract_name in all_implementations:
            contract_info = self._contracts.get(contract_name)
            if (
                contract_info
                and contract_info.kind.value in ("contract", "library")
                and contract_info.is_compilable
            ):
                verifiable_implementations.append(contract_name)

        return verifiable_implementations

    def get_implementing_contracts_by_signature(self, signature: str) -> List[str]:
        """
        Get all contracts that implement a given function signature string.

        Args:
            signature: Function signature like "transfer(address,uint256)"

        Returns:
            List of contract names that implement this signature
        """
        # Find the selector for this signature
        for sig in self._signatures.values():
            if sig.signature == signature:
                return self.get_implementing_contracts(sig.selector)
        return []

    def get_source_file_for_contract(self, contract_name: str) -> Optional[Path]:
        """Get the source file path for a contract."""
        contract_info = self._contracts.get(contract_name)
        return contract_info.source_file if contract_info else None

    def get_solidity_version(self, contract_name: str) -> Optional[str]:
        """Solidity pragma spec for ``contract_name``, computed lazily.

        Returns ``contract_info.solidity_version`` if it has been populated.
        Otherwise parses, memoizes and returns the pragma from the contract's source
        file via ``read_pragma_from_source_file``.
        Returns ``None`` if the contract is unknown, the source file can't
        be read, or no parseable pragma is found.
        """
        contract_info = self._contracts.get(contract_name)
        if contract_info is None:
            return None
        if contract_info.solidity_version is not None:
            return contract_info.solidity_version
        spec = read_pragma_from_source_file(contract_info.source_file, self._project_root)
        if spec is None:
            return None
        contract_info.solidity_version = spec
        return spec

    def _inherits_from(self, child_contract: str, parent_contract: str) -> bool:
        """Check if child_contract inherits from parent_contract."""
        if child_contract not in self._contracts:
            return False

        # Use cached inheritance chain if available
        if child_contract in self._inheritance_cache:
            return parent_contract in self._inheritance_cache[child_contract]

        # Build inheritance chain for this contract
        inheritance_chain = self._build_inheritance_chain(child_contract)
        self._inheritance_cache[child_contract] = inheritance_chain

        return parent_contract in inheritance_chain

    def _build_inheritance_chain(self, contract_name: str) -> Set[str]:
        """Build the complete inheritance chain for a contract (recursive via queue)."""
        if contract_name not in self._contracts:
            return set()

        visited = set()
        to_visit = [contract_name]
        inheritance_chain = set()

        while to_visit:
            current = to_visit.pop()
            if current in visited:
                continue

            visited.add(current)
            contract_info = self._contracts.get(current)

            if contract_info:
                for parent in contract_info.inherits_from:
                    inheritance_chain.add(parent)
                    to_visit.append(parent)

        return inheritance_chain

    def sighashes_represent_same_function(self, sighash1: str, sighash2: str) -> bool:
        """
        Check if two sighashes represent the same function, accounting for canonical vs non-canonical signatures.

        A function can have both canonical signatures (using basic types like uint256)
        and non-canonical signatures (using custom types), which result in different sighashes.
        This method checks all possible combinations to determine if they represent the same function.

        Args:
            sighash1: First sighash to compare
            sighash2: Second sighash to compare

        Returns:
            True if they represent the same function
        """
        if sighash1 == sighash2:
            return True

        # Check if sighash1 is the internal selector of sighash2's signature
        signature2 = self.get_signature(sighash2)
        if signature2 and hasattr(signature2, 'internal_type_selector') and signature2.internal_type_selector == sighash1:
            return True

        # Check if sighash2 is the internal selector of sighash1's signature
        signature1 = self.get_signature(sighash1)
        if signature1 and hasattr(signature1, 'internal_type_selector') and signature1.internal_type_selector == sighash2:
            return True

        # Check if both are internal selectors pointing to the same canonical signature
        sig1_canonical = self.get_signature_by_internal_selector(sighash1)
        sig2_canonical = self.get_signature_by_internal_selector(sighash2)
        if sig1_canonical and sig2_canonical and sig1_canonical.selector == sig2_canonical.selector:
            return True

        return False

    def get_all_signatures(self) -> Dict[str, FunctionSignature]:
        """Get all function signatures in the database."""
        return self._signatures.copy()

    def get_all_contracts(self) -> Dict[str, ContractInfo]:
        """Get all contracts in the database."""
        return self._contracts.copy()

    def build_inheritance_graph(self) -> InheritanceGraph:
        """Build inheritance graph from contracts in the database.

        Returns:
            InheritanceGraph with ContractHandle -> set of parent ContractHandles mappings
        """
        graph: Dict[ContractHandle, Set[ContractHandle]] = {}
        for _, contract_info in self._contracts.items():
            if contract_info.inherits_from:
                child_handle = ContractHandle(
                    contract_name=contract_info.name,
                    source_file=str(contract_info.source_file) if contract_info.source_file else ""
                )
                parent_handles: Set[ContractHandle] = set()
                for parent_name in contract_info.inherits_from:
                    parent_info = self._contracts.get(parent_name)
                    parent_source = str(parent_info.source_file) if parent_info and parent_info.source_file else ""
                    parent_handles.add(ContractHandle(
                        contract_name=parent_name,
                        source_file=parent_source
                    ))
                graph[child_handle] = parent_handles
        return InheritanceGraph(graph)


def compute_signature_selector(signature: str) -> Optional[str]:
    """
    Compute 4-byte function selector using cast sig command.
    Reuses the same logic as ContractDispatcher._compute_sighash_with_cast.
    """
    try:

        result = subprocess.run(
            ["cast", "sig", signature], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            sighash = result.stdout.strip()
            if sighash.startswith("0x") and len(sighash) == 10:  # 0x + 8 hex chars
                return sighash
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return None


def normalize_selector(selector: str) -> str:
    """Normalize a selector to 0x + 8 hex chars. The prover sometimes drops leading zeros."""
    if selector.startswith("0x"):
        return "0x" + selector[2:].zfill(8)
    return selector


def extract_sighash_from_callee(callee_name: str) -> Optional[str]:
    """Extract or compute sighash from callee name.

    Args:
        callee_name: The callee string, e.g., "[sighash=0x12345678]" or "[?].balanceOf(address)"

    Returns:
        The sighash as a hex string, or None if extraction/computation fails
    """
    if "[sighash=" in callee_name:
        # Extract sighash from format like "[sighash=0x12345678]"
        start = callee_name.find("[sighash=") + len("[sighash=")
        end = callee_name.find("]", start)
        if end > start:
            return normalize_selector(callee_name[start:end])
    else:
        # Try to extract function signature and compute sighash
        # Format like "[?].balanceOf(address)" -> "balanceOf(address)"
        if "]." in callee_name:
            function_sig = callee_name.split("].", 1)[1]
            return compute_signature_selector(function_sig)
    return None


