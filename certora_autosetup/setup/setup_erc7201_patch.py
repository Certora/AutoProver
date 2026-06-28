"""
ERC-7201 Annotation Patcher

Identifies Solidity structs that follow the ERC-7201 namespaced storage pattern
but are missing the required `/// @custom:storage-location erc7201:<namespace>`
annotation, and patches the source files to add them.

This pass runs before the ERC-7201 scanner (setup_erc7201.py) so that the scanner
automatically picks up newly-added annotations.
"""

import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.llm_util import call_llm_structured, ledger_component


# --- Pydantic models for structured LLM output ---


class ERC7201StructAnnotation(BaseModel):
    """A single struct that needs an ERC-7201 annotation."""

    struct_name: str = Field(description="The exact name of the struct definition (e.g., 'AccessControlStorage')")
    namespace: str = Field(
        description=(
            "The ERC-7201 namespace string derived from the nearby keccak256 comment "
            "or constant (e.g., 'openzeppelin.storage.AccessControl')"
        )
    )
    explanation: str = Field(
        description="A brief (one sentence) explanation of how you determined this struct and namespace pairing"
    )


class ERC7201PatchAnalysisResult(BaseModel):
    """Result of analyzing a Solidity file for missing ERC-7201 annotations."""

    structs_to_annotate: List[ERC7201StructAnnotation] = Field(
        description=(
            "List of structs that serve as ERC-7201 storage types and are missing "
            "the /// @custom:storage-location annotation. Empty list if none found."
        )
    )


# --- Prompt template ---

ERC7201_PATCH_PROMPT_TEMPLATE = """\
You are analyzing a Solidity source file to identify structs that follow the \
ERC-7201 namespaced storage pattern but are MISSING the optional \
`/// @custom:storage-location erc7201:<namespace>` annotation.

## What is ERC-7201?

ERC-7201 defines a pattern for namespaced storage in upgradeable contracts. The pattern involves:
1. A struct that holds storage variables (e.g., `struct AccessControlStorage {{ ... }}`)
2. A constant slot derived from `keccak256` of a namespace string (e.g., \
`keccak256(abi.encode(uint256(keccak256("openzeppelin.storage.AccessControl")) - 1)) & ~bytes32(uint256(0xff))`)
3. A getter function that returns `<StructType> storage $` via inline assembly that \
assigns the constant to `$.slot`

## Example of an ALREADY ANNOTATED struct (do NOT include these):

```solidity
/// @custom:storage-location erc7201:openzeppelin.storage.AccessControl
struct AccessControlStorage {{
    mapping(bytes32 role => RoleData) _roles;
}}
```

## What to look for

Identify structs that:
1. Are used as the return type of a function returning `storage` via assembly slot assignment
2. Have a corresponding `bytes32 constant` derived from a keccak256 hash of a namespace string
3. Do NOT already have a `/// @custom:storage-location` annotation on the line(s) immediately before `struct`

For each such struct, extract the namespace string from the nearby keccak256 comment or source. \
The namespace is the string literal inside `keccak256("...")`, e.g., \
from `keccak256(abi.encode(uint256(keccak256("openzeppelin.storage.AccessControl")) - 1))` \
the namespace is `openzeppelin.storage.AccessControl`.

If there is no keccak256 comment but a constant name like `MyFeatureStorageLocation` is used \
with a getter function for `MyFeatureStorage`, and no namespace string can be determined, \
skip that struct rather than guessing a namespace.

## Source file to analyze

File: {file_path}

```solidity
{file_contents}
```

Identify ALL structs that need a `/// @custom:storage-location erc7201:<namespace>` annotation added. \
If no structs need annotation (either none exist or all are already annotated), return an empty list.
"""


# --- Helper functions ---


def _find_struct_line(lines: List[str], struct_name: str) -> Optional[int]:
    """Find the 0-based line index of a `struct <name>` definition.

    Returns None if not found.
    """
    pattern = re.compile(r"\bstruct\s+" + re.escape(struct_name) + r"\b")
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        if pattern.search(line):
            return i
    return None


def _already_annotated(lines: List[str], struct_line_idx: int) -> bool:
    """Check if the struct at the given line already has an ERC-7201 annotation."""
    annotation_pattern = re.compile(r"///\s*@custom:storage-location\s+erc7201:", re.IGNORECASE)
    for offset in range(1, 4):
        check_idx = struct_line_idx - offset
        if check_idx < 0:
            break
        line = lines[check_idx].strip()
        if annotation_pattern.search(line):
            return True
        if line and not line.startswith("//") and not line.startswith("*"):
            break
    return False


def _patch_file_with_annotations(
    file_path: Path,
    annotations: List[ERC7201StructAnnotation],
    log_func: Callable,
) -> bool:
    """Patch a Solidity file to add ERC-7201 annotations before struct definitions.

    Returns True if any patches were applied, False otherwise.
    """
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    stripped_lines = [line.rstrip("\n").rstrip("\r") for line in lines]

    insertions: List[Tuple[int, str]] = []

    for annotation in annotations:
        struct_line_idx = _find_struct_line(stripped_lines, annotation.struct_name)
        if struct_line_idx is None:
            log_func(
                f"Warning: Could not find struct '{annotation.struct_name}' in {file_path}, skipping", "WARNING"
            )
            continue

        if _already_annotated(stripped_lines, struct_line_idx):
            log_func(f"Struct '{annotation.struct_name}' in {file_path} is already annotated, skipping", "DEBUG")
            continue

        # Match indentation of the struct line
        struct_line = lines[struct_line_idx]
        indent = ""
        for ch in struct_line:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break

        annotation_line = f"{indent}/// @custom:storage-location erc7201:{annotation.namespace}\n"
        insertions.append((struct_line_idx, annotation_line))

    if not insertions:
        return False

    # Insert bottom-to-top to preserve indices
    insertions.sort(key=lambda x: x[0], reverse=True)
    for line_idx, annotation_line in insertions:
        lines.insert(line_idx, annotation_line)

    file_path.write_text("".join(lines), encoding="utf-8")
    return True


# --- Main entry point ---


def run_erc7201_patch(
    log_func: Callable = logger.log,
    skip_llm: bool = False,
    verbose: bool = False,
) -> int:
    """Analyze candidate Solidity files and patch missing ERC-7201 annotations.

    Args:
        log_func: Logging function with signature log_func(message, level="INFO")
        skip_llm: If True, skip LLM analysis entirely
        verbose: Enable verbose output

    Returns:
        Number of files patched
    """
    if skip_llm:
        log_func("Skipping ERC-7201 annotation patching (LLM disabled)")
        return 0

    # Load all_sources.json produced by compilation analysis
    sources_path = Path(".certora_internal/all_sources.json")
    if not sources_path.exists():
        log_func("all_sources.json not found, skipping ERC-7201 annotation patching", "WARNING")
        return 0

    with open(sources_path, "r") as f:
        all_sources: dict = json.load(f)

    # Filter to candidate files ending in Storage.sol
    candidate_files = [path for path in all_sources.keys() if path.endswith("Storage.sol")]

    if not candidate_files:
        log_func("No *Storage.sol files found in compilation sources, skipping annotation patching")
        return 0

    log_func(f"Found {len(candidate_files)} candidate Storage.sol file(s) for ERC-7201 annotation patching")

    total_patched = 0
    for file_path_str in candidate_files:
        file_path = Path(file_path_str)
        if not file_path.exists():
            log_func(f"Warning: Candidate file {file_path} does not exist, skipping", "WARNING")
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            log_func(f"Warning: Could not read {file_path}: {e}", "WARNING")
            continue

        prompt = ERC7201_PATCH_PROMPT_TEMPLATE.format(file_path=str(file_path), file_contents=content)

        with ledger_component("erc7201"):
            result = call_llm_structured(
                prompt=prompt,
                ty=ERC7201PatchAnalysisResult,
                max_tokens=2000,
                temperature=0.0,
                max_retries=3,
                log_to_file=True,
                verbose=verbose,
            )

        if result is None:
            log_func(f"Warning: LLM analysis failed for {file_path}, skipping", "WARNING")
            continue

        if not result.structs_to_annotate:
            if verbose:
                log_func(f"No missing annotations found in {file_path}", "DEBUG")
            continue

        log_func(
            f"Found {len(result.structs_to_annotate)} struct(s) to annotate in {file_path}: "
            + ", ".join(a.struct_name for a in result.structs_to_annotate)
        )

        patched = _patch_file_with_annotations(file_path, result.structs_to_annotate, log_func)
        if patched:
            total_patched += 1
            log_func(f"Patched {file_path} with ERC-7201 annotations")

    if total_patched > 0:
        log_func(f"ERC-7201 annotation patching complete: {total_patched} file(s) patched")
    else:
        log_func("ERC-7201 annotation patching complete: no files needed patching")

    return total_patched
