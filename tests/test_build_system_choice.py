"""Choosing which build system's artifacts to read.

Two decisions, both made from what is on disk rather than from config files alone:
``BuildSystemDetector`` picking between a Foundry and a Hardhat config that sit side by
side, and ``ContractExtractor`` picking the directory those artifacts live in when the
project configures its output somewhere other than the default.
"""

import json
from pathlib import Path

import pytest

from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.parsers.foundry import FoundryContractExtractor


def _foundry_config(project: Path, out: str | None = None) -> None:
    body = "[profile.default]\n"
    if out is not None:
        body += f'out = "{out}"\n'
    (project / "foundry.toml").write_text(body)


def _hardhat_config(project: Path) -> None:
    (project / "hardhat.config.ts").write_text("export default {};")


def _foundry_artifacts(out_dir: Path, contract: str = "Widget", source: str = "src/Widget.sol") -> None:
    """Write the artifact `forge build` leaves for one compiled contract."""
    artifact_dir = out_dir / Path(source).name
    artifact_dir.mkdir(parents=True)
    (artifact_dir / f"{contract}.json").write_text(json.dumps({
        "bytecode": {"object": "0x6080604052"},
        "metadata": {
            "compiler": {"version": "0.8.20+commit.a1b79de6"},
            "settings": {"compilationTarget": {source: contract}},
        },
    }))


def _hardhat_artifacts(artifacts_dir: Path) -> None:
    """Hardhat's own layout: the sources tree mirrored, plus build-info beside it."""
    (artifacts_dir / "contracts" / "Vault.sol").mkdir(parents=True)
    (artifacts_dir / "build-info").mkdir(parents=True)


# --- detector: one build system ------------------------------------------------------


def test_foundry_alone_is_foundry_built_or_not(tmp_path: Path) -> None:
    _foundry_config(tmp_path)

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY

    _foundry_artifacts(tmp_path / "out")
    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_hardhat_alone_is_hardhat_built_or_not(tmp_path: Path) -> None:
    _hardhat_config(tmp_path)

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.HARDHAT

    _hardhat_artifacts(tmp_path / "artifacts")
    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.HARDHAT


# --- detector: both configs present --------------------------------------------------


def test_both_configs_with_foundry_artifacts_stays_foundry(tmp_path: Path) -> None:
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)
    _foundry_artifacts(tmp_path / "out")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_both_configs_unbuilt_stays_foundry(tmp_path: Path) -> None:
    # No evidence either way, which is every run that happens before the build.
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_both_configs_both_built_stays_foundry(tmp_path: Path) -> None:
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)
    _foundry_artifacts(tmp_path / "out")
    _hardhat_artifacts(tmp_path / "artifacts")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_both_configs_with_only_hardhat_artifacts_picks_hardhat(tmp_path: Path) -> None:
    # The tie-break: a Hardhat project whose foundry.toml governs a forge test harness
    # nobody built. Reading Foundry's empty out/ there yields no contracts at all.
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)
    _hardhat_artifacts(tmp_path / "artifacts")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.HARDHAT


def test_an_empty_foundry_out_is_not_evidence(tmp_path: Path) -> None:
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)
    (tmp_path / "out").mkdir()
    _hardhat_artifacts(tmp_path / "artifacts")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.HARDHAT


def test_foundrys_evidence_is_read_from_its_configured_out(tmp_path: Path) -> None:
    # Foundry built into a non-default directory: the artifacts are still Foundry's, so
    # the tie must not go to Hardhat just because out/ is nowhere to be seen.
    _foundry_config(tmp_path, out="artifacts-forge")
    _hardhat_config(tmp_path)
    _foundry_artifacts(tmp_path / "artifacts-forge")
    _hardhat_artifacts(tmp_path / "artifacts")

    assert BuildSystemDetector.detect(tmp_path) == BuildSystem.FOUNDRY


def test_an_explicit_build_system_beats_the_artifacts(tmp_path: Path) -> None:
    _foundry_config(tmp_path)
    _hardhat_config(tmp_path)
    _hardhat_artifacts(tmp_path / "artifacts")

    assert BuildSystemDetector.resolve(tmp_path, "foundry") == BuildSystem.FOUNDRY


# --- extractor: which directory the artifacts are read from --------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-contract Foundry project; the source has to exist to be in scope."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Widget.sol").write_text("contract Widget {}")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _extracted(project: Path) -> list[str]:
    return [h.contract_name for h in FoundryContractExtractor(project).extract_logic_contracts()]


def test_artifacts_are_read_from_the_default_out(project: Path) -> None:
    _foundry_config(project)
    _foundry_artifacts(project / "out")

    assert _extracted(project) == ["Widget"]


def test_a_configured_out_nested_under_the_default_name_is_read(project: Path) -> None:
    # `out/foundry` leaves a bare `out/` that exists and holds no artifacts; reading it
    # instead of the configured directory finds nothing to verify.
    _foundry_config(project, out="out/foundry")
    _foundry_artifacts(project / "out" / "foundry")

    assert _extracted(project) == ["Widget"]


def test_a_populated_default_out_wins_over_the_configured_one(project: Path) -> None:
    # Foundry itself reads the profile's `out`, so the two agreeing is the normal case;
    # when they disagree the artifacts that are actually there are the useful ones.
    _foundry_config(project, out="artifacts-forge")
    _foundry_artifacts(project / "out")

    assert _extracted(project) == ["Widget"]


def test_an_unbuilt_project_names_the_build_command(project: Path) -> None:
    _foundry_config(project)

    with pytest.raises(Exception, match="forge build"):
        _extracted(project)
