import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from composer.pipeline.ecosystem import EVM, main_instance
from composer.pipeline.run_mode import RunMode
from composer.pipeline.user_properties import (
    USER_PROPERTIES_ENV, PropertySuite, UserPropertiesGenerator, UserPropertyFile, user_property_set,
)
from composer.spec.source.harness import lift_unharnessed
from composer.spec.system_model import SourceApplication


def _component(name: str, description: str = "What it does.") -> dict[str, str]:
    return {"name": name, "description": description}


def _prop(pid: str, group: str | None = None, description: str = "holds") -> dict[str, Any]:
    return {"id": pid, "description": description, **({"group": group} if group else {})}


ACCOUNTING = {
    "component": _component("Accounting", "Minting and burning shares."),
    "groups": [{"name": "Supply", "description": "The total supply."}],
    "properties": [
        {"id": "total_supply_matches_shares", "description": "totalSupply equals the sum of all shares.",
         "sort": "invariant", "group": "Supply"},
        {"id": "mint_rounding", "description": "Minting rounds against the user.",
         "source": "property_generator"},
    ],
}
EXITS = {
    "component": _component("Exits", "Withdrawing and redeeming."),
    "properties": [_prop("Withdraw-Is.Bounded")],
}
EMPTY = {"component": _component("Empty"), "properties": []}
FULL = {"imports": ["accounting.property-suite.json", "empty.property-suite.json", "exits.property-suite.json"]}


def _write(root: Path, name: str, data: dict[str, Any]) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _full(root: Path) -> Path:
    _write(root, "accounting.property-suite.json", ACCOUNTING)
    _write(root, "exits.property-suite.json", EXITS)
    _write(root, "empty.property-suite.json", EMPTY)
    return _write(root, "full.property-suite.json", FULL)


def _set(suite: Path):
    return user_property_set(
        PropertySuite.load(suite), ecosystem="evm", contract_name="Vault",
        contract_path="src/Vault.sol", run_mode=RunMode.COMPREHENSIVE,
    )


def test_each_file_that_defines_properties_is_a_component_of_the_main_contract(tmp_path: Path) -> None:
    ps = _set(_full(tmp_path))

    # A file that only imports is no component, and a component with no property is left out.
    assert [(c.name, c.unit_index, [p.title for p in c.properties]) for c in ps.property_components] == [
        ("Accounting", 0, ["total_supply_matches_shares", "mint_rounding"]),
        ("Exits", 1, ["Withdraw-Is.Bounded"]),
    ]
    # A property without a sort is a safety property.
    assert ps.property_components[1].properties[0].sort == "safety_property"


def test_the_set_rebuilds_the_units_the_driver_matches(tmp_path: Path) -> None:
    ps = _set(_full(tmp_path))
    app = ps.analyzed(SourceApplication)
    main = main_instance(lift_unharnessed(app), cast(Any, SimpleNamespace(contract_name="Vault")))
    units = EVM.units(main)

    assert [u.component.description for u in units] == ["Minting and burning shares.", "Withdrawing and redeeming."]
    paired = ps.match_units(units)
    assert [(u.display_name, len(props)) for u, props in paired] == [("Accounting", 2), ("Exits", 1)]


def test_a_component_keeps_its_file_and_its_properties_groups_and_sources(tmp_path: Path) -> None:
    suite = PropertySuite.load(_full(tmp_path))

    accounting = suite.components[0]
    assert accounting.file == (tmp_path / "accounting.property-suite.json").resolve()
    assert [(p.id, p.group, p.source) for p in accounting.properties] == [
        ("total_supply_matches_shares", "Supply", "user"), ("mint_rounding", None, "property_generator"),
    ]


@pytest.mark.parametrize(("data", "message"), [
    ({"component": _component("C"), "properties": [_prop("a"), _prop("a")]}, "more than once"),
    ({"component": _component("C"), "groups": [{"name": "g"}, {"name": "g"}]}, "more than once"),
    ({"properties": [_prop("a")]}, "names its component"),
    ({"component": _component(""), "properties": [_prop("a")]}, "at least 1 character"),
    ({"component": _component("C", ""), "properties": [_prop("a")]}, "at least 1 character"),
    ({"component": {"name": "C"}, "properties": [_prop("a")]}, "Field required"),
])
def test_a_file_must_be_well_formed(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        UserPropertyFile.model_validate(data)


def test_a_file_that_only_imports_needs_no_component() -> None:
    assert UserPropertyFile.model_validate(FULL).component is None


def test_a_suite_is_the_components_of_its_file_and_of_every_file_it_imports(tmp_path: Path) -> None:
    _write(tmp_path, "common/base.property-suite.json", {"component": _component("Base"), "properties": [_prop("supply")]})
    _write(tmp_path, "common/shared.property-suite.json", {
        "component": _component("Shared"), "imports": ["base.property-suite.json"],
        "properties": [_prop("solvency")],
    })
    _write(tmp_path, "exits.property-suite.json", {
        "component": _component("Exits"), "imports": ["common/base.property-suite.json"],
        "properties": [_prop("exit")],
    })
    # The base file is reached along two paths: it counts once. A file that defines properties and
    # imports others is the component of its own properties only.
    suite = PropertySuite.load(_write(tmp_path, "all.property-suite.json", {
        "component": _component("Own"),
        "imports": ["common/shared.property-suite.json", "exits.property-suite.json"],
        "properties": [_prop("own")],
    }))

    assert [(c.component.name, [p.id for p in c.properties]) for c in suite.components] == [
        ("Own", ["own"]), ("Shared", ["solvency"]), ("Base", ["supply"]), ("Exits", ["exit"]),
    ]
    assert [p.id for p in suite.properties] == ["own", "solvency", "supply", "exit"]


def test_a_cycle_of_imports_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.property-suite.json", {"imports": ["a.property-suite.json"]})
    a = _write(tmp_path, "a.property-suite.json", {"imports": ["b.property-suite.json"]})

    with pytest.raises(ValueError, match="cycle"):
        PropertySuite.load(a)


def test_an_id_two_files_define_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.property-suite.json", {"component": _component("B"), "properties": [_prop("same", description="other")]})
    a = _write(tmp_path, "a.property-suite.json", {
        "component": _component("A"), "imports": ["b.property-suite.json"], "properties": [_prop("same")],
    })

    with pytest.raises(ValueError, match="'same' is defined in"):
        PropertySuite.load(a)


def test_a_component_two_files_name_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.property-suite.json", {"component": _component("Same"), "properties": [_prop("b")]})
    a = _write(tmp_path, "a.property-suite.json", {
        "component": _component("Same"), "imports": ["b.property-suite.json"], "properties": [_prop("a")],
    })

    with pytest.raises(ValueError, match="component 'Same' is named in"):
        PropertySuite.load(a)


def test_generate_reads_the_file_the_environment_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _full(tmp_path / "certora")
    ctx = cast(Any, SimpleNamespace(
        run=SimpleNamespace(
            source=SimpleNamespace(project_root=str(tmp_path), contract_name="Vault",
                                   relative_path="src/Vault.sol"),
            run_mode=RunMode.COMPREHENSIVE,
        ),
        ecosystem=EVM,
    ))

    monkeypatch.delenv(USER_PROPERTIES_ENV, raising=False)
    with pytest.raises(ValueError, match=USER_PROPERTIES_ENV):
        asyncio.run(UserPropertiesGenerator().generate(ctx))

    monkeypatch.setenv(USER_PROPERTIES_ENV, "certora/full.property-suite.json")
    ps = asyncio.run(UserPropertiesGenerator().generate(ctx))
    assert ps.contract_name == "Vault" and ps.ecosystem == "evm"
    assert ps.keys() == {
        ("Accounting", "total_supply_matches_shares"), ("Accounting", "mint_rounding"),
        ("Exits", "Withdraw-Is.Bounded"),
    }
