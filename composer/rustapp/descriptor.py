"""Python mirror of the Rust ``AppDescriptor`` (see ``autoprover-sdk``).

These pydantic models are the Python side of the descriptor ABI. They are parsed
from the JSON a Rust wheel returns from ``descriptor()`` and consumed by the host
to synthesize the phase enum, argparse, frontend and artifact store. Keep the
field names in lockstep with ``rust/autoprover-sdk/src/lib.rs``.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, Field

#: Ecosystem/chain tag. Mirrors ``composer.pipeline.ecosystem.ChainTag`` (kept local so this
#: ABI-mirror module stays decoupled from the pipeline); the host resolves it against the
#: ecosystem registry.
ChainTag = Literal["evm", "solana", "soroban"]


class CoreSlot(str, enum.Enum):
    """Which driver-tagged core phase a declared phase fills."""

    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    FORMALIZATION = "formalization"
    REPORT = "report"


class PhaseSpec(BaseModel):
    """One task-grouping phase; ``key`` becomes the synthesized enum member name."""

    key: str
    label: str
    order: int = 0
    core_slot: CoreSlot | None = None


class ArgDefault(BaseModel):
    """Tagged default value for a declared CLI argument."""

    kind: Literal["str", "int", "bool"]
    value: str | int | bool | None = None


class ArgSpec(BaseModel):
    """A CLI flag the generic entry point adds beyond the positional inputs."""

    flag: str
    help: str
    default: ArgDefault
    required: bool = False


class EventKind(BaseModel):
    """A domain event kind the frontend should render (see ``Command::Emit``).

    ``notice`` events are surfaced as a persistent, always-visible callout (plus a toast)
    rather than a line in the collapsible per-task events log — for one-shot important
    results such as a per-invariant verdict. Defaults to ``False`` so wheels built before
    the field existed still load."""

    kind: str
    label: str
    notice: bool = False


class ArtifactLayout(BaseModel):
    """Project-root-relative deliverable layout."""

    deliverable_dir: str
    internal_dir: str
    report_dir: str
    artifact_dir: str
    artifact_prefix: str
    artifact_extension: str
    property_suffix: str


class AppDescriptor(BaseModel):
    """The complete declaration a Rust wheel exports."""

    name: str
    header_text: str
    #: The ecosystem (chain) whose system model / prompts the shared front half uses. The
    #: host resolves it against ``composer.pipeline.ecosystem.ECOSYSTEMS``. Defaults to
    #: ``"evm"`` so wheels built before this field existed keep working.
    ecosystem: ChainTag = "evm"
    backend_tag: str
    backend_guidance: str
    analysis_key: str
    phases: list[PhaseSpec]
    args: list[ArgSpec] = Field(default_factory=list)
    rag_db_default: str | None = None
    event_kinds: list[EventKind] = Field(default_factory=list)
    artifact_layout: ArtifactLayout

    def ordered_phases(self) -> list[PhaseSpec]:
        return sorted(self.phases, key=lambda p: (p.order, p.key))

    def core_slot_map(self) -> dict[CoreSlot, str]:
        """The declared phase ``key`` for each core slot it fills."""
        return {p.core_slot: p.key for p in self.phases if p.core_slot is not None}
