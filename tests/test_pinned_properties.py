"""Properties supplied instead of extracted.

The point of the mechanism is a cheap end-to-end run over a large program, so the tests are about
the two things that would make it untrustworthy: a pin file that silently means something other
than it says, and a pin file that has quietly drifted away from what analysis now produces.
"""

import json
import pathlib
from dataclasses import dataclass

import pytest

from composer.pipeline.pinned import PinnedProperties, load_pinned_properties
from composer.spec.types import PropertyFormulation

PROPS = [
    {"sort": "safety_property", "title": "manager_must_sign",
     "description": "Initialize fails unless the manager signed."},
    {"sort": "invariant", "title": "supply_matches_reserve",
     "description": "Pool token supply equals the reserve excess."},
]


@dataclass(frozen=True)
class _Unit:
    """Enough of ``FeatureUnit`` for the matcher; the pipeline's real units satisfy it."""

    slug: str

    @property
    def display_name(self) -> str:
        return self.slug.replace("_", " ").title()

    @property
    def unit_index(self) -> int:
        return 0

    def cache_material(self) -> str:
        return self.slug

    def context_tag(self) -> dict[str, object]:
        return {"component": self.slug}

    def feature_json(self) -> dict[str, object]:
        return {}


def _write(tmp_path: pathlib.Path, payload: object, name: str = "pins.json") -> pathlib.Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return p


def test_an_object_keyed_by_slug_loads(tmp_path: pathlib.Path) -> None:
    pinned = load_pinned_properties(_write(tmp_path, {"pool_initialization": PROPS}))

    assert pinned.slugs == {"pool_initialization"}
    assert pinned.total() == 2
    got = pinned.get(_Unit("pool_initialization"))
    assert got is not None and [p.title for p in got] == ["manager_must_sign", "supply_matches_reserve"]
    assert all(isinstance(p, PropertyFormulation) for p in got)


def test_a_bare_list_takes_its_slug_from_the_artifact_filename(tmp_path: pathlib.Path) -> None:
    """The round trip that makes pinning a copy rather than an edit: the artifact store writes
    ``cvlr_<slug>.properties.json`` holding a bare list, and that file is usable as-is."""
    path = _write(tmp_path, PROPS, name="cvlr_pool_initialization.properties.json")

    pinned = load_pinned_properties(path)

    assert pinned.slugs == {"pool_initialization"}


def test_a_bare_list_without_a_usable_filename_is_refused(tmp_path: pathlib.Path) -> None:
    """Guessing a slug here would attach the properties to the wrong component and formalize them
    against source they were never written about."""
    with pytest.raises(ValueError, match="needs a filename of the form"):
        load_pinned_properties(_write(tmp_path, PROPS, name="properties.json"))


def test_a_pin_naming_no_unit_is_an_error(tmp_path: pathlib.Path) -> None:
    """A fixture exists to catch analysis drifting out from under it, so this is the load-bearing
    case: silently formalizing nothing would look like a passing cheap run."""
    pinned = load_pinned_properties(_write(tmp_path, {"stake_rebalancing": PROPS}))

    with pytest.raises(ValueError) as exc:
        pinned.check_every_pin_matched([_Unit("pool_initialization"), _Unit("deposits")])

    msg = str(exc.value)
    assert "stake_rebalancing" in msg, "the failure must name the pin that missed"
    assert "pool_initialization" in msg and "deposits" in msg, (
        "and the units analysis produced, since re-pinning against them is the next action"
    )


def test_matching_pins_pass_the_check(tmp_path: pathlib.Path) -> None:
    pinned = load_pinned_properties(_write(tmp_path, {"deposits": PROPS}))

    pinned.check_every_pin_matched([_Unit("deposits"), _Unit("withdrawals")])


def test_unmentioned_units_are_dropped_not_extracted(tmp_path: pathlib.Path) -> None:
    """Pinning one component of ten is what makes the run cheap; a unit the file omits must
    produce no batch rather than falling back to the extraction agent."""
    pinned = load_pinned_properties(_write(tmp_path, {"deposits": PROPS}))

    assert pinned.get(_Unit("deposits")) is not None
    assert pinned.get(_Unit("withdrawals")) is None


def test_an_empty_property_list_is_refused(tmp_path: pathlib.Path) -> None:
    """An empty list and an absent key mean the same thing to the run, so only one of them is
    allowed to express it."""
    with pytest.raises(ValueError, match="drop the key instead"):
        load_pinned_properties(_write(tmp_path, {"deposits": []}))


def test_a_malformed_property_is_refused_at_load(tmp_path: pathlib.Path) -> None:
    """Validation happens while reading the file, not when a unit reaches formalization an hour in."""
    with pytest.raises(Exception):
        load_pinned_properties(_write(tmp_path, {"deposits": [{"title": "no_sort_or_description"}]}))


def test_a_non_list_value_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="must map to a list"):
        load_pinned_properties(_write(tmp_path, {"deposits": {"sort": "invariant"}}))


def test_an_empty_file_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="no components pinned"):
        load_pinned_properties(_write(tmp_path, {}))


def test_the_source_path_is_carried_for_the_error_message(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, {"deposits": PROPS})

    pinned = load_pinned_properties(path)

    assert pinned.source == path
    with pytest.raises(ValueError, match=str(path)):
        pinned.check_every_pin_matched([_Unit("other")])


def test_a_real_artifact_file_round_trips(tmp_path: pathlib.Path) -> None:
    """The shape the CVLR artifact store actually writes, verbatim from run 6 of SPL stake-pool."""
    path = _write(tmp_path, [
        {"sort": "safety_property",
         "title": "initialize_requires_manager_signature",
         "description": "The `Initialize` handler must fail unless the `manager` account "
                        "(account index 1) has `is_signer == true`."},
    ], name="cvlr_pool_initialization.properties.json")

    pinned = load_pinned_properties(path)

    props = pinned.get(_Unit("pool_initialization"))
    assert props is not None and len(props) == 1
    assert props[0].title == "initialize_requires_manager_signature"
    assert props[0].sort == "safety_property"


def test_construction_is_direct_for_callers_that_already_have_properties() -> None:
    """``PinnedProperties`` is usable without a file, so a test fixture need not write one."""
    pinned = PinnedProperties(
        {"deposits": [PropertyFormulation.model_validate(PROPS[0])]}, pathlib.Path("<memory>")
    )

    assert pinned.total() == 1
