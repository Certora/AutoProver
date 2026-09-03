"""The preflight scaffold: what it writes, what it refuses, and what it leaves alone.

``docs/cvlr-backend-plan.md`` §7.4. Three properties carry most of the risk, and each is a way a
scaffold quietly ruins a project rather than failing:

* **Idempotence.** A second run must be a no-op. The upstream ``certora-setup.py`` appends to
  ``Cargo.toml`` unconditionally, so running it twice leaves a manifest cargo will not parse — and
  a scaffold gets re-run whenever anybody is unsure whether it ran.
* **The manifest still parses.** Every manifest change here is a *text* edit to a file that was only
  read, because reserializing somebody's TOML would rewrite their comments to make one change. That
  trade is only worth it if the result is valid, so every case that touches a manifest re-parses it.
* **Refusing beats guessing.** The two decisions a template must not make are refusals, and a plan
  carrying one must apply *nothing*: a half-scaffolded project turns the next build failure into a
  question with two candidate answers.

No cargo and no network here — the workspace objects are built directly, the way
``test_cvlr_knowledge.py`` does, so these run in the routine env.
"""

import dataclasses
import tomllib
from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage, LibTarget, Workspace, parse_metadata
from composer.spec.cvlr import preflight, scaffold
from composer.spec.cvlr.harness import CvlrArtifactStore, HarnessModule
from composer.spec.cvlr.scaffold import (
    HARNESS_DIR,
    INLINING,
    AppendSection,
    InsertInTable,
    NewFile,
    ScaffoldBlocked,
    apply,
    plan_scaffold,
)
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
                source="registry+https://github.com/rust-lang/crates.io-index",
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
                source="registry+https://github.com/rust-lang/crates.io-index",
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

    assert parsed["features"]["certora"] == ["dep:cvlr", "dep:cvlr-solana"]
    # Optional is what keeps CVLR out of a release build, and what makes `dep:` legal above.
    assert parsed["dependencies"]["cvlr"] == {"version": "=0.6.1", "optional": True}
    metadata = parsed["package"]["metadata"]["certora"]
    assert metadata["sources"] == ["Cargo.toml", "src/**/*.rs"]
    assert metadata["solana_inlining"] == ["src/certora/envs/cvlr_inlining.txt"]


def test_the_path_a_mock_names_resolves_from_the_programs_own_file(tmp_path):
    """`cvlr::mock_fn(with = crate::certora::specs::<unit>::<fn>)` expands where the *munged*
    function is — the program's own source, which is outside `certora`. So every segment between
    there and the stand-in has to be `pub`, and the segments are written by two different modules:
    `certora/mod.rs` by the scaffold, `specs/mod.rs` by `declare_modules`.

    Both shipped as bare `mod`, which compiles and is invisible until a munge names a path through
    them: E0603, from a generated file that neither the author nor the judge ever reads, on the one
    tool whose whole purpose is to get past a rejected function. Verified against rustc that `pub`
    inside a private `mod certora;` is what makes the path resolve without widening the crate.
    """
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    root = (tmp_path / HARNESS_DIR / "mod.rs").read_text()
    assert "pub mod specs;" in root and "pub mod mocks;" in root

    store = CvlrArtifactStore(tmp_path, Path("."))
    declared = store.declare_modules([HarnessModule("withdraw")]).read_text()
    assert "pub mod withdraw;" in declared


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
    assert parsed["features"]["certora"] == ["no-entrypoint", "dep:cvlr", "dep:cvlr-solana"]


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
    # The regression that motivated the witness list. Solana's v3 split moved `AccountInfo` into
    # `solana-account-info` and stopped publishing `solana-program`, so a v3 target resolves *no*
    # `solana-program` at all — and a gate that asked only about that crate read the absence as
    # "this project has no opinion" and pinned CVLR 0.5 against it. The scaffold then compiles,
    # because it contains nothing that bridges the two; the failure surfaces on the first authored
    # rule, as `expected AccountInfo, found AccountInfo`. Observed on a real Anchor 1.x program.
    plan, _ = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=STANDALONE,
        platform="3.1.1",
        platform_crate="solana-account-info",
    )
    assert any("3.1.1" in b.problem for b in plan.blocked)


def test_a_project_on_the_reference_generation_passes_on_the_specific_witness(tmp_path):
    # The other side of the ordering: a 2.x target resolves `solana-account-info` too, and the
    # specific witness is consulted first. It has to *accept* on a match rather than merely not
    # blocking, or the fix above would refuse every project it was written to admit.
    plan, _ = _plan(
        tmp_path,
        manifest=STANDALONE,
        workspace_manifest=STANDALONE,
        platform="2.3.0",
        platform_crate="solana-account-info",
    )
    assert not plan.blocked


def test_the_platform_is_not_second_guessed_when_the_project_pins_cvlr_itself(tmp_path):
    # The scaffold inherits the project's pins, so the reference set's platform says nothing about
    # what will be built. Refusing here would reject a project whose own pairing is consistent —
    # which is what the gate did on the first real project it was pointed at.
    manifest = STANDALONE.replace(
        "[dependencies]\nsolana-program",
        '[dependencies]\ncvlr = "0.4"\ncvlr-solana = "0.4"\nsolana-program',
    )
    plan, _ = _plan(
        tmp_path,
        manifest=manifest,
        workspace_manifest=manifest,
        platform="1.18.26",
        cvlr_resolved={"cvlr": "0.4.1", "cvlr-solana": "0.4.5"},
    )
    assert not plan.blocked


def test_a_partly_pinned_project_is_still_checked(tmp_path):
    # One CVLR crate pinned and one not: the scaffold would write the reference version for the
    # missing one, so the pairing question is live and the exemption above must not apply.
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


def test_an_existing_harness_declaration_is_not_added_twice(tmp_path):
    workspace, package = _project(
        tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE
    )
    assert package.lib is not None
    package.lib.src_path.write_text(PROGRAM + "\npub mod certora;\n")
    plan = plan_scaffold(workspace, package, SOLANA)

    assert not any(c.path.name == "lib.rs" for c in plan.changes)
    assert any("already declares the harness module" in note for note in plan.satisfied)


def test_the_composite_env_file_carries_both_canonical_layers_and_names_the_editable_one(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    composite = (tmp_path / "src" / "certora" / "envs" / INLINING.composite).read_text()
    assert "DO NOT EDIT" in composite
    # The generated file has to say which layer is the reader's, or the header's instruction not to
    # edit has nowhere to send them.
    assert INLINING.package in composite
    for layer in (INLINING.core, INLINING.anchor):
        marker = scaffold.canonical_env(layer).strip().splitlines()[-1]
        assert marker in composite
    assert scaffold.env_provenance() in composite


def test_the_package_layer_ships_empty_and_is_what_a_recompose_picks_up(tmp_path):
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)
    layer = tmp_path / "src" / "certora" / "envs" / INLINING.package
    assert "yours" in layer.read_text()

    layer.write_text("#[inline] ^my_program::helper$\n")
    recomposed = scaffold.compose_env(INLINING, package_layer=layer.read_text())
    assert "^my_program::helper$" in recomposed


def test_gitignore_gains_only_what_is_missing(tmp_path):
    (tmp_path / ".gitignore").write_text("target/\n.certora_internal\n")
    plan, workspace = _plan(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    apply(plan, workspace.root)

    text = (tmp_path / ".gitignore").read_text()
    assert text.count(".certora_internal") == 1
    assert ".certora\n" in text and "certora_out" in text


def test_nothing_is_ever_overwritten(tmp_path):
    workspace, package = _project(tmp_path, manifest=STANDALONE, workspace_manifest=STANDALONE)
    plan = plan_scaffold(workspace, package, SOLANA)
    # A file that appeared between planning and applying — a concurrent edit, or a re-plan against a
    # stale snapshot. Overwriting is the one thing this module never does.
    created = next(c for c in plan.changes if isinstance(c, NewFile))
    (workspace.root / created.path).parent.mkdir(parents=True, exist_ok=True)
    (workspace.root / created.path).write_text("hand written\n")

    apply(plan, workspace.root)
    assert (workspace.root / created.path).read_text() == "hand written\n"


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
    """Stands in for ``cargo metadata``, recording how it was asked.

    The recording is the point of two of the tests below: *which* graph preflight resolves is a
    correctness question, not an implementation detail, and it is invisible in the result.
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
    # The scaffold declares CVLR `optional = true`, so a default-feature read reports it absent —
    # which silently empties the version-gap report and the source mount §5.5 depends on. And
    # features resolve against the package cargo considers current, so the directory matters too.
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
    second = dataclasses.replace(package, name="other")
    fake_cargo(dataclasses.replace(workspace, members=(package, second)))

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
    # Preflight shares the driver's task group, so raising here cancels system analysis — the whole
    # value of the gate. What it raises has to carry the fix, since that message is all anyone sees.
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
    """Not a refinement: without it a rule that reaches an Anchor handler cannot be analyzed at all,
    because upstream's boxed `Error` trips [3006]. The scaffold is the only place that knows the
    resolved version, so it is the only place that can pick the branch."""
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
    """The scaffold's central property, applied to the one change that is a manifest *append*: a
    second run must not add the table again, because two `[patch.crates-io.anchor-lang]` entries is a
    manifest cargo will not parse."""
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
    """0.30.0 is the real gap — the fork has a branch for 0.30.1 and not 0.30.0. Blocking beats
    scaffolding a project that builds, submits, and then reports a pointer-analysis error with
    nothing connecting it to Anchor."""
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
