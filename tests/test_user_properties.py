import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from composer.pipeline.ecosystem import EVM, main_instance
from composer.pipeline.run_mode import RunMode
from composer.pipeline.user_properties import (
    DEFAULT_GROUP, USER_PROPERTIES_ENV, UserPropertiesGenerator, UserPropertyFile, user_property_set,
)
from composer.spec.source.harness import lift_unharnessed
from composer.spec.system_model import SourceApplication

FILE = {
    "groups": [
        {"name": "Accounting", "description": "Minting and burning shares."},
        {"name": "Unused"},
    ],
    "properties": [
        {"id": "total_supply_matches_shares", "description": "totalSupply equals the sum of all shares.",
         "sort": "invariant", "group": "Accounting"},
        {"id": "no_round_trip_profit", "description": "A deposit then a redeem never profits."},
        {"id": "Withdraw-Is.Bounded", "description": "Withdrawals never exceed the share.",
         "group": "Exits"},
        {"id": "mint_rounding", "description": "Minting rounds against the user.",
         "group": "Accounting"},
    ],
}


def _set(data: dict[str, Any] = FILE):
    return user_property_set(
        UserPropertyFile.model_validate(data), ecosystem="evm", contract_name="Vault",
        contract_path="src/Vault.sol", run_mode=RunMode.COMPREHENSIVE,
    )


def test_each_group_is_a_component_of_the_main_contract_with_its_properties() -> None:
    ps = _set()

    assert [(c.name, c.unit_index, [p.title for p in c.properties]) for c in ps.property_components] == [
        ("Accounting", 0, ["total_supply_matches_shares", "mint_rounding"]),
        # A group a property names without declaring it, then the default group.
        ("Exits", 1, ["Withdraw-Is.Bounded"]),
        (DEFAULT_GROUP, 2, ["no_round_trip_profit"]),
    ]
    # A declared group with no property is left out; a property without a sort is a safety
    # property.
    assert ps.property_components[2].properties[0].sort == "safety_property"


def test_the_set_rebuilds_the_units_the_driver_matches() -> None:
    ps = _set()
    app = ps.analyzed(SourceApplication)
    main = main_instance(lift_unharnessed(app), cast(Any, SimpleNamespace(contract_name="Vault")))
    units = EVM.units(main)

    assert [u.component.description for u in units] == ["Minting and burning shares.", "", ""]
    paired = ps.match_units(units)
    assert [(u.display_name, len(props)) for u, props in paired] == [
        ("Accounting", 2), ("Exits", 1), (DEFAULT_GROUP, 1),
    ]


@pytest.mark.parametrize("data", [
    {"properties": [{"id": "a", "description": "x"}, {"id": "a", "description": "y"}]},
    {"groups": [{"name": "g"}, {"name": "g"}], "properties": []},
])
def test_ids_and_group_names_must_be_unique(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="more than once"):
        UserPropertyFile.model_validate(data)


def _write(root: Path, name: str, data: dict[str, Any]) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _prop(pid: str, group: str | None = None, description: str = "holds") -> dict[str, Any]:
    return {"id": pid, "description": description, **({"group": group} if group else {})}


def test_a_suite_is_its_properties_and_those_of_every_file_it_imports(tmp_path: Path) -> None:
    _write(tmp_path, "common/base.properties.json", {
        "groups": [{"name": "Accounting", "description": "Mint and burn."}],
        "properties": [_prop("supply", "Accounting")],
    })
    _write(tmp_path, "common/shared.properties.json", {
        "imports": ["base.properties.json"], "properties": [_prop("solvency", "Accounting")],
    })
    _write(tmp_path, "exits.properties.json", {
        "imports": ["common/base.properties.json"], "properties": [_prop("exit")],
    })
    # The base file is reached along two paths: it counts once.
    suite = UserPropertyFile.load_suite(_write(tmp_path, "all.properties.json", {
        "imports": ["common/shared.properties.json", "exits.properties.json"],
        "groups": [{"name": "Accounting"}],
        "properties": [_prop("own")],
    }))

    assert [p.id for p in suite.properties] == ["own", "solvency", "supply", "exit"]
    assert suite.imports == []
    # A group the suite names without a description takes the imported one.
    assert [(g.name, g.description) for g in suite.groups] == [("Accounting", "Mint and burn.")]


def test_a_cycle_of_imports_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.properties.json", {"imports": ["a.properties.json"], "properties": [_prop("b")]})
    a = _write(tmp_path, "a.properties.json", {"imports": ["b.properties.json"], "properties": [_prop("a")]})

    with pytest.raises(ValueError, match="cycle"):
        UserPropertyFile.load_suite(a)


def test_an_id_two_files_define_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.properties.json", {"properties": [_prop("same", description="other")]})
    a = _write(tmp_path, "a.properties.json", {"imports": ["b.properties.json"], "properties": [_prop("same")]})

    with pytest.raises(ValueError, match="'same' is defined in"):
        UserPropertyFile.load_suite(a)


def test_a_group_two_files_describe_differently_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "b.properties.json", {"groups": [{"name": "G", "description": "one"}], "properties": []})
    a = _write(tmp_path, "a.properties.json", {
        "imports": ["b.properties.json"], "groups": [{"name": "G", "description": "two"}], "properties": [],
    })

    with pytest.raises(ValueError, match="group 'G' has one description"):
        UserPropertyFile.load_suite(a)


def test_generate_reads_the_file_the_environment_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "certora").mkdir()
    (tmp_path / "certora/properties.json").write_text(json.dumps(FILE))
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

    monkeypatch.setenv(USER_PROPERTIES_ENV, "certora/properties.json")
    ps = asyncio.run(UserPropertiesGenerator().generate(ctx))
    assert ps.contract_name == "Vault" and ps.ecosystem == "evm"
    assert ps.keys() == {
        ("Accounting", "total_supply_matches_shares"), ("Accounting", "mint_rounding"),
        ("Exits", "Withdraw-Is.Bounded"), (DEFAULT_GROUP, "no_round_trip_profit"),
    }
