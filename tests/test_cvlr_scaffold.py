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

from composer.cargo.metadata import (
    CratePackage,
    LibTarget,
    RegistrySource,
    Workspace,
    parse_metadata,
)
from composer.spec.cvlr import preflight, scaffold, tuning
from composer.spec.cvlr.scaffold import (
    HARNESS_DIR,
    AppendSection,
    InsertInTable,
    ScaffoldBlocked,
    Write,
    apply,
    plan_scaffold,
)
from composer.spec.cvlr.tuning import ENV_FAMILIES, INLINING, SUMMARIES
from composer.spec.cvlr_reference import SOLANA

PROGRAM = """\
use solana_program::account_info::AccountInfo;

pub fn process(_accounts: &[AccountInfo]) {}
"""


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
) -> tuple[Workspace, CratePackage]:
    """A project on disk plus the ``Workspace`` cargo would report for it.

    ``platform`` and ``cvlr_resolved`` stand in for the resolved graph, which is what the platform
    gate and the version-gap report read; the manifests on disk are what the planner parses.

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
                source=RegistrySource("registry+https://github.com/rust-lang/crates.io-index"),
            )
        )
    workspace = Workspace(
        root=root,
        target_directory=root / "target",
        members=(package,),
        packages=tuple(resolved),
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
    return plan_scaffold(workspace, package, SOLANA), workspace


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


def test_the_manifest_a_fresh_project_ends_up_with_still_parses(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    parsed = tomllib.loads((tmp_path / "Cargo.toml").read_text())

    assert parsed["features"]["certora"] == [
        "dep:cvlr", "dep:cvlr-solana", "dep:cvlr-solana-stake", "dep:cvlr-spl-token",
    ]
    # Optional is what keeps CVLR out of a release build, and what makes `dep:` legal above.
    assert parsed["dependencies"]["cvlr"] == {"version": "=0.6.1", "optional": True}
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
    assert any(isinstance(c, InsertInTable) for c in plan.changes)
    apply(plan, workspace.root)

    text = (tmp_path / "Cargo.toml").read_text()
    assert text.count("[features]") == 1
    assert "The project's own comment" in text
    parsed = tomllib.loads(text)
    assert parsed["features"]["no-entrypoint"] == []
    # no-entrypoint leads because suppressing the program's entrypoint is what lets a rule call a
    # handler directly; it is enabled only because this package has it.
    assert parsed["features"]["certora"] == [
        "no-entrypoint", "dep:cvlr", "dep:cvlr-solana", "dep:cvlr-solana-stake",
        "dep:cvlr-spl-token",
    ]


def test_a_package_with_no_entrypoint_feature_does_not_get_one_invented(tmp_path):
    plan, _ = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    entry = next(c for c in plan.changes if "dep:cvlr" in c.contents)
    assert "no-entrypoint" not in entry.contents


def test_a_workspace_gets_the_pins_and_its_member_inherits_them(tmp_path):
    plan, workspace = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=WORKSPACE_ROOT,
        package_dir="programs/prog",
    )
    apply(plan, workspace.root)

    root = tomllib.loads((tmp_path / "Cargo.toml").read_text())
    assert root["workspace"]["dependencies"]["cvlr"] == {"version": "=0.6.1"}
    member = tomllib.loads((tmp_path / "programs" / "prog" / "Cargo.toml").read_text())
    assert member["dependencies"]["cvlr"] == {"workspace": True, "optional": True}


def test_a_package_that_builds_no_loadable_object_is_refused_rather_than_patched(tmp_path):
    # Adding cdylib to somebody's library changes how it builds everywhere, so it is a decision for
    # a human. The refusal has to stop the whole plan, not just that one change.
    plan, workspace = _plan(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE, crate_types=("lib",)
    )
    assert [b.problem for b in plan.blocked] == [
        "prog builds no cdylib, so cargo produces no loadable object and the prover has nothing "
        "to read"
    ]
    with pytest.raises(ScaffoldBlocked):
        apply(plan, workspace.root)
    assert not (tmp_path / "src" / "certora").exists()


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
        '[features]\ncertora = ["dep:cvlr"]\n\n[dependencies]\ncvlr = "0.6.1"\nsolana-program',
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


def test_a_project_already_on_the_pinned_release_is_not_refused(tmp_path):
    # The idempotence case, and the one the gate must not catch: `=0.6.1` is what the scaffold
    # itself writes, and a bare `0.6.1` is the same release written by hand.
    for requirement in ("=0.6.1", "0.6.1"):
        root = tmp_path / requirement
        manifest = STANDALONE.replace(
            "[dependencies]\nsolana-program",
            f'[dependencies]\ncvlr = "{requirement}"\nsolana-program',
        )
        plan, _ = _plan(
            root,
            manifest=manifest,
            workspace_manifest=manifest,
            cvlr_resolved={"cvlr": "0.6.1"},
        )
        assert not plan.blocked, requirement


def test_a_reference_set_crate_the_project_does_not_name_is_not_a_refusal(tmp_path):
    # Absent, not mismatched, and the reason the two are separate types. This project is on the
    # pinned core and chain crate and names neither specialization; the scaffold adds them. A gate
    # that read "not in the graph" as a disagreement would refuse every project it is meant to set
    # up, starting with the fresh one.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program",
        '[dependencies]\ncvlr = "=0.6.1"\ncvlr-solana = "=0.5.0"\nsolana-program',
    )
    plan, _ = _plan(
        tmp_path,
        manifest=manifest,
        workspace_manifest=manifest,
        cvlr_resolved={"cvlr": "0.6.1", "cvlr-solana": "0.5.0"},
    )
    assert not plan.blocked
    written = "".join(
        c.contents for c in plan.changes if getattr(c, "path", None) == Path("Cargo.toml")
    )
    assert "cvlr-solana-stake" in written
    assert "cvlr-spl-token" in written


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
    plan = plan_scaffold(workspace, package, SOLANA)

    assert not any(c.path.name == "lib.rs" for c in plan.changes)
    assert any("already declares the harness module" in note for note in plan.satisfied)


def test_the_composite_env_file_carries_both_canonical_layers_and_names_them(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    composite = (tmp_path / "src" / "certora" / "envs" / INLINING.composite).read_text()
    # The generated file names the layers it was built from, so a reader who wants to change a
    # directive can tell which of them carries it.
    for layer in (INLINING.core, INLINING.anchor):
        assert layer in composite
        marker = tuning.starting_env(layer).strip().splitlines()[-1]
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


def test_an_ambiguous_table_header_stops_the_edit(tmp_path):
    # The insert is a text edit to a parsed file, so it has to fail loudly rather than land in
    # whichever of two tables comes first.
    with pytest.raises(ScaffoldBlocked):
        scaffold._insert_in_table("[features]\na = []\n[features]\nb = []\n", "[features]", "c=[]\n")


def test_the_lib_target_reports_where_cargo_says_its_source_is(tmp_path):
    # `[lib] path` can move it, and a scaffold that assumed src/lib.rs would append a module
    # declaration to a file nothing compiles.
    workspace = parse_metadata(
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
                        {"name": "build-script-build", "kind": ["custom-build"]},
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

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    async def __call__(self, root, *, offline=False, features=(), timeout_s=0):
        self.calls.append((Path(root), tuple(features)))
        return self.workspace


@pytest.fixture
def fake_cargo(monkeypatch):
    def install(workspace: Workspace) -> _FakeCargo:
        fake = _FakeCargo(workspace)
        monkeypatch.setattr(preflight, "read_workspace", fake)
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

    result = await preflight.prepare_workspace(tmp_path, package="prog")

    assert fake.calls[0] == (tmp_path, ())
    assert fake.calls[-1] == (package.root, ("certora",))
    assert result.artifact_stem == "prog"


@pytest.mark.asyncio
async def test_preflight_refuses_to_choose_between_verifiable_packages(tmp_path, fake_cargo):
    # Which program is under verification is a fact about the engagement, not about the layout.
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    second = replace(package, name="other")
    fake_cargo(replace(workspace, members=(package, second)))

    with pytest.raises(preflight.PreflightFailed, match="name the one to verify"):
        await preflight.prepare_workspace(tmp_path)


@pytest.mark.asyncio
async def test_preflight_names_the_members_when_asked_for_one_that_is_not_there(
    tmp_path, fake_cargo
):
    workspace, _ = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    fake_cargo(workspace)

    with pytest.raises(preflight.PreflightFailed, match="members: prog"):
        await preflight.prepare_workspace(tmp_path, package="nope")


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
        await preflight.prepare_workspace(tmp_path, package="prog")
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
    plan = plan_scaffold(workspace, package, SOLANA)
    assert plan.blocked == ()
    appended = [
        c
        for c in plan.changes
        if isinstance(c, AppendSection)
        and c.path == Path("Cargo.toml")
        and "patch.crates-io" in c.contents
    ]
    assert len(appended) == 1, [type(c).__name__ for c in plan.changes]
    contents = appended[0].contents
    assert 'branch = "certora-v0.31.1"' in contents
    assert 'git = "https://github.com/Certora/anchor.git"' in contents


def test_redirecting_twice_is_a_no_op(tmp_path):
    """A second run must not append the patch table again. Two
    ``[patch.crates-io.anchor-lang]`` entries is a manifest cargo will not parse."""
    kwargs = dict(
        manifest=ANCHOR_MANIFEST,
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        cvlr_resolved={"anchor-lang": "0.31.1"},
    )
    workspace, package = _project(tmp_path, **kwargs)
    apply(plan_scaffold(workspace, package, SOLANA), tmp_path)

    workspace, package = _project(tmp_path, **kwargs)
    again = plan_scaffold(workspace, package, SOLANA)
    assert not [c for c in again.changes if "patch.crates-io" in getattr(c, "contents", "")]
    assert any("already redirected" in note for note in again.satisfied)
    assert (tmp_path / "Cargo.toml").read_text().count("[patch.crates-io.anchor-lang]") == 1
    tomllib.loads((tmp_path / "Cargo.toml").read_text())


def test_an_anchor_version_the_fork_does_not_cover_blocks_the_whole_plan(tmp_path):
    """The fork has a branch for 0.30.1 and not 0.30.0. The plan blocks instead of scaffolding a
    project that builds and then reports a pointer-analysis error with nothing about Anchor."""
    workspace, package = _project(
        tmp_path,
        manifest=ANCHOR_MANIFEST.replace("0.31.1", "0.30.0"),
        workspace_manifest='[workspace]\nmembers = ["."]\n',
        cvlr_resolved={"anchor-lang": "0.30.0"},
    )
    plan = plan_scaffold(workspace, package, SOLANA)
    assert any("0.30.0" in b.problem for b in plan.blocked), plan.blocked
    with pytest.raises(ScaffoldBlocked):
        apply(plan, tmp_path)
    # A blocked plan applies nothing at all, including the parts that were fine.
    assert not (tmp_path / "src" / "certora").exists()


def test_a_non_anchor_target_gets_no_patch_section(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest='[workspace]\nmembers = ["."]\n'
    )
    plan = plan_scaffold(workspace, package, SOLANA)
    assert not [c for c in plan.changes if "patch.crates-io" in getattr(c, "contents", "")]
    assert any("anchor-lang is not a dependency" in note for note in plan.satisfied)
