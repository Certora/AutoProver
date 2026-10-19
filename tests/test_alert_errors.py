"""ERROR entries of a prover alert report, and the failure text an author sees."""

import json

from prover_output_utility.models import JobStatus

from composer.prover.cloud import CloudJobError
from composer.prover.core import cloud_job_failure_message
from composer.prover.results import OUT_OF_RESOURCES, ErrorAlert, error_alerts

_LINK = "https://prover.certora.com/jobStatus/37632/9fc4c8ae6e1944dca697e568d679772f?anonymousKey=k"
_HINT = (
    "TAC Clock::get expected to extract 5 TAC variables from call "
    "sol_get_clock_sysvar  /* 0x1c4b0 */"
)
_MESSAGE = (
    "An internal Prover error occurred, please contact support, "
    "and see low-level crash details below:"
)


def test_an_error_alert_shows_its_hint_and_skips_warnings():
    text = json.dumps([
        {"type": "Summarization", "severity": "WARNING", "message": "external [sol_sha256]"},
        {"type": "General", "severity": "ERROR", "message": _MESSAGE, "hint": _HINT},
    ])
    assert error_alerts(text) == (ErrorAlert("General", _HINT),)


def test_a_blank_hint_falls_back_to_the_message():
    text = json.dumps([
        {"type": "General", "severity": "ERROR", "message": _MESSAGE, "hint": "  "},
        {"type": "General", "severity": "ERROR", "message": "second"},
    ])
    assert error_alerts(text) == (
        ErrorAlert("General", _MESSAGE),
        ErrorAlert("General", "second"),
    )


def test_unreadable_or_non_error_reports_say_nothing():
    assert error_alerts("not json") == ()
    assert error_alerts("{}") == ()
    assert error_alerts(json.dumps([
        {"type": "General", "severity": "ERROR", "message": "  ", "hint": ""},
        {"severity": "WARNING", "message": "no"},
        "not an object",
    ])) == ()


def test_the_failure_text_names_the_crash_and_the_job():
    text = cloud_job_failure_message(
        CloudJobError(JobStatus.FAILED, _LINK, (ErrorAlert("General", _HINT),))
    )
    assert text == (
        "Prover cloud job did not produce results (status FAILED). "
        f"The job and its failure output are at {_LINK}\n\n"
        "The prover reported an internal error. "
        "Resubmitting this draft will fail the same way.\n"
        f"{_HINT}"
    )


_TIMEOUT = (
    "Reached global timeout. Hard stopping. If you have tried to verify multiple rules"
    " and the Prover did not finish the computation for some of them, we suggest to run the "
    "Prover for the individual unfinished rules separately."
)


def test_a_global_timeout_says_to_rerun_the_unfinished_rules():
    text = cloud_job_failure_message(
        CloudJobError(
            JobStatus.HALTED,
            _LINK,
            (ErrorAlert(OUT_OF_RESOURCES, _TIMEOUT),),
        )
    )
    assert text == (
        "Prover cloud job did not produce results (status HALTED). "
        f"The job and its failure output are at {_LINK}\n\n"
        "The prover ran out of resources before finishing. "
        "Rerun the unfinished rules separately.\n"
        f"{_TIMEOUT}"
    )


def test_a_crash_alongside_a_timeout_is_an_internal_error():
    text = cloud_job_failure_message(
        CloudJobError(
            JobStatus.FAILED,
            _LINK,
            (ErrorAlert(OUT_OF_RESOURCES, _TIMEOUT), ErrorAlert("General", _HINT)),
        )
    )
    assert "Resubmitting this draft will fail the same way." in text
    assert "Rerun the unfinished rules separately." not in text
    assert _TIMEOUT in text
    assert _HINT in text


def test_a_failure_without_alerts_is_the_status_and_the_link():
    text = cloud_job_failure_message(CloudJobError(JobStatus.CANCELED, _LINK))
    assert text == (
        "Prover cloud job did not produce results (status CANCELED). "
        f"The job and its failure output are at {_LINK}"
    )
