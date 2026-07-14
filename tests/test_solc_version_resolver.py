"""Unit tests for pragma-to-solc-version resolution (installed-binary preference)."""

import pytest

import certora_autosetup.utils.solc_version_resolver as svr


@pytest.fixture(autouse=True)
def _fixed_version_list(monkeypatch):
    monkeypatch.setattr(
        svr, "fetch_available_solc_versions",
        lambda: ["0.8.36", "0.8.35", "0.8.28", "0.8.0", "0.7.6"],
    )


def _with_installed(monkeypatch, names):
    monkeypatch.setattr(svr.shutil, "which", lambda n: f"/usr/local/bin/{n}" if n in names else None)


def test_prefers_highest_installed_binary(monkeypatch) -> None:
    # 0.8.36 is the newest listed release but has no binary in the environment
    # (nor prover-toolchain support); a floating pragma must land on the
    # highest INSTALLED match instead of breaking the day a new solc ships.
    _with_installed(monkeypatch, {"solc8.35", "solc8.28"})
    assert svr.resolve_pragma_to_version("^0.8.0") == "0.8.35"


def test_supports_solc_select_naming(monkeypatch) -> None:
    _with_installed(monkeypatch, {"solc-0.8.28"})
    assert svr.resolve_pragma_to_version("^0.8.0") == "0.8.28"


def test_falls_back_to_listed_when_nothing_installed(monkeypatch) -> None:
    # Environments that fetch compilers on demand keep the old behavior.
    _with_installed(monkeypatch, set())
    assert svr.resolve_pragma_to_version("^0.8.0") == "0.8.36"


def test_exact_pragma_ignores_installed_state(monkeypatch) -> None:
    # An exact constraint has one candidate either way.
    _with_installed(monkeypatch, {"solc8.35"})
    assert svr.resolve_pragma_to_version("0.8.28") == "0.8.28"


def test_preferred_version_still_wins(monkeypatch) -> None:
    _with_installed(monkeypatch, {"solc8.35"})
    assert svr.resolve_pragma_to_version("^0.8.0", preferred_version="solc8.28") == "0.8.28"
