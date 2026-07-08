"""Shared base for run-scoped artifact writers.

Each workflow's ``ArtifactStore`` owns its own deliverable layout so the path
conventions live in one place rather than smeared across the pipeline. This base
hosts what is *identical* across workflows — the analysis-phase
``properties.json``, the ``{property title: [demonstrating names]}`` map, and
``commentary.md`` — keyed off a per-component ``stem`` plus two abstract
directories. The workflow-specific bundles (CVL ``.spec``/``.conf`` for the prover,
``.t.sol`` metadata for foundry) live in the subclasses, which translate their
domain objects into ``stem``s and call these primitives.
"""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict, Unpack

from composer.diagnostics.timing import RunSummary
from composer.spec.gen_types import PROPERTIES_SUBDIR, under_project
from composer.spec.types import PropertyFormulation
from composer.spec.util import ensure_dir
from .types import ArtifactIdentifier, FormalResult
from composer.spec.source.report.schema import AutoProverReport


class StoreConfiguration(TypedDict):
    internal_dir: Path | str
    deliverable_dir: Path | str
    report_dir : Path | str

class ArtifactStore[I: ArtifactIdentifier, FormT: FormalResult](ABC):
    """Persists a pipeline's outputs under a single project root.

    Holds only the run-constant project root, so it is cheap to construct ad hoc
    wherever a write is needed. Subclasses fix the deliverable / diagnostics
    directories and add their format-specific bundles.
    """

    def __init__(
        self,
        project_root: str | Path,
        property_suffix: str,
        **store_config: Unpack[StoreConfiguration]
    ):
        self._project_root = project_root
        self._property_suffix = property_suffix

        self.store_conf = store_config

    def write_properties(self, i: I, props: list[PropertyFormulation]):
        self._write_properties(i.stem, props)

    def write_artifact(self, i: I, artifact: FormT) -> Path:
        target_dir = ensure_dir(self._artifact_dir())
        target_path = (target_dir / i.artifact_file)
        target_path.write_text(artifact.artifact_text)
        self._write_commentary(i.stem, artifact.commentary)
        self._write_property_map(
            i.stem, self._property_suffix,
            {k: v for (k,v) in artifact.property_units()},
        )
        return target_path.relative_to(self._project_root)

    def _deliverable_dir(self) -> Path:
        """Absolute base dir (under the project root) for human-facing deliverables."""
        return ensure_dir(under_project(self._project_root, self.store_conf["deliverable_dir"]))

    def _internal_dir(self) -> Path:
        return ensure_dir(under_project(self._project_root, self.store_conf["internal_dir"]))
    
    def _report_dir(self) -> Path:
        return ensure_dir(under_project(self._project_root, self.store_conf["report_dir"]))

    @abstractmethod
    def _artifact_dir(self) -> Path:
        "absolute base dir for verification artifacts (tests, spec files)"
        ...

    def _properties_dir(self) -> Path:
        return ensure_dir(self._deliverable_dir() / PROPERTIES_SUBDIR)

    # -- shared per-component primitives ------------------------------------

    def _write_properties(self, stem: str, props: list[PropertyFormulation]) -> None:
        """Analysis-phase properties → ``{deliverable}/properties/{stem}.properties.json``.
        ``title`` is the cross-reference key used by the accompanying property map."""
        (self._properties_dir() / f"{stem}.properties.json").write_text(
            json.dumps([p.model_dump() for p in props], indent=2)
        )

    def _write_commentary(self, stem: str, commentary: str) -> None:
        (self._properties_dir() / f"{stem}.commentary.md").write_text(commentary)

    def _write_property_map(
        self, stem: str, suffix: str, mapping: dict[str, list[str]],
    ) -> None:
        """A ``{property title: [demonstrating names]}`` map → ``{stem}.{suffix}.json``.
        Titles are unique (enforced at extraction). ``suffix`` is the workflow's term
        for the demonstrators (``property_rules`` for CVL, ``property_tests`` for foundry)."""
        (self._properties_dir() / f"{stem}.{suffix}.json").write_text(
            json.dumps(mapping, indent=2)
        )

    def write_report(self, report: AutoProverReport):
        report_dir = self._report_dir()
        out = report_dir / "report.json"
        out.write_text(report.model_dump_json(indent=2) + "\n")


    # -- shared run-level ---------------------------------------------------

    def write_token_usage(self, summary: RunSummary) -> None:
        """The run's accumulated LLM token usage → ``{internal}/token_usage.json``.

        Raw counts only (``input`` / ``output`` / ``cache_read`` / ``cache_write``),
        broken down ``by_model`` / ``by_phase`` plus run-wide ``totals``. Captures every
        call through the LLM factory (including out-of-graph prover/CEX side-calls) via
        the usage callback attached at model construction."""
        payload = {"run_id": summary.run_id, **summary.token_usage_summary()}
        (ensure_dir(self._internal_dir()) / "token_usage.json").write_text(
            json.dumps(payload, indent=2)
        )
