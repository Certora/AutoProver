"""The billing record, job_info.json, as a file any command can write."""

import json
from pathlib import Path

from composer.diagnostics.timing import RunSummary
from composer.spec.artifacts import job_info_payload, write_job_info_file


def test_the_payload_carries_the_identity_and_the_token_usage() -> None:
    summary = RunSummary()
    payload = job_info_payload(summary, user_id="u1", run_mode="standard")
    assert payload["user_id"] == "u1"
    assert payload["run_id"] == summary.run_id
    assert payload["run_mode"] == "standard"
    assert payload["token_usage"] == summary.token_usage_summary()


def test_the_file_lands_in_the_report_dir_with_extra_usage(tmp_path: Path) -> None:
    summary = RunSummary()
    body = {
        **job_info_payload(summary, user_id="u1", run_mode="standard"),
        "prover_usage": summary.prover_usage_summary(),
    }
    out = write_job_info_file(tmp_path / "ap_report", body)
    assert out == tmp_path / "ap_report" / "job_info.json"
    written = json.loads(out.read_text())
    assert set(written) == {"user_id", "run_id", "run_mode", "token_usage", "prover_usage"}
    assert written["run_id"] == summary.run_id
