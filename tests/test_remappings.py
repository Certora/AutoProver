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
