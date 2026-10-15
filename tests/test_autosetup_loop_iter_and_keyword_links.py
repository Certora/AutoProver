"""Autosetup: a failed sanity Stage 1 still writes a loop_iter, and a storage path whose field
is a CVL keyword is never emitted into a `links {}` block."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from certora_autosetup.setup.sanity import DEFAULT_LOOP_ITER, SanityPhase
from certora_autosetup.utils.contract_linker import ContractLinker, LinkStatus, _cvl_keyword_segments


def test_keyword_segments_of_storage_paths():
    assert _cvl_keyword_segments("hook") == ["hook"]
    assert _cvl_keyword_segments("holder.hook") == ["hook"]
    assert _cvl_keyword_segments("rule[0].token") == ["rule"]
    assert _cvl_keyword_segments("holder.token") == []
    assert _cvl_keyword_segments("fixedTokens[0]") == []


def _call(base: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(
        storage_path=SimpleNamespace(base_contract=base, path=path),
        selector="0x12345678",
        callee_name="beforeTransfer(address)",
    )


def test_keyword_named_field_is_not_linked(tmp_path: Path):
    linker = ContractLinker(SimpleNamespace(project_root=tmp_path))
    call = _call("Vault", "hook")

    links, remaining = linker.generate_links_from_call_resolution([call], MagicMock())

    assert links == []
    assert remaining == [call]
    assert linker.generate_links_spec_lines() == []
    decision = linker._linking_decisions["Vault:hook"]
    assert decision.status == LinkStatus.UNRESOLVED


def test_stage1_failure_writes_default_loop_iter(tmp_path: Path):
    config_manager = MagicMock()
    conf = tmp_path / "Main.conf"
    phase = SanityPhase(
        contract_name="Main",
        config_file=conf,
        prover_runner=MagicMock(),
        config_manager=config_manager,
        orchestration_timestamp="20260929_000000",
    )

    async def no_loop_iter():
        return None, None

    phase._find_optimal_loop_iter = no_loop_iter

    assert asyncio.run(phase.execute()) == {}
    config_manager.update_config_with_properties.assert_called_once_with(
        conf, {"loop_iter": DEFAULT_LOOP_ITER, "optimistic_loop": True}
    )
