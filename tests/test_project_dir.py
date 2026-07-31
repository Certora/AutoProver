"""Tests for locating the build-config directory that owns the contract under analysis.

Regression origin: a monorepo whose Foundry project sat one level down
(``<pkg>/foundry.toml``) was run from the repo root, where nothing but a root
``package.json`` existed. Build-system detection reported ``unknown``, the packages list
was built from that root package.json alone, and solc failed with
``Source "@pkg/Foo.sol" not found ... Searched the following locations: ""``.
"""

from pathlib import Path

from certora_autosetup.utils.project_dir import (
    describe_build_config_dir,
    find_build_config_dir,
)


def test_finds_nested_foundry_project(tmp_path: Path) -> None:
    # The shape that broke: build config one level down, only a package.json at the root.
    (tmp_path / "package.json").write_text('{"dependencies": {"ethers": "^6"}}')
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text('[profile.default]\nremappings = ["@pkg/=node_modules/@pkg/"]\n')
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_accepts_a_contract_path_relative_to_root(tmp_path: Path) -> None:
    # Contract handles carry root-relative paths, which is how autosetup calls this.
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    (project / "src" / "Widget.sol").write_text("contract Widget {}")

    assert find_build_config_dir(Path("sub/src/Widget.sol"), tmp_path) == project.resolve()


def test_root_level_project_resolves_to_root(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "src").mkdir()
    contract = tmp_path / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_no_build_config_anywhere_falls_back_to_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    contract = tmp_path / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_nearest_ancestor_wins_over_an_outer_one(tmp_path: Path) -> None:
    # A monorepo may carry a root foundry.toml too; the sub-project's own config governs.
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    project = tmp_path / "packages" / "sub"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_hardhat_config_also_anchors(tmp_path: Path) -> None:
    project = tmp_path / "app"
    (project / "contracts").mkdir(parents=True)
    (project / "hardhat.config.ts").write_text("export default {};")
    contract = project / "contracts" / "Vault.sol"
    contract.write_text("contract Vault {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_walk_stops_at_root(tmp_path: Path) -> None:
    # A foundry.toml *above* the run root must not be picked up — the run root bounds
    # what the conf can reference.
    outer = tmp_path / "outer"
    root = outer / "repo"
    (root / "src").mkdir(parents=True)
    (outer / "foundry.toml").write_text("[profile.default]\n")
    contract = root / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, root) == root.resolve()


def test_contract_outside_root_returns_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root).mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "foundry.toml").write_text("[profile.default]\n")
    contract = elsewhere / "Stray.sol"
    contract.write_text("contract Stray {}")

    assert find_build_config_dir(contract, root) == root.resolve()


def test_describe_returns_none_for_the_root_itself(tmp_path: Path) -> None:
    assert describe_build_config_dir(tmp_path, tmp_path) is None


def test_describe_returns_the_relative_subdir(tmp_path: Path) -> None:
    project = tmp_path / "packages" / "sub"
    project.mkdir(parents=True)

    assert describe_build_config_dir(project, tmp_path) == str(Path("packages") / "sub")
