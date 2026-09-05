"""Replace a library main contract with its generated harness.

Both entry points — autosetup's CLI and AutoProver's pipeline — perform the swap here,
before anything downstream derives state from the contract name. That matters because
the name reaches the sanity spec, the base conf, the ``verify`` target and the result
keys; swapping later would leave those naming a contract the Prover cannot verify.

The library stays in the scene alongside the harness. It has to: the harness's wrappers
call into it, and the build only reports a library's own functions when it is named as a
file in its own right.
"""

import json
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from certora_autosetup.harnesser.detect import is_library_main_contract
from certora_autosetup.harnesser.run import HarnessResult, ensure_library_harness
from certora_autosetup.utils.contract_utils import split_contract_spec
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


def library_behind_harness(
    project_root: Path, main_contract_handle: ContractHandle
) -> Optional[ContractHandle]:
    """The library a generated harness wraps, or ``None`` if this is not one of ours.

    A run can reach AutoSetup with the swap already done: AutoProver's pipeline swaps
    before it invokes AutoSetup, which then sees a contract that is not a library and has
    nothing to detect. The manifest written beside the harness is what still names the
    library, and the library has to be in the conf either way, because the Prover's scene
    is the files the conf lists and solc inlining does not put it there.
    """
    manifest = (project_root / main_contract_handle.source_file).with_suffix(".manifest.json")
    if not manifest.is_file():
        return None
    try:
        record = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("harness_name") != main_contract_handle.contract_name:
        return None
    library_name = record.get("library_name")
    library_file = record.get("library_file")
    if not library_name or not library_file:
        return None
    return ContractHandle(contract_name=library_name, source_file=library_file)


def with_harnessed_library(
    project_root: Path,
    main_contract_handle: ContractHandle,
    additional_contracts: Sequence[str],
    swapped_from: Optional[ContractHandle] = None,
) -> List[str]:
    """``additional_contracts`` plus the library the harness wraps, if there is one.

    The conf is the Prover's scene, and a library reaches it only by being listed in its
    own right: solc inlines the calls, which leaves curated summaries that name the
    library unable to typecheck and the build reporting none of its functions.

    ``swapped_from`` is the library this run just swapped away from. Without it the
    library is recovered from the harness manifest, which is the case that matters when
    the swap happened in an earlier process.
    """
    library = swapped_from or library_behind_harness(project_root, main_contract_handle)
    if library is None or library == main_contract_handle:
        return list(additional_contracts)
    # Compare on the (file, name) pair rather than the string: ``to_config_str`` drops the
    # name when it matches the file stem, so one contract has two spellings.
    already = {split_contract_spec(spec) for spec in additional_contracts}
    if (library.source_file, library.contract_name) in already:
        return list(additional_contracts)
    return [*additional_contracts, library.to_config_str()]


def swap_library_main_contract(
    project_root: Path,
    main_contract_handle: ContractHandle,
    contract_handles: Sequence[ContractHandle],
    solc: Optional[str] = None,
    certora_run_command: str = "certoraRun",
    validate: bool = True,
) -> Tuple[ContractHandle, List[ContractHandle], Optional[HarnessResult]]:
    """Return the handle to verify, the scene to verify it in, and what was generated.

    When the main contract is not a library everything is returned unchanged and no
    build is run, so this is safe to call on every run.
    """
    source_file = Path(main_contract_handle.source_file)
    absolute_source = source_file if source_file.is_absolute() else project_root / source_file

    if not is_library_main_contract(
        absolute_source, main_contract_handle.contract_name, project_root, solc
    ):
        scene = list(contract_handles)
        library = library_behind_harness(project_root, main_contract_handle)
        if library is not None and library not in scene:
            scene.append(library)
        return main_contract_handle, scene, None

    logger.log(
        f"Main contract {main_contract_handle.contract_name} is a library; the Prover "
        f"cannot verify it directly. Generating a harness.",
        "INFO",
        "Harnesser",
    )

    result = ensure_library_harness(
        project_root=project_root,
        library_file=source_file,
        library_name=main_contract_handle.contract_name,
        solc=solc,
        certora_run_command=certora_run_command,
        validate=validate,
    )

    harness_handle = ContractHandle(
        contract_name=result.harness_name, source_file=result.harness_file
    )

    scene = list(contract_handles)
    if main_contract_handle not in scene:
        scene.append(main_contract_handle)
    if harness_handle not in scene:
        scene.append(harness_handle)

    logger.log(
        f"Verifying {result.harness_name} instead of {main_contract_handle.contract_name}: "
        f"{result.coverage['wrapped']}/{result.coverage['total']} library function(s) exposed, "
        f"{result.coverage['skipped']} skipped",
        "INFO",
        "Harnesser",
    )
    return harness_handle, scene, result


def swap_library_main_contract_paths(
    project_root: Path,
    relative_path: str,
    contract_name: str,
    solc: Optional[str] = None,
) -> Tuple[str, str]:
    """Path/name form of the swap, for callers that carry the target as two strings.

    Returns the pair unchanged when the target is not a library, so it is safe on every
    run.
    """
    handle = ContractHandle(contract_name=contract_name, source_file=relative_path)
    swapped, _, result = swap_library_main_contract(
        project_root=project_root, main_contract_handle=handle, contract_handles=[handle], solc=solc
    )
    if result is None:
        return relative_path, contract_name
    return swapped.source_file, swapped.contract_name
