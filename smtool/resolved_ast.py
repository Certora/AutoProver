"""The TRUSTED, import+scene-resolved view of the setup spec.

The effective `methods{}` set of a setup spec cannot be read from the top file's text: a `methods`
entry may be declared in a NESTED import. The only trusted source is the resolved CVL AST produced by
`Typechecker.jar -printAst` (which runs the real `CVLAstBuilder`, resolving imports + scene).
`ASTExtraction.jar --raw` does NOT resolve imports (it blanks them), so it must not be used here.

We obtain the dump via `certoraRun <setup.conf> --compilation_steps_only --dump_cvl_ast <out>`:
`--compilation_steps_only` runs the local build + typecheck but skips the cloud/SMT submit, and the
same build step also writes `all_methods.json` (the facts for smtool/scene.py). So ONE local pass —
no cloud, cacheable per setup — yields both the facts and this resolved AST.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def dump_resolved_ast(setup_conf_path: str | Path, out_path: str | Path,
                      cwd: str | Path | None = None) -> Path:
    """Run a LOCAL build+typecheck of `setup_conf_path` (no cloud) that writes the resolved CVL AST
    JSON to `out_path`. Reuses certoraRun. `cwd` should be the sources root the conf's paths resolve
    against (default: the conf's parents[2], matching smtool.setup.consume_setup)."""
    setup_conf_path = Path(setup_conf_path)
    cwd = Path(cwd) if cwd else setup_conf_path.resolve().parents[2]
    subprocess.run(
        ["certoraRun", str(setup_conf_path), "--compilation_steps_only",
         "--dump_cvl_ast", str(Path(out_path).resolve())],
        cwd=str(cwd), check=True,
    )
    return Path(out_path)


def _method_key(sig_holder: dict) -> tuple[str, int] | None:
    """(functionName, arity) from a summary signature holder (`internal/externalSummaries[i].first`).
    Returns None if the shape is unexpected (never guess — an imperfect key must not cause a
    false skip)."""
    sig = sig_holder.get("signature")
    if not isinstance(sig, dict):
        return None
    name = sig.get("functionName")
    if not isinstance(name, str):
        return None
    params = sig.get("params")
    arity = len(params) if isinstance(params, list) else 0
    return (name, arity)


def summarized_methods(ast: str | Path | dict) -> set[tuple[str, int]]:
    """The set of `(functionName, arity)` the setup's RESOLVED closure summarizes
    (internal + external + unresolved), flattened across nested imports. This is the trusted set to
    reconcile our own `methods{}` entries against. Plain `envfree` DECLARATIONS (no summary) are NOT
    in the resolved AST's summary lists — those clashes need the reactive typecheck fallback."""
    if not isinstance(ast, dict):
        ast = json.loads(Path(ast).read_text())
    keys: set[tuple[str, int]] = set()
    for grp in ("internalSummaries", "externalSummaries", "unresolvedSummaries"):
        for pair in ast.get(grp, []):
            holder = pair.get("first") if isinstance(pair, dict) else None
            if isinstance(holder, dict):
                k = _method_key(holder)
                if k is not None:
                    keys.add(k)
    return keys
