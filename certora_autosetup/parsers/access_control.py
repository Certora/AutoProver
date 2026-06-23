"""Utilities for detecting AccessControl inheritance patterns in Solidity contracts."""

from certora_autosetup.setup.signature_types import InheritanceGraph
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


def detect_access_control_contracts(
    contracts: list[ContractHandle],
    inheritance_graph: InheritanceGraph,
):
    """Detect which contracts inherit from AccessControl or AccessControlUpgradeable by checking the inheritance graph."""
    contracts_with_access_control = {}  # Returns a dict: {contract_name: 'regular' or 'upgradeable'}

    logger.info(f"Inheritance graph has {len(inheritance_graph._graph)} contract entries")
    for handle in contracts:
        contract_name = handle.contract_name

        # Check which type of AccessControl this contract inherits from
        access_control_type = get_access_control_type(contract_name, inheritance_graph)
        if access_control_type:
            contracts_with_access_control[contract_name] = access_control_type
            logger.info(f"✓ Found {access_control_type} AccessControl inheritance in {contract_name}")

    return contracts_with_access_control


def get_access_control_type(
    contract_name: str,
    inheritance_graph: InheritanceGraph,
) -> str | None:
    """Check if a contract inherits from AccessControl or AccessControlUpgradeable and return the type."""
    # Check if this contract exists in the inheritance graph
    if inheritance_graph.find_handle_by_name(contract_name) is None:
        return None

    # BFS to check all parent contracts
    visited: set[str] = set()
    queue = [contract_name]
    found_type = None

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if current == "AccessControlUpgradeable":
            found_type = "upgradeable"
            break
        elif current == "AccessControl":
            # Only set to regular if we haven't found upgradeable yet
            if not found_type:
                found_type = "regular"
            # Continue searching in case there's AccessControlUpgradeable higher up

        # Add parent contracts to queue
        current_handle = inheritance_graph.find_handle_by_name(current)
        if current_handle is not None:
            for parent_handle in inheritance_graph.get_parents(current_handle):
                if parent_handle.contract_name not in visited:
                    queue.append(parent_handle.contract_name)

    return found_type
