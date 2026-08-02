"""Scene detection anchors on the contract, not on the process CWD.

``cli.main`` resolves the scene before the pipeline starts, so these cover a repo whose
Foundry project sits below the directory autosetup was invoked from: detection has to find
that project, and the handles it returns have to come back in the caller's frame.
"""

from pathlib import Path

import pytest

import certora_autosetup.utils.contract_utils as cu
from certora_autosetup.utils.contract_utils import auto_detect_contracts, resolve_contract_handles
from certora_autosetup.utils.types import ContractHandle


@pytest.fixture
def nested_project(tmp_path: Path) -> Path:
    """Repo root holding no build config at all; the Foundry project is one level down."""
    (tmp_path / "package.json").write_text('{"dependencies": {"ethers": "^6"}}')
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    (project / "src" / "Widget.sol").write_text("contract Widget {}")
    return tmp_path


def _stub_extractor(monkeypatch, handles):
    """Stand in for the build system, which would need a real forge build otherwise."""

    class _Extractor:
        def extract_logic_contracts_and_files(self):
            return handles

    monkeypatch.setattr(
        cu.BuildSystemDetector, "get_contract_extractor", lambda *a, **k: _Extractor()
    )


def test_auto_detect_from_repo_root_finds_the_nested_project(nested_project, monkeypatch) -> None:
    # The exact regression: anchored at the root this raised ValueError.
    _stub_extractor(monkeypatch, [ContractHandle("Widget", "src/Widget.sol")])

    handles = auto_detect_contracts(
        nested_project / "sub", emit_relative_to=nested_project
    )

    # Paths must come back usable from the CWD, not from the nested project dir.
    assert [h.source_file for h in handles] == [str(Path("sub/src/Widget.sol"))]


def test_auto_detect_leaves_paths_alone_for_a_root_project(tmp_path, monkeypatch) -> None:
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    _stub_extractor(monkeypatch, [ContractHandle("Widget", "src/Widget.sol")])

    handles = auto_detect_contracts(tmp_path, emit_relative_to=tmp_path)

    assert [h.source_file for h in handles] == ["src/Widget.sol"]


def test_auto_detect_still_raises_when_there_is_no_build_config_anywhere(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(ValueError, match="No build system detected"):
        auto_detect_contracts(tmp_path, emit_relative_to=tmp_path)


def test_resolve_rebases_the_artifact_lookup(nested_project, monkeypatch) -> None:
    # The artifact map is keyed relative to the project dir ("src/Widget.sol"), while the
    # incoming handle is relative to the CWD ("sub/src/Widget.sol"). Without re-basing the
    # lookup misses and the inferred name is silently left wrong.
    class _Extractor:
        def build_source_path_to_contracts_map(self):
            return {"src/Widget.sol": [("TheRealName", "0.8.27")]}

    monkeypatch.setattr(cu, "FoundryContractExtractor", lambda *a, **k: _Extractor())

    resolved = resolve_contract_handles(
        [ContractHandle("Widget", "sub/src/Widget.sol")],
        nested_project / "sub",
        handles_relative_to=nested_project,
    )

    assert resolved[0].contract_name == "TheRealName"
    # The returned path stays in the caller's frame of reference.
    assert resolved[0].source_file == "sub/src/Widget.sol"
