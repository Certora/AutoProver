"""Wire schema for the detector's serialized output (``summarization_candidates.json``).

TypedDict views of ``DetectionReport.to_dict`` — the single source of truth for consumers (autosetup
writes the file, composer renders it into the CVL-generation prompt). Kept import-light (typing only) so a
consumer can type against the schema without pulling in the detector's analysis code.

These mirror the runtime dataclasses in ``detect.py`` (``Candidate`` / ``Boundary``); keep them in step.
"""

from typing import NotRequired, TypedDict


class HostileBoundary(TypedDict):
    """A caller (or shared inlined primitive) offered as an alternative place to summarize a candidate.
    Serialized form of ``detect.Boundary`` — all fields are always present."""
    function: str
    hops: int
    signature: str
    mutating: bool
    direction: str
    shared: int


class HostileCandidate(TypedDict):
    """One prover-hostile summarization target. Serialized form of ``detect.Candidate``:
    ``function``/``signals``/``score``/``evidence`` are always present; every other field is omitted when
    it is at its default (see ``DetectionReport.to_dict``), so consumers must treat them as optional."""
    function: str
    signals: list[str]
    score: float
    evidence: str
    file: NotRequired[str]
    line: NotRequired[int | None]
    signature: NotRequired[str]
    mutating: NotRequired[bool | None]
    reaching_count: NotRequired[int]
    summarizable: NotRequired[bool]
    candidate_summary: NotRequired[str]
    boundaries: NotRequired[list[HostileBoundary]]
