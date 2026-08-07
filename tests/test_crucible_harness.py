"""Unit tests for the crucible wheel's crate rendering (was Python's `CrucibleHarness`).

Pure/fast (no build, no LLM): the wheel now owns crate assembly (`docs/rust-applications.md`).
`workspace_prep` places a deps-only manifest for warming, and `finalize` renders the whole crate
(shared fixture + one feature-gated test section per delivered invariant) from the outcome set.
These pin that rendering — including that the manifest declares each invariant's feature, which
replaces the old cumulative-feature-reservation dance (per-run manifests remove the race
entirely, so there is no shared manifest to clobber).
"""

import json

import pytest

crucible_app = pytest.importorskip(
    "crucible_app",
    reason="crucible_app wheel not built (maturin build -m rust/crucible-app/Cargo.toml)",
)


@pytest.fixture(autouse=True)
def _crucible_repo(monkeypatch):
    # Crate rendering only needs the checkout path as a *string* for the path-deps; a real dir
    # isn't required to exercise the manifest/main.rs assembly.
    monkeypatch.setenv("CRUCIBLE_REPO", "/nonexistent/crucible")


#: The program under test's crate as the host resolves it (``composer.spec.cargo``) — the awkward
#: shape, where the directory and Cargo names all differ from the analysis identifier. ``anchor``
#: matches Crucible's stack, so the harness can depend on the crate directly.
_CRATE = {
    "dir": "programs/lend", "package": "example-lending", "lib": "example_lending",
    "anchor": "1.0.1",
}
#: The same crate as it really is: an Anchor major Crucible cannot link, so the types must come
#: from the IDL instead.
_SKEWED = {**_CRATE, "anchor": "0.29.0"}
#: Where the host reports it placed the IDL (workdir-relative), as ``workspace_prep`` asked. It
#: travels as one of this chain's prep *facts* (``autoprover_solana::SolanaPrepFacts``) — the
#: framework carries the object without knowing what an IDL is.
_IDL_AT = "fuzz/vault/idls/example_lending.json"


def _crate_root(*slugs: str, idl: str | None = None) -> dict[str, str]:
    """The run's build scaffolding, as the host asks for it once the unit set is known.

    Only the unit *slugs* matter here: the crate root and manifest are a function of the unit set
    and the shared fixture, which is exactly why this can be written before any unit has authored
    anything — and therefore why the gated builds never have to rewrite it."""
    payload = {
        "program": "vault",
        "source_unit": _CRATE,
        "prep_facts": {"idl": idl} if idl is not None else {},
        "setup": "// FIXTURE\nstruct Fixture {}",
        "units": [{"slug": s} for s in slugs],
    }
    return json.loads(crucible_app.crate_root(json.dumps(payload)))


def _finalize(
    *sections: tuple[str, str],
    idl: str | None = None,
    gave_up: list[tuple[str, str, str]] | None = None,
) -> dict[str, str]:
    """Render the section files for delivered components, each ``(harness_fn, test_src)``, plus any
    ``(name, slug, reason)`` the author gave up on.

    ``targets`` is what keys a delivered section — the host mirrors the distinct validation targets
    it ran onto each outcome entry (``RustFormalResult.targets``). It is NOT ``property_checks``:
    those are per-property checks, and several share one target, so keying on them wrote the harness
    fn once per property. Both are sent here, as the host sends both. A component that gave up ran no
    build and so has no targets — it carries its unit instead, which is what names its feature.
    """
    payload = {
        "program": "vault",
        "source_unit": _CRATE,
        "prep_facts": {"idl": idl} if idl is not None else {},
        "setup": "// FIXTURE\nstruct Fixture {}",
        "components": [
            {
                "name": fn,
                "outcome": {
                    "status": "delivered",
                    "artifact_text": src,
                    "targets": [fn],
                    "property_checks": [[f"p {fn}", [f"c_p_{i}"]]],
                    "skipped": [],
                    "unit_file": None,
                    "run_link": None,
                },
            }
            for i, (fn, src) in enumerate(sections)
        ] + [
            {
                "name": name,
                "outcome": {"status": "gave_up", "unit": {"slug": slug}, "reason": reason},
            }
            for name, slug, reason in (gave_up or [])
        ],
    }
    return json.loads(crucible_app.finalize(json.dumps(payload)))


def _prep(source_unit: dict | None = None, args: dict | None = None) -> dict:
    """The wheel's plan, with its chain-shaped ``toolchain_request`` lifted out — that object is what
    the host's Solana toolchain reads, and the only place ``warm_dirs``/``build_program``/``idl_dest``
    are named on either side."""
    plan = json.loads(
        crucible_app.workspace_prep(
            json.dumps({
                "kind": "preflight", "program": "vault", "props": [], "run_props": [],
                "setup": None,
                "source_unit": source_unit or {}, "prep_facts": {}, "args": args or {},
            })
        )
    )
    return {"files": plan["files"], **plan["toolchain_request"]}


def test_workspace_prep_places_deps_only_manifest_and_warm_plan():
    plan = _prep(_CRATE)
    # The harness crate is named for the analysis identifier (`crucible run vault` finds it there)…
    assert plan["warm_dirs"] == ["fuzz/vault"]
    # …and pins its own toolchain, because rustup resolves by directory and the crate lives inside
    # the target project: otherwise the project's rust-toolchain.toml decides which cargo warms the
    # deps, which is not the one `crucible` builds with (and may be too old for a dep's manifest).
    assert 'channel = "stable"' in plan["files"]["fuzz/vault/rust-toolchain.toml"]
    cargo = plan["files"]["fuzz/vault/Cargo.toml"]
    assert 'name = "vault_fuzz"' in cargo
    assert "\nprobe = []" in cargo  # a feature to select for the setup dry-run
    # …while everything naming the *program under test* comes from the resolved crate: the `.so` to
    # build is its lib target, and the path dep its package name + real directory (neither of which
    # is derivable from `vault` — assuming they were is what broke on a real program).
    assert plan["build_program"] == "example_lending"
    assert 'example-lending = { path = "../../programs/lend", features = ["no-entrypoint"] }' in cargo


def test_workspace_prep_falls_back_to_the_programs_convention_without_a_crate():
    # A host that resolved nothing sends an empty object, and the wheel applies its own layout
    # convention (`SolanaSourceUnit::resolved`) — the framework has none to apply for it.
    plan = _prep(None)
    assert plan["build_program"] == "vault"
    cargo = plan["files"]["fuzz/vault/Cargo.toml"]
    assert 'vault = { path = "../../programs/vault", features = ["no-entrypoint"] }' in cargo


def test_crate_root_renders_the_fixture_plus_a_gated_entry_per_unit():
    # Written ONCE per run, between the setup step and fan-out — the only point the unit set is
    # known — and never rewritten, so this is the crate root every gated build compiles against and
    # the one the user receives.
    files = _crate_root("deposit", "withdraw")
    main_rs = files["fuzz/vault/src/main.rs"]
    assert main_rs.startswith("// FIXTURE")
    assert "mod c_deposit;" in main_rs and "mod c_withdraw;" in main_rs
    assert "c_deposit::invariants(fixture)" in main_rs
    # Scaffolding only — no unit has authored anything at this point in a run.
    assert "f: &mut Fixture" not in main_rs
    # The manifest declares one feature per unit — each gates its own `main()` — and the same path
    # dep the gated builds use, so the delivered crate still compiles for the user. (A build selects
    # exactly one: each `#[invariant_test]` emits its own `main` and `#[global_allocator]`, so
    # enabling two at once cannot compile.)
    cargo = files["fuzz/vault/Cargo.toml"]
    assert "c_deposit = []" in cargo and "c_withdraw = []" in cargo
    assert "c_invariants" not in cargo, "no fallback feature when the host supplies slugs"
    assert 'example-lending = { path = "../../programs/lend", features = ["no-entrypoint"] }' in cargo
    # The gates' sanity check ships as a target too, so a user can re-run it the same way they run a
    # suite: `crucible run vault probe --dry-run`. It lives OUTSIDE the `c_` component namespace, so
    # a component named "probe" (which slugs to `c_probe`) is a different target, not an overwrite.
    assert "\nprobe = []" in cargo
    assert "mod probe;" in main_rs and "probe::invariants(fixture)" in main_rs
    assert "pub fn invariants" in files["fuzz/vault/src/probe.rs"]
    # …and this manifest points the one `[[bin]]` at the real crate root, not the gates' probe root.
    assert 'path = "src/main.rs"' in cargo


def test_finalize_writes_only_section_files():
    files = _finalize(
        ("c_deposit", "#[invariant_test]\nfn c_deposit(f: &mut Fixture) {}"),
        ("c_withdraw", "#[invariant_test]\nfn c_withdraw(f: &mut Fixture) {}"),
    )
    deposit = files["fuzz/vault/src/c_deposit.rs"]
    assert deposit.endswith("fn c_deposit(f: &mut Fixture) {}"), deposit
    assert "use super::*;" in deposit
    assert "fn c_withdraw" in files["fuzz/vault/src/c_withdraw.rs"]
    # The crate root and manifest were written up front and are already right for the whole unit
    # set — rewriting them here is what would let the deliverable drift from what was built.
    assert "fuzz/vault/src/main.rs" not in files
    assert "fuzz/vault/Cargo.toml" not in files


def test_finalize_emits_the_shared_harness_fn_once_not_once_per_property():
    # Every report row maps to the same authored source (all properties live in one invariant fn),
    # so folding one section per row used to emit that fn N times — a crate with duplicate
    # definitions, which cannot compile.
    spec = "#[invariant_test]\nfn invariants(f: &mut Fixture) {}"
    files = _finalize(("c_solvency", spec), ("c_no_drain", spec), ("c_bounded", spec))
    assert list(files) == ["fuzz/vault/src/c_bounded.rs"], files


def test_a_component_that_gave_up_gets_a_compile_error_behind_its_feature():
    # Its feature was declared by `crate_root` before anything was authored, so the target exists
    # either way. What goes behind it is an honest build-time refusal naming the gap — never a
    # failing test, which `validate` would read as a refuted property about the user's program.
    files = _finalize(
        ("c_deposit", "fn invariants(f: &mut Fixture) {}"),
        gave_up=[("Referrals", "referrals", "the fixture exposes no referral action")],
    )
    section = files["fuzz/vault/src/c_referrals.rs"]
    assert "compile_error!" in section
    assert "the fixture exposes no referral action" in section
    assert "fuzz_assert" not in section and "fn invariants" not in section


def test_finalize_is_empty_without_any_component():
    # Nothing delivered and nothing given up → nothing to write; finalize returns None (host skips).
    raw = crucible_app.finalize(json.dumps({
        "program": "vault", "setup": "// F", "components": [],
        "source_unit": _CRATE, "prep_facts": {},
    }))
    assert raw is None


def test_workspace_prep_requests_an_idl_for_a_program_it_cannot_link():
    # A real program's Anchor major can't satisfy Crucible's trait bounds (nor co-resolve with libafl),
    # so the wheel asks the host for an IDL and warms the IDL-path manifest instead.
    plan = _prep(_SKEWED)
    assert plan["idl_dest"] == _IDL_AT
    assert plan["build_program"] == "example_lending"  # the .so is still needed by LiteSVM
    cargo = plan["files"]["fuzz/vault/Cargo.toml"]
    assert "crucible-idl-gen = { path =" in cargo
    assert "programs/lend" not in cargo
    # A linkable program is left alone, and an operator can force the IDL path with --program-idl.
    assert _prep(_CRATE).get("idl_dest") is None
    assert _prep(_CRATE, {"program_idl": "/tmp/lend.json"})["idl_dest"] == _IDL_AT


def test_the_crate_root_takes_the_idl_path_when_prep_placed_an_idl():
    files = _crate_root("deposit", idl=_IDL_AT)
    # The delivered crate must be the one that was gated: generated types, no program dependency.
    main_rs = files["fuzz/vault/src/main.rs"]
    assert 'declare_fuzz_program!(example_lending = "idls/example_lending.json")' in main_rs
    assert main_rs.index("declare_fuzz_program") < main_rs.index("// FIXTURE")
    cargo = files["fuzz/vault/Cargo.toml"]
    assert "crucible-idl-gen" in cargo and 'ctor = "0.6"' in cargo
    assert "programs/lend" not in cargo


# ---------------------------------------------------------------------------
# The preflight skeleton — what `compile` builds when nothing has been authored yet.
# ---------------------------------------------------------------------------

#: A launcher that cannot be spawned, so `compile` fails at the exec and never runs a real build.
#: `run_confined` materializes the crate *before* spawning, so the files on disk afterwards are
#: exactly what a real build would have seen — which is what these tests assert on.
_NO_LAUNCH = json.dumps({"argv_prefix": ["/nonexistent/run-confined", "--"], "timeout_s": 5})


def _compile_crate(tmp_path, *, spec: str | None = None, idl: str | None = None) -> dict[str, str]:
    """Ask the wheel to compile a preflight input; return the crate it wrote."""
    result = json.loads(
        crucible_app.compile(
            json.dumps({
                "kind": "preflight", "program": "vault", "source_unit": _CRATE,
                "props": [], "run_props": [], "setup": None, "args": {},
                "prep_facts": {"idl": idl} if idl is not None else {},
            }),
            spec, str(tmp_path), _NO_LAUNCH,
        )
    )
    assert result["status"] == "failed"  # the launcher doesn't exist — the *files* are the subject
    root = tmp_path / "fuzz" / "vault"
    return {
        str(p.relative_to(tmp_path)): p.read_text()
        for p in root.rglob("*") if p.is_file()
    }


def test_preflight_renders_a_skeleton_that_needs_no_program_knowledge(tmp_path):
    # The preflight runs alongside system analysis, so nothing is known about the program's API and
    # nothing has been authored — the wheel supplies the whole crate itself, and there is no spec for
    # the host to send.
    files = _compile_crate(tmp_path)
    probe_rs = files["fuzz/vault/src/gate_root.rs"]

    # It exercises what an authored fixture will depend on: the program's module (here its crate),
    # the `.so` named after the crate's *lib* target, and the crucible fixture/test macros.
    assert "use example_lending::*;" in probe_rs
    assert '"../../target/deploy/example_lending.so"' in probe_rs
    assert "#[fuzz_fixture]" in probe_rs and "struct Fixture" in probe_rs
    # `#[fuzz_fixture]` refuses to expand an impl block with no action, and a preflight has no
    # instruction to offer — hence the stand-in.
    assert "fn action_noop" in probe_rs
    # …and the probe test whose name is the feature the dry-run selects, as a section like any
    # component's — the body in its own file, the entry generated at the crate root.
    assert "fn probe" in probe_rs and "probe::invariants(fixture)" in probe_rs
    assert "pub fn invariants" in files["fuzz/vault/src/probe.rs"]
    assert "\nprobe = []" in files["fuzz/vault/Cargo.toml"]
    # No property, no invariant, no instruction call: the program's API is the fixture author's job.
    assert "fuzz_assert" not in probe_rs
    assert "instruction::" not in probe_rs


def test_preflight_does_not_touch_the_deliverables_crate_root(tmp_path):
    # It runs before analysis, so it cannot write the real root — and must not clobber it either,
    # which is what used to leave a half-crate at `src/main.rs` when a run died mid-setup. Same
    # `[[bin]]` name (the only one the crucible CLI executes), its own path.
    files = _compile_crate(tmp_path)
    assert "fuzz/vault/src/main.rs" not in files
    cargo = files["fuzz/vault/Cargo.toml"]
    assert 'name = "invariant_test"' in cargo
    assert 'path = "src/gate_root.rs"' in cargo


def test_a_preflight_spec_from_the_host_is_ignored(tmp_path):
    # The host has nothing to send (it passes `None`), but the skeleton is the wheel's regardless —
    # the `compile` signature is shared with the authoring gates and this must not become a way in.
    files = _compile_crate(tmp_path, spec="// NOT THE SKELETON")
    assert "NOT THE SKELETON" not in files["fuzz/vault/src/gate_root.rs"]


def test_the_preflight_skeleton_follows_the_idl_path_when_prep_placed_one(tmp_path):
    # Same decision as every other build in the run: types from the IDL the prep placed, and no
    # dependency on the program's crate — so the preflight gates the codegen too, which is one of
    # the biggest failure surfaces (it macro-expands the whole IDL at compile time).
    files = _compile_crate(tmp_path, idl=_IDL_AT)
    probe_rs = files["fuzz/vault/src/gate_root.rs"]
    assert 'declare_fuzz_program!(example_lending = "idls/example_lending.json")' in probe_rs
    assert probe_rs.index("declare_fuzz_program") < probe_rs.index("use example_lending::*;")
    assert "programs/lend" not in files["fuzz/vault/Cargo.toml"]
