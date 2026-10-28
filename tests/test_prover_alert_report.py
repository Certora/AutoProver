"""Reading a job's alert report, locally and from the cloud."""

import asyncio
import json
from pathlib import Path

from prover_output_utility.exceptions import JobNotFoundError

import composer.prover.cloud as cloud
from composer.prover.results import ALERT_REPORT, AlertReport, UnreadableAlertReport, read_alert_report


def _write(root: Path, alerts: object) -> Path:
    (root / ALERT_REPORT).parent.mkdir(parents=True, exist_ok=True)
    (root / ALERT_REPORT).write_text(json.dumps(alerts))
    return root


def test_messages_are_kept_in_order_with_repeats(tmp_path):
    root = _write(tmp_path, [
        {"type": "Summarization", "severity": "WARNING", "message": "b"},
        {"type": "Other", "severity": "INFO", "message": "a"},
        {"type": "Summarization", "severity": "WARNING", "message": "b"},
    ])
    assert read_alert_report(root) == AlertReport(("b", "a", "b"))


def test_an_alert_without_a_message_is_skipped(tmp_path):
    root = _write(tmp_path, [{"type": "Other"}, "not an alert", {"message": 3}, {"message": "kept"}])
    assert read_alert_report(root) == AlertReport(("kept",))


def test_no_report_means_no_alerts(tmp_path):
    assert read_alert_report(tmp_path) == AlertReport()


def test_a_report_that_is_not_a_json_list_is_not_an_empty_one(tmp_path):
    (tmp_path / ALERT_REPORT).parent.mkdir(parents=True)
    (tmp_path / ALERT_REPORT).write_text("not json")
    assert isinstance(read_alert_report(tmp_path), UnreadableAlertReport)
    (tmp_path / ALERT_REPORT).write_text(json.dumps({"message": "not a list"}))
    assert isinstance(read_alert_report(tmp_path), UnreadableAlertReport)


class _AlertAPI:
    def __init__(self, outcome: str | Exception):
        self.outcome = outcome

    def fetch_output_file(self, job_id: str, rel_path: str) -> str:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _fetch_alerts(outcome: str | Exception, dest: Path, monkeypatch) -> AlertReport | UnreadableAlertReport:
    monkeypatch.setattr(cloud, "_results_api", lambda: _AlertAPI(outcome))
    return asyncio.run(cloud._fetch_alert_report("deadbeefcafebabe", dest))


def test_a_job_with_no_alert_report_has_no_alerts(tmp_path, monkeypatch):
    assert _fetch_alerts(JobNotFoundError("404"), tmp_path, monkeypatch) == AlertReport()


def test_a_failed_alert_report_fetch_is_not_reported_as_no_alerts(tmp_path, monkeypatch):
    out = _fetch_alerts(ConnectionResetError("reset"), tmp_path, monkeypatch)

    assert isinstance(out, UnreadableAlertReport)


def test_a_fetched_alert_report_is_saved_and_read(tmp_path, monkeypatch):
    out = _fetch_alerts(json.dumps([{"message": "m"}]), tmp_path, monkeypatch)

    assert out == AlertReport(("m",))
    assert (tmp_path / ALERT_REPORT).exists()
