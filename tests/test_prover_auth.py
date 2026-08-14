"""Cloud login must never wait on a browser, and must say what to do when it fails.

AutoProver submits prover jobs unattended. When the stored session could not be
refreshed, ``certora_login.login`` falls back to the PKCE browser flow, whose
callback server then waits out its deadline — a headless run pays five minutes
per prover call and fails anyway:

    AuthenticationError: PKCE login deadline of 300.0s expired before a callback
    completed.

``CERTORA_LOGIN_NO_PKCE`` removes the fallback, leaving the refresh path intact,
so the failure is immediate. These pin that we set it, and that the resulting
error names the command a human has to run.
"""

import os

import pytest

import composer.prover.auth as auth


@pytest.fixture(autouse=True)
def _clean_login_state(monkeypatch: pytest.MonkeyPatch):
    """``ensure_prover_login`` is process-cached and sets an env var; isolate both."""
    monkeypatch.delenv("CERTORA_LOGIN_NO_PKCE", raising=False)
    monkeypatch.setattr(auth, "resolve_login_env", lambda: "production")
    auth.ensure_prover_login.cache_clear()
    yield
    auth.ensure_prover_login.cache_clear()


def test_login_is_refresh_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(auth, "login", lambda **kw: calls.append(kw))

    auth.ensure_prover_login()

    assert os.environ["CERTORA_LOGIN_NO_PKCE"] == "1"
    assert calls == [{"env": "production", "force_file": True}]


def test_an_operator_override_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set as a default, so a host run that wants the browser flow can opt back in."""
    monkeypatch.setenv("CERTORA_LOGIN_NO_PKCE", "0")
    monkeypatch.setattr(auth, "login", lambda **kw: None)

    auth.ensure_prover_login()

    assert os.environ["CERTORA_LOGIN_NO_PKCE"] == "0"


def test_login_happens_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(auth, "login", lambda **kw: calls.append(kw))

    auth.ensure_prover_login()
    auth.ensure_prover_login()

    assert len(calls) == 1


def test_failure_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kw):
        raise RuntimeError("Failed to obtain or refresh credentials")

    monkeypatch.setattr(auth, "login", _boom)

    with pytest.raises(auth.ProverAuthError) as excinfo:
        auth.ensure_prover_login()

    message = str(excinfo.value)
    assert "certora-cloud login" in message
    # The underlying cause survives — it distinguishes "expired" from "no network".
    assert "Failed to obtain or refresh credentials" in message


def test_api_factory_logs_in_before_constructing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ProverOutputAPI.__init__`` authenticates by itself, so the order matters."""
    order: list[str] = []
    monkeypatch.setattr(auth, "login", lambda **_kw: order.append("login"))
    monkeypatch.setattr(
        auth, "ProverOutputAPI", lambda **kw: order.append(f"api(enable_cache={kw['enable_cache']})")
    )

    auth.prover_output_api(enable_cache=False)

    assert order == ["login", "api(enable_cache=False)"]
