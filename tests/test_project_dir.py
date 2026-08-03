"""Tests for locating the build-config directory that owns the contract under analysis.

The interesting layout is a monorepo run from the repo root: the Foundry project sits at
``<pkg>/foundry.toml`` and the root holds only a ``package.json``, so the owning directory
has to be found by walking up from the contract rather than by looking where the run began.
"""

from pathlib import Path

from certora_autosetup.utils.project_dir import (
    describe_build_config_dir,
    find_build_config_dir,
    rebase,
)


def test_finds_nested_foundry_project(tmp_path: Path) -> None:
    # Build config one level down, only a package.json at the root.
    (tmp_path / "package.json").write_text('{"dependencies": {"ethers": "^6"}}')
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text('[profile.default]\nremappings = ["@pkg/=node_modules/@pkg/"]\n')
    (project / "out").mkdir()
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_accepts_a_contract_path_relative_to_root(tmp_path: Path) -> None:
    # Contract handles carry root-relative paths, which is how autosetup calls this.
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "out").mkdir()
    (project / "foundry.toml").write_text("[profile.default]\n")
    (project / "src" / "Widget.sol").write_text("contract Widget {}")

    assert find_build_config_dir(Path("sub/src/Widget.sol"), tmp_path) == project.resolve()


def test_root_level_project_resolves_to_root(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "out").mkdir()
    (tmp_path / "src").mkdir()
    contract = tmp_path / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_no_build_config_anywhere_falls_back_to_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    contract = tmp_path / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_nearest_built_ancestor_wins_over_an_outer_one(tmp_path: Path) -> None:
    # Both carry a config, both were built: the sub-project's own config governs.
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "out").mkdir()
    project = tmp_path / "packages" / "sub"
    (project / "src").mkdir(parents=True)
    (project / "out").mkdir()
    (project / "foundry.toml").write_text("[profile.default]\n")
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_unbuilt_inner_config_yields_to_the_built_root(tmp_path: Path) -> None:
    # The Maple shape, and the regression this rule exists to prevent: a vendored
    # per-package foundry.toml under modules/ that the root build never writes into.
    # Anchoring there points the extractor at a modules/pool/out that does not exist.
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "out").mkdir()
    project = tmp_path / "modules" / "pool"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_no_artifacts_anywhere_falls_back_to_root(tmp_path: Path) -> None:
    # Nothing was built, so there is no evidence for any nested project: stay at root,
    # which is exactly where the pre-existing behaviour anchored.
    project = tmp_path / "lib" / "dependency"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_honors_a_custom_foundry_out_dir(tmp_path: Path) -> None:
    project = tmp_path / "sub"
    (project / "src").mkdir(parents=True)
    (project / "artefacts").mkdir()
    (project / "foundry.toml").write_text('[profile.default]\nout = "artefacts"\n')
    contract = project / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_hardhat_config_also_anchors(tmp_path: Path) -> None:
    project = tmp_path / "app"
    (project / "contracts").mkdir(parents=True)
    (project / "artifacts").mkdir()
    (project / "hardhat.config.ts").write_text("export default {};")
    contract = project / "contracts" / "Vault.sol"
    contract.write_text("contract Vault {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_truffle_config_also_anchors(tmp_path: Path) -> None:
    # The shape Truffle monorepos ship: one config per app, artifacts beside it.
    project = tmp_path / "apps" / "token"
    (project / "contracts").mkdir(parents=True)
    (project / "build" / "contracts").mkdir(parents=True)
    (project / "truffle-config.js").write_text("module.exports = {};")
    contract = project / "contracts" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_truffle_v4_config_name_also_anchors(tmp_path: Path) -> None:
    project = tmp_path / "apps" / "token"
    (project / "contracts").mkdir(parents=True)
    (project / "build" / "contracts").mkdir(parents=True)
    (project / "truffle.js").write_text("module.exports = {};")
    contract = project / "contracts" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == project.resolve()


def test_unbuilt_truffle_config_yields_to_root(tmp_path: Path) -> None:
    # Same rule as the other build systems: a config without artifacts is not evidence
    # that this is the project that got built.
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    (tmp_path / "out").mkdir()
    project = tmp_path / "apps" / "token"
    (project / "contracts").mkdir(parents=True)
    (project / "truffle-config.js").write_text("module.exports = {};")
    contract = project / "contracts" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_walk_stops_at_root(tmp_path: Path) -> None:
    # A foundry.toml *above* the run root must not be picked up — the run root bounds
    # what the conf can reference.
    outer = tmp_path / "outer"
    root = outer / "repo"
    (root / "src").mkdir(parents=True)
    (outer / "foundry.toml").write_text("[profile.default]\n")
    (outer / "out").mkdir()
    contract = root / "src" / "Token.sol"
    contract.write_text("contract Token {}")

    assert find_build_config_dir(contract, root) == root.resolve()


def test_contract_outside_root_returns_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root).mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "foundry.toml").write_text("[profile.default]\n")
    (elsewhere / "out").mkdir()
    contract = elsewhere / "Stray.sol"
    contract.write_text("contract Stray {}")

    assert find_build_config_dir(contract, root) == root.resolve()


def test_describe_returns_none_for_the_root_itself(tmp_path: Path) -> None:
    assert describe_build_config_dir(tmp_path, tmp_path) is None


def test_describe_returns_the_relative_subdir(tmp_path: Path) -> None:
    project = tmp_path / "packages" / "sub"
    project.mkdir(parents=True)

    assert describe_build_config_dir(project, tmp_path) == str(Path("packages") / "sub")


# ---------------------------------------------------------------------------
# rebase — reinterpreting a build-system-relative path against the run CWD
# ---------------------------------------------------------------------------


def test_rebase_pushes_a_nested_projects_path_down_to_the_root(tmp_path: Path) -> None:
    # Foundry records compilationTarget relative to its own project dir, so a nested
    # project reports "src/Widget.sol" while the CWD is the repo root.
    assert rebase("src/Widget.sol", tmp_path / "sub", tmp_path) == str(Path("sub/src/Widget.sol"))


def test_rebase_is_identity_when_the_dirs_match(tmp_path: Path) -> None:
    assert rebase("src/Widget.sol", tmp_path, tmp_path) == str(Path("src/Widget.sol"))


def test_rebase_leaves_absolute_paths_alone(tmp_path: Path) -> None:
    absolute = str(tmp_path / "src" / "Widget.sol")
    assert rebase(absolute, tmp_path / "sub", tmp_path) == absolute


def test_rebase_handles_a_deeper_nesting(tmp_path: Path) -> None:
    assert rebase(
        "contracts/Vault.sol", tmp_path / "packages" / "app", tmp_path
    ) == str(Path("packages/app/contracts/Vault.sol"))


def test_rebase_can_walk_upward(tmp_path: Path) -> None:
    # Not produced by find_build_config_dir (it is bounded by the run root), but the
    # helper is pure path math and should not silently mangle the reverse direction.
    assert rebase("sub/src/Widget.sol", tmp_path, tmp_path / "sub") == str(Path("src/Widget.sol"))
