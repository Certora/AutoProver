"""Tests for locating the build-config directory that owns the contract under analysis.

The interesting layout is a monorepo run from the repo root: the Foundry project sits at
``<pkg>/foundry.toml`` and the root holds only a ``package.json``, so the owning directory
has to be found by walking up from the contract rather than by looking where the run began.
"""

import json
from pathlib import Path

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.build_systems.hardhat import HardhatManager
from certora_autosetup.build_systems.truffle import TruffleManager
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


def _artifact(out_dir: Path, source: str, name: str) -> None:
    """Write a Foundry artifact that records the source it was compiled from."""
    d = out_dir / Path(source).name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        '{"metadata": {"settings": {"compilationTarget": {"%s": "%s"}}}}' % (source, name)
    )


def test_shared_out_dir_resolves_to_the_project_that_wrote_it(tmp_path: Path) -> None:
    # Two configs naming the same physical artifact directory: the root builds the tree with
    # out = 'pkg/out', and pkg/ carries its own foundry.toml with the default out = 'out'. Only
    # the root ran, and its artifacts say so by recording paths relative to the root, so pkg/ is
    # the wrong frame to read them in even though it has a config and a populated out/ beside it.
    (tmp_path / "foundry.toml").write_text("[profile.default]\nsrc = 'pkg/src'\nout = 'pkg/out'\n")
    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "foundry.toml").write_text("[profile.default]\nsrc = 'src'\nout = 'out'\n")
    contract = pkg / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")
    _artifact(pkg / "out", "pkg/src/Widget.sol", "Widget")

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_nested_project_that_wrote_its_own_artifacts_still_wins(tmp_path: Path) -> None:
    # The counterpart: the nested project really did build, and its artifacts record paths
    # relative to itself. Anchoring there is right, and the shared-out check must not undo it.
    (tmp_path / "package.json").write_text("{}")
    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "foundry.toml").write_text("[profile.default]\n")
    contract = pkg / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")
    _artifact(pkg / "out", "src/Widget.sol", "Widget")

    assert find_build_config_dir(contract, tmp_path) == pkg.resolve()


def test_artifacts_without_recorded_sources_keep_the_nearest_built_config(tmp_path: Path) -> None:
    # Older Foundry, or metadata stripped: nothing says which project wrote these, and absence
    # of evidence should not move the anchor.
    (tmp_path / "package.json").write_text("{}")
    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "foundry.toml").write_text("[profile.default]\n")
    contract = pkg / "src" / "Widget.sol"
    contract.write_text("contract Widget {}")
    (pkg / "out" / "Widget.sol").mkdir(parents=True)
    (pkg / "out" / "Widget.sol" / "Widget.json").write_text('{"abi": []}')

    assert find_build_config_dir(contract, tmp_path) == pkg.resolve()


def _truffle_artifact(build_dir: Path, name: str, source: Path) -> None:
    """Write a Truffle artifact. Its `sourcePath` is the absolute path the source had on the
    machine that compiled it, which is what separates it from the other two build systems."""
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / f"{name}.json").write_text(
        json.dumps({"contractName": name, "sourcePath": str(source), "bytecode": "0x60"})
    )


def test_truffle_shared_build_dir_resolves_to_the_project_that_wrote_it(tmp_path: Path) -> None:
    # The Truffle spelling of the shared-artifact-directory problem. Both configs answer for the
    # same default build/contracts/, only the root ran, and its artifacts name sources under the
    # root — so pkg/ is holding somebody else's output.
    (tmp_path / "truffle-config.js").write_text("module.exports = {};")
    pkg = tmp_path / "pkg"
    (pkg / "contracts").mkdir(parents=True)
    (pkg / "truffle-config.js").write_text("module.exports = {};")
    contract = pkg / "contracts" / "Widget.sol"
    contract.write_text("contract Widget {}")
    root_source = tmp_path / "contracts" / "Widget.sol"
    root_source.parent.mkdir(parents=True)
    root_source.write_text("contract Widget {}")
    _truffle_artifact(pkg / "build" / "contracts", "Widget", root_source)

    assert find_build_config_dir(contract, tmp_path) == tmp_path.resolve()


def test_truffle_project_that_wrote_its_own_artifacts_still_wins(tmp_path: Path) -> None:
    # The counterpart, so the absolute-path test is containment and not a blanket rejection.
    (tmp_path / "package.json").write_text("{}")
    pkg = tmp_path / "pkg"
    (pkg / "contracts").mkdir(parents=True)
    (pkg / "truffle-config.js").write_text("module.exports = {};")
    contract = pkg / "contracts" / "Widget.sol"
    contract.write_text("contract Widget {}")
    _truffle_artifact(pkg / "build" / "contracts", "Widget", contract)

    assert find_build_config_dir(contract, tmp_path) == pkg.resolve()


def test_an_absolute_source_outside_the_candidate_is_not_ownership(tmp_path: Path) -> None:
    # An absolute recorded path has to be tested for containment. Joining it onto the candidate
    # would discard the candidate entirely under pathlib, so a source that exists somewhere else
    # on disk would read as proof that this directory built it.
    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    (root / "truffle-config.js").write_text("module.exports = {};")
    contract = root / "contracts" / "Widget.sol"
    contract.write_text("contract Widget {}")
    elsewhere = tmp_path / "elsewhere" / "Widget.sol"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("contract Widget {}")
    _truffle_artifact(root / "build" / "contracts", "Widget", elsewhere)

    assert TruffleManager.artifacts_belong_to(root, root / "build" / "contracts") is False


def test_hardhat_sidecars_are_not_read_as_source_records(tmp_path: Path) -> None:
    # `.dbg.json` and build-info/ sit in the same tree as the artifacts and record no source of
    # their own; the `_format` stamp is what tells them apart.
    artifacts = tmp_path / "artifacts"
    (artifacts / "contracts" / "Widget.sol").mkdir(parents=True)
    (artifacts / "contracts" / "Widget.sol" / "Widget.dbg.json").write_text(
        '{"buildInfo": "../../build-info/1234.json"}'
    )
    (artifacts / "build-info").mkdir(parents=True)
    (artifacts / "build-info" / "1234.json").write_text('{"solcVersion": "0.8.20"}')

    assert HardhatManager.recorded_source(json.loads('{"buildInfo": "x"}')) is None
    # Nothing in the tree records a source, so the directory cannot say whose it is.
    assert HardhatManager.artifacts_belong_to(tmp_path, artifacts) is True


def test_hardhat_artifact_names_its_source(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    (artifacts / "contracts" / "Widget.sol").mkdir(parents=True)
    (artifacts / "contracts" / "Widget.sol" / "Widget.json").write_text(
        json.dumps({"_format": "hh-sol-artifact-1", "sourceName": "contracts/Widget.sol"})
    )
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "Widget.sol").write_text("contract Widget {}")

    assert HardhatManager.artifacts_belong_to(tmp_path, artifacts) is True
    assert HardhatManager.artifacts_belong_to(tmp_path / "pkg", artifacts) is False


def test_foundry_artifact_covering_several_sources_names_none(tmp_path: Path) -> None:
    # compilationTarget with more than one entry does not identify a single source, so it says
    # nothing about which project wrote the artifact.
    both = {"a/A.sol": "A", "b/B.sol": "B"}
    assert FoundryManager.recorded_source({"metadata": {"settings": {"compilationTarget": both}}}) is None
    assert FoundryManager.recorded_source({"abi": []}) is None
    assert TruffleManager.recorded_source({"contractName": "Widget"}) is None


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
