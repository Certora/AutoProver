"""A run's analysis and properties, pinned to disk so a later run can start at formalization.

Everything before formalization is expensive and none of it is where this backend breaks. On SPL
stake-pool, analysis and extraction together were **98% of the wall clock before the first prover
job** — 436s and ~56 minutes against a formalization phase where the munges, tree materialization,
env loading, submission and sanity all live. Two runs spent an hour and ~$100 each to reach the part
under test.

**Both halves are pinned, and that is the point.** Pinning properties alone does not work: a unit is
an index into the analyzed model (``SolanaComponentInstance(ind=i, _program=main)``), so properties
keyed by component name, matched against an analysis regenerated each run, can attach to a component
that has since changed underneath the name. That is not hypothetical — stake-pool decomposed into 7
components one day and 10 the next, and "Pool Administration" became "Pool Initialization" plus
"Admin & Fee Configuration". Pinning the analysis makes units come from the fixture, so the
mismatch cannot arise rather than being checked for.

What can still drift is the fixture against the *source*, and that is a fact rather than a name, so
:attr:`PinnedRun.target_commit` records it and :meth:`PinnedRun.check_target` says so when the
checkout has moved.

Write one with ``--pin-to`` and read it back with ``--properties``. Components the fixture omits are
dropped from the run, which is the other half of what makes this cheap: one component of ten.

Properties are keyed by :attr:`~composer.spec.system_model.FeatureUnit.slug`, which is the unit's
own identity and therefore the only key guaranteed to address exactly one unit. Note it is *not*
quite the name on disk: the harness module and the artifact stem run the slug through
:func:`~composer.spec.cvlr.harness.module_name`, which lowercases it, so a fixture says
``Pool_Initialization`` where the file says ``pool_initialization``. The keys are machine-written by
``--pin-to``, so nothing has to spell one by hand; keying on the lossy name instead would let two
components collide onto one key.
"""

import json
import logging
import pathlib
import subprocess
from dataclasses import dataclass

from composer.spec.system_model import BaseApplication
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)

#: Fixture format. Bumped when a field changes meaning, so a stale file fails on its version rather
#: than on whatever downstream error its old shape happens to produce.
PIN_VERSION = 1


@dataclass(frozen=True)
class PinnedRun[App: BaseApplication]:
    """One run's analysis and properties, enough to re-enter the pipeline at formalization."""

    analysis: App
    properties: dict[str, list[PropertyFormulation]]
    target_commit: str | None
    source: pathlib.Path

    def total(self) -> int:
        return sum(len(v) for v in self.properties.values())

    def check_target(self, project_root: pathlib.Path) -> None:
        """Warn when the checkout has moved out from under the fixture.

        A warning rather than a refusal: the analysis may still be perfectly good after a commit
        that touched a different crate, and a fixture that refused on every unrelated commit would
        be re-pinned reflexively, which is how a stale one gets blessed. The run says what it is
        working from and the reader decides.
        """
        if self.target_commit is None:
            return
        head = git_head(project_root)
        if head is None or head == self.target_commit:
            return
        _log.warning(
            "pinned run %s was taken at %s but %s is at %s — the analysis may no longer describe "
            "this source; re-pin if the properties look wrong for what you are reading",
            self.source, self.target_commit[:12], project_root, head[:12],
        )


def git_head(project_root: pathlib.Path) -> str | None:
    """The checkout's commit, or ``None`` when it is not a git repository (or git is unavailable)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def load_pinned_run[App: BaseApplication](
    path: pathlib.Path, model: type[App]
) -> PinnedRun[App]:
    """Read a fixture, validating the analysis against the ecosystem's own model type.

    ``model`` is the ecosystem's ``system_model``, so a fixture taken from one ecosystem cannot be
    fed to another: the analysis simply fails to validate, and it fails here rather than at the
    first attribute the wrong model does not have.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a pinned-run object, got {type(raw).__name__}")

    version = raw.get("version", PIN_VERSION)
    if version != PIN_VERSION:
        raise ValueError(
            f"{path}: pin format v{version}, but this build reads v{PIN_VERSION}. Re-pin with "
            "--pin-to against the current build."
        )

    for key in ("analysis", "properties"):
        if key not in raw:
            raise ValueError(f"{path}: missing {key!r}. Write fixtures with --pin-to.")

    analysis = model.model_validate(raw["analysis"])

    props_raw = raw["properties"]
    if not isinstance(props_raw, dict):
        raise ValueError(f"{path}: 'properties' must be an object keyed by component slug")
    properties: dict[str, list[PropertyFormulation]] = {}
    for slug, props in props_raw.items():
        if not isinstance(props, list):
            raise ValueError(f"{path}: {slug!r} must map to a list of properties")
        if not props:
            raise ValueError(f"{path}: {slug!r} has no properties; drop the key instead")
        properties[slug] = [PropertyFormulation.model_validate(p) for p in props]
    if not properties:
        raise ValueError(f"{path}: no components pinned")

    return PinnedRun(analysis, properties, raw.get("target_commit"), path)


def write_pinned_run(
    path: pathlib.Path,
    analysis: BaseApplication,
    properties: dict[str, list[PropertyFormulation]],
    project_root: pathlib.Path,
) -> None:
    """Write the fixture a later ``--properties`` reads.

    Called once analysis and extraction have both produced their results, which is the only moment
    the two halves are known to belong together.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {
            "version": PIN_VERSION,
            "target_commit": git_head(project_root),
            "analysis": analysis.model_dump(mode="json"),
            "properties": {
                slug: [p.model_dump(mode="json") for p in props]
                for slug, props in properties.items()
            },
        },
        indent=2,
    ) + "\n")
    total = sum(len(v) for v in properties.values())
    _log.info(
        "pinned %d propert%s across %d component(s) to %s — re-run with --properties %s to skip "
        "analysis and extraction",
        total, "y" if total == 1 else "ies", len(properties), path, path,
    )
