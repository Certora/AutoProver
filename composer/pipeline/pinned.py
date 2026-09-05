"""Properties supplied as input instead of extracted, so a run can start at formalization.

Extraction is where a real target's money goes. On SPL stake-pool it was **87% of the wall clock
before the first prover job** — ten agents at 500-3350s each — while everything the CVLR backend
actually keeps breaking (munges, tree materialization, env files, submission, sanity) lives in
formalization, on the other side of it. So a run that re-derives the properties every time pays the
expensive phase to reach the risky one.

The properties themselves are the natural cut. They are a flat list of ``{sort, title,
description}``, they do not change while the target's commit does not, and the artifact store
already writes them per unit. Pinning is therefore a round trip: take what a run wrote under
``certora/cvlr/properties/``, hand it back with ``--properties``, and formalization sees the same
input at no LLM cost.

**Analysis still runs.** Units come from the analyzed model, so skipping analysis would mean
synthesizing one — a much larger change for the 11% it costs. What is bought here is the 87%.

Keyed by :attr:`~composer.spec.system_model.FeatureUnit.slug` rather than display name, because the
slug is what the artifact filename already carries, so which key belongs to which file needs no
explanation. A pin naming no unit is an error rather than a warning: the file is a fixture, and
analysis having drifted out from under it is the thing a fixture exists to catch.
"""

import json
import logging
import pathlib
from collections.abc import Sequence

from composer.spec.system_model import FeatureUnit
from composer.spec.types import PropertyFormulation

_log = logging.getLogger(__name__)


class PinnedProperties:
    """Per-unit properties read off disk, standing in for the extraction phase.

    Units the file does not mention are **dropped from the run**, which is the other half of what
    this is for: pinning one component of ten is what makes an end-to-end test over a large program
    cheap enough to run often.
    """

    def __init__(self, by_slug: dict[str, list[PropertyFormulation]], source: pathlib.Path) -> None:
        self._by_slug = by_slug
        self.source = source

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(self._by_slug)

    def total(self) -> int:
        return sum(len(v) for v in self._by_slug.values())

    def get(self, unit: FeatureUnit) -> list[PropertyFormulation] | None:
        return self._by_slug.get(unit.slug)

    def check_every_pin_matched(self, units: Sequence[FeatureUnit]) -> None:
        """Raise unless every pinned slug names a unit this analysis produced.

        The failure carries both lists, because the useful next action is to re-pin against the
        names analysis now gives — and a message that reported only the mismatch would send the
        reader back to the log to find them.
        """
        present = {u.slug for u in units}
        missing = sorted(self.slugs - present)
        if not missing:
            return
        raise ValueError(
            f"{self.source}: pinned properties name {len(missing)} component(s) this analysis did "
            f"not produce: {', '.join(missing)}. Analysis produced: {', '.join(sorted(present))}. "
            "Re-pin from a run of the current analysis, or correct the file."
        )


def load_pinned_properties(path: pathlib.Path) -> PinnedProperties:
    """Read a pin file: ``{slug: [{sort, title, description}, ...], ...}``.

    Also accepts the shape the artifact store writes for a *single* unit — a bare list — when the
    filename is ``<something>_<slug>.properties.json``, so the common case of pinning one component
    is a copy rather than an edit.
    """
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        slug = _slug_from_artifact_name(path)
        raw = {slug: raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object keyed by component slug, or a bare list")

    by_slug: dict[str, list[PropertyFormulation]] = {}
    for slug, props in raw.items():
        if not isinstance(props, list):
            raise ValueError(f"{path}: {slug!r} must map to a list of properties")
        if not props:
            raise ValueError(f"{path}: {slug!r} has no properties; drop the key instead")
        by_slug[slug] = [PropertyFormulation.model_validate(p) for p in props]

    if not by_slug:
        raise ValueError(f"{path}: no components pinned")
    return PinnedProperties(by_slug, path)


def _slug_from_artifact_name(path: pathlib.Path) -> str:
    """``cvlr_pool_initialization.properties.json`` -> ``pool_initialization``.

    The leading segment is the backend's own prefix, which is not part of the slug.
    """
    stem = path.name.removesuffix(".properties.json")
    _, _, slug = stem.partition("_")
    if not slug:
        raise ValueError(
            f"{path}: a bare list needs a filename of the form '<prefix>_<slug>.properties.json' "
            "so the component can be identified; otherwise use an object keyed by slug"
        )
    return slug
