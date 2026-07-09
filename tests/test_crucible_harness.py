"""Unit tests for the Crucible harness manifest assembly — the feature-race fix.

Pure/fast (no build): pins that per-component `prepare_component` reserves Cargo
features **cumulatively**, so a later component can't clobber an earlier one's
feature out of the shared `Cargo.toml` (the concurrency bug that dropped an
instruction: `package does not contain this feature: c_<slug>`).
"""

from __future__ import annotations

from pathlib import Path

from composer.crucible.harness import CrucibleDep, CrucibleHarness
from composer.crucible.store import CrucibleArtifactStore


def _store(tmp_path: Path) -> CrucibleArtifactStore:
    dep = CrucibleDep(
        crucible_repo=Path("/nonexistent/crucible"),  # only used to render dep paths as strings
        program_crate="vault",
        program_rel="../../programs/vault",
    )
    return CrucibleArtifactStore(str(tmp_path), program="vault", dep=dep)


def test_prepare_component_features_are_cumulative(tmp_path):
    store = _store(tmp_path)
    store.write_setup_manifest()  # reserves c_probe
    store.prepare_component("initialize")  # reserves c_initialize
    store.prepare_component("deposit")  # reserves c_deposit — must NOT drop c_initialize
    store.prepare_component("withdraw")

    cargo = (store.fuzz_dir() / "Cargo.toml").read_text()
    for feat in ("c_probe", "c_initialize", "c_deposit", "c_withdraw"):
        assert f"{feat} = []" in cargo, f"{feat} missing from manifest:\n{cargo}"


def test_reserved_features_render_even_without_test_sections(tmp_path):
    """reserve_features shows up in Cargo.toml before any `add_component` (test fold-in)."""
    h = CrucibleHarness(program="vault", dep=_store(tmp_path).harness.dep)
    h.reserve_features("c_probe", "c_deposit")
    cargo = h.render_cargo_toml()
    assert "c_probe = []" in cargo and "c_deposit = []" in cargo
    # main.rs body is still empty (no sections folded in yet)
    assert h.render_main_rs().strip() == ""


def test_add_component_and_reserved_features_union(tmp_path):
    h = CrucibleHarness(program="vault", dep=_store(tmp_path).harness.dep)
    h.reserve_features("c_deposit")
    h.add_component("c_withdraw", "#[invariant_test]\nfn c_withdraw(f: &mut Fixture) {}")
    cargo = h.render_cargo_toml()
    # both the reserved-only and the folded-in feature appear
    assert "c_deposit = []" in cargo
    assert "c_withdraw = []" in cargo
