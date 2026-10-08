"""Turn one compilation into signature-database entries.

The input is what ``certoraRun --compilation_steps_only --dump_asts`` leaves behind: a build
JSON, and the AST dump written beside it. The build JSON carries selectors, methods and
source files; the AST dump carries inheritance and abstractness, which selectors alone cannot
recover and which decide whether a contract is deployable at all — a link to an interface or
an abstract contract yields a scene that will not compile.

Separated from :mod:`setup_prover` for two reasons. A caller that wants only the index should
not have to run the setup phase to get it, and — because :func:`index_build` takes the
database to write into — an index can be *extended* by a later, smaller compilation rather
than rebuilt from the whole scene. Indexing one added contract then costs one contract's
compile instead of the project's.

Nothing here writes: no dump, no cache prefix, no working-directory assumption. Paths come
from arguments.
"""

import json
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from certora_autosetup.setup.signature_manager import SignatureManager
from certora_autosetup.setup.signature_types import ContractInfo, SignatureDatabase
from certora_autosetup.solidity_ast import AstDump, iter_contract_declarations
from certora_autosetup.utils.build_json import contract_source_file, iter_contracts
from certora_autosetup.utils.types import (
    ContractKind, TypeParseMode, parse_type_descriptor,
)

#: ``(message, level="INFO") -> None``, matching ``SetupProver.log``.
Log = Callable[..., None]


def _silent(message: str, level: str = "INFO") -> None:
    """Default log sink. Indexing is a query; a caller that wants narration passes one."""


def inheritance_and_abstract_from_ast(
    ast_file_path: Optional[Path], log: Log = _silent,
) -> tuple[Dict[str, List[str]], Set[str]]:
    """Extract inheritance information and abstract contracts from AST dumps.

    Args:
        ast_file_path: Path to the .asts.json file, or None if not available

    Returns:
        Tuple of (inheritance_info, abstract_contracts) where:
        - inheritance_info: Dict mapping contract_name -> list of parent contracts
        - abstract_contracts: Set of contract names that are abstract or interfaces
    """
    inheritance_info = {}
    abstract_contracts = set()

    if not ast_file_path:
        log("No AST file provided - inheritance info will be missing", "WARNING")
        return inheritance_info, abstract_contracts

    try:
        log(f"Extracting inheritance info from {ast_file_path}")

        # Stream the (multi-GB) .asts.json once, keeping only the slim per-contract
        # views needed below so the dump is never fully materialized.
        declarations = list(iter_contract_declarations(AstDump.stream_units(ast_file_path)))

        # Build ID to contract name mapping once
        id_to_name = {
            decl.node_id: decl.name for decl in declarations if decl.node_id and decl.name
        }

        # Now process contracts and resolve inheritance using the pre-built mapping
        for decl in declarations:
            if not decl.name:
                continue
            # Check if abstract or interface
            if decl.abstract or decl.contract_kind is ContractKind.INTERFACE:
                abstract_contracts.add(decl.name)
                log(f"Identified {'abstract' if decl.abstract else 'interface'}: {decl.name}", "DEBUG")

            # Get linearized base contracts (includes self + all inherited contracts)
            linearized = decl.linearized_base_ids
            if len(linearized) > 1:  # More than just self
                # Convert IDs to contract names using pre-built mapping
                base_contracts = [id_to_name[contract_id] for contract_id in linearized[1:] if contract_id in id_to_name]
                if base_contracts:
                    inheritance_info[decl.name] = base_contracts

        log(f"Extracted inheritance for {len(inheritance_info)} contracts", "DEBUG")
        log(f"Found {len(abstract_contracts)} abstract/interface contracts to skip", "INFO")
        return inheritance_info, abstract_contracts

    except Exception as e:
        log(f"Failed to extract inheritance from AST: {e}", "WARNING")
        return inheritance_info, abstract_contracts


def merge_inheritance_info(
    contract_infos: List[ContractInfo], inheritance_info: Dict[str, List[str]],
) -> None:
    """Merge inheritance information into ContractInfo objects in place."""
    for contract_info in contract_infos:
        if contract_info.name in inheritance_info:
            contract_info.inherits_from = (contract_info.inherits_from or []) + inheritance_info[contract_info.name]


def contract_infos_from_build(
    build_json_path: Path, log: Log = _silent,
) -> List[ContractInfo]:
    """Extract contract information from build JSON to create ContractInfo objects."""
    contract_infos = []

    try:
        with open(build_json_path, "r") as f:
            build_data = json.load(f)

        seen_contracts = set()

        # Discover contracts and create ContractInfo objects
        for contract in iter_contracts(build_data):
            methods = contract.get("methods", [])
            if not methods:
                continue

            contract_name = contract.get("name", "Unknown")

            # Skip if already processed
            if contract_name in seen_contracts:
                continue
            seen_contracts.add(contract_name)

            # Get source file directly from contract object (canonical source)
            source_file_str = contract_source_file(contract)

            # Fall back to method inspection only if contract-level fields are missing
            if not source_file_str:
                fallback_source = None
                for method in methods:
                    original_file = method.get("originalFile")
                    if original_file:
                        fallback_source = original_file

                    # Prefer file that matches contract name (where contract is actually defined)
                    if original_file.endswith(f"/{contract_name}.sol") or original_file.endswith(f"\\{contract_name}.sol"):
                        source_file_str = original_file
                        break
                if not source_file_str and fallback_source:
                    source_file_str = fallback_source

            # Final fallback
            if not source_file_str:
                source_file_str = "unknown.sol"

            # Determine contract kind (basic heuristic)
            is_library = any(
                method.get("isLibrary", False) for method in methods
            )
            kind = ContractKind.LIBRARY if is_library else ContractKind.CONTRACT

            # Extract constructor params
            ctor_params = None
            for method in methods:
                if method.get("name", "") == "constructor":
                    params = []
                    for arg, param_name in zip(
                        method.get("fullArgs", []), method.get("paramNames", [])
                    ):
                        type_desc = arg.get("typeDesc", {})
                        sol_type = parse_type_descriptor(type_desc, TypeParseMode.SOLIDITY)
                        location = arg.get("location", "")
                        if location in ("memory", "calldata", "storage"):
                            sol_type = f"{sol_type} {location}"
                        params.append((sol_type, param_name))
                    if params:
                        ctor_params = params
                    break

            # Create contract info with inheritance
            contract_info = ContractInfo(
                name=contract_name,
                kind=kind,
                source_file=Path(source_file_str),
                inherits_from=[],  # added later via _extract_inheritance_from_ast()
                artifact_path=build_json_path,
                constructor_params=ctor_params,
            )

            contract_infos.append(contract_info)

        return contract_infos

    except Exception as e:
        log(f"Error extracting contract infos: {e}", "ERROR")
        return []


def index_build(
    build_json_path: Path,
    ast_file_path: Optional[Path] = None,
    *,
    project_root: Path,
    into: Optional[SignatureDatabase] = None,
    log: Log = _silent,
) -> SignatureDatabase:
    """Index one compilation, creating a database or extending ``into``.

    ``into`` is what makes this usable incrementally: pass an existing database and the
    contracts in this build are added to it, replacing any entry of the same name. A caller
    that has compiled one new contract can therefore index just that contract, rather than
    recompiling everything indexed so far to learn one thing.

    ``ast_file_path`` may be omitted, and the result is still usable — but every interface
    and abstract contract then looks deployable, so anything choosing link targets from the
    result should pass it.
    """
    manager = SignatureManager(project_root, database=into)
    signatures = manager.extract_signatures_from_build(build_json_path)
    contract_infos = contract_infos_from_build(build_json_path, log)

    inheritance_info, abstract_contracts = inheritance_and_abstract_from_ast(ast_file_path, log)
    if inheritance_info:
        merge_inheritance_info(contract_infos, inheritance_info)

    # Forget the previous version of anything this build redefines, so a re-index replaces
    # rather than accumulates. Without it a contract that dropped a function stays listed as
    # implementing it, because ``add_signature`` only ever adds to an implementor set.
    if into is not None:
        for contract_info in contract_infos:
            into.remove_contract(contract_info.name)

    log(f"Detected abstract contracts: {abstract_contracts}")
    manager.populate_signature_database(contract_infos, signatures, abstract_contracts)
    return manager.get_signature_database()
