"""A descriptor's phase declarations, resolved into the one model an application runs on."""

import enum
from dataclasses import dataclass
from typing import Any, cast

from composer.pipeline.core import CorePhases
from composer.rustapp.descriptor import AppDescriptor, PhaseRole, PhaseSpec


@dataclass(frozen=True)
class PhaseModel:
    """The descriptor's phase declarations, resolved once into everything the pipeline and the
    frontend need: the synthesized enum, the driver's core-phase mapping, the frontend's labels
    and section order.

    :func:`build_phase_model` is the *only* place that synthesizes the enum, and every consumer
    takes a built model rather than a descriptor. That is not tidiness: ``enum.Enum(...)`` mints a
    fresh class per call, and both the frontend's label lookup and the driver's phase tagging match
    members by identity — a second model built from the same descriptor would compare unequal to the
    first and silently lose every label.
    """

    phase: type[enum.Enum]
    #: The four slots the driver tags, as members of :attr:`phase`.
    core: CorePhases
    ordered: tuple[PhaseSpec, ...]

    @property
    def labels(self) -> dict[Any, str]:
        """Every declared phase's label, keyed by the enum member — what a frontend looks up."""
        return {self.phase[p.key]: p.label for p in self.ordered}

    @property
    def section_order(self) -> list[str]:
        """The labels in declared order — the frontend's section layout."""
        return [p.label for p in self.ordered]

    def member(self, key: str) -> enum.Enum:
        """The member for a declared phase ``key``."""
        return self.phase[key]

    def role_member(self, role: PhaseRole) -> enum.Enum | None:
        """The member of the phase claiming ``role``, or ``None`` when no phase claims it."""
        return next((self.phase[p.key] for p in self.ordered if p.role is role), None)

    @property
    def first_member(self) -> enum.Enum:
        """The first declared phase — where a task with no phase of its own is grouped."""
        return self.phase[self.ordered[0].key]


def build_phase_model(descriptor: AppDescriptor) -> PhaseModel:
    """Synthesize an application's phase model from its descriptor.

    The enum is safe to synthesize: the code only ever uses phase members for ``.name`` and as dict
    keys (no isinstance / identity checks against a static class). Every *required* role must be
    claimed — the driver tags all four (the optional ones fall back; see :class:`PhaseRole`)."""
    ordered = descriptor.ordered_phases()
    name = "".join(part.capitalize() for part in descriptor.name.split("_")) + "Phase"
    # enum.Enum's functional API is typed as returning an ``Enum`` instance, not the new
    # class; it does return a class at runtime.
    phase = cast(type[enum.Enum], enum.Enum(name, {p.key: p.key for p in ordered}))

    role_to_key = descriptor.role_map()
    missing = [r.value for r in PhaseRole.required() if r not in role_to_key]
    if missing:
        raise ValueError(
            f"descriptor {descriptor.name!r} is missing core phase(s): {missing}. "
            "Every application must map analysis/extraction/formalization/report."
        )
    core = CorePhases(
        analysis=phase[role_to_key[PhaseRole.ANALYSIS]],
        extraction=phase[role_to_key[PhaseRole.EXTRACTION]],
        formalization=phase[role_to_key[PhaseRole.FORMALIZATION]],
        report=phase[role_to_key[PhaseRole.REPORT]],
    )
    return PhaseModel(phase=phase, core=core, ordered=tuple(ordered))
