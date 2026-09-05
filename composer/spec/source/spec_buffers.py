"""Multi-buffer CVL specs: the agent authors several independent spec buffers that share
infrastructure through CVL ``import``.

Policy-neutral substrate for generalizing the single ``curr_spec`` buffer
(:mod:`composer.authoring.buffer`) into a named *set* of buffers. A *run-target* buffer is a
self-contained spec — its own rules, its ``methods{}`` block, and ``import`` statements pulling in
shared buffers — verified and reviewed on its own. A *shared* buffer holds common ghosts, invariants,
and models that run-target buffers import; it runs no rules itself. Every non-skipped property is
owned by exactly one run-target buffer, and every rule lives in exactly one buffer.

Each buffer has a content *digest* over its own text plus its transitive import closure (reusing the
autosetup content-cache hashing). Editing a shared buffer therefore changes the digest of every buffer
that imports it, which is what lets the pipeline skip re-verifying / re-reviewing an unchanged buffer
while correctly invalidating its importers.

(``NamedBuffer`` is the value object — one buffer's text plus metadata — distinct from
:class:`composer.authoring.buffer.SpecBuffer`, which is the single-buffer *state* shape.)
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from certora_autosetup.cache.content_cache import hash_content_parts, hash_text


SPEC_BUFFERS_ENV = "AUTOPROVER_SPEC_BUFFERS"


def spec_buffers_enabled() -> bool:
    """Whether the multi-buffer authoring tools are offered to the agent (opt-in). Off by default so
    the single ``curr_spec`` flow is untouched until multi-buffer is fully wired (publish, report)."""
    return os.environ.get(SPEC_BUFFERS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


class NamedBuffer(BaseModel):
    """One named CVL spec buffer the agent authors. Frozen and pydantic so it is both the substrate's
    algorithm type and the shape stored (serializably) in graph state."""

    model_config = {"frozen": True}

    #: Stable identifier; also the on-disk stem when the buffer is materialized to a ``.spec`` file.
    name: str
    #: The buffer's own CVL text — its rules, its ``methods{}`` block, and its ``import`` statements.
    cvl: str
    #: For a run-target buffer, its property -> rule mapping: each property title it verifies -> the
    #: rule/invariant names in ``cvl`` that verify it. Empty for a shared (imported-only) buffer.
    property_rules: dict[str, list[str]] = Field(default_factory=dict)
    #: Names of the buffers this one imports (its shared dependencies), used to build the digest
    #: closure. Should track the actual ``import`` statements in ``cvl``.
    imports: tuple[str, ...] = ()
    #: False for a shared buffer that only supplies imports and runs no rules of its own.
    is_run_target: bool = True

    @property
    def properties(self) -> frozenset[str]:
        """The property titles this buffer covers (the keys of ``property_rules``)."""
        return frozenset(self.property_rules)

    @property
    def owned_rules(self) -> frozenset[str]:
        """The rules this buffer verifies (the union of its ``property_rules`` values)."""
        return frozenset(r for rs in self.property_rules.values() for r in rs)


def merge_buffers(
    left: Mapping[str, NamedBuffer], right: Mapping[str, "NamedBuffer | None"]
) -> dict[str, NamedBuffer]:
    """State reducer for the buffers map: right-wins per name; a ``None`` value removes that buffer
    (so a tool can merge/drop buffers). Granularity is the whole buffer — a tool that edits one
    buffer's text passes the updated :class:`NamedBuffer` under its name."""
    out = dict(left)
    for name, val in right.items():
        if val is None:
            out.pop(name, None)
        else:
            out[name] = val
    return out


class SpecBuffersExtra(TypedDict):
    """Graph-state slice holding the agent's spec buffers, keyed by name. Empty until the agent
    creates buffers; a single run-target buffer is the behavior-preserving single-spec case."""

    buffers: Annotated[dict[str, NamedBuffer], merge_buffers]


def import_closure(buffers: Mapping[str, NamedBuffer], name: str) -> list[NamedBuffer]:
    """Buffer ``name`` plus every buffer reachable through its ``imports``, transitively — deduped and
    returned sorted by name. An import naming no known buffer is skipped here (a dangling import is a
    coverage/validation concern, not a hashing one), and import cycles terminate safely."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        n = stack.pop()
        if n in seen or n not in buffers:
            continue
        seen.add(n)
        stack.extend(buffers[n].imports)
    return [buffers[n] for n in sorted(seen)]


def buffer_digest(
    buffers: Mapping[str, NamedBuffer], name: str, *, extra_parts: Sequence[str] = ()
) -> str:
    """A content digest of buffer ``name`` and its transitive import closure, plus any ``extra_parts``
    (e.g. skipped-property or conf-flag markers). Editing the buffer OR any buffer it imports changes
    the digest, so it keys the buffer's cached verify/review. Mirrors
    :meth:`ContentCache.compute_cache_key` (content-keyed, order-independent)."""
    parts = [f"{b.name}:{hash_text(b.cvl)}" for b in import_closure(buffers, name)]
    parts += [f"extra:{p}" for p in extra_parts]
    return hash_content_parts(parts)


def buffer_files(buffers: Mapping[str, NamedBuffer], name: str) -> dict[str, str]:
    """The ``.spec`` files to materialize in order to run buffer ``name``: the buffer itself plus its
    transitive import closure, each as ``"{buffer_name}.spec" -> its CVL``. A buffer's own
    ``import "X.spec"`` lines resolve against these siblings written into the same directory."""
    return {f"{b.name}.spec": b.cvl for b in import_closure(buffers, name)}


def run_targets(buffers: Mapping[str, NamedBuffer]) -> list[NamedBuffer]:
    """The run-target buffers (those that verify rules), sorted by name."""
    return [buffers[n] for n in sorted(buffers) if buffers[n].is_run_target]


def buffer_state_digest(
    buffers: Mapping[str, NamedBuffer],
    name: str,
    *,
    skipped: Sequence[tuple[str, str]],
    version_history: Sequence[str],
) -> str:
    """The per-buffer analogue of ``spec_digest``: a buffer's content + import closure bound to the
    current authoring state (skip declarations as ``(title, reason)`` pairs, and the applied-edit
    history). Every per-buffer stamp — feedback and prover — and the completion check key off this, so
    editing the buffer, anything it imports, a skip, or the source invalidates that buffer's stamps."""
    return buffer_digest(
        buffers, name,
        extra_parts=[
            *(f"skip:{t}:{r}" for (t, r) in sorted(skipped)),
            *(f"edit:{e}" for e in version_history),
        ],
    )


def check_buffer_completion(
    buffers: Mapping[str, NamedBuffer],
    validations: Mapping[str, str],
    required_validations: Sequence[str],
    *,
    skipped: Sequence[tuple[str, str]],
    version_history: Sequence[str],
) -> str | None:
    """None if every run-target buffer carries each required validation (e.g. ``feedback``, ``prover``)
    stamped at its current digest, else the first buffer/validation missing or stale. The buffers
    analogue of ``check_completion``: a per-buffer stamp is keyed ``"<validation>:<buffer>"`` and goes
    stale when that buffer (or anything it imports, or the skips/edit history) changes."""
    targets = run_targets(buffers)
    if not targets:
        return "Completion REJECTED: no run-target buffers."
    for b in targets:
        d = buffer_state_digest(buffers, b.name, skipped=skipped, version_history=version_history)
        for key in required_validations:
            if validations.get(f"{key}:{b.name}") != d:
                return f"Completion REJECTED: buffer {b.name!r} {key} validation not satisfied or stale."
    return None


def buffer_review_text(buffers: Mapping[str, NamedBuffer], name: str) -> str:
    """One buffer's reviewable text for the judge: the buffer plus its transitive import closure, each
    under a header marking the one under review vs its imports — so the judge sees the buffer in the
    context it is actually verified in, without the unrelated buffers."""
    parts: list[str] = []
    for b in import_closure(buffers, name):
        role = "under review" if b.name == name else "imported"
        parts.append(f"// ===== buffer {b.name} ({role}) =====\n{b.cvl.rstrip()}")
    return "\n\n".join(parts)


def combined_buffers_view(buffers: Mapping[str, NamedBuffer]) -> str:
    """All buffers (shared and run-target) concatenated into one reviewable document, each under a
    header. For a judge or report that consumes the spec as a single text — NOT for verification
    (each buffer is compiled and run separately)."""
    parts: list[str] = []
    for name in sorted(buffers):
        b = buffers[name]
        kind = "run-target" if b.is_run_target else "shared"
        parts.append(f"// ===== buffer {name} ({kind}) =====\n{b.cvl.rstrip()}")
    return "\n\n".join(parts)


def validate_coverage(
    buffers: Mapping[str, NamedBuffer], *, all_properties: set[str], skipped: set[str]
) -> str | None:
    """Whether the run-target buffers cover the property space exactly once — the publish-time contract:
    every non-skipped property assigned to exactly one buffer, and no unknown or skipped property
    assigned. Returns None when valid, else one message enumerating every problem."""
    assigned: list[str] = [p for b in run_targets(buffers) for p in b.properties]
    seen: set[str] = set()
    duplicated: set[str] = set()
    for p in assigned:
        (duplicated if p in seen else seen).add(p)
    required = all_properties - skipped
    problems: list[str] = []
    if duplicated:
        problems.append(f"properties assigned to more than one buffer: {sorted(duplicated)}")
    if missing := required - seen:
        problems.append(f"non-skipped properties assigned to no buffer: {sorted(missing)}")
    if unknown := seen - all_properties:
        problems.append(f"unknown property titles: {sorted(unknown)}")
    if skipped_assigned := seen & skipped:
        problems.append(f"skipped properties should not be assigned to a buffer: {sorted(skipped_assigned)}")
    return "; ".join(problems) if problems else None


@dataclass(frozen=True)
class BufferRun:
    """One run-target buffer's execution decision for a verify pass."""

    buffer: NamedBuffer
    #: The buffer's current content digest (its text + import closure); keys its completion history.
    digest: str
    #: False when the buffer is already complete at this digest, so its prover run is skipped.
    needs_run: bool


def plan_buffer_runs(
    buffers: Mapping[str, NamedBuffer],
    *,
    digest_of: Callable[[NamedBuffer], str],
    is_complete: Callable[[NamedBuffer, str], bool],
) -> list[BufferRun]:
    """Decide, per run-target buffer, whether this verify pass must (re-)run it. A buffer already
    complete at its current digest is skipped; editing it or any buffer it imports changes the digest
    (:func:`buffer_digest`) and forces a re-run. ``digest_of`` and ``is_complete`` are injected so this
    stays pure — the prover layer supplies the history-backed checks."""
    plan: list[BufferRun] = []
    for b in run_targets(buffers):
        d = digest_of(b)
        plan.append(BufferRun(buffer=b, digest=d, needs_run=not is_complete(b, d)))
    return plan


def validate_disjoint_rules(buffers: Mapping[str, NamedBuffer]) -> str | None:
    """Whether every rule is owned by exactly one run-target buffer. Unlike overlay groups, buffers
    hold their rules physically, so a rule name appearing in two buffers is an authoring mistake
    (ambiguous ownership). Returns None when disjoint, else a message naming the shared rules."""
    seen: set[str] = set()
    dup: set[str] = set()
    for b in run_targets(buffers):
        for r in b.owned_rules:
            (dup if r in seen else seen).add(r)
    return f"rules owned by more than one buffer: {sorted(dup)}" if dup else None
