#!/usr/bin/env python3
"""certoraSolanaProver's ``build_script``.

:func:`composer.cargo.sbf.write_build_script` copies this file next to a
command file. Edit this file, not the copy.

The prover runs the copy directly and reads the build manifest from stdout.
The copy imports nothing from this package: the package is not installed
where the prover runs it.
"""

import json
import pathlib
import subprocess
import sys


def main() -> int:
    here = pathlib.Path(__file__).resolve()
    command = json.loads(here.with_suffix(".json").read_text())
    workdir = pathlib.Path(command["cwd"])
    if here.parent.parent != workdir:
        sys.stderr.write(
            f"{here} was written for the working tree at {workdir}, and its command builds that "
            f"tree. Regenerate it here instead of copying it.\n"
        )
        return 1
    argv = [*command["argv_prefix"], *command["argv"]]

    # The prover passes --json, -l, and --cargo_features. Only --cargo_features
    # changes the build. Dropping it builds the wrong crate.
    if "--cargo_features" in sys.argv:
        extra = sys.argv[sys.argv.index("--cargo_features") + 1 :]
        if extra:
            argv += ["--features", " ".join(extra)]

    result = subprocess.run(argv, capture_output=True, text=True, cwd=workdir)
    sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
