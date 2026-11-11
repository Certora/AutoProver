import asyncio
import json
from pathlib import Path

import pytest

from composer.pipeline.refusal import RunRefusal
from composer.pipeline.run_failure import failure_report, reporting_run_failure, run_failure
from composer.spec.artifacts import REPORT_FILE, write_report_file
from composer.spec.source.report.render import render_html
from composer.spec.source.report.schema import (
    AutoProverReport, InternalError, Interrupted, Refused,
)


def _wrap(report_dir: Path):
    return reporting_run_failure(
        report_dir, contract_name="vault", backend="prover", run_mode="comprehensive"
    )


def _read(report_dir: Path) -> AutoProverReport:
    return AutoProverReport.model_validate_json((report_dir / REPORT_FILE).read_text())


def test_a_refusal_is_reported_in_its_own_words(tmp_path):
    with pytest.raises(RunRefusal):
        with _wrap(tmp_path):
            raise RunRefusal("vault is on no Solana platform generation this build supports.")

    report = _read(tmp_path)
    assert report.run_failure == Refused(
        message="vault is on no Solana platform generation this build supports."
    )
    assert report.contract_name == "vault"
    assert report.run_mode == "comprehensive"
    assert (report.properties, report.rules, report.groups) == ([], [], [])


def test_any_other_exception_is_an_internal_error_with_no_text(tmp_path):
    with pytest.raises(KeyError):
        with _wrap(tmp_path):
            raise KeyError("/home/someone/secret/path")

    assert _read(tmp_path).run_failure == InternalError()
    assert "secret" not in (tmp_path / REPORT_FILE).read_text()


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_a_stopped_run_is_interrupted(tmp_path, exc):
    with pytest.raises(type(exc)):
        with _wrap(tmp_path):
            raise exc

    assert _read(tmp_path).run_failure == Interrupted()


def test_a_refusal_wins_inside_an_exception_group():
    group = BaseExceptionGroup("tasks", [
        asyncio.CancelledError(),
        ExceptionGroup("inner", [RunRefusal("no supported platform")]),
    ])
    assert run_failure(group) == Refused(message="no supported platform")
    assert run_failure(BaseExceptionGroup("tasks", [asyncio.CancelledError(), ValueError()])) == (
        Interrupted()
    )
    assert run_failure(ExceptionGroup("tasks", [ValueError()])) == InternalError()


@pytest.mark.parametrize("exc", [SystemExit(2), GeneratorExit()])
def test_a_deliberate_exit_is_not_a_failure(tmp_path, exc):
    with pytest.raises(type(exc)):
        with _wrap(tmp_path):
            raise exc

    assert not (tmp_path / REPORT_FILE).exists()


def test_an_earlier_runs_report_is_removed_before_the_run(tmp_path):
    write_report_file(tmp_path, failure_report(
        contract_name="old", backend="prover", run_mode="comprehensive", failure=InternalError(),
    ))

    with _wrap(tmp_path):
        assert not (tmp_path / REPORT_FILE).exists()

    assert not (tmp_path / REPORT_FILE).exists()


def test_the_runs_own_report_survives_a_later_failure(tmp_path):
    own = failure_report(
        contract_name="vault", backend="prover", run_mode="comprehensive", failure=Interrupted(),
    ).model_copy(update={"run_failure": None})

    with pytest.raises(RuntimeError):
        with _wrap(tmp_path):
            write_report_file(tmp_path, own)
            raise RuntimeError("teardown")

    assert _read(tmp_path).run_failure is None


def test_the_failure_survives_a_round_trip_and_renders(tmp_path):
    with pytest.raises(RunRefusal):
        with _wrap(tmp_path):
            raise RunRefusal("Supported: solana-program 2.x <and> 1.18.")

    raw = json.loads((tmp_path / REPORT_FILE).read_text())
    assert raw["schema_version"] == "4.0"
    assert raw["run_failure"] == {
        "kind": "refused", "message": "Supported: solana-program 2.x <and> 1.18.",
    }
    html = render_html(_read(tmp_path))
    assert "The run failed before it could report its results." in html
    assert "solana-program 2.x &lt;and&gt; 1.18." in html


def test_a_report_without_the_field_still_loads():
    report = failure_report(
        contract_name="vault", backend="prover", run_mode="comprehensive", failure=InternalError(),
    )
    raw = json.loads(report.model_dump_json())
    del raw["run_failure"]
    raw["schema_version"] = "3.8"
    assert AutoProverReport.model_validate(raw).run_failure is None
