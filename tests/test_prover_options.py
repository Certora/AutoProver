"""Unit tests for the global-prover-timeout override."""

from __future__ import annotations

from composer.prover.core import (
    DEFAULT_GLOBAL_TIMEOUT,
    GLOBAL_PROVER_TIMEOUT_ENV,
    _resolved_global_prover_timeout,
)

DEFAULT = int(DEFAULT_GLOBAL_TIMEOUT)


class TestResolvedGlobalProverTimeout:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(GLOBAL_PROVER_TIMEOUT_ENV, raising=False)
        assert _resolved_global_prover_timeout() == DEFAULT

    def test_integer_env_value_used(self, monkeypatch):
        monkeypatch.setenv(GLOBAL_PROVER_TIMEOUT_ENV, "600")
        assert _resolved_global_prover_timeout() == 600

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(GLOBAL_PROVER_TIMEOUT_ENV, "not-a-number")
        assert _resolved_global_prover_timeout() == DEFAULT
