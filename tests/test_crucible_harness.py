"""Unit tests for the crucible wheel's crate rendering (was Python's `CrucibleHarness`).

Pure/fast (no build, no LLM): the wheel now owns crate assembly (`docs/rust-pure-app.md`).
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


def _finalize(*sections: tuple[str, str]) -> dict[str, str]:
    """Render the crate for delivered invariants, each ``(feature, test_src)``."""
    payload = {
        "program": "vault",
        "setup": "// FIXTURE\nstruct Fixture {}",
        "components": [
            {
                "name": feat,
                "delivered": True,
                "artifact_text": src,
                "property_units": [[f"p {feat}", [feat]]],
            }
            for feat, src in sections
        ],
    }
    return json.loads(crucible_app.finalize(json.dumps(payload)))


def test_workspace_prep_places_deps_only_manifest_and_warm_plan():
    plan = json.loads(
        crucible_app.workspace_prep(
            json.dumps({"kind": "setup", "program": "vault", "component": {}, "props": [], "context": {}})
        )
    )
    assert plan["warm_dirs"] == ["fuzz/vault"]
    assert plan["build_program"] == "vault"
    cargo = plan["files"]["fuzz/vault/Cargo.toml"]
    assert 'name = "vault_fuzz"' in cargo
    assert "c_probe = []" in cargo  # a feature to select for the setup dry-run
    # The pinned crucible/solana stack + the program path dep (was CrucibleDep).
    assert 'vault = { path = "../../programs/vault", features = ["no-entrypoint"] }' in cargo


def test_finalize_renders_fixture_plus_each_section_and_features():
    files = _finalize(
        ("c_deposit", "#[invariant_test]\nfn c_deposit(f: &mut Fixture) {}"),
        ("c_withdraw", "#[invariant_test]\nfn c_withdraw(f: &mut Fixture) {}"),
    )
    main_rs = files["fuzz/vault/src/main.rs"]
    # Fixture first, then every section (verbatim; the macro self-gates by fn name).
    assert main_rs.startswith("// FIXTURE")
    assert "fn c_deposit" in main_rs and "fn c_withdraw" in main_rs
    # Both features are declared in the manifest (sorted, stable).
    cargo = files["fuzz/vault/Cargo.toml"]
    assert "c_deposit = []" in cargo and "c_withdraw = []" in cargo


def test_finalize_skips_undelivered_and_is_empty_without_sections():
    # No delivered components → nothing to assemble; finalize returns None (the host skips it).
    raw = crucible_app.finalize(json.dumps({"program": "vault", "setup": "// F", "components": []}))
    assert raw is None
