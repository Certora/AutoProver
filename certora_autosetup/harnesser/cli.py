"""``python -m certora_autosetup.harnesser`` — generate a library harness.

    python -m certora_autosetup.harnesser --library src/utils/BitMaps.sol:BitMaps \
        --project-dir . --output harness.json

It compiles a probe build to learn the library's API, writes
``certora/harnesses/CertoraLibraryHarness_<Library>.sol``, and records what it wrapped.
The JSON record goes to the file named by ``--output`` rather than to stdout, because
the probe build and the logger both write there; whoever runs this decides what to do
with the harness, so nothing here swaps a main contract.
"""

import argparse
import json
import sys
from pathlib import Path

from certora_autosetup.harnesser.model import LibraryHarnessError
from certora_autosetup.harnesser.run import ensure_library_harness
from certora_autosetup.utils.contract_utils import parse_contract_files, split_contract_spec
from certora_autosetup.utils.types import ContractHandle


def _project_relative(handle: ContractHandle, root: Path) -> ContractHandle:
    """Probe-build file arguments are resolved from the project root, so keep them there.

    ``parse_contract_files`` absolutizes against the root in order to check the file
    exists; the build wants the path back the way the user wrote it.
    """
    path = Path(handle.source_file)
    if path.is_absolute() and path.is_relative_to(root):
        path = path.relative_to(root)
    return ContractHandle(contract_name=handle.contract_name, source_file=path.as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="certora_autosetup.harnesser",
        description="Generate a verifiable contract harness for a library main contract.",
    )
    parser.add_argument(
        "--library",
        required=True,
        help="Library to wrap, as path/To/Lib.sol:LibName (name defaults to the file stem)",
    )
    parser.add_argument(
        "--project-dir", default=".", help="Project root; defaults to the current directory"
    )
    parser.add_argument("--solc", default=None, help="solc to build with, e.g. solc8.16")
    parser.add_argument(
        "--extra-file",
        action="append",
        default=[],
        dest="extra_files",
        help="Additional path:Contract to include in the probe build; repeatable",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Do not recompile the filled harness (faster; leaves compile errors to autosetup)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the JSON result here; without it only the human summary is printed",
    )
    args = parser.parse_args(argv)

    library_path, library_name = split_contract_spec(args.library)
    project_root = Path(args.project_dir).resolve()

    try:
        # Parsed rather than passed through, so a mistyped --extra-file is reported here
        # instead of as a probe-build failure minutes later.
        extra_files = [
            _project_relative(handle, project_root)
            for handle in parse_contract_files(args.extra_files, project_root)
        ] if args.extra_files else []
        result = ensure_library_harness(
            project_root=project_root,
            library=ContractHandle(contract_name=library_name, source_file=library_path),
            solc=args.solc,
            extra_files=extra_files,
            validate=not args.skip_validation,
        )
    except (LibraryHarnessError, ValueError) as e:
        print(f"library harness generation failed: {e}", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(json.dumps(result.to_dict(), indent=2) + "\n")

    coverage = result.coverage
    print(
        f"{result.harness.contract_name} -> {result.harness.source_file}: "
        f"{coverage['wrapped']}/{coverage['total']} function(s) wrapped, "
        f"{coverage['readers']} storage reader(s), {coverage['skipped']} skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
