"""Prover (autoprove) artifact writer: the ``certora/`` deliverable layout.

A subclass of the shared :class:`composer.spec.artifacts.ArtifactStore`. Adds the
CVL-specific bundle (``specs/``, ``confs/``) and the autoprove report on top of the
base's shared property / commentary / token-usage primitives. The stem / filename /
run-key conventions for a spec (``autospec_{slug}`` vs ``invariants``) are captured by
the :data:`SpecIdentity` sum type, not interpolated at call sites.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import override

from composer.diagnostics.timing import RunSummary
from composer.spec.artifacts import ArtifactStore
from composer.spec.cvl_generation import GeneratedCVL
from composer.spec.gen_types import (
    AP_REPORT_DIR, AUTOPROVE_INTERNAL_DIR, CERTORA_DIR, under_project,
)
from composer.spec.source.prover import prover_config_overlay
from composer.spec.util import ensure_dir

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
    def artifact_file(self) -> str:
        return self.spec_filename


@dataclass(frozen=True)
class InvariantSpec:
    """The single structural-invariants spec."""

    @property
    def stem(self) -> str:
        return "invariants"

    @property
    def spec_filename(self) -> str:
        return f"{self.stem}.spec"

    @property
    def run_key(self) -> str:
        return "invariants"
    
    @property
    def artifact_file(self) -> str:
        return self.spec_filename


type SpecIdentity = ComponentSpec | InvariantSpec


class ProverArtifactStore(ArtifactStore[SpecIdentity, GeneratedCVL]):
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
    def write_artifact(self, i: ComponentSpec | InvariantSpec, artifact: GeneratedCVL) -> Path:
        written_spec = super().write_artifact(i, artifact)
        self._write_conf(i, artifact.config, written_spec)
        return written_spec

    def _write_conf(
        self, spec: SpecIdentity, base_config: dict | None, spec_path: Path,
    ) -> None:
        """The prover conf for the run: the generation's final ``state["config"]`` plus
        the fixed run overlay (shared with the live ``verify_spec`` run). No-op if no
        base config."""
        if base_config is None:
            _log.warning("no base config for %s; skipping conf dump", spec.stem)
            return
        conf = prover_config_overlay(
            base_config,
            main_contract=self._main_contract,
            verify_target=f"{self._main_contract}:{spec_path}",
        )
        confs_dir = ensure_dir(self._deliverable_dir() / "confs")
        (confs_dir / f"{spec.stem}.conf").write_text(json.dumps(conf, indent=2))

    # -- run-level ----------------------------------------------------------

    def write_component_runs(self, runs: dict[str, str]) -> None:
        """``{spec run-key: final prover-run link}`` to
        ``.certora_internal/autoProve/components_to_prover_runs.json``."""
        out_dir = ensure_dir(self._internal_dir())
        (out_dir / "components_to_prover_runs.json").write_text(json.dumps(runs, indent=2))

    def write_job_info(self, summary: RunSummary, *, user_id: str) -> None:
        """The run's identity + usage manifest — ``user_id``, ``run_id``, and the
        ``token_usage`` / ``prover_usage`` summaries — to ``certora/ap_report/job_info.json``.
        ``user_id`` is passed in so this stays a pure serializer of run state."""
        payload = {
            "user_id": user_id,
            "run_id": summary.run_id,
            "token_usage": summary.token_usage_summary(),
            "prover_usage": summary.prover_usage_summary(),
        }
        out = self._report_dir() / "job_info.json"
        out.write_text(json.dumps(payload, indent=2) + "\n")
        _log.info("autoprove job info: wrote %s", out)
