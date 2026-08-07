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


def _finalize(*sections: tuple[str, str], idl: str | None = None) -> dict[str, str]:
    """Render the crate for delivered components, each ``(harness_fn, test_src)``.

    ``targets`` is what keys a section and declares its feature — the host mirrors the distinct
    validation targets it ran onto each outcome entry (``RustFormalResult.targets``). It is NOT
    ``property_checks``: those are per-property checks, and several share one target, so keying
    on them wrote the harness fn once per property. Both are sent here, as the host sends both.
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
                "kind": "preflight", "program": "vault", "props": [], "setup": None,
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
    assert "c_probe = []" in cargo  # a feature to select for the setup dry-run
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


def test_finalize_renders_fixture_plus_each_section_and_the_harness_feature():
    files = _finalize(
        ("c_deposit", "#[invariant_test]\nfn c_deposit(f: &mut Fixture) {}"),
        ("c_withdraw", "#[invariant_test]\nfn c_withdraw(f: &mut Fixture) {}"),
    )
    main_rs = files["fuzz/vault/src/main.rs"]
    # Fixture first, then every section (verbatim; the macro self-gates by fn name).
    assert main_rs.startswith("// FIXTURE")
    assert "fn c_deposit" in main_rs and "fn c_withdraw" in main_rs
    # The manifest declares one feature per delivered component — each gates its own `main()` —
    # and the same path dep the gated builds used, so the delivered crate still compiles for the
    # user. (A build selects exactly one: each `#[invariant_test]` emits its own `main` and
    # `#[global_allocator]`, so enabling two at once cannot compile.)
    cargo = files["fuzz/vault/Cargo.toml"]
    assert "c_deposit = []" in cargo and "c_withdraw = []" in cargo
    assert "c_invariants" not in cargo, "no fallback feature when the host supplies targets"
    assert 'example-lending = { path = "../../programs/lend", features = ["no-entrypoint"] }' in cargo


def test_finalize_emits_the_shared_harness_fn_once_not_once_per_property():
    # Every report row maps to the same authored source (all properties live in one
    # `#[invariant_test]`), so folding one section per row used to emit that fn N times — a crate
    # with duplicate definitions, which cannot compile. Caught only by reading the delivered
    # artifact: the gated builds assemble fixture + spec directly and never see this render.
    spec = "#[invariant_test]\nfn c_invariants(f: &mut Fixture) {}"
    files = _finalize(("c_solvency", spec), ("c_no_drain", spec), ("c_bounded", spec))
    assert files["fuzz/vault/src/main.rs"].count("fn c_invariants") == 1


def test_finalize_skips_undelivered_and_is_empty_without_sections():
    # No delivered components → nothing to assemble; finalize returns None (the host skips it).
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


def test_finalize_delivers_the_idl_path_crate_when_prep_placed_an_idl():
    files = _finalize(("c_deposit", "#[invariant_test]\nfn c_deposit(f: &mut Fixture) {}"), idl=_IDL_AT)
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
                "props": [], "setup": None, "args": {},
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
    # nothing has been authored — the wheel supplies the whole `main.rs` itself, and there is no
    # spec for the host to send.
    files = _compile_crate(tmp_path)
    main_rs = files["fuzz/vault/src/main.rs"]

    # It exercises what an authored fixture will depend on: the program's module (here its crate),
    # the `.so` named after the crate's *lib* target, and the crucible fixture/test macros.
    assert "use example_lending::*;" in main_rs
    assert '"../../target/deploy/example_lending.so"' in main_rs
    assert "#[fuzz_fixture]" in main_rs and "struct Fixture" in main_rs
    # `#[fuzz_fixture]` refuses to expand an impl block with no action, and a preflight has no
    # instruction to offer — hence the stand-in.
    assert "fn action_noop" in main_rs
    # …and the probe test whose name is the feature the dry-run selects.
    assert "fn c_probe" in main_rs
    assert "c_probe = []" in files["fuzz/vault/Cargo.toml"]
    # No property, no invariant, no instruction call: the program's API is the fixture author's job.
    assert "fuzz_assert" not in main_rs
    assert "instruction::" not in main_rs


def test_a_preflight_spec_from_the_host_is_ignored(tmp_path):
    # The host has nothing to send (it passes `None`), but the skeleton is the wheel's regardless —
    # the `compile` signature is shared with the authoring gates and this must not become a way in.
    files = _compile_crate(tmp_path, spec="// NOT THE SKELETON")
    assert "NOT THE SKELETON" not in files["fuzz/vault/src/main.rs"]


def test_the_preflight_skeleton_follows_the_idl_path_when_prep_placed_one(tmp_path):
    # Same decision as every other build in the run: types from the IDL the prep placed, and no
    # dependency on the program's crate — so the preflight gates the codegen too, which is one of
    # the biggest failure surfaces (it macro-expands the whole IDL at compile time).
    files = _compile_crate(tmp_path, idl=_IDL_AT)
    main_rs = files["fuzz/vault/src/main.rs"]
    assert 'declare_fuzz_program!(example_lending = "idls/example_lending.json")' in main_rs
    assert main_rs.index("declare_fuzz_program") < main_rs.index("use example_lending::*;")
    assert "programs/lend" not in files["fuzz/vault/Cargo.toml"]
