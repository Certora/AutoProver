#!/usr/bin/env python3
"""certoraSolanaProver's ``build_script``. Copied beside a command file by
:func:`composer.cargo.sbf.write_build_script`; do not edit the copy.

The prover runs the copy directly and reads the build manifest from stdout. It
must run without this package installed, so it imports nothing from it.
"""

import json
import pathlib
import subprocess
import sys


def main() -> int:
    command = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
    argv = [*command["argv_prefix"], *command["argv"]]

    # Certora passes --json and -l; those flags do nothing here. --cargo_features
    # is how the prover adds features, and ignoring it would build the wrong crate.
    if "--cargo_features" in sys.argv:
        extra = sys.argv[sys.argv.index("--cargo_features") + 1 :]
        if extra:
            argv += ["--features", " ".join(extra)]

    result = subprocess.run(argv, capture_output=True, text=True, cwd=command["cwd"])
    sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
