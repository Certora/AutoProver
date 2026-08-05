#!/usr/bin/env python
"""Assert that `import urllib3` resolves to urllib3 and not to urllib3-future.

urllib3-future's default wheel installs a second copy of the `urllib3` package over the
real one and a `urllib3_future.pth` startup hook that keeps reinstating it, so that
`import urllib3` yields the fork. `[tool.uv] no-binary-package` plus URLLIB3_NO_OVERRIDE
suppress both, leaving the two distributions installed side by side — see the note in
pyproject.toml.

This runs as a build step because the substitution is silent when it succeeds: `import
urllib3` keeps working, just against different code. Only a partially-applied
substitution raises, and it does so far from the cause — deep inside a pipeline phase,
as an ImportError claiming a circular import. Failing the build instead keeps that
diagnosis from having to happen twice.
"""

import importlib.metadata as metadata
import sys
import sysconfig
from pathlib import Path

site_packages = Path(sysconfig.get_paths()["purelib"])
problems: list[str] = []

hook = site_packages / "urllib3_future.pth"
if hook.exists():
    problems.append(f"{hook.name} is installed; it substitutes urllib3 on interpreter start")

# urllib3-future's marker directory. Its presence under `urllib3/` means that tree is the
# fork's, whether or not the hook is still around to have put it there.
if (site_packages / "urllib3" / "backend").exists():
    problems.append("urllib3/backend/ exists; the urllib3 package directory is urllib3-future's")

imported_version: str | None = None
try:
    import urllib3
except ImportError as exc:
    problems.append(f"import urllib3 failed: {exc}")
else:
    # getattr rather than attribute access: urllib3 does not re-export __version__ in its
    # type stubs, and a substituted package may not define it at all.
    imported_version = getattr(urllib3, "__version__", None)
    declared = metadata.version("urllib3")
    if imported_version != declared:
        problems.append(
            f"the imported urllib3 reports version {imported_version} but the installed "
            f"urllib3 distribution is {declared}; `import urllib3` is resolving to "
            "another package"
        )

# The consumers that made this worth gating: requests reaches urllib3, niquests reaches
# urllib3_future, and both must work off the same environment.
for module in ("requests", "niquests", "urllib3_future"):
    try:
        __import__(module)
    except ImportError as exc:
        problems.append(f"import {module} failed: {exc}")

if problems:
    print("urllib3 identity check FAILED:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nExpected urllib3 and urllib3-future installed side by side. Check that "
        "[tool.uv] no-binary-package still lists urllib3-future and that "
        "URLLIB3_NO_OVERRIDE is set wherever the environment is installed.",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    f"urllib3 identity OK: urllib3 {imported_version}, "
    f"urllib3-future {metadata.version('urllib3-future')}"
)
