"""Unit tests for the AUTOPROVER_SANITY_TIMEOUT override on the sanity phase's per-job timeout."""

from certora_autosetup.setup.sanity import (
    DEFAULT_SANITY_TIMEOUT,
    SANITY_TIMEOUT_ENV,
    sanity_global_timeout,
)


class TestSanityGlobalTimeout:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(SANITY_TIMEOUT_ENV, raising=False)
        assert sanity_global_timeout() == DEFAULT_SANITY_TIMEOUT == 1200

    def test_integer_env_value_used(self, monkeypatch):
        monkeypatch.setenv(SANITY_TIMEOUT_ENV, "300")
        assert sanity_global_timeout() == 300

    def test_non_integer_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(SANITY_TIMEOUT_ENV, "five-minutes")
        assert sanity_global_timeout() == DEFAULT_SANITY_TIMEOUT

    def test_non_positive_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(SANITY_TIMEOUT_ENV, "0")
        assert sanity_global_timeout() == DEFAULT_SANITY_TIMEOUT
