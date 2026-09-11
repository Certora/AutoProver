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
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, Field, model_validator
from typing_extensions import TypedDict

from certora_autosetup.cache.content_cache import hash_content_parts, hash_text
from certora_autosetup.parsers.spec_imports import imports_in_cvl
from composer.spec.cvl_generation import FEEDBACK_VALIDATION_KEY
from composer.spec.gen_types import SPECS_DIR


MAX_SPEC_BUFFERS_ENV = "AUTOPROVER_MAX_SPEC_BUFFERS"
DEFAULT_MAX_SPEC_BUFFERS = 6


def max_spec_buffers() -> int:
    """The most run-target buffers the agent may create. Multi-buffer authoring is the only mode; this
    caps how far the agent partitions. A cap of 1 is effectively the single-spec case (one run-target
    buffer). Overridable via ``AUTOPROVER_MAX_SPEC_BUFFERS``; a non-integer or <1 value falls back to
    the default."""
    raw = os.environ.get(MAX_SPEC_BUFFERS_ENV)
    if raw is None:
        return DEFAULT_MAX_SPEC_BUFFERS
    try:
        n = int(raw.strip())
    except ValueError:
        return DEFAULT_MAX_SPEC_BUFFERS
    return n if n >= 1 else DEFAULT_MAX_SPEC_BUFFERS


class NamedBuffer(BaseModel):
    """One named CVL spec buffer the agent authors. A frozen pydantic model, so the same type is both
    what the buffer logic operates on and what is stored (serializably) in graph state."""

    model_config = {"frozen": True}

    #: Stable identifier, and the key this buffer is stored under: ``buffers[nm].name == nm`` holds.
    name: str
    #: The buffer's own CVL text — its rules, its ``methods{}`` block, and its ``import`` statements.
    cvl: str
    #: Project-relative path of the ``.spec`` this buffer occupies — its identity for import
    #: resolution: an ``import "<target>"`` in another buffer that resolves to this path depends on
    #: this buffer (see :func:`buffer_imports`). The agent never sets this — it is not a ``put_buffer``
    #: argument — and for now it is derived from ``name`` as ``SPECS_DIR/<name>.spec`` (see the
    #: validator). It is a field rather than a computed property only as the hook for later letting the
    #: *system* place a buffer elsewhere — notably overlaying an existing autosetup ``.spec`` so the
    #: agent can edit it — without reshaping the type; resolution already keys on it. Lifting that stays
    #: system-driven: it does not expose ``path`` to the agent.
    path: str = ""
    #: For a run-target buffer, its property -> rule mapping: each property title it verifies -> the
    #: rule/invariant names in ``cvl`` that verify it. Empty for a shared (imported-only) buffer.
    property_rules: dict[str, list[str]] = Field(default_factory=dict)
    #: False for a shared buffer that only supplies imports and runs no rules of its own.
    is_run_target: bool = True

    @model_validator(mode="before")
    @classmethod
    def _derive_path_from_name(cls, data):
        # The agent never supplies a path (it is not a put_buffer argument), so for now every buffer's
        # location is just derived from its name: SPECS_DIR/<name>.spec. To later let the SYSTEM place a
        # buffer elsewhere (e.g. overlay an existing autosetup .spec), fill this only when a path is
        # absent instead of deriving unconditionally — still not agent-controlled.
        if isinstance(data, dict) and data.get("name"):
            data = {**data, "path": (SPECS_DIR / f"{data['name']}.spec").as_posix()}
        return data

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
    creates buffers; a single run-target buffer is the simple one-spec case."""

    buffers: Annotated[dict[str, NamedBuffer], merge_buffers]


def buffer_imports(buffers: Mapping[str, NamedBuffer], name: str) -> tuple[str, ...]:
    """The names of the sibling buffers ``name`` imports: each ``import "<target>"`` in its CVL,
    resolved against where ``name`` is written (``buffers[name].path``), and looked up among the paths
    the buffers occupy. A resolved path no buffer occupies — e.g. an autosetup summary under
    ``specs/summaries/`` — is a resource, not a sibling, and is skipped. No string surgery: just path
    resolution + a map lookup, mirroring how the prover resolves a spec's imports on disk."""
    by_path = {posixpath.normpath(b.path): nm for nm, b in buffers.items()}
    here = PurePosixPath(buffers[name].path).parent
    out: list[str] = []
    for target in imports_in_cvl(buffers[name].cvl):
        if (nm := by_path.get(posixpath.normpath(str(here / target)))) is not None:
            out.append(nm)
    return tuple(out)


def import_closure(buffers: Mapping[str, NamedBuffer], name: str) -> list[NamedBuffer]:
    """Buffer ``name`` plus every buffer reachable through its imports, transitively — deduped and
    returned sorted by name. A shared (``is_run_target=false``) buffer is in a run-target's closure only
    if that run-target transitively imports it, so a shared buffer belongs to the closure (and
    invalidation set) of exactly the run-targets that use it — that is what lets a subset of groups share
    a summary without invalidating the others. An import resolving to no known buffer is skipped (a
    dangling/resource import is a coverage concern, not a hashing one), and cycles terminate safely."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        n = stack.pop()
        if n in seen or n not in buffers:
            continue
        seen.add(n)
        stack.extend(buffer_imports(buffers, n))
    return [buffers[n] for n in sorted(seen)]


def buffer_digest(
    buffers: Mapping[str, NamedBuffer], name: str, *, extra_parts: Sequence[str] = ()
) -> str:
    """A content digest of buffer ``name`` and its transitive import closure, plus any ``extra_parts``
    (e.g. skipped-property or conf-flag markers). Editing the buffer OR any buffer it imports changes
    the digest, so it keys the buffer's cached verify/review. Mirrors
    :meth:`ContentCache.compute_cache_key` (content-keyed, order-independent)."""
    # TODO: a pure-comment edit (e.g. reframing a property's justification docstring) changes this
    # digest and forces the prover to re-verify identical logic — a wasted job. A comment-stripped
    # normalization would avoid that, but note this digest also keys the *feedback* review, and the
    # judge legitimately reads justification comments — so stripping comments here would wrongly skip
    # re-review after a comment-only edit. So find a solution to avoid running
    # prover just because of comment-only change.
    parts = [f"{b.name}:{hash_text(b.cvl)}" for b in import_closure(buffers, name)]
    parts += [f"extra:{p}" for p in extra_parts]
    return hash_content_parts(parts)


def run_targets(buffers: Mapping[str, NamedBuffer]) -> list[NamedBuffer]:
    """The run-target buffers (those that verify rules), sorted by name."""
    return [buffers[n] for n in sorted(buffers) if buffers[n].is_run_target]


def buffer_state_digest(
    buffers: Mapping[str, NamedBuffer],
    name: str,
    *,
    skipped: Sequence[tuple[str, str]],
    version_history: Sequence[str],
    include_claim: bool = False,
) -> str:
    """The per-buffer analogue of ``spec_digest``: a buffer's content + import closure bound to the
    current authoring state (skip declarations as ``(title, reason)`` pairs, and the applied-edit
    history). Every per-buffer stamp — feedback and prover — and the completion check key off this, so
    editing the buffer, anything it imports, a skip, or the source invalidates that buffer's stamps.

    With ``include_claim`` the buffer's declared ``property_rules`` also key the digest, so re-assigning
    a claim re-triggers review. The feedback stamp sets it (the judge reviews a buffer against the
    properties it claims); the prover stamp leaves it False (a claim change does not affect what was
    verified)."""
    extra = [
        *(f"skip:{t}:{r}" for (t, r) in sorted(skipped)),
        *(f"edit:{e}" for e in version_history),
    ]
    if include_claim:
        claim = ";".join(
            f"{t}={','.join(rs)}" for t, rs in sorted(buffers[name].property_rules.items())
        )
        extra.append(f"claim:{claim}")
    return buffer_digest(buffers, name, extra_parts=extra)


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
    stale when that buffer (or anything it imports, or the skips/edit history) changes.

    With no run-target buffers this is vacuously satisfied (there is nothing to stamp) — the
    all-properties-skipped case, whose validity is decided by ``validate_coverage`` instead."""
    for b in run_targets(buffers):
        for key in required_validations:
            # The feedback stamp tracks a buffer's claimed properties (the judge reviews against them);
            # the prover stamp does not.
            d = buffer_state_digest(
                buffers, b.name, skipped=skipped, version_history=version_history,
                include_claim=(key == FEEDBACK_VALIDATION_KEY),
            )
            if validations.get(f"{key}:{b.name}") != d:
                return f"Completion REJECTED: buffer {b.name!r} {key} validation not satisfied or stale."
    return None


def _render_buffers(ordered: Sequence[NamedBuffer], label: Callable[[NamedBuffer], str]) -> str:
    """The given buffers concatenated into one document, each under a ``// ===== buffer <name>
    (<label>) =====`` header. For a judge or report that consumes the spec as a single text — NOT for
    verification (each buffer is compiled and run separately)."""
    return "\n\n".join(
        f"// ===== buffer {b.name} ({label(b)}) =====\n{b.cvl.rstrip()}" for b in ordered
    )


def buffer_review_text(buffers: Mapping[str, NamedBuffer], name: str) -> str:
    """One buffer's reviewable text for the judge: the buffer plus its transitive import closure, so
    the judge sees it in the context it is actually verified in, without the unrelated buffers."""
    return _render_buffers(
        import_closure(buffers, name),
        lambda b: "under review" if b.name == name else "imported",
    )


def combined_buffers_view(buffers: Mapping[str, NamedBuffer]) -> str:
    """All buffers (shared and run-target) concatenated into one reviewable document."""
    return _render_buffers(
        [buffers[n] for n in sorted(buffers)],
        lambda b: "run-target" if b.is_run_target else "shared",
    )


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


def validate_disjoint_rules(buffers: Mapping[str, NamedBuffer]) -> str | None:
    """Whether every rule is owned by exactly one run-target buffer — a rule name appearing in two
    buffers is an authoring mistake (ambiguous ownership). Returns None when disjoint, else a message
    naming the shared rules."""
    seen: set[str] = set()
    dup: set[str] = set()
    for b in run_targets(buffers):
        for r in b.owned_rules:
            (dup if r in seen else seen).add(r)
    return f"rules owned by more than one buffer: {sorted(dup)}" if dup else None


_METHODS_BLOCK = re.compile(r"methods\s*\{([^}]*)\}", re.DOTALL)
_GHOST_DECL = re.compile(r"\bghost\b[^;{]*;", re.DOTALL)


def _normalize_decl(s: str) -> str:
    """Strip comments and collapse whitespace so two textually-equivalent declarations compare equal."""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//[^\n]*", "", s)
    return " ".join(s.split())


def _extract_decls(cvl: str) -> set[str]:
    """The `methods{}` entries and simple `ghost` declarations in ``cvl``, normalized. A light-regex
    heuristic: ``methods`` entries are split on ``;`` within each block; ghost decls are single-statement
    (axiom-block ghosts and multi-line CVL function *bodies* are not extracted). Good enough to flag
    verbatim copy-paste across buffers, not a CVL parser."""
    decls: set[str] = set()
    for block in _METHODS_BLOCK.findall(cvl):
        for entry in block.split(";"):
            n = _normalize_decl(entry)
            if n.startswith("function "):
                decls.add(n + ";")
    for g in _GHOST_DECL.findall(cvl):
        decls.add(_normalize_decl(g))
    return decls


def duplicated_declarations(buffers: Mapping[str, NamedBuffer]) -> dict[str, list[str]]:
    """Declarations (``methods{}`` entries / simple ghost decls) that appear verbatim in more than one
    run-target buffer — copy-paste that likely belongs in a shared buffer the duplicating buffers import.
    Returns ``{declaration: sorted buffer names}`` for each duplicated declaration (heuristic, text-based;
    see :func:`_extract_decls`). Because it compares only the declaration text, it can false-positive:
    two identical summary entries like ``function _.transfer() => cvlTransfer();`` are flagged even when
    the CVL function ``cvlTransfer()`` they point at is defined differently in each buffer."""
    where: dict[str, list[str]] = {}
    for b in run_targets(buffers):
        for decl in _extract_decls(b.cvl):
            where.setdefault(decl, []).append(b.name)
    return {d: sorted(names) for d, names in where.items() if len(names) > 1}
