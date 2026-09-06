"""Reading ``.certora_build.json``, certoraRun's record of what it actually compiled.

The file is keyed by compilation unit; each unit holds a ``contracts`` list, and a
contract reached through several units appears once per unit. Five places used to walk
that structure with their own nesting checks and their own idea of where the file
lives, which is one place to get it wrong per caller.
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL

BUILD_JSON_NAME = ".certora_build.json"

#: Where certoraRun writes it under the run directory it reports as ``latest``.
BUILD_JSON_RELPATH = Path(DIR_CERTORA_INTERNAL) / "latest" / BUILD_JSON_NAME


def build_json_path(project_root: Path) -> Optional[Path]:
    """The build json of the most recent run under ``project_root``, or None."""
    latest = project_root / BUILD_JSON_RELPATH
    if latest.exists():
        return latest
    # `latest` is normally a symlink to the timestamped run dir; fall back to the
    # newest run dir by name (they sort chronologically) if it is absent.
    candidates = sorted(project_root.glob(f"{DIR_CERTORA_INTERNAL}/*/{BUILD_JSON_NAME}"))
    return candidates[-1] if candidates else None


def load_build_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def iter_contracts(build_data: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Every contract record across all compilation units in the build.

    A contract reached through several units appears once per unit; callers that need
    a single record must disambiguate themselves.
    """
    for obj in build_data.values():
        if isinstance(obj, dict):
            for contract in obj.get("contracts", []):
                if isinstance(contract, dict):
                    yield contract


def contract_source_file(contract: Dict[str, Any]) -> str:
    """Where the contract was written, preferring the pre-instrumentation path.

    ``file`` points into the instrumented tree certora compiles; ``original_file`` is
    the project path the user knows, and is what a conf entry has to name.
    """
    return contract.get("original_file") or contract.get("file") or ""
