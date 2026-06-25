#!/usr/bin/env python3
"""
Contract Dispatcher - Resolves unresolved function calls via DISPATCHER entries.

Generates dispatcher entries for function signatures found in the signature database,
and manages the corresponding contract files in prover configs.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

# Setup path for local modules
current_dir = Path(__file__).parent
preaudit_root = current_dir.parent
sys.path.insert(0, str(preaudit_root))

from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle, FunctionSignature
from certora_autosetup.utils.scope import Scope
from certora_autosetup.setup.signature_types import extract_sighash_from_callee
from prover_output_utility.models import CallResolutionInfo  # type: ignore


@dataclass
class DispatchingResult:
    """Result from a single dispatching step."""

    unresolved_calls_before: int
    signatures_added: int
    converged: bool
    success: bool
    error_message: Optional[str] = None


class ContractDispatcher:
    """
    Custom dispatching implementation with prover invocation caps.

    This class implements the dispatching algorithm while using existing infrastructure:
    - ConfigManager for management of .conf files
    - Scope, including SignatureDatabase for defining the search space
    """

    def __init__(
        self,
        scope: Scope,
        config_manager: ConfigManager,
    ):
        """
        Initialize contract dispatcher.

        Args:
            scope: Scope defining project boundaries and file filtering
            config_manager: Configuration manager for dependency tracking
        """
        self.scope = scope
        self.config_manager = config_manager

        # Caching for dispatcher resolution
        self.failed_sighashes: set = set()  # Track sighashes we've failed to resolve
        self.resolved_sighashes: Dict[
            str, FunctionSignature
        ] = {}  # Cache successful resolutions

        # Accumulated dispatcher entries across all iterations (normalized, without trailing semicolons)
        self._all_dispatcher_entries: Set[str] = set()


    @staticmethod
    def _generate_dispatcher_entry(signature: FunctionSignature) -> str:
        """Generate dispatcher entry for CVL spec file using appropriate signature."""
        # Use dispatcher_entry_name if available (for contract-qualified user-defined types)
        # Otherwise fall back to canonical signature
        if signature.dispatcher_entry_name:
            dispatcher_signature = signature.dispatcher_entry_name
        else:
            # Fallback to canonical signature for backward compatibility
            dispatcher_signature = signature.signature

        return f"function _.{dispatcher_signature} external => DISPATCHER(true);"

    def _resolve_signature_with_canonical_and_noncanonical_lookup(self, sighash: str) -> tuple[Optional[FunctionSignature], List[str]]:
        """
        Helper method to resolve a signature using both canonical and non-canonical lookups.

        A function can have both canonical signatures (using basic types like uint256)
        and non-canonical signatures (using custom types), which result in different sighashes.
        This method tries both approaches and returns the union of results when
        both exist.
        TODO: Using union might be wrong and should be checked. It's on the
        sound side though; we might revisit this if we see prover issues in a
        customer project.

        TODO: The optimal solution might be to use always canonical signatures in the signature database.

        Args:
            sighash: The function selector to resolve

        Returns:
            Tuple of (signature_info, implementing_contracts) or (None, []) if not found
        """
        signature_info = None
        implementing_contracts = []

        # Try canonical selector first (standard approach)
        canonical_implementing_contracts = self.scope.get_implementing_contracts_by_selector(sighash)
        if canonical_implementing_contracts:
            signature_info = self.scope.signature_database.get_signature(sighash)
            if signature_info:
                implementing_contracts.extend(canonical_implementing_contracts)

        # Try non-canonical (internal type) selector lookup
        noncanonical_signature_info = self.scope.signature_database.get_signature_by_internal_selector(sighash)
        if noncanonical_signature_info:
            # Get implementing contracts for the canonical selector of the non-canonical signature
            noncanonical_implementing_contracts = self.scope.get_implementing_contracts_by_selector(
                noncanonical_signature_info.selector
            )

            if noncanonical_implementing_contracts:
                # Take union of results when both canonical and non-canonical signatures exist
                if signature_info:
                    # Both exist - take union of implementing contracts
                    implementing_contracts.extend(noncanonical_implementing_contracts)
                    implementing_contracts = list(set(implementing_contracts))  # Remove duplicates
                else:
                    # Only non-canonical found
                    signature_info = noncanonical_signature_info
                    implementing_contracts = noncanonical_implementing_contracts

        return signature_info, implementing_contracts

    def _resolve_calls_to_signatures(
        self, unresolved_calls: List[CallResolutionInfo]
    ) -> Dict[str, FunctionSignature]:
        """Resolve unresolved calls to function signatures with caching."""
        resolved = {}
        attempted_to_fix_count = 0
        new_failures = 0
        cached_successes = 0
        cached_failures = 0

        all_signatures = self.scope.signature_database.get_all_signatures()
        logger.debug(f"Scope signature database has {len(all_signatures)} entries")

        for call in unresolved_calls:
            # Extract sighash from callee_name (CallResolutionInfo doesn't have sighash field)
            sighash = extract_sighash_from_callee(call.callee_name)
            logger.debug(
                f"Processing call: caller={call.caller_name}, callee={call.callee_name}, sighash={sighash}, location={call.source_location}"
            )
            if sighash:
                # Check cache first
                if sighash in self.resolved_sighashes:
                    # Previously resolved successfully
                    signature = self.resolved_sighashes[sighash]
                    if sighash not in resolved:
                        resolved[sighash] = signature
                        logger.debug(
                            f"🔄 Using cached resolution for {sighash}: {signature.signature}"
                        )
                    attempted_to_fix_count += 1
                    cached_successes += 1
                elif sighash in self.failed_sighashes:
                    # Previously failed to resolve - skip silently
                    cached_failures += 1
                else:
                    # New sighash - attempt resolution using scope's signature database
                    signature_info, implementing_contracts = self._resolve_signature_with_canonical_and_noncanonical_lookup(sighash)

                    if signature_info and implementing_contracts:
                        # Cache successful resolution
                        self.resolved_sighashes[sighash] = signature_info
                        if sighash not in resolved:
                            resolved[sighash] = signature_info
                            logger.info(
                                f"✅ Found signature for {sighash}: {signature_info.signature} (implemented by: {', '.join(implementing_contracts[:3])}{'+' + str(len(implementing_contracts) - 3) + ' more' if len(implementing_contracts) > 3 else ''})"
                            )
                        attempted_to_fix_count += 1
                    else:
                        # Cache failure
                        self.failed_sighashes.add(sighash)
                        logger.warning(
                            f"❌ Sighash {sighash} (call: {call.callee_name}) not found in signature database"
                        )
                        new_failures += 1
            else:
                logger.warning(
                    f"❌ No sighash extracted for call: {call.callee_name}"
                )

        if attempted_to_fix_count > 0:
            logger.info(
                f"Attempted to fix {attempted_to_fix_count} calls using {len(resolved)} unique signatures"
            )
        if cached_failures > 0:
            logger.debug(f"Skipped {cached_failures} previously failed calls")
        return resolved

    def generate_new_dispatcher_entries(
        self,
        signatures: Dict[str, FunctionSignature],
    ) -> List[str]:
        """
        Generate dispatcher entry lines for signatures not already tracked.

        Deduplicates against previously generated entries across all iterations.

        Args:
            signatures: Dictionary of function signatures to generate entries for

        Returns:
            List of new dispatcher entry strings (e.g. "function _.foo() external => DISPATCHER(true);")
        """
        new_entries = []
        for signature in signatures.values():
            entry = self._generate_dispatcher_entry(signature)
            if entry not in self._all_dispatcher_entries:
                self._all_dispatcher_entries.add(entry)
                new_entries.append(entry)

        if new_entries:
            if len(new_entries) == 1:
                logger.info(f"New dispatcher entry: {new_entries[0]}")
            else:
                logger.info(f"{len(new_entries)} new dispatcher entries:")
                for entry in new_entries:
                    logger.info(f"  {entry}")

        return new_entries

    def get_all_dispatcher_entries(self) -> List[str]:
        """Return all accumulated dispatcher entries across all iterations."""
        return sorted(self._all_dispatcher_entries)

    def add_contract_files_to_config(
        self,
        signatures: Dict[str, FunctionSignature],
        config_file: Path,
        exclude_contracts: Optional[Set[str]] = None,
    ) -> List[ContractHandle]:
        """
        Add contract files for the given signatures to the config file.

        Args:
            signatures: Dictionary of function signatures
            config_file: Path to the config file to update
            exclude_contracts: Contract names to exclude from being added

        Returns:
            The contract handles that were actually added (post-exclusion). Empty
            if nothing was added. Used by call resolution to drive lazy LLM
            analysis on the same contracts.
        """
        if not signatures:
            return []
        contract_files = self._get_contract_files_for_signatures(signatures)
        if contract_files and exclude_contracts:
            contract_files = [
                cf for cf in contract_files if cf.contract_name not in exclude_contracts
            ]
        if contract_files:
            self.config_manager.add_files_to_config(config_file, contract_files)
        return contract_files

    def _get_contract_files_for_signatures(
        self, signatures: Dict[str, FunctionSignature]
    ) -> List[ContractHandle]:
        """
        Get the contract handles for the given function signatures using the signature database.

        Args:
            signatures: Dictionary of function signatures from dispatcher

        Returns:
            List of ContractHandle objects (contract_name + source_file) for contracts that implement these methods
        """
        contract_handles_dict = {}  # Use dict to avoid duplicates: key = (source_file, contract_name)

        # Use scope's signature database to find contracts containing the dispatched methods
        logger.debug(
            f"Looking for {len(signatures)} dispatched methods using scope's signature database"
        )

        for sig in signatures.values():
            logger.debug(
                f"Searching for method {getattr(sig, 'signature', 'unknown')} in signature database"
            )

            # Get verifiable contracts that implement this signature (excludes abstract contracts)
            if hasattr(sig, "signature") and hasattr(sig, "selector"):
                implementing_contracts = (
                    self.scope.signature_database.get_verifiable_implementing_contracts(
                        sig.selector
                    )
                )
                if implementing_contracts:
                    # For each implementing contract, create a ContractHandle with both name and source file
                    for contract_name in implementing_contracts:
                        source_file = self.scope.signature_database.get_source_file_for_contract(
                            contract_name
                        )
                        if source_file:
                            # Make relative to project root
                            try:
                                rel_path = source_file.relative_to(self.scope.project_root)
                                source_file_str = str(rel_path)
                            except ValueError:
                                source_file_str = str(source_file)

                            # Create ContractHandle and add to dict to avoid duplicates
                            handle_key = (source_file_str, contract_name)
                            if handle_key not in contract_handles_dict:
                                contract_handle = ContractHandle(
                                    contract_name=contract_name,
                                    source_file=source_file_str
                                )
                                contract_handles_dict[handle_key] = contract_handle
                                logger.debug(
                                    f"Added {contract_handle.to_config_str()} for method {sig.signature}"
                                )

        return list(contract_handles_dict.values())

