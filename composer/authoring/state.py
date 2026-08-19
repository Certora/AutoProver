"""The state an authoring session carries, and the gate that reads it.

The publish gate is a *digest* gate, not a flag: a checker or judge that accepts the draft stamps
:func:`spec_digest` of the buffer it accepted into ``validations``, and :func:`check_completion`
requires every ``required_validations`` key to carry a stamp equal to the digest of the buffer as it
stands now. Editing the spec after a green run therefore invalidates that run without anything
having to remember to clear it — the stamp simply stops matching.

The digest covers the skip declarations as well as the spec text, because "this property is skipped,
here is why" is part of what a judge accepted.
"""

import hashlib
from dataclasses import dataclass
from typing import Annotated, Protocol, Sequence
from typing_extensions import TypedDict

from pydantic import BaseModel, Field

from composer.core.state import merge_validation
from composer.spec.types import CheckName, PropertyTitle


class SkippedProperty(BaseModel):
    """A property the agent explicitly decided not to formalize."""
    property_title: PropertyTitle = Field(description="The unique snake_case title of the property from the batch listing")
    reason: str = Field(description="Justification for why this property was skipped")


def merge_skips(
    left: list[SkippedProperty],
    right: list[SkippedProperty],
) -> list[SkippedProperty]:
    """State reducer: merge by property_title (new justification replaces old).

    An entry with an empty reason is a sentinel for "unskipped" — it removes
    the property from the skip list.
    """
    by_title = {s.property_title: s for s in left}
    for s in right:
        by_title[s.property_title] = s
    return sorted(
        (s for s in by_title.values() if s.reason),
        key=lambda s: s.property_title,
    )


def merge_expected_failures(
    left: dict[CheckName, str], right: dict[CheckName, str]
) -> dict[CheckName, str]:
    """State reducer for the check-name → reason map of checks expected to fail.

    An empty reason removes the marking — the marking tool rejects an empty reason at the tool
    boundary, so an empty value can only mean the unmarking tool's delete."""
    to_ret = left.copy()
    for k, v in right.items():
        if not v:
            to_ret.pop(k, None)
            continue
        to_ret[k] = v
    return to_ret


class AuthoringExtra(TypedDict):
    """The state every authoring session has, whatever it is authoring. A backend extends this with
    its own mapping type and whatever its gate tools record."""

    curr_spec: str | None
    skipped: Annotated[list[SkippedProperty], merge_skips]
    validations: Annotated[dict[str, str], merge_validation]
    required_validations: list[str]


def spec_digest(
    curr_spec: str,
    skipped: list[SkippedProperty],
    version_history: Sequence[str] = (),
) -> str:
    """The publish surface's identity: the buffered spec, the skip declarations, and — in the
    editing-enabled source pipeline — the applied-edit history, so a stamp earned before a source
    edit goes stale with it. Stamps from gate tools are this value, so any later edit to any of the
    three invalidates them. Sessions without source editing pass no history and hash identically."""
    digester = hashlib.md5()
    digester.update(curr_spec.encode())
    for s in skipped:
        digester.update(f"{s.property_title}:{s.reason}".encode())
    for edit_id in version_history:
        digester.update(f"edit:{edit_id}".encode())
    return digester.hexdigest()


class ValidationStamper(Protocol):
    def __call__(
        self, state: AuthoringExtra, version_history: Sequence[str] = ()
    ) -> dict[str, str]: ...


def make_validation_stamper(key: str) -> ValidationStamper:
    """A ``state -> {key: digest}`` a gate tool merges into ``validations`` when it accepts the
    draft. Returned rather than inlined so the stamping tool never spells the digest itself."""
    def stamp(state: AuthoringExtra, version_history: Sequence[str] = ()) -> dict[str, str]:
        return {key: spec_digest(state["curr_spec"] or "", state["skipped"], version_history)}
    return stamp


def check_completion(
    state: AuthoringExtra,
    version_history: Sequence[str] = (),
    *,
    nothing_written: str = "no spec written yet.",
) -> str | None:
    """None if the publish gate is satisfied, else the reason it is not.

    A stamp that doesn't match the current digest is stale (the agent edited the spec — or, when a
    ``version_history`` is in play, the source — after the stamp was issued) and is reported the
    same way as a missing one — from the gate's point of view they are the same thing: nothing has
    accepted *this* draft."""
    spec = state["curr_spec"]
    if spec is None:
        return f"Completion REJECTED: {nothing_written}"
    digest = spec_digest(spec, state["skipped"], version_history)
    validations = state["validations"]
    for key in state["required_validations"]:
        if validations.get(key) != digest:
            return f"Completion REJECTED: {key} validation not satisfied or stale."
    return None


@dataclass(frozen=True)
class MappingVocab:
    """How one backend words the property→check mapping it validates at publish time. Only wording:
    the checks themselves are the same everywhere."""

    #: What one check is called to the agent — "rule", "test", "check". Per-backend because the
    #: model does better with its own domain's word than with the framework's generic one.
    check_noun: str
    #: The name of the publish tool's mapping argument, so a rejection names the field to fix.
    field_name: str
    #: Where ground truth came from, for the message that rejects a check that never ran. Only read
    #: when :func:`validate_check_mapping` is given a ``ran`` set.
    ran_source: str = ""


def validate_check_mapping(
    mapping: Sequence[tuple[PropertyTitle, Sequence[CheckName]]],
    skipped: list[SkippedProperty],
    titles: Sequence[PropertyTitle],
    vocab: MappingVocab,
    *,
    ran: Sequence[CheckName] | None = None,
) -> str | None:
    """Validate the property→checks mapping declared at completion time. None if valid, otherwise
    one message enumerating every problem.

    Always checked: every non-skipped property is mapped to at least one non-empty check name, no
    skipped property is mapped, every referenced title is one of the batch's, and no title appears
    twice.

    ``ran`` is the set of check names the gating run actually executed, when the backend's checker
    reports them (forge names every test it ran; a backend whose checker does not is passed
    ``None``). Given it, the mapping is checked against ground truth in *both* directions — no
    claimed check that didn't run, no check that ran without being tied back to a property — which
    is what stops the agent from mapping a property to a check it never wrote.
    """
    valid_titles = set(titles)
    skipped_titles = {s.property_title for s in skipped}
    ran_names = set(ran) if ran is not None else None
    noun = vocab.check_noun
    errors: list[str] = []
    mapped: set[PropertyTitle] = set()
    claimed: set[CheckName] = set()
    for title, names_declared in mapping:
        if title not in valid_titles:
            errors.append(f"Unknown property title {title!r} (not one of the batch's properties).")
            continue
        if title in mapped:
            errors.append(f"Property {title!r} appears more than once in the mapping.")
            continue
        mapped.add(title)
        if title in skipped_titles:
            errors.append(
                f"Property {title!r} is marked as skipped and must not appear "
                "in the mapping (un-skip it or remove it)."
            )
            continue
        names = [CheckName(n.strip()) for n in names_declared if n.strip()]
        if not names:
            errors.append(f"Property {title!r} must map to at least one non-empty {noun} name.")
            continue
        if ran_names is None:
            continue
        for name in names:
            claimed.add(name)
            if name not in ran_names:
                errors.append(
                    f"Property {title!r} claims {noun} {name!r}, but no {noun} by that "
                    f"name ran in {vocab.ran_source}."
                )
    for title in titles:
        if title in skipped_titles or title in mapped:
            continue
        errors.append(f"Property {title!r} is neither skipped nor mapped to any {noun}s.")
    for name in sorted((ran_names or set()) - claimed):
        errors.append(
            f"{noun.capitalize()} {name!r} ran but is not tied back to any property in the "
            f"mapping. Every {noun} in the file must demonstrate one of the batch's properties."
        )
    if errors:
        return (
            f"Completion REJECTED: the {vocab.field_name} mapping is invalid. Fix all of the "
            "following and resubmit:\n- " + "\n- ".join(errors)
        )
    return None
