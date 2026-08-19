"""Decide whether a main contract is a ``library``, before anything is built.

The Certora Prover does not reject a library verification target — it accepts it and
silently verifies nothing, because parametric rules filter libraries out of the method
set. There is therefore no error message to key on: detection has to read the
declaration itself.

It reads it from solc's own AST rather than from the source text. ``solc
--standard-json`` with ``stopAfter: "parsing"`` returns ``ContractDefinition`` nodes
carrying ``contractKind`` without resolving imports, type-checking, or generating code,
so a single unresolved-import-laden library file parses in milliseconds with no build
and no dependency setup.

``stopAfter`` requires solc >= 0.7. Below that, solc refuses to emit an AST for a file
whose imports it cannot resolve, which is every real library file, and pre-build
detection is not possible; such a project keeps today's behavior and logs why.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from packaging.version import Version

from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.solc_version_resolver import (
    convert_solc_version_to_certora_format,
    read_pragma_from_source_file,
    resolve_pragma_to_version,
)

#: Below this, solc has no ``stopAfter`` and cannot parse a file with unresolved imports.
MIN_SOLC_FOR_PARSE_ONLY = Version("0.7.0")


def _solc_binary(version: str) -> Optional[str]:
    """Locate an installed solc binary for ``version`` under either naming convention."""
    for name in (convert_solc_version_to_certora_format(version), f"solc-{version}"):
        if path := shutil.which(name):
            return path
    return None


def _parse_only_ast(solc: str, source_file: Path, content: str) -> Optional[Dict]:
    """Parse a single file and return its AST node dict, or None if solc could not.

    Imports are deliberately left unresolved: ``stopAfter: "parsing"`` never follows
    them, so the ``sources`` map holds exactly one entry.
    """
    request = {
        "language": "Solidity",
        "sources": {source_file.name: {"content": content}},
        "settings": {
            "stopAfter": "parsing",
            "outputSelection": {"*": {"": ["ast"]}},
        },
    }
    try:
        completed = subprocess.run(
            [solc, "--standard-json"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
        )
        response = json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        logger.log(f"solc parse-only probe failed for {source_file}: {e}", "DEBUG", "Harnesser")
        return None

    for error in response.get("errors", []):
        if error.get("severity") == "error":
            logger.log(
                f"solc could not parse {source_file}: {error.get('message', '')}",
                "DEBUG",
                "Harnesser",
            )
            return None

    sources = response.get("sources", {})
    entry = sources.get(source_file.name) or next(iter(sources.values()), None)
    return entry.get("ast") if entry else None


def contract_kind(
    source_file: Path,
    contract_name: str,
    project_root: Optional[Path] = None,
    preferred_solc: Optional[str] = None,
) -> Optional[str]:
    """Return the declared kind of ``contract_name`` — "library", "contract", "interface".

    None means the question could not be answered (no usable solc, unparseable file, or
    the name is not declared here); callers treat that as "not a library" and proceed
    unchanged, which is the behavior that predates this feature.
    """
    if not source_file.exists():
        return None

    content = source_file.read_text(errors="replace")
    pragma = read_pragma_from_source_file(source_file, project_root)
    version = resolve_pragma_to_version(pragma, preferred_solc) if pragma else preferred_solc
    if not version:
        logger.log(
            f"No solc version resolvable for {source_file}; skipping library detection",
            "DEBUG",
            "Harnesser",
        )
        return None

    if Version(version) < MIN_SOLC_FOR_PARSE_ONLY:
        logger.log(
            f"{source_file} resolves to solc {version}; parse-only AST needs >= "
            f"{MIN_SOLC_FOR_PARSE_ONLY}, so a library main contract cannot be detected "
            f"before the build",
            "WARNING",
            "Harnesser",
        )
        return None

    solc = _solc_binary(version)
    if not solc:
        logger.log(f"No installed solc binary for {version}", "DEBUG", "Harnesser")
        return None

    ast = _parse_only_ast(solc, source_file, content)
    if not ast:
        return None

    for node in ast.get("nodes", []):
        if node.get("nodeType") == "ContractDefinition" and node.get("name") == contract_name:
            return node.get("contractKind")
    return None


def is_library_main_contract(
    source_file: Path,
    contract_name: str,
    project_root: Optional[Path] = None,
    preferred_solc: Optional[str] = None,
) -> bool:
    """Whether verifying ``contract_name`` requires a generated harness."""
    return contract_kind(source_file, contract_name, project_root, preferred_solc) == "library"
