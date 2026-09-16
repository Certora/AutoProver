"""Compile the fetched run once before any search starts.

A run's snapshot does not always rebuild: the execution host may lack the
compiler the conf names, or an upload may have dropped a file. Finding that
out costs one ``certoraRun --compilation_steps_only``, which is cheap next to
a proof search that would only discover it on its first prover call.
"""
from pathlib import Path

from composer.prover.core import BUILD_TIMEOUT_S, _run_captured

from .runner import compose_conf, staged_conf
from .workspace import RunWorkspace


async def compile_only(workspace: RunWorkspace) -> str | None:
    """``None`` when the run's own spec and sources compile in ``run_dir``;
    otherwise the compiler's output, which names what is missing."""
    conf = compose_conf(
        workspace, rules=None, exclude_rules=None,
        overrides={"compilation_steps_only": True, "msg": "timeout resolver preflight"},
    )
    with staged_conf(workspace.run_dir, conf) as conf_path:
        rc, output = await _run_captured(
            "certoraRun", conf_path,
            cwd=Path(workspace.run_dir),
            timeout=BUILD_TIMEOUT_S,
        )
    return None if rc == 0 else output
