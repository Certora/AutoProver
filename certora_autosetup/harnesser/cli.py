"""``python -m certora_autosetup.harnesser`` — generate a library harness.

AutoProver invokes this as a subprocess and reads the JSON record from the file named by
``--output``, so the Solidity generation stays on the autosetup side while the decision
to swap the main contract stays with the caller. The result goes to a file rather than
stdout because the probe build and the logger both write there; this mirrors how
autosetup already hands its result to composer via ``--composer-setup``.
"""

import argparse
import json
import sys
from pathlib import Path

from certora_autosetup.harnesser.model import LibraryHarnessError
from certora_autosetup.harnesser.run import ensure_library_harness


def _split_target(target: str) -> tuple[str, str]:
    """Split ``path/To/Lib.sol:LibName``, defaulting the name to the file stem."""
    if ":" in target:
        path, name = target.rsplit(":", 1)
        return path, name
    path = target
    return path, Path(path).stem


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

    library_path, library_name = _split_target(args.library)

    try:
        result = ensure_library_harness(
            project_root=Path(args.project_dir),
            library_file=Path(library_path),
            library_name=library_name,
            solc=args.solc,
            extra_files=args.extra_files,
            validate=not args.skip_validation,
        )
    except LibraryHarnessError as e:
        print(f"library harness generation failed: {e}", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(json.dumps(result.to_dict(), indent=2) + "\n")

    coverage = result.coverage
    print(
        f"{result.harness_name} -> {result.harness_file}: "
        f"{coverage['wrapped']}/{coverage['total']} function(s) wrapped, "
        f"{coverage['readers']} storage reader(s), {coverage['skipped']} skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
