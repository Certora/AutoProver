"""Tests for the shared remapping-sources → Certora `packages` builder.

Regression guard for the bug where `FoundryManager.parse_config` built the conf's
`packages` list only from foundry.toml's explicit `remappings` key, dropping
remappings.txt entries and forge's auto-inferred lib/* remappings, which made
certoraRun die with `ParserError: Source "..." not found`.

`forge` is not available in CI, so `forge remappings` is monkeypatched here: the
absent-forge cases exercise the file-reading fallback (foundry.toml + remappings.txt +
package.json), and the present-forge case feeds canned output to assert priority.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.utils import remappings as remappings_mod
from certora_autosetup.utils.remappings import (
    build_packages_from_remapping_sources,
    node_modules_package_root,
    resolve_node_modules_target,
)


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


# =============================================================================
# Hoisted node_modules: the ancestor walk
# =============================================================================
#
# npm/yarn hoist a dependency to the highest node_modules that satisfies every consumer, so a
# sub-project's own node_modules/<pkg> frequently does not exist while the repo root's does.
# solc has no such resolver, so the packages list must name the directory that exists.


def _hoisted_repo(tmp_path: Path) -> Path:
    """A repo whose Foundry project sits at <repo>/smart-contracts, with @vault/core hoisted to
    the repo root and @widget/lib installed locally in the sub-project."""
    project = tmp_path / "smart-contracts"
    (tmp_path / "node_modules" / "@vault" / "core" / "contracts").mkdir(parents=True)
    (project / "node_modules" / "@widget" / "lib").mkdir(parents=True)
    return project


def test_hoisted_package_resolves_from_the_ancestor_while_local_stays_local(
    tmp_path: Path, monkeypatch
) -> None:
    project = _hoisted_repo(tmp_path)
    _no_forge(monkeypatch)
    (project / "package.json").write_text(
        '{"dependencies": {"@vault/core": "^1.0.0", "@widget/lib": "^1.0.0"}}'
    )

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@vault/core/") == str(tmp_path / "node_modules/@vault/core") + "/"
    assert _path_of(packages, "@widget/lib/") == str(project / "node_modules/@widget/lib") + "/"


def test_nearest_node_modules_wins_over_the_ancestor(tmp_path: Path, monkeypatch) -> None:
    # Node's own resolution order: the closest node_modules answers, even when an ancestor
    # also provides the package (routinely a different version).
    project = tmp_path / "smart-contracts"
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    (project / "node_modules" / "@vault" / "core").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "package.json").write_text('{"dependencies": {"@vault/core": "^1.0.0"}}')

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@vault/core/") == str(project / "node_modules/@vault/core") + "/"


def test_walk_stops_at_the_run_root(tmp_path: Path, monkeypatch) -> None:
    # certoraRun only uploads the run root's tree, so a package above it is unusable: the walk
    # must not reach it, and the base-dir target is emitted instead.
    project = tmp_path / "smart-contracts"
    project.mkdir()
    outside = tmp_path.parent / "node_modules" / "@vault" / "core"
    outside.mkdir(parents=True, exist_ok=True)
    _no_forge(monkeypatch)
    (project / "package.json").write_text('{"dependencies": {"@vault/core": "^1.0.0"}}')

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@vault/core/") == str(project / "node_modules/@vault/core") + "/"


def test_no_run_root_performs_no_walk(tmp_path: Path, monkeypatch) -> None:
    # Every caller that does not know the run root keeps the one-candidate behaviour.
    project = _hoisted_repo(tmp_path)
    _no_forge(monkeypatch)
    (project / "package.json").write_text('{"dependencies": {"@vault/core": "^1.0.0"}}')

    packages = build_packages_from_remapping_sources(base_dir=project, log_fn=lambda *_: None)

    assert _path_of(packages, "@vault/core/") == str(project / "node_modules/@vault/core") + "/"


def test_flat_project_packages_are_identical_with_and_without_run_root(
    tmp_path: Path, monkeypatch
) -> None:
    # The overwhelming majority of projects: base_dir == run_root, so the walk has exactly one
    # candidate and the packages list must come out byte-identical to the no-run-root result.
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    (tmp_path / "lib" / "widget").mkdir(parents=True)
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        "@vault/core/=node_modules/@vault/core/\n"
        "widget/=lib/widget/\n"
        "@absent/pkg/=node_modules/@absent/pkg/\n"
    )

    with_root = build_packages_from_remapping_sources(
        base_dir=tmp_path, log_fn=lambda *_: None, run_root=tmp_path
    )
    without_root = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    assert with_root == without_root
    assert _path_of(with_root, "@vault/core/") == str(tmp_path / "node_modules/@vault/core") + "/"


def test_existing_local_target_is_never_rewritten(tmp_path: Path, monkeypatch) -> None:
    # The walk can only change an entry whose target does not exist; a local install always wins.
    project = tmp_path / "smart-contracts"
    (project / "node_modules" / "@vault" / "core" / "contracts").mkdir(parents=True)
    (tmp_path / "node_modules" / "@vault" / "core" / "contracts").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@vault/=node_modules/@vault/core/contracts/\n")

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@vault/") == \
        str(project / "node_modules/@vault/core/contracts") + "/"


def test_hoist_resolution_is_independent_of_the_entry_source(tmp_path: Path, monkeypatch) -> None:
    # The resolution is a property of the target, not of which file the entry came from.
    project = _hoisted_repo(tmp_path)
    expected = str(tmp_path / "node_modules/@vault/core") + "/"
    _no_forge(monkeypatch)

    (project / "remappings.txt").write_text("@vault/core/=node_modules/@vault/core/\n")
    from_remappings_txt = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )
    (project / "remappings.txt").unlink()
    (project / "foundry.toml").write_text(
        '[profile.default]\nremappings = ["@vault/core/=node_modules/@vault/core/"]\n'
    )
    from_foundry_toml = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(from_remappings_txt, "@vault/core/") == expected
    assert _path_of(from_foundry_toml, "@vault/core/") == expected


def test_lib_target_is_never_walked(tmp_path: Path, monkeypatch) -> None:
    # forge and soldeer do not hoist, and a sibling project's lib/<name> is routinely a
    # different pin — walking those would silently bind the wrong version.
    project = tmp_path / "smart-contracts"
    project.mkdir()
    (tmp_path / "lib" / "oz").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@oz/=lib/oz/\n")

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@oz/") == str(project / "lib/oz") + "/"


def test_unscoped_package_name_is_one_segment(tmp_path: Path, monkeypatch) -> None:
    # A scoped name spans two segments (@scope/name), an unscoped one exactly one — splitting
    # wrongly would test the existence of the wrong directory.
    project = tmp_path / "smart-contracts"
    project.mkdir()
    (tmp_path / "node_modules" / "plainpkg" / "src").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("plainpkg/=node_modules/plainpkg/src/\n")

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "plainpkg/") == str(tmp_path / "node_modules/plainpkg/src") + "/"


def test_subpath_inside_a_hoisted_package_resolves(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "smart-contracts"
    project.mkdir()
    (tmp_path / "node_modules" / "@pkg" / "artifacts" / "src").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "foundry.toml").write_text(
        '[profile.default]\nremappings = ["@pkg/=node_modules/@pkg/artifacts/src/"]\n'
    )

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@pkg/") == str(tmp_path / "node_modules/@pkg/artifacts/src") + "/"


def test_ancestor_with_the_subpath_beats_a_nearer_package_without_it(
    tmp_path: Path, monkeypatch
) -> None:
    # Only the full target is usable by solc, so a nearer package that lacks the remapped
    # subdirectory is skipped in favour of an ancestor that has the whole thing.
    project = tmp_path / "smart-contracts"
    # The nearer install really is the package directory (`@pkg/artifacts`, two segments for a
    # scoped name) — it just lacks the remapped `src/`.
    (project / "node_modules" / "@pkg" / "artifacts").mkdir(parents=True)
    (tmp_path / "node_modules" / "@pkg" / "artifacts" / "src").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@pkg/=node_modules/@pkg/artifacts/src/\n")
    logged: list[tuple[str, str]] = []

    packages = build_packages_from_remapping_sources(
        base_dir=project,
        log_fn=lambda msg, level: logged.append((msg, level)),
        run_root=tmp_path,
    )

    assert _path_of(packages, "@pkg/") == str(tmp_path / "node_modules/@pkg/artifacts/src") + "/"
    assert not [m for m, level in logged if level == "WARNING"]
    assert any(level == "INFO" and "hoisted install" in m for m, level in logged)


def test_installed_package_without_the_subpath_is_reported_as_subpath_missing(
    tmp_path: Path, monkeypatch
) -> None:
    # No ancestor has the full target either, so the installed package is the best evidence
    # there is: the entry keeps naming its subdirectory and the log says the package is there
    # while the remapped subdirectory is not — a different remedy from "not installed".
    project = tmp_path / "smart-contracts"
    (project / "node_modules" / "@oz" / "contracts").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@oz/=node_modules/@oz/contracts/token/\n")
    logged: list[tuple[str, str]] = []

    resolution = resolve_node_modules_target(
        "node_modules/@oz/contracts/token", base_dir=project, run_root=tmp_path
    )
    packages = build_packages_from_remapping_sources(
        base_dir=project,
        log_fn=lambda msg, level: logged.append((msg, level)),
        run_root=tmp_path,
    )

    assert resolution.kind == "subpath_missing"
    assert resolution.package_dir == str(project / "node_modules/@oz/contracts")
    assert _path_of(packages, "@oz/") == str(project / "node_modules/@oz/contracts/token") + "/"
    warnings = [m for m, level in logged if level == "WARNING"]
    assert any("exists but the remapped subdirectory is missing" in m for m in warnings)


def test_ancestor_walk_never_leaves_the_run_root_through_a_symlinked_base_dir(
    tmp_path: Path,
) -> None:
    # A base_dir that reaches the run root through a symlink must not widen the walk: a package
    # found above the run root is outside the tree certoraRun uploads.
    run_root = tmp_path / "repo"
    (run_root / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    base_dir = run_root / "link"
    base_dir.symlink_to(run_root / "a" / "b" / "c", target_is_directory=True)

    resolution = resolve_node_modules_target(
        "node_modules/@vault/core", base_dir=base_dir, run_root=run_root
    )

    assert resolution.kind == "unresolved"
    assert all(str(tmp_path / "node_modules") not in candidate for candidate in resolution.searched)


def test_ancestor_walk_reaches_the_run_root_through_a_symlinked_base_dir(tmp_path: Path) -> None:
    # The converse: a base_dir textually deeper than it resolves must still be walked all the
    # way up to the run root, or a hoisted package there is silently missed.
    run_root = tmp_path / "repo"
    (run_root / "x").mkdir(parents=True)
    (run_root / "p" / "q").mkdir(parents=True)
    hoisted = run_root / "node_modules" / "@vault" / "core"
    hoisted.mkdir(parents=True)
    base_dir = run_root / "p" / "q" / "s"
    base_dir.symlink_to(run_root / "x", target_is_directory=True)

    resolution = resolve_node_modules_target(
        "node_modules/@vault/core", base_dir=base_dir, run_root=run_root
    )

    assert resolution.kind == "hoisted"
    assert resolution.path == str(hoisted)


def test_package_root_of_a_target_takes_the_last_node_modules(tmp_path: Path) -> None:
    # Scoped names span two segments, unscoped one, and a nested install is governed by the
    # innermost node_modules — the classifier tests exactly this directory for existence.
    assert node_modules_package_root("/r/node_modules/@oz/contracts/token") == \
        "/r/node_modules/@oz/contracts"
    assert node_modules_package_root("/r/node_modules/solady/src") == "/r/node_modules/solady"
    assert node_modules_package_root("/r/node_modules/a/node_modules/@b/c/src") == \
        "/r/node_modules/a/node_modules/@b/c"
    assert node_modules_package_root("/r/node_modules/@oz/contracts") == \
        "/r/node_modules/@oz/contracts"
    assert node_modules_package_root("/r/lib/openzeppelin/contracts") is None


def test_package_missing_everywhere_keeps_the_base_dir_target_and_warns(
    tmp_path: Path, monkeypatch
) -> None:
    # Dropping the entry would turn a precise `Source "…" not found` into a vaguer failure, or
    # let the bare import resolve against a same-named path in the project tree.
    project = tmp_path / "smart-contracts"
    project.mkdir()
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@vault/=node_modules/@vault/core/\n")
    logged: list[tuple[str, str]] = []

    packages = build_packages_from_remapping_sources(
        base_dir=project,
        log_fn=lambda msg, level: logged.append((msg, level)),
        run_root=tmp_path,
    )

    assert _path_of(packages, "@vault/") == str(project / "node_modules/@vault/core") + "/"
    warnings = [m for m, level in logged if level == "WARNING" and "does not exist" in m]
    assert warnings
    assert str(tmp_path / "node_modules/@vault/core") in warnings[0]


def test_prefixed_node_modules_target_is_left_untouched(tmp_path: Path, monkeypatch) -> None:
    # An explicit prefix before node_modules names a location the author chose; only a bare
    # node_modules/... target is the node-resolution idiom hoisting applies to.
    project = tmp_path / "smart-contracts"
    project.mkdir()
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    _no_forge(monkeypatch)
    (project / "remappings.txt").write_text("@vault/=packages/a/node_modules/@vault/core/\n")

    packages = build_packages_from_remapping_sources(
        base_dir=project, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@vault/") == \
        str(project / "packages/a/node_modules/@vault/core") + "/"


def test_parse_config_emits_run_root_relative_hoisted_packages(tmp_path: Path, monkeypatch) -> None:
    # End-to-end: a hoisted target stays inside the run root, so the conf keeps relative paths
    # (BuildSystemConfig._relativize_packages does a textual relative_to against the run root).
    project = _hoisted_repo(tmp_path)
    foundry_toml = project / "foundry.toml"
    foundry_toml.write_text(
        '[profile.default]\nsrc = "src"\nremappings = ["@vault/=node_modules/@vault/core/contracts/"]\n'
    )
    _no_forge(monkeypatch)
    # _relativize_packages relativizes against the process CWD, which in a run IS the run root.
    monkeypatch.chdir(tmp_path)

    manager = FoundryManager(project_root=project, scope=None, run_root=tmp_path)
    config = manager.parse_config(foundry_toml)
    packages = config.to_certora_dict()["packages"]

    # Relative (no `../`, no absolute fallback): the walk never leaves the run root.
    assert packages == ["@vault/=node_modules/@vault/core/contracts"]


# =============================================================================
# Several projects under one run root: context-scoped entries
# =============================================================================
#
# A run root can hold more than one built project, each declaring its own resolution for the
# same package prefix. solc matches a remapping context against the importing file's source
# unit name and prefers the longest match, so every project other than the anchor contributes
# `<project>/:prefix=target` entries that govern its own files, while the anchor's unscoped
# entries stay the default for every file no context claims.

_SAME_PREFIX_REMAPPING = "@pkg/contracts/=node_modules/@pkg/contracts/\n"


def _build_project(project: Path, remappings: str = _SAME_PREFIX_REMAPPING) -> Path:
    """A Foundry project that compiled: a config (which is how discovery recognises it), an
    artifact dir and its own installed copy of the remapped package.

    The copy is really created: an absent target keeps the base-dir path and only warns, so a
    forgotten mkdir would let a wrong result pass for a right one.
    """
    (project / "out").mkdir(parents=True, exist_ok=True)
    (project / "foundry.toml").write_text("[profile.default]\n")
    (project / "remappings.txt").write_text(remappings)
    (project / "node_modules" / "@pkg" / "contracts").mkdir(parents=True, exist_ok=True)
    return project


def _two_project_repo(tmp_path: Path) -> Tuple[Path, Path]:
    """Two built projects side by side under the run root, both binding @pkg/contracts/ to
    their own node_modules copy."""
    return _build_project(tmp_path / "app"), _build_project(tmp_path / "other")


def test_a_sibling_project_binds_the_shared_prefix_to_its_own_copy(
    tmp_path: Path, monkeypatch
) -> None:
    app, other = _two_project_repo(tmp_path)
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _path_of(packages, "@pkg/contracts/") == str(app / "node_modules/@pkg/contracts") + "/"
    assert (
        _path_of(packages, "other/:@pkg/contracts/")
        == str(other / "node_modules/@pkg/contracts") + "/"
    )


def test_a_sibling_target_that_is_absent_leaves_the_prefix_alone(
    tmp_path: Path, monkeypatch
) -> None:
    # A scoped entry outranks the global one for every file under it, so a target already known
    # to be missing must not replace a binding that may work.
    app, other = _two_project_repo(tmp_path)
    shutil.rmtree(other / "node_modules")
    _no_forge(monkeypatch)
    logged: list = []

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda m, level: logged.append((m, level)), run_root=tmp_path
    )
    anchor_only = build_packages_from_remapping_sources(base_dir=app, log_fn=lambda *_: None)

    assert not [key for key in _keys(packages) if key.startswith("other/:")]
    assert packages == anchor_only
    warnings = [m for m, level in logged if level == "WARNING"]
    assert any(
        "@pkg/contracts/" in m
        and str(other / "node_modules/@pkg/contracts") in m
        and str(app / "node_modules/@pkg/contracts") in m
        for m in warnings
    ), warnings


def test_a_projects_own_context_is_not_prefixed_twice(tmp_path: Path, monkeypatch) -> None:
    # This project's authored context names a directory it really has, so `_rebase_context`
    # expresses it run-root-relative and it is taken as it stands; prefixing it again would
    # name nothing on disk.
    app, other = _two_project_repo(tmp_path)
    (other / "node_modules" / "dep").mkdir(parents=True)
    (other / "node_modules" / "@pkg" / "alt").mkdir(parents=True)
    (other / "remappings.txt").write_text(
        "node_modules/dep/:@pkg/contracts/=node_modules/@pkg/alt/\n"
    )
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert (
        _path_of(packages, "other/node_modules/dep/:@pkg/contracts/")
        == str(other / "node_modules/@pkg/alt") + "/"
    )
    assert not [key for key in _keys(packages) if key.count(":") > 1]


def test_a_sibling_context_naming_nothing_is_confined_to_the_sibling(
    tmp_path: Path, monkeypatch
) -> None:
    # `_rebase_context` leaves a context alone when it names no directory in the project, so
    # the authored spelling reaches here as it stands. Emitted verbatim it would be longer than
    # the anchor's empty context and would govern every file whose source unit name starts with
    # it — the anchor's own files included.
    # The anchor sits at the run root, so its own source unit names start with `vendored/` —
    # the very prefix the sibling's context spells.
    app = _build_project(tmp_path)
    other = _build_project(tmp_path / "other")
    (app / "vendored").mkdir()
    (other / "node_modules" / "@pkg" / "alt").mkdir(parents=True)
    (other / "remappings.txt").write_text("vendored/:@pkg/contracts/=node_modules/@pkg/alt/\n")
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert (
        _path_of(packages, "other/vendored/:@pkg/contracts/")
        == str(other / "node_modules/@pkg/alt") + "/"
    )
    assert _keys(packages) == {"@pkg/contracts/", "other/vendored/:@pkg/contracts/"}


def test_the_scoped_context_keeps_its_boundary_slash(tmp_path: Path, monkeypatch) -> None:
    # The context is a source-unit-name prefix whose boundary is the slash: without it,
    # `other` would also claim the files of a sibling named `other-tools`.
    app, _other = _two_project_repo(tmp_path)
    _build_project(tmp_path / "other-tools")
    _no_forge(monkeypatch)

    keys = _keys(
        build_packages_from_remapping_sources(
            base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
        )
    )

    assert "other/:@pkg/contracts/" in keys
    assert "other:@pkg/contracts/" not in keys
    assert "other-tools/:@pkg/contracts/" in keys


def test_single_project_packages_are_identical_with_and_without_run_root(
    tmp_path: Path, monkeypatch
) -> None:
    # The ordinary repo: one project at the run root with its vendored trees. Nothing else is
    # built under it, so the list must come out byte-identical to the no-run-root result and
    # carry no context at all.
    _build_project(tmp_path)
    (tmp_path / "lib" / "widget").mkdir(parents=True)
    _no_forge(monkeypatch)
    (tmp_path / "remappings.txt").write_text(
        _SAME_PREFIX_REMAPPING + "widget/=lib/widget/\n@absent/pkg/=node_modules/@absent/pkg/\n"
    )

    with_root = build_packages_from_remapping_sources(
        base_dir=tmp_path, log_fn=lambda *_: None, run_root=tmp_path
    )
    without_root = build_packages_from_remapping_sources(base_dir=tmp_path, log_fn=lambda *_: None)

    assert with_root == without_root
    assert not [key for key in _keys(with_root) if ":" in key]


def test_a_sibling_whose_build_failed_is_still_scoped(tmp_path: Path, monkeypatch) -> None:
    # A project whose own build failed has no artifact directory, and its resolution is
    # precisely what the failure is about — a config declares the resolution whether or not
    # anything came of it.
    app, _other = _two_project_repo(tmp_path)
    unbuilt = _build_project(tmp_path / "unbuilt")
    shutil.rmtree(unbuilt / "out")
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert (
        _path_of(packages, "unbuilt/:@pkg/contracts/")
        == str(unbuilt / "node_modules/@pkg/contracts") + "/"
    )


def test_projects_inside_vendored_dependencies_are_never_walked(
    tmp_path: Path, monkeypatch
) -> None:
    # A built project under lib/ or node_modules/ belongs to a dependency, not to the repo
    # under analysis, and its files are not the ones the conf verifies.
    app, _other = _two_project_repo(tmp_path)
    _build_project(app / "lib" / "dep")
    _build_project(tmp_path / "node_modules" / "pkg")
    _no_forge(monkeypatch)

    keys = _keys(
        build_packages_from_remapping_sources(
            base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
        )
    )

    assert not [key for key in keys if "lib/dep" in key or "node_modules/pkg" in key]


def test_a_project_nested_under_the_anchor_is_scoped(tmp_path: Path, monkeypatch) -> None:
    # Its context is longer than the anchor's globals, hence more specific — which is exactly
    # what solc should prefer for its own files.
    app, _other = _two_project_repo(tmp_path)
    nested = _build_project(app / "modules" / "inner")
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert (
        _path_of(packages, "app/modules/inner/:@pkg/contracts/")
        == str(nested / "node_modules/@pkg/contracts") + "/"
    )


def test_the_run_root_and_the_anchors_ancestors_are_never_scoped(
    tmp_path: Path, monkeypatch
) -> None:
    # An ancestor's context prefixes the anchor's own source unit names too, and being longer
    # than the empty context it would outrank the anchor's globals for exactly those files.
    app = tmp_path / "packages" / "app"
    for project in (tmp_path, tmp_path / "packages", app):
        _build_project(project)
    _no_forge(monkeypatch)

    packages = build_packages_from_remapping_sources(
        base_dir=app, log_fn=lambda *_: None, run_root=tmp_path
    )

    assert _keys(packages) == {"@pkg/contracts/"}
    assert _path_of(packages, "@pkg/contracts/") == str(app / "node_modules/@pkg/contracts") + "/"
