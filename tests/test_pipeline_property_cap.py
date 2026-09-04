"""``--max-properties``: bounding what a run *takes on*, as distinct from what it spends.

A budget stops a run once the money is gone, curtailing whatever happened to be in flight. That is
the wrong instrument for the first run against an unfamiliar program, where the question is "does
this work at all" and the answer wants to arrive before the spend rather than through it. The cap is
the other half: it is applied to the extracted batches before the staged formalizer begins, so a
component with nothing left to author never gets a harness module or a cargo feature declared for
it.

Pure list surgery, tested as such — no pipeline, no services.
"""

from dataclasses import dataclass

from composer.pipeline.core import _capped
from composer.spec.types import PropertyFormulation


@dataclass
class _FakeBatch:
    """Structurally what ``_capped`` reads: a unit and its properties. ``dataclasses.replace`` is
    what it uses to narrow one, so a dataclass is the whole requirement."""
    feat: str
    props: list[PropertyFormulation]


def _prop(title: str) -> PropertyFormulation:
    return PropertyFormulation(
        sort="safety_property", description=f"the {title} holds", title=title
    )


def _batches(*sizes: int) -> list[_FakeBatch]:
    return [
        _FakeBatch(feat=f"unit{i}", props=[_prop(f"p{i}_{j}") for j in range(n)])
        for i, n in enumerate(sizes)
    ]


def _shape(batches) -> list[tuple[str, int]]:
    return [(b.feat, len(b.props)) for b in batches]


def test_no_cap_is_not_a_copy_of_the_run():
    # `None` has to be the identity, not "cap at some default": every run that does not ask for a
    # cap must reach formalization with exactly what the extractor produced.
    batches = _batches(3, 4)
    assert _capped(batches, None) is batches


def test_the_cap_counts_properties_across_components_not_within_them():
    # 5 properties over components of 3 and 4 is "all of the first, two of the second" — the run is
    # bounded by total authoring work, which is what the money is spent on.
    assert _shape(_capped(_batches(3, 4), 5)) == [("unit0", 3), ("unit1", 2)]


def test_a_component_left_with_nothing_is_dropped_rather_than_kept_empty():
    """An empty batch is not harmless: the staged formalizer declares a module and a cargo feature
    per component it is given, so an empty one scaffolds a unit that authors nothing and then
    reports as a component with no deliverable."""
    assert _shape(_capped(_batches(2, 3, 4), 2)) == [("unit0", 2)]


def test_a_cap_above_what_was_extracted_changes_nothing():
    assert _shape(_capped(_batches(2, 1), 99)) == [("unit0", 2), ("unit1", 1)]


def test_a_cap_of_zero_authors_nothing():
    # Legal, and it means what it says. The driver raises on an empty batch list, which is the right
    # outcome: a run that was told to author nothing has no result to report.
    assert _capped(_batches(2, 3), 0) == []


def test_the_original_batches_are_not_mutated():
    # The cap narrows copies. The uncapped batch is what the report's "extracted" count and the
    # cache entry are derived from, and a run that quietly shortened them in place would make the
    # cap invisible after the fact.
    batches = _batches(3, 4)
    _capped(batches, 2)
    assert _shape(batches) == [("unit0", 3), ("unit1", 4)]
