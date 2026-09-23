"""Prover (autoprove) artifact writer: the ``certora/`` deliverable layout.

A subclass of the shared :class:`composer.spec.artifacts.ArtifactStore`. Adds the
CVL-specific bundle (``specs/``, ``confs/``) and the autoprove report on top of the
base's shared property / commentary / token-usage primitives. The stem / filename /
run-key conventions for a spec (``autospec_{slug}``) are captured by
:class:`ComponentSpec`, not interpolated at call sites.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import override

from composer.diagnostics.timing import RunSummary
from composer.spec.artifacts import ArtifactStore, QUARANTINE_SUFFIX
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.gen_types import (
    AP_REPORT_DIR, AUTOPROVE_INTERNAL_DIR, CERTORA_DIR, component_specs_dir, under_project,
)
from composer.spec.source.prover import prover_config_overlay
from composer.spec.util import ensure_dir


def _write_checked(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` (creating parents). If ``path`` already holds DIFFERENT content,
    raise: the deliverable is materialized from many buffers (and, across the run, many components) into
    one tree, so an overwrite that changes content means two sources disagree on a file — a bug, never a
    silent clobber. Identical re-writes are idempotent no-ops."""
    if path.exists():
        if path.read_text() != content:
            raise AssertionError(
                f"deliverable overwrite of {path} with different content — two sources disagree on it"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentSpec:
    """A per-component generated spec. ``slug`` is the component's slugified name."""
    slug: str

    @property
    def stem(self) -> str:
        return f"autospec_{self.slug}"

    @property
    def spec_filename(self) -> str:
        return f"{self.stem}.spec"

    @property
    def run_key(self) -> str:
        """Key under which this spec's prover run is recorded in the run-link map."""
        return self.slug

    @property
    def specs_dir(self) -> Path:
        """Project-relative dir this component's buffers occupy."""
        return component_specs_dir(self.slug)

    def buffer_spec_rel(self, name: str) -> str:
        """Project-relative path of buffer ``name``'s ``.spec`` under this component's spec dir."""
        return (self.specs_dir / f"{name}.spec").as_posix()

    @property
    def artifact_file(self) -> str:
        return self.spec_filename


class ProverArtifactStore(ArtifactStore[ComponentSpec, GeneratedCVL]):
    """Persists the autoprove pipeline's outputs under ``certora/`` (plus
    ``.certora_internal/autoProve/`` diagnostics)."""

    def __init__(self, project_root: str, main_contract: str):
        super().__init__(
            project_root,
            "property_rules",
            deliverable_dir=CERTORA_DIR,
            internal_dir=AUTOPROVE_INTERNAL_DIR,
            report_dir=AP_REPORT_DIR
        )
        self._main_contract = main_contract

    @override
    def _artifact_dir(self) -> Path:
        return under_project(self._project_root, CERTORA_DIR)

    @override
    def write_artifact(self, i: ComponentSpec, artifact: GeneratedCVL) -> Path:
        specs_root = under_project(self._project_root, i.specs_dir)
        # Every spec -- entrypoints and the shared specs they import -- as its own file.
        for name, cvl in artifact.spec_files.items():
            _write_checked(specs_root / f"{name}.spec", cvl)
        if artifact.config is not None:
            confs_root = ensure_dir(self._deliverable_dir() / "confs" / i.slug)
            # A runnable .conf for each entrypoint (shared specs are imported, not run directly).
            for name in artifact.entrypoint_specs:
                conf = prover_config_overlay(
                    artifact.config,
                    main_contract=self._main_contract,
                    verify_target=f"{self._main_contract}:{i.buffer_spec_rel(name)}",
                )
                _write_checked(confs_root / f"verify_{name}.conf", json.dumps(conf, indent=2))
        else:
            _log.warning("no base config for %s; skipping conf dump", i.stem)
        self._write_commentary(i.stem, artifact.commentary)
        self._write_property_map(
            i.stem, self._property_suffix, {k: v for (k, v) in artifact.property_checks()},
        )
        return specs_root.relative_to(self._project_root)

    @override
    def write_quarantined(self, i: ComponentSpec, artifact: GeneratedCVL) -> Path:
        """Persist a budget-curtailed component for inspection: each buffer under a poisoned
        ``.spec.unverified`` name, no conf. Returns the component's spec directory."""
        specs_root = under_project(self._project_root, i.specs_dir)
        for name, cvl in artifact.spec_files.items():
            _write_checked(specs_root / f"{name}.spec{QUARANTINE_SUFFIX}", cvl)
        return specs_root.relative_to(self._project_root)

    # -- run-level ----------------------------------------------------------

    def write_component_runs(self, runs: dict[str, str]) -> None:
        """``{spec run-key: final prover-run link}`` to
        ``.certora_internal/autoProve/components_to_prover_runs.json``."""
        out_dir = ensure_dir(self._internal_dir())
        (out_dir / "components_to_prover_runs.json").write_text(json.dumps(runs, indent=2))

    @override
    def _job_info_payload(
        self, summary: RunSummary, *, user_id: str, run_mode: str
    ) -> dict[str, object]:
        """Extends the shared manifest body with the prover-reported runtime: the
        autoprove ``job_info.json`` also records ``prover_usage`` (summed prover
        start-to-end time), alongside the base identity + ``token_usage``."""
        return {
            **super()._job_info_payload(summary, user_id=user_id, run_mode=run_mode),
            "prover_usage": summary.prover_usage_summary(),
        }
