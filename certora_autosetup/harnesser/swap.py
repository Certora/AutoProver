"""Replace a library main contract with its generated harness.

Both entry points — autosetup's CLI and AutoProver's pipeline — perform the swap here,
before anything downstream derives state from the contract name. That matters because
the name reaches the sanity spec, the base conf, the ``verify`` target and the result
keys; swapping later would leave those naming a contract the Prover cannot verify.

The library stays in the scene alongside the harness. It has to: the harness's wrappers
call into it, and the build only reports a library's own functions when it is named as a
file in its own right.
"""

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from certora_autosetup.harnesser.detect import is_library_main_contract
from certora_autosetup.harnesser.run import HarnessResult, ensure_library_harness
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


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
        return main_contract_handle, list(contract_handles), None

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
