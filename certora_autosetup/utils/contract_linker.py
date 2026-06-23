#!/usr/bin/env python3
"""
Contract Linker - Prover-driven contract linking using globalCallResolution.

Uses prover call resolution data to generate CVL `links` block entries.
For unresolved calls through storage variables, the prover provides the exact
storage path, which we combine with the signature database to find implementing contracts.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from certora_autosetup.setup.signature_types import SignatureDatabase, extract_sighash_from_callee, normalize_selector
from certora_autosetup.utils.paths import user_harness_path, user_harnesses_dir
from certora_autosetup.utils.scope import Scope

from .logger import logger
from .types import ContractHandle


@dataclass
class ContractLink:
    """A single resolved contract link."""

    owner_contract: str  # Contract containing the variable (e.g., "Main")
    variable_path: str  # Full storage path (e.g., "holder.token", "fixedTokens[0]")
    impl_contracts: List[ContractHandle]  # One or more target contracts (e.g., TokenA, TokenB)


def render_wrapper_contract(
    harness_name: str,
    parent_name: str,
    pragma_line: str,
    import_lines: List[str],
    ctor_forward: Optional[Tuple[str, List[str]]],
    body_blocks: Optional[List[str]] = None,
    header_comment_lines: Optional[List[str]] = None,
) -> str:
    """Render the source of a ``contract <harness_name> is <parent_name>`` wrapper.

    Emits the SPDX header, the pragma (omitted when empty), the import lines,
    an optional constructor forwarding to the parent, and optional extra body blocks.

    ``ctor_forward`` is a ``(params_source, arg_names)`` pair; None means the
    parent needs no constructor arguments and the implicit default constructor
    suffices.
    """
    body_parts: List[str] = []
    if ctor_forward is not None:
        params_src, arg_names = ctor_forward
        body_parts.append(
            f"    constructor({params_src}) {parent_name}({', '.join(arg_names)}) {{}}"
        )
    body_parts.extend(body_blocks or [])

    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        *([pragma_line] if pragma_line else []),
        "",
        *import_lines,
        "",
        *(header_comment_lines or []),
        f"contract {harness_name} is {parent_name} {{",
        *body_parts,
        "}",
        "",
    ]
    return "\n".join(lines)


class LinkStatus(Enum):
    """Status of a linking attempt for a state variable."""

    LINKED = "linked"
    UNRESOLVED = "unresolved"


@dataclass
class LinkingDecision:
    """Represents a linking decision for a specific state variable."""

    contract_name: str  # Contract containing the variable
    variable_name: str  # Name of the state variable
    selectors: List[str]  # Sighashes from prover call resolution (debug only)
    status: LinkStatus  # Linking status
    link_targets: List[str] = field(default_factory=list)  # (debug only)
    reason: str = ""  # Explanation of the decision
    # Selectors that the intersection skipped because no contract in scope implements them.
    # When non-empty on an UNRESOLVED decision, these are the reason the link could not be
    # established. Each entry is the sighash.
    missing_selectors: List[str] = field(default_factory=list)



class ContractLinker:
    """
    Handles contract linking for Solidity verification.

    Uses prover call resolution data (globalCallResolution) to generate CVL `links` block
    entries. For unresolved calls through storage variables, the prover provides the exact
    storage path, which we combine with the signature database to find implementing contracts.
    """

    # TODO: Internal state and most parameters are keyed by bare contract name (str) rather
    # than ContractHandle. Same-name contracts in different files are collapsed by name-keyed
    # structures here (`_harnesses_by_parent`, `_processed_paths`, `LinkingDecision`,
    # implementing-contract sets) and by the signature database's name-keyed APIs. Eventually
    # we should propagate ContractHandle through all internal state and provide handle-keyed
    # signature-DB queries so the linker is sound under name collisions.

    def __init__(self, scope: Scope, skip_harnessing: bool = False):
        """
        Initialize the contract linker.

        Args:
            scope: Centralized scope for file filtering
        """
        self.project_root = scope.project_root
        self.scope = scope
        self.skip_harnessing = skip_harnessing

        # State tracking for incremental linking across iterations
        self._linking_decisions: Dict[str, LinkingDecision] = {}
        self._processed_paths: Set[tuple[str, str]] = set()  # (base_contract, path) already linked
        self._all_links: List[ContractLink] = []  # Accumulated links across all iterations
        # harness_name -> (parent_handle, harness_path)
        self._harness_contracts: Dict[str, tuple[ContractHandle, Path]] = {}
        self._harnesses_by_parent: Dict[str, List[str]] = defaultdict(list)  # parent_name -> [harness_names]

    @property
    def harnessed_contracts(self) -> Set[str]:
        """Contracts that have harness wrappers and should not be added to the scene directly."""
        return {parent.contract_name for parent, _ in self._harness_contracts.values()}

    def _generate_harness_contracts(
        self, contract_name: str, signature_database: SignatureDatabase, count: int = 2
    ) -> List[str]:
        """
        Generate harness wrapper contracts that inherit from the given contract.

        Creates certora/harnesses/{contract_name}_{i}.sol for i in 1..count.
        If the parent has constructor params, the harness forwards them.

        Returns:
            List of harness contract names
        """
        all_contracts = signature_database.get_all_contracts()
        assert contract_name in all_contracts, logger.error(f"Cannot generate harness for unknown contract {contract_name}")

        parent_info = all_contracts[contract_name]
        source_file = parent_info.source_file
        if not source_file.is_absolute():
            source_file = self.project_root / source_file

        parent_handle = ContractHandle(
            contract_name=contract_name,
            source_file=self._relative_path_str(source_file),
        )

        pragma_spec = signature_database.get_solidity_version(contract_name)
        pragma = f"pragma solidity {pragma_spec};" if pragma_spec else ""

        # Create harness directory
        harness_dir = user_harnesses_dir(self.project_root)
        harness_dir.mkdir(parents=True, exist_ok=True)

        # Compute relative import path from harness dir to the original source
        rel_import = os.path.relpath(source_file, harness_dir)

        harness_names: List[str] = []
        for i in range(1, count + 1):
            harness_name = f"{contract_name}_{i}"

            # Skip if already generated
            if harness_name in self._harness_contracts:
                harness_names.append(harness_name)
                continue

            harness_path = user_harness_path(self.project_root, harness_name)

            # Build constructor forwarding if parent has constructor params
            ctor_forward = None
            if parent_info.constructor_params:
                param_list = ", ".join(
                    f"{sol_type} a{j}" for j, (sol_type, _) in enumerate(parent_info.constructor_params)
                )
                arg_names = [f"a{j}" for j in range(len(parent_info.constructor_params))]
                ctor_forward = (param_list, arg_names)

            content = render_wrapper_contract(
                harness_name=harness_name,
                parent_name=contract_name,
                pragma_line=pragma,
                import_lines=[f'import "{rel_import}";'],
                ctor_forward=ctor_forward,
            )

            harness_path.write_text(content)
            logger.info(f"Generated harness contract: {harness_path}")

            self._harness_contracts[harness_name] = (parent_handle, harness_path)
            self._harnesses_by_parent[contract_name].append(harness_name)
            harness_names.append(harness_name)

        return harness_names

    def generate_links_from_call_resolution(
        self,
        unresolved_calls: list,
        signature_database: SignatureDatabase,
    ) -> tuple[List[ContractLink], list]:
        """
        Generate links from prover call resolution data.

        For unresolved calls with a storage path, uses the signature database to find
        implementing contracts. Groups calls by storage path and intersects implementations
        across all selectors for the same path.

        Args:
            unresolved_calls: List of CallResolutionInfo objects from the prover
            signature_database: SignatureDatabase for looking up implementing contracts

        Returns:
            Tuple of (links, remaining_calls_for_dispatcher)
        """
        # Group calls by storage path; calls without storage_path go to dispatcher
        storage_path_groups: Dict[tuple[str, str], list] = defaultdict(list)
        remaining_for_dispatcher: list = []

        for call in unresolved_calls:
            storage_path = getattr(call, "storage_path", None)
            if storage_path and storage_path.base_contract and storage_path.path:
                key = (storage_path.base_contract, storage_path.path)
                storage_path_groups[key].append(call)
            else:
                remaining_for_dispatcher.append(call)

        links: List[ContractLink] = []

        for (base_contract, path), calls in storage_path_groups.items():
            # Skip paths already processed in a previous iteration
            if (base_contract, path) in self._processed_paths:
                continue

            # Collect all distinct selectors for this storage path
            selectors: Set[str] = set()
            for call in calls:
                selector = getattr(call, "selector", None)
                if selector:
                    selectors.add(normalize_selector(selector))
                else:
                    # extract_sighash_from_callee already normalizes the extracted selector
                    extracted = extract_sighash_from_callee(call.callee_name)
                    if extracted:
                        selectors.add(extracted)

            if not selectors:
                logger.debug(f"No selectors found for {base_contract}.{path}, skipping")
                remaining_for_dispatcher.extend(calls)
                continue

            # For each selector, find implementing contracts; then intersect.
            # Strict: if a selector has zero implementers in scope, the intersection collapses
            # to empty and no link is established.
            impl_sets: List[Set[str]] = []
            missing_selectors: List[str] = []
            for selector in sorted(selectors):
                impls = signature_database.get_verifiable_implementing_contracts(selector)
                if not impls:
                    missing_selectors.append(selector)
                impl_sets.append(set(impls))

            common_impls = set.intersection(*impl_sets) if impl_sets else set()

            # Create linking decision
            sorted_impls = sorted(common_impls)
            decision_key = f"{base_contract}:{path}"

            if not self.skip_harnessing:
                # For indexed paths (arrays/mappings), generate harness contracts for distinct instances
                if sorted_impls and "[" in path:
                    expanded_impls = []
                    for impl in sorted_impls:
                        harness_names = self._generate_harness_contracts(impl, signature_database, count=2)
                        expanded_impls.extend(harness_names)
                    sorted_impls = sorted(expanded_impls)

            # Resolve names to full ContractHandles
            impl_handles: List[ContractHandle] = []
            for name in sorted_impls:
                handle = self._resolve_handle(name, signature_database)
                if handle:
                    impl_handles.append(handle)
                else:
                    logger.warning(f"Could not resolve source file for contract {name}, skipping")

            if impl_handles:
                link = ContractLink(
                    owner_contract=base_contract,
                    variable_path=path,
                    impl_contracts=impl_handles,
                )
                links.append(link)

                decision = LinkingDecision(
                    contract_name=base_contract,
                    variable_name=path,
                    selectors=sorted(selectors),
                    status=LinkStatus.LINKED,
                    link_targets=[h.contract_name for h in impl_handles],
                    reason=f"Found {len(impl_handles)} implementing contract(s) via signature database",
                )
                logger.info(
                    f"Linked {base_contract}.{path} => {[h.contract_name for h in impl_handles]}"
                )
            else:
                if missing_selectors:
                    reason = (
                        f"No contract in scope implements all called selectors. "
                        f"Missing implementer(s) for: {missing_selectors}"
                    )
                elif impl_sets:
                    reason = (
                        f"Selectors require different implementations, no single contract "
                        f"satisfies all: {sorted(selectors)}"
                    )
                else:
                    reason = f"No selectors found in signature database: {sorted(selectors)}"
                decision = LinkingDecision(
                    contract_name=base_contract,
                    variable_name=path,
                    selectors=sorted(selectors),
                    status=LinkStatus.UNRESOLVED,
                    reason=reason,
                    missing_selectors=missing_selectors,
                )
                logger.debug(
                    f"Could not link {base_contract}.{path}: {reason}"
                )
                # Falls through to dispatcher since we can't link it
                remaining_for_dispatcher.extend(calls)

            self._linking_decisions[decision_key] = decision
            self._processed_paths.add((base_contract, path))

        # Accumulate links across iterations
        self._all_links.extend(links)

        # Retroactively replace original contracts with harnesses in all prior links
        if self._harnesses_by_parent:
            for link in self._all_links:
                replaced: List[ContractHandle] = []
                changed = False
                for impl in link.impl_contracts:
                    harness_names = self._harnesses_by_parent.get(impl.contract_name)
                    if harness_names:
                        for h_name in sorted(harness_names):
                            h_handle = self._resolve_handle(h_name, signature_database)
                            if h_handle:
                                replaced.append(h_handle)
                        changed = True
                    else:
                        replaced.append(impl)
                if changed:
                    link.impl_contracts = replaced

        return links, remaining_for_dispatcher

    def get_linked_contracts(self, links: List[ContractLink]) -> List[ContractHandle]:
        """Extract all target contracts referenced in links (deduplicated)."""
        seen: Set[tuple[str, str]] = set()
        result: List[ContractHandle] = []
        for link in links:
            for handle in link.impl_contracts:
                key = (handle.contract_name, handle.source_file)
                if key not in seen:
                    seen.add(key)
                    result.append(handle)
        return result

    def _relative_path_str(self, path: Path) -> str:
        """Convert a path to a string relative to project_root."""
        try:
            return str(path.relative_to(self.project_root) if path.is_absolute() else path)
        except ValueError:
            return str(path)

    def _resolve_handle(self, contract_name: str, signature_database: SignatureDatabase) -> Optional[ContractHandle]:
        """Resolve a contract name to a ContractHandle using the signature database or harness registry.

        Note: the signature database is keyed by contract name, so same-name contracts in different
        files are not distinguishable here. If that changes, this should return multiple handles.
        """
        if contract_name in self._harness_contracts:
            _, harness_path = self._harness_contracts[contract_name]
            return ContractHandle(contract_name=contract_name, source_file=self._relative_path_str(harness_path))

        source_file = signature_database.get_source_file_for_contract(contract_name)
        if not source_file:
            return None
        return ContractHandle(contract_name=contract_name, source_file=self._relative_path_str(source_file))

    def get_additional_source_files(self, links: List[ContractLink]) -> List[ContractHandle]:
        """Get additional source files needed based on the links."""
        return self.get_linked_contracts(links)

    def _generate_alias(self, contract_name: str) -> str:
        """Generate a CVL alias for a contract name by lowercasing the first letter."""
        if not contract_name:
            return contract_name
        return contract_name[0].lower() + contract_name[1:]

    def generate_links_spec_lines(self) -> List[str]:
        """
        Generate CVL spec lines for all accumulated links (using statements + links block).

        Returns:
            List of spec lines, or empty list if no links
        """
        all_links = self._all_links
        if not all_links:
            return []

        # Collect all unique contract names and generate aliases
        all_contract_names: Set[str] = set()
        for link in all_links:
            all_contract_names.add(link.owner_contract)
            all_contract_names.update(h.contract_name for h in link.impl_contracts)

        # Generate aliases, handling collisions
        aliases: Dict[str, str] = {}
        used_aliases: Set[str] = set()
        for name in sorted(all_contract_names):
            alias = self._generate_alias(name)
            if alias in used_aliases:
                suffix = 2
                while f"{alias}{suffix}" in used_aliases:
                    suffix += 1
                alias = f"{alias}{suffix}"
            aliases[name] = alias
            used_aliases.add(alias)

        spec_lines: List[str] = []

        # Using statements
        for name in sorted(all_contract_names):
            spec_lines.append(f"using {name} as {aliases[name]};")

        spec_lines.append("")

        # Links block
        spec_lines.append("links {")
        for link in all_links:
            owner_alias = aliases[link.owner_contract]
            if len(link.impl_contracts) == 1:
                impl_alias = aliases[link.impl_contracts[0].contract_name]
                spec_lines.append(f"    {owner_alias}.{link.variable_path} => {impl_alias};")
            else:
                impl_aliases = ", ".join(aliases[h.contract_name] for h in link.impl_contracts)
                spec_lines.append(f"    {owner_alias}.{link.variable_path} => [{impl_aliases}];")
        spec_lines.append("}")
        spec_lines.append("")

        return spec_lines
