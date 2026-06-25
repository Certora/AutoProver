"""
Automatic source code munging/patching based on AST analysis.

This module provides functions to detect patterns in compiled Solidity ASTs
and create patches to automatically rewrite problematic code patterns.
"""

import json
import re
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from certora_autosetup.utils.scope import Scope

CODE_ACCESS_PATCH_FILE = ".certora_internal/code_access_patches.json"


@dataclass
class CodeAccessPatch:
    """Represents a patch to replace a .code access with loadCode() call."""

    file: str
    offset: int
    length: int
    original: str
    replacement: str

LOAD_CODE_FUNCTION = """
    function certora_loadCode(address a) internal view returns (bytes memory) {
		bytes memory code;
		assembly {
    		code := mload(0x40)
            let sz := extcodesize(a)
            let round := and(add(0x1f, sz), not(0x1f))
            let new_fp := add(code, add(0x20, round))
            mstore(0x40, new_fp)
            mstore(code, sz)
            let data := add(code, 0x20)
            extcodecopy(a, data, 0, sz)
            mstore(add(data, sz), 0)
		}
		return code;
	}
"""


def _find_end_brace_after(content: str, start_after: int) -> int:
    """
    Find the matching closing brace for an opening brace.

    Args:
        content: The text content
        start_after: Position in the content 
                     after which we search for the first opening/closing brace pair

    Returns:
        Position of the closing brace, or -1 if not found
    """
    # Find the opening brace
    brace_start = content.find('{', start_after)
    if brace_start == -1:
        return -1
    brace_count = 0
    for i in range(brace_start, len(content)):
        if content[i] == '{':
            brace_count += 1
        elif content[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                return i
    return -1


def _find_contract_at_offset(content: str, offset: int) -> Tuple[int, int, str]:
    """
    Find in content the contract definition that contains the given offset.

    Args:
        content: The Solidity file content
        offset: Character offset in the file

    Returns:
        Tuple of (contract_start, contract_end, contract_name)
        Returns (-1, -1, "") if no contract found
    """
    # Pattern to match contract/interface/library declarations
    pattern = r'\b(contract|interface|library)\s+(\w+)'

    contracts = []
    for match in re.finditer(pattern, content):
        contract_type = match.group(1)
        contract_name = match.group(2)
        contract_start = match.start()

        # Find the closing brace of the contract
        brace_end = _find_end_brace_after(content, contract_start)
        if brace_end == -1:
            continue

        contracts.append((contract_start, brace_end + 1, contract_name))

    # Find which contract contains the offset
    for contract_start, contract_end, contract_name in contracts:
        if contract_start <= offset < contract_end:
            return (contract_start, contract_end, contract_name)

    return (-1, -1, "")


def _inject_load_code_into_contract(original_content: str, modified_content: str, patches: List[CodeAccessPatch]) -> str:
    """
    Inject loadCode function into contracts that have patches applied.

    Args:
        original_content: The original Solidity file content (before patches, for offset calculation)
        modified_content: The modified content after patches have been applied
        patches: List of patches that were applied (sorted by offset descending)

    Returns:
        Content with loadCode function injected
    """
    # Check if loadCode already exists
    if 'function certora_loadCode(address' in modified_content:
        return modified_content

    # Find all unique contracts that need loadCode injected using original offsets
    contracts_needing_injection = set()
    for patch in patches:
        offset = patch.offset
        contract_start, contract_end, contract_name = _find_contract_at_offset(original_content, offset)
        if contract_start != -1:
            contracts_needing_injection.add((contract_name,))

    # Inject loadCode into each contract by finding it in the modified content
    result = modified_content
    for (contract_name,) in contracts_needing_injection:
        # Find this contract in the modified content by name
        pattern = r'\b(contract|interface|library)\s+' + re.escape(contract_name) + r'\b'
        match = re.search(pattern, result)
        if not match:
            continue

        # Find the closing brace of the contract
        brace_end = _find_end_brace_after(result, match.start())
        if brace_end == -1:
            continue

        # Inject before the closing brace
        result = result[:brace_end] + LOAD_CODE_FUNCTION + '\n' + result[brace_end:]

    return result


def detect_and_apply_code_access_patches(
    log_func: Callable, ast_path: Path, ast_graph_path: Path, scope: Scope
) -> bool:
    """
    Detect .code accesses in the AST, create patches, and apply them.

    Args:
        log_func: Logging function to use for output (signature: log_func(message, level="INFO"))
        ast_path: Path to the .asts.json file
        ast_graph_path: Path to the .ast_parent_graph.json file
        scope: Scope object to filter files

    Returns:
        True if any patches were applied, False otherwise
    """
    # First detect and create patches
    detect_code_accesses(log_func, ast_path, ast_graph_path, scope)

    # Then apply the patches
    patch_file = Path(CODE_ACCESS_PATCH_FILE)
    if not patch_file.exists():
        return False

    try:
        with open(patch_file, 'r') as f:
            patches = json.load(f)

        if not patches:
            return False

        # Apply the patches
        success = apply_code_access_patches(log_func)
        return success and len(patches) > 0

    except Exception as e:
        log_func(f"Warning: Failed to check patch file: {e}", "WARNING")
        return False


def _load_ast_parent_graph(graph_path: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Load the AST parent graph from JSON.

    Args:
        graph_path: Path to the .ast_graph.json file

    Returns:
        Parent graph with structure: dict[relative_path][absolute_path][node_id] = parent_node_id
    """
    if not graph_path.exists():
        return {}

    try:
        with open(graph_path, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def detect_code_accesses(log_func: Callable, ast_path: Path, ast_graph_path: Path, scope: Scope) -> None:
    """
    Detect .code accesses in the AST and create patches to rewrite them as loadCode(pointer) calls.

    Args:
        log_func: Logging function to use for output (signature: log_func(message, level="INFO"))
        ast_path: Path to the .asts.json file
        ast_graph_path: Path to the .ast_parent_graph.json file
        scope: Scope object to filter files
    """
    log_func("Analyzing AST for .code accesses...")

    try:
        if not ast_path.exists():
            log_func("Warning: .asts.json file not found, skipping code access munging", "WARNING")
            return

        with open(ast_path, 'r') as f:
            asts_data = json.load(f)

        # Load parent graph for efficient parent lookups
        parent_graph = _load_ast_parent_graph(ast_graph_path)

        # Track patches to apply
        patches = []

        # Structure: dict[relative_path: dict[absolute_path: dict[node_id: node_data]]]
        for relative_path, path_data in asts_data.items():
            # Skip files not in scope
            if not scope.is_file_in_scope(Path(relative_path)):
                continue

            for absolute_path, nodes in path_data.items():
                # Convert absolute path to relative for scope checking
                # The scope object works with paths relative to project_root
                try:
                    abs_path_obj = Path(absolute_path)
                    if abs_path_obj.is_absolute():
                        # Resolve project_root to absolute path for comparison
                        project_root_abs = scope.project_root.resolve()
                        rel_path_for_scope = abs_path_obj.relative_to(project_root_abs)
                    else:
                        rel_path_for_scope = abs_path_obj
                except ValueError:
                    # Path is not relative to project_root, skip it
                    continue

                # Check if the file is in scope
                if not scope.is_file_in_scope(rel_path_for_scope):
                    continue

                # Iterate through all nodes (they're already flattened)
                for _, node in nodes.items():
                    if not isinstance(node, dict):
                        continue

                    # Check if this is a MemberAccess node with memberName="code"
                    if node.get('nodeType') == 'MemberAccess' and node.get('memberName') == 'code':
                        node_id = str(node.get('id'))

                        # Check if this .code access is used as expression in another node using parent graph
                        # If the parent graph exists, use it for O(1) lookup
                        if parent_graph:
                            parent_map = parent_graph.get(relative_path, {}).get(absolute_path, {})
                            parent_id = parent_map.get(node_id)

                            if parent_id:
                                parent_node = nodes.get(parent_id, {})
                                parent_type = parent_node.get('nodeType')

                                # Skip if parent is MemberAccess (like .code.length) or FunctionCall (like x.code())
                                if parent_type in ['MemberAccess', 'FunctionCall']:
                                    continue
                        else:
                            # Fallback: check manually if graph not available
                            is_chained = False
                            for _, other_node in nodes.items():
                                if not isinstance(other_node, dict):
                                    continue

                                # Check if used in MemberAccess (like .code.length) or FunctionCall (like x.code())
                                if other_node.get('nodeType') in ['MemberAccess', 'FunctionCall']:
                                    expr = other_node.get('expression', {})
                                    if str(expr.get('id')) == node_id:
                                        is_chained = True
                                        break

                            if is_chained:
                                continue  # Skip this .code access, it's part of a chain or function call

                        # Extract source location: "offset:length:file_id"
                        src = node.get('src', '')
                        if not src:
                            continue

                        parts = src.split(':')
                        if len(parts) != 3:
                            continue

                        offset = int(parts[0])
                        length = int(parts[1])
                        # file_id = int(parts[2])  # Not needed since we already know the file from absolute_path

                        # Get the expression being accessed (e.g., "pointer" from "pointer.code")
                        expression = node.get('expression', {})
                        expr_src = expression.get('src', '')
                        if not expr_src:
                            continue

                        expr_parts = expr_src.split(':')
                        if len(expr_parts) != 3:
                            continue

                        expr_offset = int(expr_parts[0])
                        expr_length = int(expr_parts[1])

                        # Read the original expression from the source file
                        try:
                            with open(relative_path, 'r') as src_file:
                                src_content = src_file.read()
                                expr_text = src_content[expr_offset:expr_offset + expr_length]
                                original_text = src_content[offset:offset + length]

                                # Create replacement: "certora_loadCode(expression)"
                                replacement = f"certora_loadCode({expr_text})"

                                patches.append(
                                    CodeAccessPatch(
                                        file=relative_path,
                                        offset=offset,
                                        length=length,
                                        original=original_text,
                                        replacement=replacement,
                                    )
                                )

                        except Exception as e:
                            log_func(f"Warning: Failed to read source for patch at {relative_path}: {e}", "WARNING")

        if not patches:
            log_func("✓ No .code accesses found")
            return

        # Write patches to a JSON file
        patches_file = Path(CODE_ACCESS_PATCH_FILE)
        patches_file.parent.mkdir(parents=True, exist_ok=True)

        with open(patches_file, 'w') as f:
            json.dump([asdict(p) for p in patches], f, indent=2)

        log_func(f"✓ Found {len(patches)} .code access(es) requiring patches")
        log_func(f"✓ Patches written to {patches_file}")

    except Exception as e:
        log_func(f"Warning: Failed to analyze .code accesses: {e}", "WARNING")
        log_func(f"Traceback: {traceback.format_exc()}", "WARNING")


def apply_code_access_patches(log_func: Callable) -> bool:
    """
    Apply the .code access patches from the patch file.

    Args:
        log_func: Logging function to use for output (signature: log_func(message, level="INFO"))

    Returns:
        True if patches were applied successfully, False otherwise
    """
    patch_file = Path(CODE_ACCESS_PATCH_FILE)
    if not patch_file.exists():
        log_func("No code access patches to apply")
        return True

    try:
        with open(patch_file, 'r') as f:
            patch_dicts = json.load(f)

        # Convert from dicts to dataclasses
        patches = [CodeAccessPatch(**p) for p in patch_dicts]

        if not patches:
            log_func("No code access patches to apply")
            return True

        log_func(f"Applying {len(patches)} code access patch(es)...")

        # Group patches by file
        patches_by_file: Dict[str, List[CodeAccessPatch]] = {}
        for patch in patches:
            file_path = patch.file
            if file_path not in patches_by_file:
                patches_by_file[file_path] = []
            patches_by_file[file_path].append(patch)

        # Apply patches file by file
        backups = {}
        try:
            for file_path, file_patches in patches_by_file.items():
                with open(file_path, 'r') as f:
                    original_content = f.read()

                backups[file_path] = original_content

                # Sort patches by offset in descending order to apply from end to start
                # This prevents offset shifts from affecting subsequent patches
                sorted_patches = sorted(file_patches, key=lambda p: p.offset, reverse=True)

                new_content = original_content
                for patch in sorted_patches:
                    offset = patch.offset
                    length = patch.length
                    original = patch.original
                    replacement = patch.replacement

                    # Verify the original text matches
                    actual_text = new_content[offset:offset + length]
                    if actual_text != original:
                        log_func(
                            f"Warning: Patch mismatch in {file_path} at offset {offset}: "
                            f"expected '{original}', found '{actual_text}'",
                            "WARNING"
                        )
                        continue

                    # Apply the patch
                    new_content = new_content[:offset] + replacement + new_content[offset + length:]

                # Inject loadCode function into contracts that need it
                # Use original_content for offset calculation, new_content for injection
                new_content = _inject_load_code_into_contract(original_content, new_content, sorted_patches)

                # Write the patched content
                with open(file_path, 'w') as f:
                    f.write(new_content)

                log_func(f"  ✓ Patched {file_path}")

            log_func("✓ All code access patches applied successfully")
            return True

        except Exception as e:
            log_func(f"Error applying patches: {e}. Reverting changes...", "ERROR")
            # Revert all changes
            for file_path, original_content in backups.items():
                with open(file_path, 'w') as f:
                    f.write(original_content)
            log_func("✓ Changes reverted", "WARNING")
            return False

    except Exception as e:
        log_func(f"Warning: Failed to apply code access patches: {e}", "WARNING")
        log_func(f"Traceback: {traceback.format_exc()}", "WARNING")
        return False


# Note: This function is not currently called automatically during orchestration.
# The code access patches are applied and remain in place during verification runs,
# similar to how import patches work. This function is available for future use if
# we need to clean up the applied changes (e.g., for debugging or post-processing).
# The setup_prover tracks whether patches were applied via code_access_patches_applied flag.
def revert_code_access_patches(log_func: Callable) -> bool:
    """
    Revert the .code access patches using the patch file.

    Args:
        log_func: Logging function to use for output (signature: log_func(message, level="INFO"))

    Returns:
        True if patches were reverted successfully, False otherwise
    """
    patch_file = Path(CODE_ACCESS_PATCH_FILE)
    if not patch_file.exists():
        log_func("No code access patches to revert")
        return True

    try:
        with open(patch_file, 'r') as f:
            patch_dicts = json.load(f)

        # Convert from dicts to dataclasses
        patches = [CodeAccessPatch(**p) for p in patch_dicts]

        if not patches:
            log_func("No code access patches to revert")
            return True

        log_func(f"Reverting {len(patches)} code access patch(es)...")

        # Group patches by file
        patches_by_file: Dict[str, List[CodeAccessPatch]] = {}
        for patch in patches:
            file_path = patch.file
            if file_path not in patches_by_file:
                patches_by_file[file_path] = []
            patches_by_file[file_path].append(patch)

        # Revert patches file by file
        for file_path, file_patches in patches_by_file.items():
            with open(file_path, 'r') as f:
                content = f.read()

            # Sort patches by offset in descending order to apply from end to start
            sorted_patches = sorted(file_patches, key=lambda p: p.offset, reverse=True)

            new_content = content
            for patch in sorted_patches:
                offset = patch.offset
                replacement = patch.replacement
                original = patch.original

                # Calculate the length of the replacement (not the original length)
                repl_length = len(replacement)

                # Verify the replacement text matches
                actual_text = new_content[offset:offset + repl_length]
                if actual_text != replacement:
                    log_func(
                        f"Warning: Revert mismatch in {file_path} at offset {offset}: "
                        f"expected '{replacement}', found '{actual_text}'",
                        "WARNING"
                    )
                    continue

                # Revert the patch (replace back with original)
                new_content = new_content[:offset] + original + new_content[offset + repl_length:]

            # Write the reverted content
            with open(file_path, 'w') as f:
                f.write(new_content)

            log_func(f"  ✓ Reverted {file_path}")

        log_func("✓ All code access patches reverted successfully")
        return True

    except Exception as e:
        log_func(f"Warning: Failed to revert code access patches: {e}", "WARNING")
        log_func(f"Traceback: {traceback.format_exc()}", "WARNING")
        return False
