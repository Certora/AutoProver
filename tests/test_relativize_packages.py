"""Conf ``packages`` paths inside the project root are relative to it.

Prover runs execute in temporary copies of the project. An absolute package path into the
original tree makes such a run compile the original files instead of the copy's (and for
ERC-7201 namespaced storage the prover's build then fails on a doubled import path), while a
path outside the project root has to stay absolute, as the copy does not contain it.
"""

from pathlib import Path

from certora_autosetup.utils.paths import relativize_packages
from composer.spec.source.autosetup import project_relative_packages


def test_absolute_paths_inside_the_root_become_relative(tmp_path: Path) -> None:
    packages = [
        f"@oz/={tmp_path}/lib/oz/contracts/",
        f"other/:@pkg/={tmp_path}/other/node_modules/@pkg/",
    ]

    assert relativize_packages(packages, tmp_path) == [
        "@oz/=lib/oz/contracts",
        "other/:@pkg/=other/node_modules/@pkg",
    ]


def test_relative_paths_and_entries_without_a_path_are_kept(tmp_path: Path) -> None:
    packages = ["@oz/=lib/oz/contracts/", "not-a-mapping"]

    assert relativize_packages(packages, tmp_path) == packages


def test_absolute_paths_outside_the_root_are_kept_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = f"@ext/={tmp_path}/shared/ext/"
    logged: list[tuple[str, str]] = []

    result = relativize_packages([outside], root, lambda msg, level: logged.append((msg, level)))

    assert result == [outside]
    assert [level for _, level in logged] == ["WARNING"]


def test_containment_is_by_path_component_not_string_prefix(tmp_path: Path) -> None:
    root = tmp_path / "project"
    sibling = f"@x/={tmp_path}/project-other/x/"

    assert relativize_packages([sibling], root) == [sibling]


def test_the_autosetup_conf_reaches_composer_with_project_relative_packages(tmp_path: Path) -> None:
    conf = {
        "files": ["src/A.sol"],
        "packages": [f"@oz/={tmp_path}/lib/oz/contracts/", f"@ext/=/elsewhere/ext/"],
    }

    result = project_relative_packages(conf, tmp_path)

    assert result["packages"] == ["@oz/=lib/oz/contracts", "@ext/=/elsewhere/ext/"]
    assert result["files"] == conf["files"]
    assert conf["packages"][0] == f"@oz/={tmp_path}/lib/oz/contracts/", "the conf read from disk is not mutated"


def test_a_conf_without_packages_is_unchanged(tmp_path: Path) -> None:
    conf = {"files": ["src/A.sol"]}

    assert project_relative_packages(conf, tmp_path) == conf
