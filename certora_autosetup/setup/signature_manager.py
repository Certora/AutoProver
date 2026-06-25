#!/usr/bin/env python3
"""
Signature Database Manager for PreAudit.

This module handles creation and management of function signature databases
from Certora build artifacts, similar to the autosetup functionality.
"""


import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections.abc import Set as AbstractSet

from certora_autosetup.setup.signature_types import (
    SignatureDatabase,
    compute_signature_selector,
)
from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL, DIR_SIGNATURE_STATE
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import (
    ContractInfo,
    ContractKind,
    FunctionSignature,
    TypeParseMode,
    parse_type_descriptor,
)


class SignatureManager:
    """
    Manages function signature database creation and manipulation.

    Extracts signatures from Certora build artifacts and provides
    database management functionality.
    """

    def __init__(self, project_root: Path):
        """
        Initialize the signature manager.

        Args:
            project_root: Root directory of the project
        """
        self.project_root = project_root
        self.signature_database = SignatureDatabase(project_root=project_root)
        self.build_json_path: Optional[Path] = None
    def extract_signatures_from_build(
        self, build_json_path: Path
    ) -> Dict[str, Dict[str, Any]]:
        """
        Extract function signatures from the compiled build JSON.

        Args:
            build_json_path: Path to the Certora build JSON file

        Returns:
            Dictionary mapping selector -> signature info
        """
        self.build_json_path = build_json_path

        if not build_json_path.exists():
            logger.error(f"Build JSON not found: {build_json_path}")
            return {}

        try:
            with open(build_json_path, "r") as f:
                build_data = json.load(f)

            signatures = {}

            for contract_key, contract_data in build_data.items():
                if (
                    not isinstance(contract_data, dict)
                    or "contracts" not in contract_data
                ):
                    continue

                for contract in contract_data.get("contracts", []):
                    if not isinstance(contract, dict):
                        continue

                    methods = contract.get("methods", [])
                    if not methods:
                        continue

                    # Get contract name from first method
                    contract_name = methods[0].get("contractName", "Unknown")

                    for method in methods:
                        method_name = method.get("name", "")

                        if method_name == "constructor":
                            continue

                        # Get the sighash directly from Certora (this is the correct ABI selector)
                        sighash_str = str(method.get("sighash", "0"))

                        # Skip methods with zero sighash (internal/constructor methods)
                        if sighash_str == "0":
                            continue

                        certora_selector = self._convert_sighash_to_selector(sighash_str)

                        # Build canonical and internal parameter types
                        canonical_param_types = []
                        internal_param_types = []
                        type_descs = []

                        for arg in method.get("fullArgs", []):
                            type_desc = arg.get("typeDesc", {})
                            type_descs.append(type_desc)
                            # Get canonical type (MarketId -> bytes32, InternalUserData -> (address,uint256,bool))
                            canonical_type = parse_type_descriptor(type_desc, TypeParseMode.CANONICAL)
                            canonical_param_types.append(canonical_type)

                            # Get internal type (keeps MarketId as MarketId, InternalUserData as InternalUserData)
                            internal_type = parse_type_descriptor(type_desc, TypeParseMode.INTERNAL)
                            internal_param_types.append(internal_type)

                        # Build signatures
                        canonical_signature = (
                            f"{method_name}({','.join(canonical_param_types)})"
                        )
                        internal_type_signature = (
                            f"{method_name}({','.join(internal_param_types)})"
                        )

                        # Use Certora's sighash directly - it's already computed correctly!
                        canonical_selector = certora_selector

                        # For internal selector: if signatures differ, compute it; otherwise reuse canonical
                        if canonical_signature == internal_type_signature:
                            # No user-defined types, selectors are identical
                            internal_selector = canonical_selector
                        else:
                            # Different signatures due to user-defined types, compute internal selector
                            internal_selector = compute_signature_selector(internal_type_signature)
                            if not internal_selector or internal_selector == "0x00000000":
                                # Fallback to canonical selector if computation fails
                                logger.debug(
                                    f"Internal selector computation failed for {internal_type_signature}, using canonical"
                                )
                                internal_selector = canonical_selector

                        if not canonical_selector:
                            logger.warning(
                                f"Failed to get valid selectors for: {canonical_signature} / {internal_type_signature}"
                            )
                            continue

                        # Generate dispatcher entry name with contract-qualified types
                        dispatcher_entry_name = self._generate_dispatcher_entry_name(
                            method_name, type_descs, contract_name
                        )

                        # Get state mutability info
                        state_mutability = method.get("stateMutability", "nonpayable")
                        is_view = state_mutability in ["view", "pure"]
                        is_pure = state_mutability == "pure"

                        # Create signature info object
                        signature_info = {
                            "signature": canonical_signature,
                            "selector": canonical_selector,
                            "internal_type_signature": internal_type_signature,
                            "internal_type_selector": internal_selector,
                            "dispatcher_entry_name": dispatcher_entry_name,
                            "is_view": is_view,
                            "is_pure": is_pure,
                            "source_file": method.get("originalFile", ""),
                        }

                        # Store by canonical selector, accumulating all implementing contracts
                        if canonical_selector in signatures:
                            signatures[canonical_selector]["contracts"].add(contract_name)
                        else:
                            signature_info["contracts"] = {contract_name}
                            signatures[canonical_selector] = signature_info

                        # Also store by internal selector for dispatcher lookup
                        if internal_selector != canonical_selector:
                            if internal_selector in signatures:
                                signatures[internal_selector]["contracts"].add(contract_name)
                            else:
                                internal_info = dict(signature_info)
                                internal_info["contracts"] = {contract_name}
                                signatures[internal_selector] = internal_info

            logger.info(f"Extracted {len(signatures)} function signatures")
            return signatures

        except Exception as e:
            logger.error(f"Error extracting signatures from build: {e}")
            return {}

    def populate_signature_database(
        self,
        contract_infos: List[ContractInfo],
        signature_data: Dict[str, Dict[str, Any]],
        abstract_contracts: AbstractSet[str] = frozenset(),
    ) -> None:
        """
        Populate the signature database with contract information and signatures.

        Args:
            contract_infos: List of contract information objects
            signature_data: Signature data extracted from build JSON
            abstract_contracts: Set of contract names that are abstract (defaults to empty set)
        """
        # Add all contract information
        for contract_info in contract_infos:
            # Skip abstract contracts - they cannot be deployed/compiled
            logger.debug(f"Checking whether {contract_info.name} in {abstract_contracts}")
            if contract_info.name in abstract_contracts:
                logger.debug(f"Skipping abstract contract {contract_info.name}")
                continue

            if contract_info.kind == ContractKind.INTERFACE:
                logger.debug(f"Skipping interface {contract_info.name}")
                continue

            self.signature_database.add_contract(contract_info)

            # Add function signatures if available
            if contract_info.function_signatures:
                for signature in contract_info.function_signatures.values():
                    self.signature_database.add_signature(signature, contract_info.name)

        # Add signatures from build data
        for selector, sig_info in signature_data.items():
            signature = FunctionSignature(
                signature=sig_info["signature"],
                selector=sig_info["selector"],
                is_view=sig_info.get("is_view", False),
                is_pure=sig_info.get("is_pure", False),
                internal_type_signature=sig_info.get("internal_type_signature"),
                internal_type_selector=sig_info.get("internal_type_selector"),
                dispatcher_entry_name=sig_info.get("dispatcher_entry_name"),
            )

            # Register for all implementing contracts
            contract_names = sig_info.get("contracts", [])
            for contract_name in contract_names:
                if contract_name in abstract_contracts:
                    logger.debug(f"Skipping signature from abstract contract {contract_name}")
                    continue
                self.signature_database.add_signature(signature, contract_name)

        signature_count = len(self.signature_database.get_all_signatures())

        logger.info(
            f"Populated signature database with {len(contract_infos)} contracts "
            f"({signature_count} total signatures)"
        )

    def get_signature_db_path(self) -> Path:
        """Canonical *local* path for the signature database JSON file."""
        return self.project_root / DIR_CERTORA_INTERNAL / DIR_SIGNATURE_STATE / "signature_database.json"

    @staticmethod
    def signature_db_cache_path() -> str:
        """Canonical fsspec cache path for the signature database JSON file.

        Single source of truth shared by the writer (``dump_signature_database``)
        and the readers (``load_from_json`` / existence checks). Resolves to a
        local path in CLI mode and to ``s3://…`` under the SaaS cache prefix —
        so the autosetup cache-hit path can find the DB that was persisted to S3.
        """
        return cache_path(DIR_CERTORA_INTERNAL, DIR_SIGNATURE_STATE, "signature_database.json")

    def dump_signature_database(self, output_file: Optional[Path] = None) -> Path:
        """
        Dump the complete signature database to JSON for debugging and analysis.

        Args:
            output_file: Optional custom output path (defaults to .certora_internal/preaudit_state/signature_database.json)

        Returns:
            Path to the created dump file
        """
        use_cache_fs = output_file is None
        if use_cache_fs:
            fs = get_fs()
            cache_file = cache_path(DIR_CERTORA_INTERNAL, DIR_SIGNATURE_STATE, "signature_database.json")
            fs.mkdirs(cache_path(DIR_CERTORA_INTERNAL, DIR_SIGNATURE_STATE), exist_ok=True)

        # Collect all data
        all_contracts = self.signature_database.get_all_contracts()
        all_signatures = self.signature_database.get_all_signatures()

        # Build comprehensive dump
        dump_data: dict[str, Any] = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "project_root": str(self.project_root),
                "total_contracts": len(all_contracts),
                "total_signatures": len(all_signatures),
            },
            "contracts": {},
            "signatures": {},
            "statistics": {
                "by_kind": {},
                "compilable_contracts": 0,
                "abstract_contracts": 0,
                "contracts_with_signatures": 0,
            },
        }

        # Process all contracts
        for contract_name, contract_info in all_contracts.items():
            # Convert ContractInfo to serializable dict
            contract_data = {
                "name": contract_info.name,
                "kind": contract_info.kind.value,
                "source_file": str(contract_info.source_file),
                "inherits_from": contract_info.inherits_from,
                "compilation_error": contract_info.compilation_error,
                "solidity_version": contract_info.solidity_version,
                "artifact_path": str(contract_info.artifact_path)
                if contract_info.artifact_path
                else None,
                "bytecode": contract_info.bytecode[:100] + "..."
                if contract_info.bytecode and len(contract_info.bytecode) > 100
                else contract_info.bytecode,
                "function_signatures": {
                    selector: {
                        "signature": sig.signature,
                        "selector": sig.selector,
                        "is_view": sig.is_view,
                        "is_pure": sig.is_pure,
                        "internal_type_signature": sig.internal_type_signature,
                        "internal_type_selector": sig.internal_type_selector,
                    }
                    for selector, sig in contract_info.function_signatures.items()
                },
                "compilation_metadata": contract_info.compilation_metadata,
                "constructor_params": contract_info.constructor_params,
            }

            dump_data["contracts"][contract_name] = contract_data

            # Update statistics
            kind_value = contract_info.kind.value
            dump_data["statistics"]["by_kind"][kind_value] = (
                dump_data["statistics"]["by_kind"].get(kind_value, 0) + 1
            )

            if contract_info.kind.value == "abstract":
                dump_data["statistics"]["abstract_contracts"] += 1
            if contract_info.function_signatures:
                dump_data["statistics"]["contracts_with_signatures"] += 1

        # Process all signatures with implementing contracts info
        for selector, signature in all_signatures.items():
            implementing_contracts = self.signature_database.get_implementing_contracts(
                selector
            )

            # Categorize implementing contracts
            concrete_implementations = []
            abstract_implementations = []
            interface_implementations = []

            for contract_name in implementing_contracts:
                contract_info_lookup: ContractInfo | None = all_contracts.get(contract_name)
                if contract_info_lookup:
                    if contract_info_lookup.kind.value == "abstract":
                        abstract_implementations.append(contract_name)
                    elif contract_info_lookup.kind.value == "interface":
                        interface_implementations.append(contract_name)
                    else:
                        concrete_implementations.append(contract_name)
                else:
                    # Contract not found in our database (should not happen)
                    concrete_implementations.append(f"{contract_name} (MISSING)")

            signature_data = signature.to_dict()
            signature_data.update({
                "implementing_contracts": {
                    "concrete": concrete_implementations,
                    "abstract": abstract_implementations,
                    "interfaces": interface_implementations,
                    "total_count": len(implementing_contracts),
                },
                "resolution_analysis": {
                    "has_concrete_implementation": len(concrete_implementations) > 0,
                    "only_abstract_or_interface": len(concrete_implementations) == 0
                    and len(implementing_contracts) > 0,
                    "unresolved": len(implementing_contracts) == 0,
                },
            })

            dump_data["signatures"][selector] = signature_data

        # Add signature resolution summary
        concrete_resolvable = sum(
            1
            for sig in dump_data["signatures"].values()
            if sig["resolution_analysis"]["has_concrete_implementation"]
        )
        unresolved = sum(
            1
            for sig in dump_data["signatures"].values()
            if sig["resolution_analysis"]["unresolved"]
        )

        dump_data["statistics"]["signature_resolution"] = {
            "concrete_resolvable": concrete_resolvable,
            "abstract_only": len(all_signatures) - concrete_resolvable - unresolved,
            "unresolved": unresolved,
            "resolution_rate": concrete_resolvable / len(all_signatures)
            if all_signatures
            else 0,
        }

        # Write to file
        if use_cache_fs:
            with fs.open(cache_file, "w") as f:
                json.dump(dump_data, f, indent=2)
            logger.info(f"Signature database dumped to: {cache_file}")
            return self.get_signature_db_path()
        else:
            with open(output_file, "w") as f:  # type: ignore[arg-type]
                json.dump(dump_data, f, indent=2)
            logger.info(f"Signature database dumped to: {output_file}")
        logger.info(f"Statistics: {dump_data['statistics']}")

        return output_file

    def _convert_sighash_to_selector(self, sighash_str: str) -> Optional[str]:
        """Convert Certora sighash to standard 0x-prefixed selector."""
        try:
            # Try parsing as hex first (common case: "95d89b41")
            if len(sighash_str) == 8 and all(c in '0123456789abcdefABCDEF' for c in sighash_str):
                return f"0x{sighash_str.lower()}"
            # Fallback: try parsing as decimal
            sighash_int = int(sighash_str)
            return f"0x{sighash_int:08x}"
        except ValueError:
            logger.warning(f"Failed to convert sighash to selector: '{sighash_str}'")
            return None

    def _generate_dispatcher_entry_name(
        self, method_name: str, type_descs: List[Dict], contract_name: str
    ) -> str:
        """Generate dispatcher entry name with contract-qualified types.

        Contract/interface types (e.g. ISomeContract, IERC20) are replaced with address,
        while other user-defined types (structs, enums, value types) are qualified with
        the defining contract extracted from the type descriptor's canonicalId, falling
        back to contract_name.
        """
        qualified_types = [parse_type_descriptor(td, TypeParseMode.DISPATCHER, contract_name) for td in type_descs]
        return f"{method_name}({','.join(qualified_types)})"

    def get_signature_database(self) -> SignatureDatabase:
        """Get the current signature database."""
        return self.signature_database

    def get_implementing_contracts(self, selector: str) -> List[str]:
        """Get all contracts that implement a given selector."""
        return self.signature_database.get_implementing_contracts(selector)

    def get_signature(self, selector: str) -> Optional[FunctionSignature]:
        """Get the function signature for a selector."""
        return self.signature_database.get_signature(selector)

    def load_from_json(self, json_file: Path | str) -> None:
        """
        Load signature database from a JSON dump file created by dump_signature_database().

        Reads through ``get_fs()`` so it resolves both a local path (CLI) and an
        ``s3://…`` cache path (SaaS) — the DB is written via fsspec by
        ``dump_signature_database``, so the cache-hit reader must read it the same
        way. Pass ``signature_db_cache_path()`` for the canonical location.

        Args:
            json_file: Path to the JSON dump file containing contracts and signatures
        """
        fs = get_fs()
        json_file = str(json_file)
        if not fs.exists(json_file):
            raise FileNotFoundError(f"Signature database file not found: {json_file}")

        logger.debug(f"Loading signature database from: {json_file}")

        with fs.open(json_file, "r") as f:
            db_data = json.load(f)

        # Extract and create contract infos from contracts section
        contract_infos = {}  # Use dict for easy lookup by contract_name
        if "contracts" in db_data:
            for contract_name, contract_data in db_data["contracts"].items():
                # Resolve source_file path relative to project_root if it's relative
                source_file_path = Path(contract_data["source_file"])
                if not source_file_path.is_absolute():
                    source_file_path = self.project_root / source_file_path

                contract_info = ContractInfo(
                    name=contract_data["name"],
                    kind=ContractKind(contract_data["kind"]),
                    source_file=source_file_path,
                    inherits_from=contract_data["inherits_from"],
                    is_compilable=(contract_data["compilation_error"] is None),
                    compilation_error=contract_data["compilation_error"],
                    solidity_version=contract_data["solidity_version"],
                    artifact_path=Path(contract_data["artifact_path"]) if contract_data["artifact_path"] else None,
                    bytecode=contract_data["bytecode"],
                    function_signatures={},  # Will be populated below
                    compilation_metadata=contract_data["compilation_metadata"],
                    state_vars=None,  # Initialize as None for JSON-loaded contracts
                    constructor_params=[tuple(p) for p in contract_data["constructor_params"]]
                    if contract_data.get("constructor_params")
                    else None,
                )
                contract_infos[contract_name] = contract_info

        # Extract signature data from signatures section
        # Add signatures directly to the database for all implementing contracts
        if "signatures" in db_data:
            for selector, sig_data in db_data["signatures"].items():
                # Get all concrete implementing contracts
                concrete_contracts = sig_data.get("implementing_contracts", {}).get("concrete", [])

                if concrete_contracts:
                    # Create the function signature object
                    signature = FunctionSignature(
                        signature=sig_data["signature"],
                        selector=sig_data["selector"],
                        is_view=sig_data.get("is_view", False),
                        is_pure=sig_data.get("is_pure", False),
                        internal_type_signature=sig_data.get("internal_type_signature"),
                        internal_type_selector=sig_data.get("internal_type_selector"),
                        dispatcher_entry_name=sig_data.get("dispatcher_entry_name"),
                    )

                    # Add the signature for each implementing contract
                    for contract_name in concrete_contracts:
                        self.signature_database.add_signature(signature, contract_name)

                        # Also populate ContractInfo.function_signatures
                        if contract_name in contract_infos:
                            contract_infos[contract_name].function_signatures[selector] = signature

        # Add contracts directly to database (consistent with how signatures were added)
        for contract_info in contract_infos.values():
            # Skip interfaces and abstract contracts - they cannot be deployed
            if contract_info.kind in (ContractKind.INTERFACE, ContractKind.ABSTRACT):
                logger.debug(f"Skipping interface/abstract contract {contract_info.name}")
                continue

            self.signature_database.add_contract(contract_info)

        total_contracts = len([c for c in contract_infos.values() if c.kind not in (ContractKind.INTERFACE, ContractKind.ABSTRACT)])
        total_signatures = len(self.signature_database.get_all_signatures())
        logger.debug(f"Populated signature database from the file JSON {json_file}")
        logger.debug(f"Loaded {total_contracts} contracts and {total_signatures} signatures from JSON")

