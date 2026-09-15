"""Unit tests for cloud_results' fetch retry: re-fetch a *completed* job, never re-prove."""

import asyncio
from pathlib import Path

import pytest

import composer.prover.cloud as cloud


class _FakeAPI:
    """Stand-in for the POU results client whose fetch fails ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times: int, files: list[str]):
        self.fail_times = fail_times
        self.files = files
        self.calls = 0

    def fetch_sources_and_treeview_files(self, job_id: str, dest: Path) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            # a mid-transfer reset can leave a partial file behind
            (Path(dest) / f"partial_{self.calls}.txt").write_text("x")
            raise ConnectionResetError("Connection reset by peer")
        for name in self.files:
            (Path(dest) / name).write_text("ok")


@pytest.fixture(autouse=True)
def _no_backoff_wait(monkeypatch):
    # keep the test instant — exercise the retry loop, not the clock
    monkeypatch.setattr(cloud, "_FETCH_BACKOFF_BASE_S", 0.0)


def _fetch(fake: _FakeAPI, dest: Path, monkeypatch) -> None:
    monkeypatch.setattr(cloud, "_results_api", lambda: fake)
    asyncio.run(cloud._fetch_results("deadbeefcafebabe", dest))


def test_succeeds_after_transient_failures(tmp_path, monkeypatch):
    fake = _FakeAPI(fail_times=2, files=["a.json", "b.json"])
    _fetch(fake, tmp_path, monkeypatch)
    assert fake.calls == 3  # 2 transient failures, then success — the finished job is only re-read
    # each failed attempt's partial write was cleared before the next; only the good set remains
    assert {p.name for p in tmp_path.iterdir()} == {"a.json", "b.json"}


def test_gives_up_after_max_attempts(tmp_path, monkeypatch):
    fake = _FakeAPI(fail_times=99, files=["a.json"])
    with pytest.raises(ConnectionResetError):
        _fetch(fake, tmp_path, monkeypatch)
    assert fake.calls == cloud._FETCH_MAX_ATTEMPTS  # capped — it never loops forever
