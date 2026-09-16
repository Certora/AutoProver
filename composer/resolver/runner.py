"""The resolver's ``ProverRunner``: verify a spec buffer against a fetched run's
canonical configuration.

The pipeline's runner layers ``prover_config_overlay`` over the author's config
(forcing ``parametric_contracts``, ``optimistic_loop``, ``rule_sanity``) and
stages the spec under ``certora/specs/``. Against a fetched run both would
change the obligation: the settings the rule timed out under, and the spec's
own location, which is what its CVL imports resolve against. This runner
keeps the conf as the run recorded it, minus the keys that belong to one
invocation, and writes the buffer over the spec at its original path for the
duration of the run.
"""
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from prover_output_utility import cloud_server_for_env

from composer.prover.core import (
    CexHandler, ProverCallbacks, ProverOptions, ProverReport, run_prover,
)
from composer.spec.util import temp_certora_file

from .workspace import RunWorkspace

#: Keys the run recorded for its own invocation. The resolver sets its own.
RUN_SPECIFIC_KEYS: frozenset[str] = frozenset({
    "msg", "rule", "exclude_rule", "compilation_steps_only",
    "global_timeout", "server", "wait_for_results",
})

#: Where the per-run conf is written, relative to ``run_dir``. Kept out of the
#: project's own ``certora/`` so the workspace's files stay untouched.
CONF_DIR = Path(".certora_resolver")


def compose_conf(
    workspace: RunWorkspace,
    *,
    rules: list[str] | None,
    exclude_rules: list[str] | None,
    overrides: dict,
) -> dict:
    """The canonical conf with the run-specific keys removed and this
    invocation's selection and overrides applied."""
    conf = {k: v for k, v in workspace.conf.items() if k not in RUN_SPECIFIC_KEYS}
    conf["verify"] = f"{workspace.main_contract}:{workspace.spec_path.as_posix()}"
    if rules is not None:
        conf["rule"] = rules
    if exclude_rules is not None:
        conf["exclude_rule"] = exclude_rules
    conf.update(overrides)
    return conf


@contextmanager
def staged_spec(spec_file: Path, contents: str) -> Iterator[None]:
    """Write ``contents`` over ``spec_file`` for the duration of the block and
    put the previous contents back afterwards. The file is the workspace's
    diff baseline, so it must read as the run left it once the block ends."""
    previous = spec_file.read_bytes() if spec_file.exists() else None
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(contents, encoding="utf-8")
    try:
        yield
    finally:
        if previous is None:
            spec_file.unlink(missing_ok=True)
        else:
            spec_file.write_bytes(previous)


@contextmanager
def staged_conf(run_dir: Path, conf: dict) -> Iterator[str]:
    """Write ``conf`` under ``run_dir`` and yield its path relative to ``run_dir``."""
    with temp_certora_file(
        root=str(run_dir),
        ext="conf",
        content=json.dumps(conf, indent=2),
        prefix="resolver",
        dest_dir=CONF_DIR,
    ) as conf_path:
        yield conf_path


def prover_options(workspace: RunWorkspace, *, cloud: bool) -> ProverOptions:
    """The original run's timeout, and the deployment's server for cloud runs."""
    extras = ["--global_timeout", str(int(workspace.original.global_timeout))]
    if cloud:
        extras += ["--server", cloud_server_for_env()]
    return ProverOptions(extra_args=extras)


@dataclass(frozen=True)
class WorkspaceProverRunner:
    """Implements :class:`composer.spec.source.plugin.ProverRunner` over a
    :class:`RunWorkspace`. ``working_dir`` is the run directory, or a
    materialized copy of it when the agent's VFS overlay is non-empty."""
    workspace: RunWorkspace
    cloud: bool

    async def run(
        self,
        *,
        curr_spec: str,
        working_dir: str,
        cex_handler: CexHandler,
        callbacks: ProverCallbacks,
        tool_call_id: str,
        rules: list[str] | None = None,
        exclude_rules: list[str] | None = None,
        **config,
    ) -> ProverReport | str:
        work = Path(working_dir)
        conf = compose_conf(
            self.workspace, rules=rules, exclude_rules=exclude_rules, overrides=config,
        )
        with staged_spec(work / self.workspace.spec_path, curr_spec), staged_conf(work, conf) as conf_path:
            return await run_prover(
                work,
                [conf_path],
                tool_call_id,
                prover_options(self.workspace, cloud=self.cloud),
                callbacks,
                cex_handler,
            )
