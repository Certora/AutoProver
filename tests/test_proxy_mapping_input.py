"""--proxy-mapping: a declared proxy gets its implementations and contract_extensions without
proxy detection."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from certora_autosetup.autosetup.cli_args import create_parser, parse_proxy_mappings
from certora_autosetup.setup.call_resolution import CallResolutionPhase
from certora_autosetup.utils.types import ContractHandle


def test_parse_proxy_mappings() -> None:
    assert parse_proxy_mappings(["P=A", "Q=B, C", "P=D,A"]) == {"P": ["A", "D"], "Q": ["B", "C"]}


@pytest.mark.parametrize("spec", ["P", "P=", "=A", "P=,"])
def test_parse_proxy_mappings_rejects_malformed(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_proxy_mappings([spec])


def test_flag_is_repeatable() -> None:
    args = create_parser().parse_args(
        ["--main-contract", "src/A.sol:A", "--proxy-mapping", "P=A", "--proxy-mapping", "Q=B"]
    )
    assert args.proxy_mapping == ["P=A", "Q=B"]


class _RecordingConfigManager:
    def __init__(self) -> None:
        self.added: list[ContractHandle] = []
        self.properties: dict[str, Any] = {}

    def add_files_to_config(self, config_file: Path, new_contract_files: list[ContractHandle]) -> None:
        self.added.extend(new_contract_files)

    def update_config_with_properties(self, config_file: Path, props: dict[str, Any]) -> None:
        self.properties.update(props)


def _phase(declared: dict[str, list[str]]) -> tuple[CallResolutionPhase, _RecordingConfigManager]:
    # Skip __init__: it reads a real conf. Set only what _detect_proxy_for touches.
    phase = object.__new__(CallResolutionPhase)
    config_manager = _RecordingConfigManager()
    phase.config_manager = config_manager  # type: ignore[assignment]
    phase.config_file = Path("certora/confs/Main.conf")
    phase.current_iteration = 1
    phase.proxy_detector = None
    phase.proxy_mappings = []
    phase.declared_proxy_mappings = declared
    phase._proxy_scan_results = []
    phase._proxy_state_lock = asyncio.Lock()
    phase._resolve_implementation_handle = (  # type: ignore[method-assign]
        lambda impl: ContractHandle(impl.contract_name, f"src/{impl.contract_name}.sol")
    )

    async def _on_contracts_added(handles: list[ContractHandle], source: Any) -> None:
        return None

    phase._on_contracts_added = _on_contracts_added  # type: ignore[method-assign]
    return phase, config_manager


def test_declared_proxy_applied_without_detector() -> None:
    phase, config_manager = _phase({"Proxy": ["ImplA", "ImplB"]})
    asyncio.run(phase._detect_proxy_for(ContractHandle("Proxy", "src/Proxy.sol")))

    assert [h.contract_name for h in config_manager.added] == ["ImplA", "ImplB"]
    assert config_manager.properties == {
        "contract_extensions": {
            "Proxy": [{"extension": "ImplA", "exclude": []}, {"extension": "ImplB", "exclude": []}]
        },
        "contract_extensions_override": True,
    }


def test_undeclared_contract_untouched_without_detector() -> None:
    phase, config_manager = _phase({"Proxy": ["ImplA"]})
    asyncio.run(phase._detect_proxy_for(ContractHandle("Other", "src/Other.sol")))

    assert config_manager.added == []
    assert config_manager.properties == {}
