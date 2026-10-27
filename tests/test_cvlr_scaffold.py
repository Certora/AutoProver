"""What the scaffold writes, what it refuses, and what it leaves alone.

Three failures are quiet. A second run that appends to ``Cargo.toml`` again leaves a manifest
cargo will not parse, and a scaffold gets re-run when nobody is sure it ran. A text edit of a
parsed manifest has to stay valid TOML, because reserializing would rewrite the project's
comments, so every case that touches a manifest re-parses it. A blocked plan has to apply
nothing: a half-scaffolded project makes the next build failure have two causes.

No cargo and no network. The workspace objects are built in the test.
"""

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from composer.cargo.manifest import AddEntries, AddTable, ManifestAddition
from composer.cargo.metadata import (
    CargoFailed,
    CargoMetadataJson,
    CratePackage,
    GitSource,
    LibTarget,
    PackageSource,
    MetadataFailure,
    RegistrySource,
    Workspace,
    parse_metadata,
)
from composer.spec.cvlr import preflight, scaffold, tuning
from composer.spec.cvlr.scaffold import (
    HARNESS_DIR,
    EditManifest,
    ScaffoldBlocked,
    Write,
    apply,
    plan_scaffold,
)
from composer.spec.cvlr.tuning import ENV_FAMILIES, INLINING, SUMMARIES
from composer.spec.cvlr.reference import SOLANA_V1_18, SOLANA_V2, SOLANA_V3, SOROBAN, generations
from composer.sandbox.config import SandboxConfig

PROGRAM = """\
use solana_program::account_info::AccountInfo;

pub fn process(_accounts: &[AccountInfo]) {}
"""

_UNCONFINED = SandboxConfig(provider="none")


def _project(
    root: Path,
    *,
    manifest: str,
    workspace_manifest: str | None = None,
    package_dir: str = "",
    crate_types: tuple[str, ...] = ("cdylib",),
    platform: str | None = "2.2.1",
    platform_crate: str = "solana-program",
    cvlr_resolved: dict[str, str] | None = None,
    sources: dict[str, PackageSource | None] | None = None,
) -> tuple[Workspace, CratePackage]:
    """A project on disk plus the ``Workspace`` cargo would report for it.

    ``platform`` and ``cvlr_resolved`` stand in for the resolved graph, which is what the platform
    gate and the version-gap report read; the manifests on disk are what the planner parses.
    ``sources`` gives a crate in ``cvlr_resolved`` a source other than crates.io; ``None`` is a
    path in the workspace, as cargo reports one.

    ``platform_crate`` is a parameter because which crate carries ``AccountInfo`` is itself a fact
    about the generation: Solana's v3 split moved it out of ``solana-program`` and stopped
    publishing that crate, so a target on the newest line resolves a platform crate the older line
    has never heard of.
    """
    package_root = root / package_dir if package_dir else root
    # Write-if-absent, so calling this a second time reports the project as the scaffold left it
    # rather than as it started. A helper that clobbered `lib.rs` would make the idempotence test
    # pass or fail for a reason that has nothing to do with the scaffold.
    (package_root / "src").mkdir(parents=True, exist_ok=True)
    for path, contents in (
        (package_root / "Cargo.toml", manifest),
        (package_root / "src" / "lib.rs", PROGRAM),
        *(((root / "Cargo.toml", workspace_manifest),) if workspace_manifest is not None else ()),
    ):
        if not path.exists():
            path.write_text(contents)

    package = CratePackage(
        name="prog",
        version="0.1.0",
        manifest_path=package_root / "Cargo.toml",
        lib=LibTarget(
            name="prog", src_path=package_root / "src" / "lib.rs", crate_types=crate_types
        ),
        features=("no-entrypoint",) if "no-entrypoint" in manifest else (),
        source=None,
    )
    resolved = [package]
    if platform is not None:
        resolved.append(
            CratePackage(
                name=platform_crate,
                version=platform,
                manifest_path=root / "vendor" / platform_crate / "Cargo.toml",
                lib=None,
                features=(),
                source=RegistrySource("registry+https://github.com/rust-lang/crates.io-index"),
            )
        )
    for name, version in (cvlr_resolved or {}).items():
        resolved.append(
            CratePackage(
                name=name,
                version=version,
                manifest_path=root / "vendor" / name / "Cargo.toml",
                lib=None,
                features=(),
                source=(sources or {})[name]
                if name in (sources or {})
                else RegistrySource("registry+https://github.com/rust-lang/crates.io-index"),
            )
        )
    workspace = Workspace(
        root=root,
        target_directory=root / "target",
        members=(package,),
        packages=tuple(resolved),
        links={package: tuple(resolved[1:])},
    )
    return workspace, package


STANDALONE = """\
[package]
name = "prog"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
solana-program = "2.2"
"""

WITH_FEATURES = """\
[package]
name = "prog"
version = "0.1.0"

[lib]
crate-type = ["cdylib"]

# The project's own comment, which a reserializing writer would move or drop.
[features]
no-entrypoint = []

[dependencies]
solana-program = "2.2"
"""

WORKSPACE_ROOT = """\
[workspace]
members = ["programs/prog"]
resolver = "2"

[workspace.dependencies]
solana-program = "2.2"
"""


def _plan(root: Path, **kwargs):
    workspace, package = _project(root, **kwargs)
    return plan_scaffold(workspace, package, SOLANA_V2), workspace


def _additions(plan, path: Path = Path("Cargo.toml")) -> list[ManifestAddition]:
    """What ``plan`` adds to the manifest at ``path``, in order."""
    return [
        a.edit
        for c in plan.changes
        if isinstance(c, EditManifest) and c.path == path
        for a in c.additions
    ]


def test_a_fresh_project_gets_the_whole_shape_and_a_second_run_gets_nothing(tmp_path):
    # Idempotence is the property, and it has to hold through *apply*, not just through planning:
    # the second plan is computed against the files the first one wrote.
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert not plan.blocked
    touched = apply(plan, workspace.root)
    assert touched

    again, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert again.changes == ()
    assert again.satisfied


#: ``[package]`` split by another table, as solana-program-stake's program manifest has it. tomlkit
#: reads such a table back as a proxy over its fragments rather than as a table.
SPLIT_PACKAGE = STANDALONE + """
[lints]
workspace = true

[package.metadata.solana]
program-id = "Prog111111111111111111111111111111111111111"
"""


def test_a_package_table_split_by_another_table_is_scaffolded_and_idempotent(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=SPLIT_PACKAGE, workspace_manifest=SPLIT_PACKAGE)
    assert not plan.blocked
    apply(plan, workspace.root)
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert "certora" in parsed["package"]["metadata"]
    assert parsed["package"]["metadata"]["solana"]["program-id"].startswith("Prog")

    again, _ = _plan(tmp_path, manifest=SPLIT_PACKAGE, workspace_manifest=SPLIT_PACKAGE)
    assert again.changes == ()


def test_the_manifest_a_fresh_project_ends_up_with_still_parses(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())

    assert parsed["features"]["certora"] == [
        "dep:cvlr", "dep:cvlr-solana", "dep:cvlr-solana-stake", "dep:cvlr-spl-token",
    ]
    # Optional is what keeps CVLR out of a release build, and what makes `dep:` legal above.
    assert parsed["dependencies"]["cvlr"] == {"version": "=0.6.2", "optional": True}
    metadata = parsed["package"]["metadata"]["certora"]
    assert metadata["sources"] == ["Cargo.toml", "src/**/*.rs"]
    assert metadata["solana_inlining"] == ["src/certora/envs/cvlr_inlining.txt"]


def test_the_path_a_mock_names_resolves_from_the_programs_own_file(tmp_path):
    """``cvlr::mock_fn(with = crate::certora::specs::...)`` expands in the program's own source,
    outside ``certora``. The path from there has to be ``pub``. ``pub`` inside a private
    ``mod certora`` resolves it without adding to the crate's public API. This checks the
    scaffold's ``certora/mod.rs``.
    """
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    root = (tmp_path / HARNESS_DIR / "mod.rs").read_text()
    assert "pub mod specs;" in root


def test_a_feature_table_that_exists_is_edited_rather_than_reopened(tmp_path):
    # Appending `[features]` to a manifest that has one is a duplicate-table error, so this is the
    # one change that cannot be an append — and the project's comment must survive it.
    plan, workspace = _plan(tmp_path, manifest=WITH_FEATURES, workspace_manifest=WITH_FEATURES)
    assert any(isinstance(c, EditManifest) for c in plan.changes)
    apply(plan, workspace.root)

    text = (tmp_path / "Cargo.toml").read_text()
    assert text.count("[features]") == 1
    assert "The project's own comment" in text
    parsed = tomllib.loads(text)
    assert parsed["features"]["no-entrypoint"] == []
    # no-entrypoint is enabled only because this package declares it.
    assert parsed["features"]["certora"] == [
        "no-entrypoint", "dep:cvlr", "dep:cvlr-solana", "dep:cvlr-solana-stake",
        "dep:cvlr-spl-token",
    ]


def test_a_package_with_no_entrypoint_feature_does_not_get_one_invented(tmp_path):
    plan, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    (features,) = [a for a in _additions(plan) if a.table == ("features",)]
    assert isinstance(features, AddEntries)
    assert "no-entrypoint" not in dict(features.entries)["certora"]
    # Nor a heap switch: this crate has no entrypoint whose allocator would need one.
    assert "custom-heap" not in dict(features.entries)


ENTRYPOINT = """\
use solana_program_entrypoint::{entrypoint, ProgramResult};

entrypoint!(process_instruction);
"""


def _features(plan) -> dict:
    (features,) = [a for a in _additions(plan) if a.table == ("features",)]
    assert isinstance(features, AddEntries)
    return dict(features.entries)


def test_a_program_that_cannot_turn_its_entrypoint_off_turns_its_allocator_off(tmp_path):
    """Stake's program calls `entrypoint!` unconditionally and declares no features. Its bump
    allocator then shares the heap with the harness's `Box`, and every rule that allocates fails
    with Prover error 3309."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "entrypoint.rs").write_text(ENTRYPOINT)
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    features = _features(plan)
    assert features["custom-heap"] == []
    assert features["certora"][0] == "custom-heap"

    apply(plan, workspace.root)
    manifest = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert "custom-heap" in manifest["features"]["certora"]
    again, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert again.changes == ()


def test_a_heap_switch_the_package_already_declares_is_enabled_not_redeclared(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "entrypoint.rs").write_text(ENTRYPOINT)
    manifest = STANDALONE + "\n[features]\ncustom-heap = []\n"
    plan, _ = _plan(tmp_path, manifest=manifest, workspace_manifest=manifest)
    features = _features(plan)
    assert "custom-heap" not in features
    assert features["certora"][0] == "custom-heap"


def test_no_entrypoint_is_the_switch_when_the_package_has_it(tmp_path):
    """It removes the entrypoint and its allocator both, and it is the convention the program's
    authors chose."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "entrypoint.rs").write_text(ENTRYPOINT)
    plan, _ = _plan(tmp_path, manifest=WITH_FEATURES, workspace_manifest=WITH_FEATURES)
    features = _features(plan)
    assert features["certora"][0] == "no-entrypoint"
    assert "custom-heap" not in features["certora"]


def test_a_workspace_gets_the_pins_and_its_member_inherits_them(tmp_path):
    plan, workspace = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=WORKSPACE_ROOT,
        package_dir="programs/prog",
    )
    apply(plan, workspace.root)

    root = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert root["workspace"]["dependencies"]["cvlr"] == {"version": "=0.6.2"}
    member = tomllib.loads((tmp_path / "programs" / "prog" / "Cargo.toml").read_text())
    assert member["dependencies"]["cvlr"] == {"workspace": True, "optional": True}


def test_a_package_that_builds_no_loadable_object_is_refused_rather_than_patched(tmp_path):
    # Adding cdylib to somebody's library changes how it builds everywhere, so it is a decision for
    # a human. The refusal has to stop the whole plan, not just that one change.
    plan, workspace = _plan(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, crate_types=("lib",)
    )
    assert [b.problem for b in plan.blocked] == [
        "prog builds no cdylib, so there is no program for the prover to read"
    ]
    with pytest.raises(ScaffoldBlocked):
        apply(plan, workspace.root)
    assert not (tmp_path / "src" / "certora").exists()


def test_a_library_source_that_does_not_exist_is_refused_rather_than_created(tmp_path):
    # cargo metadata reports `[lib] path = "src/missing.rs"` without checking it. Appending the
    # harness declaration there would create a lib with no program in it, and it would build.
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert package.lib is not None
    missing = replace(package, lib=replace(package.lib, src_path=tmp_path / "src" / "missing.rs"))
    plan = plan_scaffold(workspace, missing, SOLANA_V2)
    assert [b.problem for b in plan.blocked] == [
        "prog's library source, src/missing.rs, does not exist, so there is no crate to add the "
        "harness to"
    ]
    with pytest.raises(ScaffoldBlocked):
        apply(plan, workspace.root)
    assert not (tmp_path / "src" / "missing.rs").exists()


def test_a_certora_feature_that_means_something_else_is_refused(tmp_path):
    # A project can legitimately have a feature by that name; extending it would change what their
    # build does. Distinguishable from an already-set-up project only by whether CVLR is a dep.
    manifest = STANDALONE.replace(
        "[dependencies]", '[features]\ncertora = ["some-other-thing"]\n\n[dependencies]'
    )
    plan, _ = _plan(tmp_path, manifest=manifest, workspace_manifest=manifest)
    assert any("means something else" in b.problem for b in plan.blocked)


def test_a_project_already_set_up_is_read_rather_than_refused(tmp_path):
    # The same feature name, but with CVLR present: this is a verified project, and the answer is
    # "nothing to do" rather than a refusal.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program",
        '[features]\ncertora = ["dep:cvlr"]\n\n[dependencies]\ncvlr = "0.6.2"\nsolana-program',
    )
    plan, _ = _plan(tmp_path, manifest=manifest, workspace_manifest=manifest)
    assert not plan.blocked
    assert any("already exists" in note for note in plan.satisfied)


def test_a_platform_generation_the_reference_set_cannot_be_paired_with_is_refused(tmp_path):
    # cvlr-solana 0.5.0 is bound to solana-program 2.x, and 1.18's AccountInfo is a different type,
    # so this pairing does not warn — it fails to compile. Caught before writing the pin.
    plan, _ = _plan(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, platform="1.18.26"
    )
    assert any("1.18.26" in b.problem for b in plan.blocked)


def test_a_platform_newer_than_the_reference_set_is_refused_though_it_deleted_the_probe(tmp_path):
    # Solana v3 moved AccountInfo into solana-account-info and stopped publishing solana-program,
    # so a v3 target resolves no solana-program. A gate that only asked about that crate would
    # treat the absence as no opinion and pin CVLR 0.5 against it. The scaffold would still
    # compile. The mismatch shows up on the first rule that passes an account to a CVLR helper,
    # as two different AccountInfo types.
    plan, _ = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=STANDALONE,
        platform="3.1.1",
        platform_crate="solana-account-info",
    )
    assert any("3.1.1" in b.problem for b in plan.blocked)


def test_a_project_on_the_reference_generation_passes_on_the_specific_witness(tmp_path):
    # A 2.x target resolves solana-account-info too, and that witness is first. A match stops
    # the check. Continuing on to the next witness would judge the project by a broader crate.
    plan, _ = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=STANDALONE,
        platform="2.3.0",
        platform_crate="solana-account-info",
    )
    assert not plan.blocked


def test_an_older_platform_copy_beside_the_reference_one_is_refused(tmp_path):
    # Both copies build. The old one is what the dependents that pulled it in hand to CVLR.
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    (current,) = workspace.resolved("solana-program")
    older = replace(current, version="1.18.26")
    workspace = replace(
        workspace,
        packages=(*workspace.packages, older),
        links={package: (*workspace.links[package], older)},
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    assert any("1.18.26" in b.problem for b in plan.blocked)


def test_a_platform_copy_the_program_does_not_link_is_not_refused(tmp_path):
    # solana-program-stake: the program is on 2.x, and a sibling `interface` member on 3.x pulls
    # solana-account-info 3.0.0 into the lockfile through its dev-dependencies. That copy is never
    # compiled with the harness.
    workspace, package = _project(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=STANDALONE,
        platform="2.2.1",
        platform_crate="solana-account-info",
    )
    (current,) = workspace.resolved("solana-account-info")
    sibling = replace(package, name="interface", manifest_path=tmp_path / "interface" / "Cargo.toml")
    newer = replace(current, version="3.0.0")
    workspace = replace(
        workspace,
        members=(package, sibling),
        packages=(*workspace.packages, sibling, newer),
        links={**workspace.links, sibling: (newer,)},
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    assert not plan.blocked


ON_AN_OLDER_LINE = STANDALONE.replace(
    "[dependencies]\nsolana-program",
    '[dependencies]\ncvlr = "0.4"\ncvlr-solana = "0.4"\nsolana-program',
)


def test_a_project_on_a_different_cvlr_line_is_refused(tmp_path):
    # One CVLR line is supported at a time, and everything the scaffold writes is that line's.
    # It used to defer to a pin like this, withhold the specializations that would collide, and
    # report the disagreement for someone to read later; now it stops.
    plan, _ = _plan(
        tmp_path,
        manifest=ON_AN_OLDER_LINE,
        workspace_manifest=ON_AN_OLDER_LINE,
        cvlr_resolved={"cvlr": "0.4.1", "cvlr-solana": "0.4.5"},
    )
    assert any("0.4.1" in b.problem for b in plan.blocked)
    assert any("0.4.5" in b.problem for b in plan.blocked)


def test_a_refused_pin_stops_the_plan_before_a_specialization_is_written(tmp_path):
    """The refusal has to arrive before the changes, not alongside them.

    A project on the 0.4 line given a 0.5.0 specialization gets two generations of ``AccountInfo``
    in one graph. That used to be avoided by withholding the specializations from such a project;
    now the project is refused outright, and :func:`apply` is what must never run.
    """
    plan, workspace = _plan(
        tmp_path,
        manifest=ON_AN_OLDER_LINE,
        workspace_manifest=ON_AN_OLDER_LINE,
        cvlr_resolved={"cvlr": "0.4.1", "cvlr-solana": "0.4.5"},
    )
    assert plan.blocked
    with pytest.raises(ScaffoldBlocked):
        apply(plan, workspace.root)
    assert 'cvlr = "0.4"' in (tmp_path / "Cargo.toml").read_text()


def test_a_cvlr_pin_no_member_depends_on_yet_is_still_refused(tmp_path):
    # Declared in [workspace.dependencies] and used by nobody, so cargo has not resolved it and
    # the graph says nothing. The scaffold is about to make this member inherit it, which is
    # exactly when reading the manifest instead of the graph is the only way to see it.
    root = WORKSPACE_ROOT.replace(
        "[workspace.dependencies]", '[workspace.dependencies]\ncvlr = "0.4.1"'
    )
    plan, _ = _plan(
        tmp_path, manifest=STANDALONE, workspace_manifest=root, package_dir="programs/prog"
    )
    assert any("0.4.1" in b.problem for b in plan.blocked)


def test_a_cvlr_dependency_with_no_readable_version_is_refused(tmp_path):
    # A git checkout could be any release. A gate cannot pass a version it cannot see, and
    # guessing that a checkout is the pinned one is the mistake the pin exists to prevent.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program",
        '[dependencies]\ncvlr = { git = "https://github.com/Certora/cvlr" }\nsolana-program',
    )
    plan, _ = _plan(tmp_path, manifest=manifest, workspace_manifest=manifest)
    assert any("git dependency" in b.problem for b in plan.blocked)


@pytest.mark.parametrize(
    "manifest, overrides",
    [
        pytest.param(STANDALONE, {"platform": "1.18.26"}, id="platform"),
        pytest.param(
            ON_AN_OLDER_LINE,
            {"cvlr_resolved": {"cvlr": "0.4.1", "cvlr-solana": "0.4.5"}},
            id="pin",
        ),
        pytest.param(
            STANDALONE.replace(
                "[dependencies]\nsolana-program",
                '[dependencies]\ncvlr = { git = "https://github.com/Certora/cvlr" }\n'
                "solana-program",
            ),
            {},
            id="unpinned",
        ),
    ],
)
def test_a_line_refusal_is_put_in_terms_the_project_author_can_act_on(
    tmp_path, manifest, overrides
):
    """The reader is whoever owns the project. AutoProver's own source is not theirs to edit, so
    a refusal names the release this build supports and what to change in the project."""
    plan, _ = _plan(tmp_path, manifest=manifest, workspace_manifest=manifest, **overrides)
    assert plan.blocked
    for b in plan.blocked:
        text = f"{b.problem} {b.resolution}"
        assert "composer/" not in text and "reference set" not in text, text
        names_a_release = any(f"{c.name} {c.version}" in b.resolution for c in SOLANA_V2.crates())
        names_a_platform = any(
            f"{w.name} {w.line}" in b.resolution for w in SOLANA_V2.platform.witnesses
        )
        assert names_a_release or names_a_platform, b.resolution


def test_a_project_already_on_the_pinned_release_is_not_refused(tmp_path):
    # The idempotence case, and the one the gate must not catch: `=0.6.2` is what the scaffold
    # itself writes, and a bare `0.6.2` is the same release written by hand.
    for requirement in ("=0.6.2", "0.6.2"):
        root = tmp_path / requirement
        manifest = STANDALONE.replace(
            "[dependencies]\nsolana-program",
            f'[dependencies]\ncvlr = "{requirement}"\nsolana-program',
        )
        plan, _ = _plan(
            root,
            manifest=manifest,
            workspace_manifest=manifest,
            cvlr_resolved={"cvlr": "0.6.2"},
        )
        assert not plan.blocked, requirement


def test_a_reference_set_crate_the_project_does_not_name_is_not_a_refusal(tmp_path):
    # Absent, not mismatched, and the reason the two are separate types. This project is on the
    # pinned core and chain crate and names neither specialization; the scaffold adds them. A gate
    # that read "not in the graph" as a disagreement would refuse every project it is meant to set
    # up, starting with the fresh one.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program",
        '[dependencies]\ncvlr = "=0.6.2"\ncvlr-solana = "=0.5.1"\nsolana-program',
    )
    plan, _ = _plan(
        tmp_path,
        manifest=manifest,
        workspace_manifest=manifest,
        cvlr_resolved={"cvlr": "0.6.2", "cvlr-solana": "0.5.1"},
    )
    assert not plan.blocked
    (dependencies,) = [a for a in _additions(plan) if a.table == ("dependencies",)]
    assert isinstance(dependencies, AddEntries)
    assert [key for key, _ in dependencies.entries] == ["cvlr-solana-stake", "cvlr-spl-token"]


def test_a_partly_pinned_project_is_still_checked(tmp_path):
    # One CVLR crate on an old line and one absent: the platform gate has to fire too, because
    # the scaffold would write the reference version for the missing one.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program", '[dependencies]\ncvlr = "0.4"\nsolana-program'
    )
    plan, _ = _plan(
        tmp_path,
        manifest=manifest,
        workspace_manifest=manifest,
        platform="1.18.26",
        cvlr_resolved={"cvlr": "0.4.1"},
    )
    assert any("1.18.26" in b.problem for b in plan.blocked)
    assert any("0.4.1" in b.problem for b in plan.blocked)


def test_an_existing_harness_declaration_is_not_added_twice(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE
    )
    assert package.lib is not None
    package.lib.src_path.write_text(PROGRAM + "\npub mod certora;\n")
    plan = plan_scaffold(workspace, package, SOLANA_V2)

    assert not any(c.path.name == "lib.rs" for c in plan.changes)
    assert any("already declares the harness module" in note for note in plan.satisfied)


def test_the_composite_env_file_carries_every_starting_layer_and_says_what_it_covers(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    composite = (tmp_path / "src" / "certora" / "envs" / INLINING.composite).read_text()
    # The layer files are not in the target, so the composite describes each one by what it covers
    # rather than naming a file the reader cannot open.
    for layer, name in zip(INLINING.layers, INLINING.starting, strict=True):
        assert layer.covers in composite
        assert name not in composite
        marker = tuning.starting_env(name).strip().splitlines()[-1]
        assert marker in composite


def test_only_the_generated_files_land_in_the_target(tmp_path):
    """The starting layers stay in AutoProver. A copy in the target would be one nothing reads."""
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    envs = tmp_path / "src" / "certora" / "envs"
    assert {p.name for p in envs.iterdir()} == {family.composite for family in ENV_FAMILIES}


def test_an_edited_harness_file_is_replaced_and_nothing_else_is_written(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    specs = tmp_path / HARNESS_DIR / "specs" / "mod.rs"
    scaffolded = specs.read_text()
    specs.write_text("hand written\n")

    again, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert [(type(c), c.path) for c in again.changes] == [(Write, HARNESS_DIR / "specs" / "mod.rs")]
    apply(again, workspace.root)
    assert specs.read_text() == scaffolded


def test_a_generated_file_that_differs_from_the_starting_configuration_is_rewritten(tmp_path):
    """What a project scaffolded by an older AutoProver looks like: the composite on disk is not
    what today's starting configuration composes to."""
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    composite = tmp_path / "src" / "certora" / "envs" / SUMMARIES.composite
    composite.write_text(";;; composed by an older starting configuration\n")

    again, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert [type(c) for c in again.changes] == [Write]
    apply(again, workspace.root)
    assert composite.read_text() == tuning.compose_env(SUMMARIES, dialect=again.dialect)


def test_gitignore_gains_only_what_is_missing(tmp_path):
    (tmp_path / ".gitignore").write_text("target/\n.certora_internal\n")
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    text = (tmp_path / ".gitignore").read_text()
    assert text.count(".certora_internal") == 1
    assert ".certora\n" in text and "certora_out" in text


def test_a_missing_gitignore_is_created(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    assert (tmp_path / ".gitignore").read_text().splitlines() == [
        "# Certora Prover build output", *scaffold.GITIGNORE_LINES
    ]


def test_the_projects_manifest_keeps_every_line_it_had(tmp_path):
    # The manifest is edited as a document, not reserialized: every line the project wrote is
    # still there, in its order, with the additions between them.
    plan, workspace = _plan(tmp_path, manifest=WITH_FEATURES, workspace_manifest=WITH_FEATURES)
    apply(plan, workspace.root)
    edited = iter((tmp_path / "Cargo.toml").read_text().splitlines())
    assert all(line in edited for line in WITH_FEATURES.splitlines())


def test_a_manifest_edited_after_planning_keeps_the_edit(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    (tmp_path / "Cargo.toml").write_text(STANDALONE + "\n# edited meanwhile\n")
    apply(plan, workspace.root)
    text = (tmp_path / "Cargo.toml").read_text()
    assert "# edited meanwhile" in text
    assert "certora = [" in text


def test_a_manifest_that_gained_what_the_plan_adds_is_not_written_over(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    meanwhile = STANDALONE + '\n[features]\ncertora = ["dep:something-else"]\n'
    (tmp_path / "Cargo.toml").write_text(meanwhile)
    with pytest.raises(scaffold.ScaffoldStale, match="already has certora"):
        apply(plan, workspace.root)
    assert (tmp_path / "Cargo.toml").read_text() == meanwhile


def test_the_lib_target_reports_where_cargo_says_its_source_is(tmp_path):
    # `[lib] path` can move it, and a scaffold that assumed src/lib.rs would append a module
    # declaration to a file nothing compiles.
    workspace = parse_metadata(
        CargoMetadataJson.model_validate(
            {
                "workspace_root": str(tmp_path),
                "workspace_members": ["prog 0.1.0 (path+file:///prog)"],
                "packages": [
                    {
                        "id": "prog 0.1.0 (path+file:///prog)",
                        "name": "prog",
                        "version": "0.1.0",
                        "manifest_path": str(tmp_path / "Cargo.toml"),
                        "features": {"certora": []},
                        "targets": [
                            {
                                "name": "build-script-build",
                                "kind": ["custom-build"],
                                "src_path": str(tmp_path / "build.rs"),
                            },
                            {
                                "name": "prog",
                                "crate_types": ["cdylib"],
                                "src_path": str(tmp_path / "program" / "entry.rs"),
                            },
                        ],
                    }
                ],
            }
        )
    )
    (member,) = workspace.members
    assert member.lib is not None
    assert member.lib.src_path == tmp_path / "program" / "entry.rs"
    assert member.lib.builds_shared_object
    assert member.lib.artifact_stem == "prog"


# ---------------------------------------------------------------------------------------------
# preflight: the orchestration around the plan


class _FakeCargo:
    """Stands in for ``cargo metadata`` and records each call.

    Two tests below check which directory and which features were requested. The result does
    not show that.
    """

    def __init__(self, workspace: Workspace | MetadataFailure) -> None:
        self.workspace = workspace
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.toolchains: list[str | None] = []
        self.cargo_homes: list[Path | None] = []

    async def __call__(
        self, root, *, offline=False, features=(), toolchain=None, cargo_home=None, timeout_s=0
    ):
        self.calls.append((Path(root), tuple(features)))
        self.toolchains.append(toolchain)
        self.cargo_homes.append(cargo_home)
        return self.workspace


@pytest.fixture
def fake_cargo(monkeypatch):
    def install(workspace: Workspace | MetadataFailure) -> _FakeCargo:
        fake = _FakeCargo(workspace)
        monkeypatch.setattr(Workspace, "read", fake)
        return fake

    return install


@pytest.mark.asyncio
async def test_preflight_resolves_the_verification_graph_from_the_packages_own_directory(
    tmp_path, fake_cargo
):
    # CVLR is optional, so a default-feature read reports it absent and the resolved versions
    # come back empty. Features also resolve against the package cargo considers current, so
    # the directory matters.
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, package_dir="programs/prog"
    )
    fake = fake_cargo(workspace)

    result = await preflight.prepare_workspace(
        tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED, package="prog"
    )

    assert fake.calls[0] == (tmp_path, ())
    assert fake.calls[-1] == (package.root, ("certora",))
    assert result.artifact_stem == "prog"


@pytest.mark.asyncio
async def test_preflight_reads_the_project_with_the_host_toolchain_not_its_pin(
    tmp_path, fake_cargo
):
    """A project's rust-toolchain.toml names a toolchain the Solana image may not have, and its
    runtime user cannot install one, so cargo would not start."""
    from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN

    workspace, _ = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, package_dir="programs/prog"
    )
    fake = fake_cargo(workspace)
    await preflight.prepare_workspace(
        tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED, package="prog"
    )
    assert fake.toolchains and set(fake.toolchains) == {HOST_RUST_TOOLCHAIN}


@pytest.mark.asyncio
async def test_a_confined_preflight_fetches_into_the_cargo_home_the_gate_warms(
    tmp_path, fake_cargo
):
    """A confined install's shared CARGO_HOME is not the runtime user's to write, so the reads
    fetch where the gate's warm step will look."""
    from composer.sandbox.recipes import sandbox_cargo_home

    workspace, _ = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, package_dir="programs/prog"
    )
    fake = fake_cargo(workspace)
    await preflight.prepare_workspace(
        tmp_path, reference=SOLANA_V2, sandbox=SandboxConfig(provider="launcher"), package="prog"
    )
    assert fake.cargo_homes and set(fake.cargo_homes) == {sandbox_cargo_home(tmp_path)}


@pytest.mark.asyncio
async def test_preflight_reports_what_cargo_said_when_the_project_cannot_be_read(
    tmp_path, fake_cargo
):
    fake_cargo(CargoFailed("error: failed to parse manifest at `Cargo.toml`\n"))
    with pytest.raises(preflight.PreflightFailed, match="failed to parse manifest") as caught:
        await preflight.prepare_workspace(tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED)
    assert str(caught.value).startswith(f"Could not read the Cargo project at {tmp_path}:")


@pytest.mark.asyncio
async def test_preflight_refuses_to_choose_between_verifiable_packages(tmp_path, fake_cargo):
    # Which program is under verification is a fact about the engagement, not about the layout.
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    second = replace(package, name="other")
    fake_cargo(replace(workspace, members=(package, second)))

    with pytest.raises(preflight.PreflightFailed, match="name the one to verify"):
        await preflight.prepare_workspace(tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED)


@pytest.mark.asyncio
async def test_preflight_names_the_members_when_asked_for_one_that_is_not_there(
    tmp_path, fake_cargo
):
    workspace, _ = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    fake_cargo(workspace)

    with pytest.raises(preflight.PreflightFailed, match="members: prog"):
        await preflight.prepare_workspace(
            tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED, package="nope"
        )


@pytest.mark.asyncio
async def test_a_blocked_plan_stops_preflight_with_the_resolution_in_the_message(
    tmp_path, fake_cargo
):
    # Preflight runs in the same task group as system analysis, so this exception cancels that
    # work. The message has to carry the fix. It is what the caller sees.
    workspace, _ = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, crate_types=("lib",)
    )
    fake_cargo(workspace)

    with pytest.raises(preflight.PreflightFailed, match="crate-type"):
        await preflight.prepare_workspace(
            tmp_path, reference=SOLANA_V2, sandbox=_UNCONFINED, package="prog"
        )
    assert not (tmp_path / "src" / "certora").exists()


# ---------------------------------------------------------------------------------------------
# pointing the target at the Anchor fork


ANCHOR_MANIFEST = """\
[package]
name = "prog"
version = "0.1.0"

[lib]
crate-type = ["cdylib"]

[dependencies]
anchor-lang = "0.31.1"
"""


def test_an_anchor_target_is_redirected_at_the_verification_fork(tmp_path):
    """Without the fork, a rule that reaches an Anchor handler cannot be analyzed. Upstream's
    boxed ``Error`` trips [3006]. The scaffold is what knows the resolved version, so it is what
    picks the branch."""
    workspace, package = _project(
        tmp_path,
        manifest=ANCHOR_MANIFEST,
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        cvlr_resolved={"anchor-lang": "0.31.1"},
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    assert plan.blocked == ()
    tables = {a.table: a for a in _additions(plan) if isinstance(a, AddTable)}
    patch = tables[("patch", "crates-io", "anchor-lang")]
    assert ("branch", "certora-v0.31.1") in patch.body
    assert ("git", "https://github.com/Certora/anchor.git") in patch.body


def test_redirecting_twice_is_a_no_op(tmp_path):
    """A second run must not append the patch table again. Two
    ``[patch.crates-io.anchor-lang]`` entries is a manifest cargo will not parse."""
    kwargs = dict(
        manifest=ANCHOR_MANIFEST,
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        cvlr_resolved={"anchor-lang": "0.31.1"},
    )
    workspace, package = _project(tmp_path, **kwargs)
    apply(plan_scaffold(workspace, package, SOLANA_V2), tmp_path)

    workspace, package = _project(tmp_path, **kwargs)
    again = plan_scaffold(workspace, package, SOLANA_V2)
    assert not [a for a in _additions(again) if a.table[0] == "patch"]
    assert any("already redirected" in note for note in again.satisfied)
    assert (tmp_path / "Cargo.toml").read_text().count("[patch.crates-io.anchor-lang]") == 1
    tomllib.loads((tmp_path / "Cargo.toml").read_text())


def test_an_anchor_version_the_fork_does_not_cover_blocks_the_whole_plan(tmp_path):
    """The fork has a branch for 0.26.0 and not 0.25.0. The plan blocks instead of scaffolding a
    project that builds and then reports a pointer-analysis error with nothing about Anchor."""
    workspace, package = _project(
        tmp_path,
        manifest=ANCHOR_MANIFEST.replace("0.31.1", "0.25.0"),
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        cvlr_resolved={"anchor-lang": "0.25.0"},
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    assert any("0.25.0" in b.problem for b in plan.blocked), plan.blocked
    with pytest.raises(ScaffoldBlocked):
        apply(plan, tmp_path)
    # A blocked plan applies nothing at all, including the parts that were fine.
    assert not (tmp_path / "src" / "certora").exists()


def test_a_non_anchor_target_gets_no_fork_patch(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest='[workspace]\nmembers = ["."]\n'
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    patched = {a.table[-1] for a in _additions(plan) if a.table[0] == "patch"}
    assert not patched & {"anchor-lang", "anchor-spl"}
    assert any("anchor-lang is not a dependency" in note for note in plan.satisfied)


def test_the_forks_planned_are_the_reference_chains_own(tmp_path):
    """Soroban has no forks, so an Anchor release in its graph is neither redirected nor refused,
    even one the Anchor fork does not cover."""
    workspace, package = _project(
        tmp_path,
        manifest=ANCHOR_MANIFEST.replace("0.31.1", "0.25.0"),
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        platform="22.0.0",
        platform_crate="soroban-sdk",
        cvlr_resolved={"anchor-lang": "0.25.0"},
    )
    plan = plan_scaffold(workspace, package, SOROBAN)
    core_commits = {p.name for p in SOROBAN.from_commits()}
    assert not [
        a for a in _additions(plan) if a.table[0] == "patch" and a.table[-1] not in core_commits
    ]
    assert not [b for b in plan.blocked if "anchor-lang" in b.problem], plan.blocked
    assert not [note for note in plan.satisfied if "anchor" in note]


# ---------------------------------------------------------------------------------------------
# unpublished CVLR releases


def _commit_patches(plan) -> dict[str, AddTable]:
    return {
        a.table[-1]: a
        for a in _additions(plan)
        if isinstance(a, AddTable) and a.table[:2] == ("patch", "crates-io")
        and a.table[-1].startswith("cvlr")
    }


def test_an_unpublished_release_is_read_from_its_commit(tmp_path):
    """The project declares the crates.io spelling, so the pin gate reads it like any release, and
    the patch is what sends cargo to the commit."""
    plan, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    assert not plan.blocked
    patches = _commit_patches(plan)
    assert set(patches) == {p.name for p in SOLANA_V2.from_commits()}
    for patch in SOLANA_V2.from_commits():
        body = patches[patch.name].body
        assert ("git", patch.commit.repo) in body and ("rev", patch.commit.rev) in body
    tomllib.loads(apply_and_read(plan, tmp_path))


def apply_and_read(plan, root: Path) -> str:
    apply(plan, root)
    return (root / "Cargo.toml").read_text()


def test_a_withheld_model_gets_no_patch(tmp_path):
    """Cargo warns, on every build, about a patch for a crate nothing depends on."""
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    plan = plan_scaffold(workspace, package, SOLANA_V2.withholding("cvlr-solana-stake"))
    assert "cvlr-solana-stake" not in _commit_patches(plan)
    assert "cvlr-solana" in _commit_patches(plan)


def test_a_project_already_reading_the_commit_is_left_as_it_is(tmp_path):
    kwargs = dict(manifest=STANDALONE, workspace_manifest=STANDALONE)
    workspace, package = _project(tmp_path, **kwargs)
    apply(plan_scaffold(workspace, package, SOLANA_V2), tmp_path)

    crate = next(p for p in SOLANA_V2.from_commits() if p.name == "cvlr-solana")
    commit = crate.commit
    at_commit = GitSource.parse(f"git+{commit.repo}?rev={commit.rev}#{commit.rev}")
    workspace, package = _project(
        tmp_path, **kwargs,
        cvlr_resolved={crate.name: crate.version}, sources={crate.name: at_commit},
    )
    again = plan_scaffold(workspace, package, SOLANA_V2)
    assert not again.blocked
    assert not _commit_patches(again)
    assert any(f"already read from {commit.repo}" in note for note in again.satisfied)
    assert (tmp_path / "Cargo.toml").read_text().count(f"[patch.crates-io.{crate.name}]") == 1


def test_a_project_reading_the_crate_from_elsewhere_is_refused(tmp_path):
    """Its entry is somebody's decision, and building on it verifies against a release this build
    does not pin."""
    crate = next(p for p in SOLANA_V2.from_commits() if p.name == "cvlr-solana")
    commit = crate.commit
    manifest = STANDALONE + f'\n[patch.crates-io]\n{crate.name} = {{ path = "../cvlr-solana" }}\n'
    workspace, package = _project(
        tmp_path, manifest=manifest, workspace_manifest=manifest,
        cvlr_resolved={crate.name: crate.version}, sources={crate.name: None},
    )
    plan = plan_scaffold(workspace, package, SOLANA_V2)
    (blocked,) = [b for b in plan.blocked if crate.name in b.problem]
    assert "a path in this workspace" in blocked.problem and commit.rev in blocked.problem


# ---------------------------------------------------------------------------------------------
# platform generations


@pytest.mark.parametrize(
    "platform_crate, version, expected",
    [
        ("solana-account-info", "3.1.1", "solana-* 3.x"),
        ("solana-account-info", "2.3.0", "solana-program 2.x"),
        ("solana-program", "2.2.1", "solana-program 2.x"),
        ("solana-program", "1.18.26", "solana-program 1.18"),
        ("solana-program", "1.17.18", None),
    ],
)
def test_a_project_is_set_up_with_the_generation_it_is_on(
    tmp_path, platform_crate, version, expected
):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, platform=version, platform_crate=platform_crate
    )
    chosen = scaffold.select_generation(workspace, package, generations("solana"))
    assert (chosen.platform.label if chosen is not None else None) == expected


def test_a_project_with_no_platform_crate_is_on_no_generation(tmp_path):
    """Evidence is required: nothing linked says which generation this is."""
    workspace, package = _project(tmp_path, manifest=STANDALONE, platform=None)
    assert scaffold.select_generation(workspace, package, generations("solana")) is None


def test_an_sdk3_project_scaffolds_on_its_generation_and_is_refused_by_the_other(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE,
        platform="3.1.1", platform_crate="solana-account-info",
    )
    assert not plan_scaffold(workspace, package, SOLANA_V3).blocked
    refused = plan_scaffold(workspace, package, SOLANA_V2).blocked
    assert any("solana-account-info 3.1.1" in b.problem for b in refused)


def test_a_1_17_project_is_refused_by_the_1_18_generation(tmp_path):
    """Before 2.0 a minor release is a generation: `cvlr-solana` 0.4's `^1.18` cannot resolve
    against a `~1.17` target, and the refusal has to say that rather than leave it to cargo."""
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, platform="1.17.18"
    )
    (blocked,) = plan_scaffold(workspace, package, SOLANA_V1_18).blocked
    assert "solana-program 1.17.18" in blocked.problem
    assert blocked.resolution == "move the project to solana-program 1.18"


def test_a_1_18_project_scaffolds_without_program_models(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, platform="1.18.26"
    )
    plan = plan_scaffold(workspace, package, SOLANA_V1_18)
    assert not plan.blocked
    assert not any("cvlr-spl-token" in str(c) or "cvlr-solana-stake" in str(c) for c in plan.changes)
