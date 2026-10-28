"""Reading a job's alert report, locally and from the cloud."""

import asyncio
import json
import socket
from pathlib import Path

from aiohttp import web

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


_JOB = "deadbeefcafebabe"
_KEY = "k"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _fetch_from(status: int, body: str) -> AlertReport | UnreadableAlertReport:
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        assert request.path == f"/v1/domain/jobs/{_JOB}/f/alertReport.json"
        assert request.query["anonymousKey"] == _KEY
        return web.Response(status=status, text=body)

    app.router.add_get("/v1/domain/jobs/{job_id}/f/alertReport.json", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        job = cloud.CloudJob(site.name, "1", _JOB, _KEY)
        return await cloud._fetch_alert_report(job)
    finally:
        await runner.cleanup()


def test_a_job_with_no_alert_report_has_no_alerts():
    assert asyncio.run(_fetch_from(404, "")) == AlertReport()


def test_a_failed_alert_report_fetch_is_not_reported_as_no_alerts():
    job = cloud.CloudJob(f"http://127.0.0.1:{_closed_port()}", "1", _JOB, _KEY)
    out = asyncio.run(cloud._fetch_alert_report(job))

    assert isinstance(out, UnreadableAlertReport)


def test_a_fetched_alert_report_is_parsed():
    assert asyncio.run(_fetch_from(200, json.dumps([{"message": "m"}]))) == AlertReport(("m",))


def test_a_fetched_report_that_is_not_a_json_list_is_not_an_empty_one():
    assert isinstance(asyncio.run(_fetch_from(200, "not json")), UnreadableAlertReport)
