"""Wire schema for the detector's serialized output (``summarization_candidates.json``).

TypedDict views of ``DetectionReport.to_dict`` — the single source of truth for consumers (one writes the
file, another renders it into a summarization prompt). Kept import-light (typing only) so a consumer can
type against the schema without pulling in the detector's analysis code.

These mirror the runtime dataclasses in ``detect.py`` (``Candidate`` / ``Boundary``); keep them in step.
"""

from typing import NotRequired, TypedDict


class HostileBoundary(TypedDict):
    """A caller (or shared inlined primitive) offered as an alternative place to summarize a candidate.
    Serialized form of ``detect.Boundary`` — all fields always present. Direction is carried by the candidate
    field that holds it (``caller_boundaries`` vs ``callee_boundaries``), not repeated per entry. ``shared``
    is meaningful only for a callee (fan-in); it is 1 for a caller."""
    function: str
    hops: int
    signature: str
    mutating: bool
    shared: int


class HostileCandidate(TypedDict):
    """One prover-hostile summarization target. Serialized form of ``detect.Candidate``:
    ``function``/``signals``/``evidence`` are always present; every other field is omitted when it is at its
    default (see ``DetectionReport.to_dict``), so consumers must treat them as optional. Candidates are
    emitted in rank order (the sequence conveys priority); the internal ``score`` is not serialized."""
    function: str
    signals: list[str]
    evidence: str
    file: NotRequired[str]
    line: NotRequired[int | None]
    signature: NotRequired[str]
    mutating: NotRequired[bool | None]
    reaching_count: NotRequired[int]
    summarizable: NotRequired[bool]
    candidate_summary: NotRequired[str]
    caller_boundaries: NotRequired[list[HostileBoundary]]   # containers to walk UP to (only one direction populated)
    callee_boundaries: NotRequired[list[HostileBoundary]]   # shared inlined primitives to descend DOWN to
