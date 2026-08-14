"""Tests for the shared remapping-sources → Certora `packages` builder.

Regression guard for the bug where `FoundryManager.parse_config` built the conf's
`packages` list only from foundry.toml's explicit `remappings` key, dropping
remappings.txt entries and forge's auto-inferred lib/* remappings, which made
certoraRun die with `ParserError: Source "..." not found`.

`forge` is not available in CI, so `forge remappings` is monkeypatched here: the
absent-forge cases exercise the file-reading fallback (foundry.toml + remappings.txt +
package.json), and the present-forge case feeds canned output to assert priority.
"""

import subprocess
from pathlib import Path

import pytest

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.utils import remappings as remappings_mod
from certora_autosetup.utils.remappings import build_packages_from_remapping_sources


def _no_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate `forge` not being installed (the CI reality)."""

    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("forge")

    monkeypatch.setattr(remappings_mod.subprocess, "run", fake_run)


def _forge_returning(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """Simulate `forge remappings` succeeding with the given stdout."""

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["forge", "remappings"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(remappings_mod.subprocess, "run", fake_run)


def _keys(packages):
    return {p.split("=", 1)[0] for p in packages}


def _path_of(packages, key):
    for p in packages:
        k, v = p.split("=", 1)
        if k == key:
            return v
    return None


def test_fallback_merges_foundry_toml_and_remappings_txt(tmp_path: Path, monkeypatch) -> None:
    # The core regression: remappings.txt entries were dropped. With forge absent,
    # the builder must still merge foundry.toml AND remappings.txt.
    _no_forge(monkeypatch)
    (tmp_path / "foundry.toml").write_text(
        '[profile.default]\nremappings = ["@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/"]\n'
    )
    (tmp_path / "remappings.txt").write_text(
        "solady/=lib/solady/src/\nforge-std/=lib/forge-std/src/\n"
    )

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    # Keys are canonicalized to a trailing-slash form (the boundary is significant).
    assert _keys(packages) == {"@openzeppelin/contracts/", "solady/", "forge-std/"}
    # relative targets resolved absolute against base_dir, also trailing-slash normalized
    assert _path_of(packages, "solady/") == str(tmp_path / "lib/solady/src") + "/"


def test_forge_remappings_take_priority_over_foundry_toml(tmp_path: Path, monkeypatch) -> None:
    # forge is authoritative: on a key conflict its path wins over foundry.toml.
    _forge_returning(monkeypatch, "@oz/=lib/forge-inferred-oz/\n")
    (tmp_path / "foundry.toml").write_text('[profile.default]\nremappings = ["@oz/=lib/stale-oz/"]\n')

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    assert _path_of(packages, "@oz/") == str(tmp_path / "lib/forge-inferred-oz") + "/"


def test_distinct_prefix_keys_keep_their_boundary_slash(tmp_path: Path, monkeypatch) -> None:
    # `@openzeppelin/contracts/` must NOT swallow `@openzeppelin/contracts-upgradeable/`.
    # The trailing slash is the prefix boundary: stripping it (the old `rstrip("/")`) turned
    # `@openzeppelin/contracts` into a prefix of `@openzeppelin/contracts-upgradeable`, so a
    # context-scoped v4 mapping mis-resolved upgradeable imports to a nonexistent path.
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        "@openzeppelin/contracts/=lib/oz/contracts/\n"
        "@openzeppelin/contracts-upgradeable/=lib/oz-upgradeable/contracts/\n"
    )

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    keys = _keys(packages)
    assert "@openzeppelin/contracts/" in keys
    assert "@openzeppelin/contracts-upgradeable/" in keys
    # the boundary-less form must NOT be emitted (that is the swallow-the-sibling bug)
    assert "@openzeppelin/contracts" not in keys


def test_context_scoped_key_keeps_trailing_slash(tmp_path: Path, monkeypatch) -> None:
    # Regression: a context-scoped remapping sending one dependency's OZ imports to a
    # vendored OZ v4 tree (a common pattern when a project mixes OZ v4 and v5). If the key's
    # trailing slash is stripped, the scoped `@openzeppelin/contracts` prefix-matches
    # `@openzeppelin/contracts-upgradeable/...` and — because solc ranks longest-context
    # first — rewrites it to `lib/openzeppelin-contracts-v4/contracts-upgradeable/...`, which
    # does not exist (v4 OZ has no contracts-upgradeable subtree).
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        "lib/some-dependency/:@openzeppelin/contracts/=lib/openzeppelin-contracts-v4/contracts/\n"
        "@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/\n"
    )

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    keys = _keys(packages)
    assert "lib/some-dependency/:@openzeppelin/contracts/" in keys
    assert "lib/some-dependency/:@openzeppelin/contracts" not in keys
    # the scoped v4 target keeps its trailing slash so key/path agree on the boundary
    assert _path_of(packages, "lib/some-dependency/:@openzeppelin/contracts/") == \
        str(tmp_path / "lib/openzeppelin-contracts-v4/contracts") + "/"


def test_file_level_remapping_keeps_exact_form(tmp_path: Path, monkeypatch) -> None:
    # An import-patch entry that remaps a specific source FILE (…/IFoo.sol=…/IFoo.sol) must NOT
    # get a trailing slash — otherwise solc looks for a directory `IFoo.sol/` and the import fails.
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        "src/interfaces/INoncesKeyed.sol=lib/aave-v4/src/interfaces/INoncesKeyed.sol\n"
        "@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/\n"
    )

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    keys = _keys(packages)
    assert "src/interfaces/INoncesKeyed.sol" in keys           # file key: unchanged
    assert "src/interfaces/INoncesKeyed.sol/" not in keys       # NOT slashed
    assert _path_of(packages, "src/interfaces/INoncesKeyed.sol") == \
        str(tmp_path / "lib/aave-v4/src/interfaces/INoncesKeyed.sol")   # file target: no slash
    # directory remappings alongside it still get the boundary slash
    assert "@openzeppelin/contracts/" in keys


def test_package_json_deps_added_as_node_modules(tmp_path: Path, monkeypatch) -> None:
    _no_forge(monkeypatch)
    (tmp_path / "package.json").write_text('{"dependencies": {"@solmate/core": "^1.0.0"}}')

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    assert _path_of(packages, "@solmate/core/") == str(tmp_path / "node_modules/@solmate/core") + "/"


def test_empty_project_yields_no_packages(tmp_path: Path, monkeypatch) -> None:
    _no_forge(monkeypatch)
    assert build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None) == []


def test_parse_config_populates_packages_from_remappings_txt(tmp_path: Path, monkeypatch) -> None:
    # End-to-end at the actual bug site: FoundryManager.parse_config must set
    # config.packages from the merged sources, not just foundry.toml's remappings key.
    _no_forge(monkeypatch)
    foundry_toml = tmp_path / "foundry.toml"
    foundry_toml.write_text(
        '[profile.default]\nsrc = "src"\n'
        'remappings = ["@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/"]\n'
    )
    (tmp_path / "remappings.txt").write_text("solady/=lib/solady/src/\n")

    manager = FoundryManager(project_root=tmp_path, scope=None)
    config = manager.parse_config(foundry_toml)

    keys = {p.split("=", 1)[0] for p in (config.packages or [])}
    assert "solady/" in keys, "remappings.txt entry missing from parse_config packages (the bug)"
    assert "@openzeppelin/contracts/" in keys


def test_parse_config_reads_foundry_toml_when_forge_absent(tmp_path: Path, monkeypatch) -> None:
    # forge absent and no remappings.txt/package.json: the builder still reads the
    # foundry.toml remappings directly.
    _no_forge(monkeypatch)
    foundry_toml = tmp_path / "foundry.toml"
    foundry_toml.write_text('[profile.default]\nremappings = ["@oz/=lib/oz/"]\n')

    manager = FoundryManager(project_root=tmp_path, scope=None)
    config = manager.parse_config(foundry_toml)

    assert config.packages and any(p.split("=", 1)[0] == "@oz/" for p in config.packages)


def test_non_default_profile_remappings_read_when_forge_absent(tmp_path: Path, monkeypatch) -> None:
    # forge absent: the foundry.toml fallback reads the requested profile's remappings.
    _no_forge(monkeypatch)
    (tmp_path / "foundry.toml").write_text(
        '[profile.default]\nremappings = ["@oz/=lib/default-oz/"]\n'
        '[profile.ci]\nremappings = ["@oz/=lib/ci-oz/"]\n'
    )

    packages = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None, profile="ci")

    assert _path_of(packages, "@oz/") == str(tmp_path / "lib/ci-oz") + "/"


def test_forge_run_with_foundry_profile_env(tmp_path: Path, monkeypatch) -> None:
    # The requested profile is passed to forge via FOUNDRY_PROFILE.
    captured: dict = {}

    def fake_run(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=["forge", "remappings"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remappings_mod.subprocess, "run", fake_run)
    build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None, profile="ci")

    assert captured["env"]["FOUNDRY_PROFILE"] == "ci"


def _nested_project(tmp_path: Path) -> Path:
    """A repo whose Foundry project sits at <repo>/chains/somechain, with a sub-project tree."""
    project = tmp_path / "chains" / "somechain"
    (project / "src" / "Widget_1234" / "dependencies" / "oz-5.4.0" / "contracts").mkdir(parents=True)
    (project / "lib" / "forge-std" / "src").mkdir(parents=True)
    return project


def test_context_is_rebased_onto_the_run_root(tmp_path: Path, monkeypatch) -> None:
    # `forge remappings` reports contexts relative to the project dir, but solc matches them
    # against source unit names, which are relative to the run root. For a project nested under
    # the run root the reported context `src/Widget_1234/` never prefixes the source unit name
    # `chains/somechain/src/Widget_1234/...`, so the remapping silently never applies and every
    # import of that sub-project fails to resolve.
    project = _nested_project(tmp_path)
    _forge_returning(
        monkeypatch,
        "src/Widget_1234/:@openzeppelin/contracts/=src/Widget_1234/dependencies/oz-5.4.0/contracts/\n"
        "forge-std/=lib/forge-std/src/\n",
    )

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    keys = _keys(packages)
    assert "chains/somechain/src/Widget_1234/:@openzeppelin/contracts/" in keys
    assert "src/Widget_1234/:@openzeppelin/contracts/" not in keys
    # the target half is untouched by the rebasing — still absolute, still pointing at the tree
    assert _path_of(packages, "chains/somechain/src/Widget_1234/:@openzeppelin/contracts/") == \
        str(project / "src/Widget_1234/dependencies/oz-5.4.0/contracts") + "/"
    # an unscoped key has no context to rebase
    assert "forge-std/" in keys


def test_context_unchanged_when_the_project_is_the_run_root(tmp_path: Path, monkeypatch) -> None:
    # The flat case (the overwhelming majority): base_dir == run_root, so contexts are already
    # expressed against the run root and must come out byte-identical.
    (tmp_path / "lib" / "some-dependency").mkdir(parents=True)
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        "lib/some-dependency/:@openzeppelin/contracts/=lib/openzeppelin-contracts-v4/contracts/\n"
    )

    with_root = build_packages_from_remapping_sources(
        base_dir=tmp_path, log_fn=lambda *_: None, run_root=tmp_path
    )
    without_root = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    assert with_root == without_root
    assert "lib/some-dependency/:@openzeppelin/contracts/" in _keys(with_root)


def test_context_naming_no_directory_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    # Only a context that names a real directory under the project is project-relative. Anything
    # else — including a context already written against the run root — is left as authored.
    project = _nested_project(tmp_path)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text(
        "chains/somechain/src/Widget_1234/:@oz/=src/Widget_1234/dependencies/oz-5.4.0/contracts/\n"
    )

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert "chains/somechain/src/Widget_1234/:@oz/" in _keys(packages)


def test_context_outside_the_run_root_is_left_alone_with_a_warning(tmp_path: Path, monkeypatch) -> None:
    # A context resolving outside the run root cannot be named by any source unit name; keep the
    # authored form and say so rather than emitting a `../`-prefixed context.
    project = _nested_project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-vendor"
    outside.mkdir(exist_ok=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text(f"{outside}/:@oz/=lib/forge-std/src/\n")
    warnings: list[tuple[str, str]] = []

    packages = build_packages_from_remapping_sources(
        base_dir=project,
        log_fn=lambda msg, level: warnings.append((msg, level)),
        run_root=tmp_path,
    )

    assert f"{outside}/:@oz/" in _keys(packages)
    assert any(level == "WARNING" and "outside the run root" in msg for msg, level in warnings)


def test_parse_config_rebases_contexts_against_the_project_root(tmp_path: Path, monkeypatch) -> None:
    # End-to-end at the bug site: the manager knows the run root, so parse_config's packages
    # come out with run-root-relative contexts.
    project = _nested_project(tmp_path)
    foundry_toml = project / "foundry.toml"
    foundry_toml.write_text(
        '[profile.default]\nsrc = "src"\n'
        'remappings = ["src/Widget_1234/:@oz/=src/Widget_1234/dependencies/oz-5.4.0/contracts/"]\n'
    )
    _no_forge(monkeypatch)

    manager = FoundryManager(project_root=tmp_path, scope=None)
    config = manager.parse_config(foundry_toml)

    keys = {p.split("=", 1)[0] for p in (config.packages or [])}
    assert "chains/somechain/src/Widget_1234/:@oz/" in keys


def test_context_that_is_the_run_root_is_left_alone_without_a_warning(tmp_path: Path, monkeypatch) -> None:
    # A context resolving to the run root itself already covers every source unit name, so
    # nothing is wrong with it — unlike a context resolving outside the run root, it must not
    # be reported as a problem.
    project = _nested_project(tmp_path)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("../../:@oz/=lib/forge-std/src/\n")
    logged: list[tuple[str, str]] = []

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda msg, level: logged.append((msg, level)), run_root=tmp_path
    )

    assert "../../:@oz/" in _keys(packages)
    assert not [m for m, level in logged if level == "WARNING" and "run root" in m]
