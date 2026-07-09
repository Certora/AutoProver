"""
Contract utility functions for auto-detection, deduplication, and name resolution.

Utilities shared across autosetup, preaudit, and other pipeline components.
"""

from pathlib import Path
from typing import List, Optional

from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle
from certora_autosetup.parsers.foundry import FoundryContractExtractor


def split_contract_spec(spec: str) -> tuple[str, str]:
    """Split a contract spec into ``(source_file, contract_name)`` without any
    filesystem validation. Accepts ``"path/to/Foo.sol"`` (name inferred from stem)
    or ``"path/to/Foo.sol:Foo"`` (name explicit). Use this for raw parsing of
    user-provided strings (e.g. ``--additional-contracts`` entries) when you
    don't want ``parse_contract_files``' file-existence checks.
    """
    if ":" in spec:
        path, name = spec.split(":", 1)
        return path, name
    return spec, Path(spec).stem


def parse_contract_files(
    contract_specs: List[str],
    project_root: Optional[Path] = None,
    strict: bool = True
) -> List[ContractHandle]:
    """
    Parse contract file specifications that may include contract names.

    Args:
        contract_specs: List of file specifications with optional contract names
        project_root: Optional root directory for resolving relative paths to absolute paths
        strict: If True (default), validates files exist and raises errors for non-.sol files.
                If False, skips non-.sol files and missing files gracefully.

    Returns:
        List of ContractHandle objects

    Raises:
        ValueError: If validation fails (when strict=True)

    Examples:
        ['file1.sol', 'file2.sol:MyContract'] ->
        [ContractHandle('file1', 'file1.sol'), ContractHandle('MyContract', 'file2.sol')]
    """
    if strict and len(contract_specs) < 1:
        raise ValueError("Must provide at least one .sol file")

    contract_handles = []

    for spec in contract_specs:
        if ':' in spec:
            # Format: file.sol:ContractName
            file_path, contract_name = spec.split(':', 1)
        else:
            # Format: file.sol (infer contract name from filename)
            file_path = spec
            # Use stem of filename as contract name
            contract_name = Path(spec).stem

        # Validate or filter file extension
        if not file_path.endswith(".sol"):
            if not strict:
                continue
            raise ValueError(f"Error: {file_path} is not a .sol file")

        # Change to absolute path if requested
        file_path = Path(file_path)
        if not file_path.is_absolute() and project_root:
            file_path = project_root / file_path

        # Final validation
        if not file_path.exists():
            if not strict:
                continue
            raise ValueError(f"Error: File {file_path} does not exist")

        contract_handles.append(ContractHandle(contract_name=contract_name, source_file=str(file_path)))

    return contract_handles


def auto_detect_contracts(
    project_root: Path,
    profile: str | None = None,
    requested_build_system: str | None = None,
) -> list[ContractHandle]:
    """Auto-detect all project contracts via the build system (Foundry/Hardhat)."""
    build_system = BuildSystemDetector.resolve(project_root, requested_build_system)
    if build_system == BuildSystem.UNKNOWN:
        raise ValueError(
            "No build system detected (Foundry or Hardhat). Expected foundry.toml or hardhat.config.{js,ts}"
        )

    contract_extractor = BuildSystemDetector.get_contract_extractor(build_system, project_root, profile=profile)
    return contract_extractor.extract_logic_contracts_and_files()


def deduplicate_contract_handles(handles: list[ContractHandle]) -> list[ContractHandle]:
    """Deduplicate ContractHandle list by contract_name, keeping shortest source_file path.

    When auto-detection finds the same contract name in multiple source files
    (e.g. ERC20Detailed in contracts/child/ and contracts/common/oz/), keep only the shortest path for each name.
    """
    by_name: dict[str, list[ContractHandle]] = {}
    for ch in handles:
        by_name.setdefault(ch.contract_name, []).append(ch)

    result: list[ContractHandle] = []
    for name, group in by_name.items():
        if len(group) > 1:
            group.sort(key=lambda ch: len(ch.source_file))
            logger.log(
                f"Deduplicated contract '{name}': keeping '{group[0].source_file}' "
                f"(shortest of {len(group)} paths: {', '.join(ch.source_file for ch in group)})",
                "INFO",
                "Orchestrator",
            )
        result.append(group[0])
    return result


def resolve_contract_handles(
    contract_handles: list[ContractHandle],
    project_root: Path,
    profile: str | None = None,
    requested_build_system: str | None = None,
) -> list[ContractHandle]:
    """Resolve inferred contract names using Foundry build artifact compilationTargets.

    When a user specifies a file without :ContractName, parse_contract_files infers
    the contract name from the filename stem. This can be wrong when the file contains
    a contract with a different name. This function fixes those inferred names by
    looking up the actual contract name from the build artifacts.
    """
    build_system = BuildSystemDetector.resolve(project_root, requested_build_system)
    if build_system != BuildSystem.FOUNDRY:
        return contract_handles

    extractor = FoundryContractExtractor(project_root, profile=profile)
    try:
        source_map = extractor.build_source_path_to_contracts_map()
    except Exception:
        return contract_handles

    if not source_map:
        return contract_handles

    resolved: list[ContractHandle] = []
    for handle in contract_handles:
        inferred_name = Path(handle.source_file).stem
        # User explicitly specified a name — keep as-is
        if handle.contract_name != inferred_name:
            resolved.append(handle)
            continue

        # Normalize and look up source path
        normalized = str(Path(handle.source_file))
        entries_in_file: list[tuple] | None = None
        for src_path, entries in source_map.items():
            if str(Path(src_path)) == normalized:
                entries_in_file = entries
                break

        if entries_in_file is None:
            resolved.append(handle)
            continue

        contract_names = [name for name, _ver in entries_in_file]

        if len(contract_names) == 1:
            actual = contract_names[0]
            if actual != handle.contract_name:
                logger.log(
                    f"Resolved contract name for {handle.source_file}: "
                    f"'{handle.contract_name}' -> '{actual}' (from build artifacts)",
                    "INFO",
                    "Orchestrator",
                )
            resolved.append(ContractHandle(contract_name=actual, source_file=handle.source_file))
        elif inferred_name in contract_names:
            # Multiple contracts but one matches the basename — keep it.
            # TODO: expand to one handle per concrete contract in the file (symmetric with
            # auto-detect's emit-all default and with --exclude-contracts when extended).
            resolved.append(handle)
        else:
            logger.log(
                f"Dropping {handle.source_file}: multiple contracts {contract_names} "
                f"and none matches filename '{inferred_name}'",
                "WARNING",
                "Orchestrator",
            )

    return resolved
