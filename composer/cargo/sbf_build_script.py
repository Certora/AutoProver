#!/usr/bin/env python3
"""certoraSolanaProver's ``build_script``.

:func:`composer.cargo.sbf.write_build_script` copies this file next to a
command file. Edit this file, not the copy.

The prover runs the copy directly and reads the build manifest from stdout.
The copy imports nothing from this package: the package is not installed
where the prover runs it.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys

#: What coreutils ``timeout`` exits with when the command timed out.
EXIT_TIMED_OUT = 124


def main() -> int:
    here = pathlib.Path(__file__).resolve()
    command = json.loads(here.with_suffix(".json").read_text())
    workdir = pathlib.Path(command["cwd"])
    if here.parent.parent != workdir:
        sys.stderr.write("This build script was written for a different working tree.\n")
        return 1
    argv = [*command["argv_prefix"], *command["argv"]]

    # The prover passes --json, -l, and --cargo_features. Only --cargo_features
    # changes the build. Dropping it builds the wrong crate.
    if "--cargo_features" in sys.argv:
        extra = sys.argv[sys.argv.index("--cargo_features") + 1 :]
        if extra:
            argv += ["--features", " ".join(extra)]

    timeout_s = command["timeout_s"]
    # A session of its own, so killing the group also kills the compilers cargo starts.
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, stderr = proc.communicate()
        sys.stderr.write(stderr)
        sys.stderr.write(f"The build did not finish within {timeout_s}s.\n")
        return EXIT_TIMED_OUT
    sys.stderr.write(stderr)
    sys.stdout.write(stdout)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
