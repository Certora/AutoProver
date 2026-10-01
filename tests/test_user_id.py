"""The tenant a run works for comes from one environment variable."""

import pytest

from composer.core.user import ANONYMOUS_UID, USER_ID_ENV, get_uid, get_uid_or_none


def test_unset_means_no_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(USER_ID_ENV, raising=False)
    assert get_uid_or_none() is None
    assert get_uid() == ANONYMOUS_UID


def test_blank_means_no_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_ID_ENV, "")
    assert get_uid_or_none() is None
    assert get_uid() == ANONYMOUS_UID


def test_a_set_tenant_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_ID_ENV, "0b0b0b0b-0000-4000-8000-000000000003")
    assert get_uid_or_none() == "0b0b0b0b-0000-4000-8000-000000000003"
    assert get_uid() == "0b0b0b0b-0000-4000-8000-000000000003"
