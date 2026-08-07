"""Drive the harnesser end to end: stub, probe build, plan, emit, validate.

Everything happens here, before autosetup's own run starts. The fill deliberately does
*not* re-enter autosetup's compilation analysis: that path re-seeds its config from the
build-system defaults, so a second pass through it would discard every workaround the
first pass discovered. Autosetup therefore sees only a finished harness and runs exactly
as it does for any hand-written contract.

The probe build lists the library in ``files`` explicitly. Importing it from the stub is
not enough — an imported-but-unused library is not compiled as its own contract, so the
build reports its structs but none of its functions.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from certora_autosetup.harnesser.model import HarnessPlan, LibraryHarnessError
from certora_autosetup.harnesser.plan import build_plan
from certora_autosetup.harnesser.read_build import BUILD_JSON_RELPATH, read_library_api
from certora_autosetup.harnesser.render import plan_hash, read_sentinel, render_harness, render_stub
from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.paths import user_harness_path
from certora_autosetup.utils.solc_version_resolver import read_pragma_from_source_file

#: Prefix of the generated contract, so a harness is recognisable in a conf, a report and
#: a rule name without consulting the manifest.
HARNESS_PREFIX = "CertoraLibraryHarness_"

#: certoraRun refuses to build without a verification target, so the probe supplies a
#: trivially-true spec. It proves nothing and is never used for verification.
_PROBE_SPEC = "rule certoraLibraryHarnessProbe { assert true; }\n"


@dataclass(frozen=True)
class HarnessResult:
    """What the caller needs in order to swap the main contract and report the outcome."""

    library_name: str
    library_file: str
    harness_name: str
    harness_file: str
    plan_hash: str
    coverage: dict
    wrappers: List[str]
    skipped: List[dict]

    def to_dict(self) -> dict:
        return {
            "library_name": self.library_name,
            "library_file": self.library_file,
            "harness_name": self.harness_name,
            "harness_file": self.harness_file,
            "plan_hash": self.plan_hash,
            "coverage": self.coverage,
            "wrappers": self.wrappers,
            "skipped": self.skipped,
        }


def harness_name_for(library_name: str) -> str:
    return f"{HARNESS_PREFIX}{library_name}"


def _pragma_line(library_file: Path, project_root: Path) -> str:
    """Reuse the library's own pragma so the harness cannot fall outside its range."""
    spec = read_pragma_from_source_file(library_file, project_root)
    return f"pragma solidity {spec};" if spec else ""


def _import_line(library_file: Path, harness_file: Path) -> str:
    """Import the library by a path relative to the harness's own directory.

    The harness lives under ``certora/harnesses/`` rather than beside the library, so
    that generating it never dirties a vendored dependency; that makes the relative path
    a rebase rather than a plain ``./``.
    """
    relative = os.path.relpath(library_file.resolve(), harness_file.parent.resolve())
    return f'import "{relative}";'


def _run_probe_build(
    project_root: Path,
    harness_file: Path,
    harness_name: str,
    library_file: Path,
    library_name: str,
    solc: Optional[str],
    extra_files: Sequence[str],
    certora_run_command: str,
) -> None:
    """Compile the stub together with the library so the build reports the library's API."""
    spec_path = project_root / DIR_CERTORA_INTERNAL / "certora_library_harness_probe.spec"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(_PROBE_SPEC)

    harness_arg = f"{harness_file.relative_to(project_root).as_posix()}:{harness_name}"
    library_arg = f"{library_file.relative_to(project_root).as_posix()}:{library_name}"

    command = [
        certora_run_command,
        harness_arg,
        library_arg,
        *extra_files,
        "--verify",
        f"{harness_name}:{spec_path.relative_to(project_root).as_posix()}",
        "--compilation_steps_only",
    ]
    if solc:
        command += ["--solc", solc]

    logger.log(f"Probe build: {' '.join(command)}", "INFO", "Harnesser")
    completed = subprocess.run(
        command, cwd=project_root, capture_output=True, text=True, timeout=1800
    )
    if completed.returncode != 0:
        raise LibraryHarnessError(
            f"probe build failed for library {library_name}:\n"
            f"{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}"
        )


def ensure_library_harness(
    project_root: Path,
    library_file: Path,
    library_name: str,
    solc: Optional[str] = None,
    extra_files: Sequence[str] = (),
    certora_run_command: str = "certoraRun",
    validate: bool = True,
) -> HarnessResult:
    """Generate (or refresh) the harness that makes ``library_name`` verifiable.

    Returns the record the caller needs to swap the main contract. Raises rather than
    degrading: a harness that silently omits the functions the user cares about still
    runs and still reports success.
    """
    project_root = project_root.resolve()
    absolute_library = library_file if library_file.is_absolute() else project_root / library_file
    if not absolute_library.exists():
        raise LibraryHarnessError(f"library source {library_file} does not exist")

    harness_name = harness_name_for(library_name)
    harness_file = user_harness_path(project_root, harness_name)
    harness_file.parent.mkdir(parents=True, exist_ok=True)

    pragma = _pragma_line(absolute_library, project_root)
    import_lines = [_import_line(absolute_library, harness_file)]

    # The stub exists only so the probe build has something to compile that is not the
    # library itself; it carries one external function because a method-less contract is
    # dropped by contract discovery and by the signature database.
    harness_file.write_text(render_stub(harness_name, library_name, pragma, import_lines))

    _run_probe_build(
        project_root,
        harness_file,
        harness_name,
        absolute_library,
        library_name,
        solc,
        extra_files,
        certora_run_command,
    )

    api = read_library_api(
        project_root / BUILD_JSON_RELPATH,
        library_name,
        absolute_library.relative_to(project_root).as_posix(),
    )

    plan = build_plan(
        api,
        harness_name=harness_name,
        harness_file=harness_file.relative_to(project_root).as_posix(),
        pragma_line=pragma,
        import_lines=import_lines,
    )
    harness_file.write_text(render_harness(plan))
    logger.log(
        f"Generated {harness_name}: {plan.coverage['wrapped']} wrapper(s), "
        f"{len(plan.owned_vars)} owned state var(s), {plan.coverage['skipped']} skipped",
        "INFO",
        "Harnesser",
    )

    if validate:
        # Re-run the same probe against the filled harness. Compiling it in the project's
        # real build environment is what proves the wrappers are legal; a bespoke solc
        # invocation here would have to reinvent the project's import resolution.
        _run_probe_build(
            project_root,
            harness_file,
            harness_name,
            absolute_library,
            library_name,
            solc,
            extra_files,
            certora_run_command,
        )

    result = _result(plan)
    # Written beside the harness, under certora/, because that is the tree a cloud run
    # uploads — the skipped list is the only record of what the harness does not cover.
    manifest = harness_file.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(result.to_dict(), indent=2) + "\n")
    return result


def _result(plan: HarnessPlan) -> HarnessResult:
    return HarnessResult(
        library_name=plan.library_name,
        library_file=plan.library_source_file,
        harness_name=plan.harness_name,
        harness_file=plan.harness_file,
        plan_hash=plan_hash(plan),
        coverage=plan.coverage,
        wrappers=[w.name for w in plan.wrappers],
        skipped=[
            {"function": s.library_function, "reason": s.reason.value, "detail": s.detail}
            for s in plan.skipped
        ],
    )


def is_generated_library_harness(path: Path) -> bool:
    """Whether ``path`` is a harness this module generated.

    Decided by the sentinel in the file, not by its name, so a hand-written file that
    happens to match the naming convention is still the author's to overwrite.
    """
    if not path.exists() or path.suffix != ".sol":
        return False
    try:
        return read_sentinel(path.read_text(errors="replace")) is not None
    except OSError:
        return False


def existing_harness_provenance(project_root: Path, library_name: str) -> Optional[dict]:
    """The provenance record of an already-generated harness, if one is present.

    Read from the file itself rather than a sidecar, so the harness and the record of
    what produced it cannot drift apart.
    """
    harness_file = user_harness_path(project_root.resolve(), harness_name_for(library_name))
    if not harness_file.exists():
        return None
    return read_sentinel(harness_file.read_text(errors="replace"))
