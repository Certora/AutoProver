"""Job link parsing in certora_autosetup: legacy /output/ links and app report links.

certora-cli prints the app report link (/orgs/ORG/jobs/UUID/report) when the server sends one,
and the /output/[NUMBER/]HASH link otherwise. Both name the same job, so both must parse to
the same HASH. All ids here are made up.
"""

import pytest

from certora_autosetup.utils.certora_wget import to_outputs_url
from certora_autosetup.utils.job_utils import (
    extract_job_hash_from_url,
    extract_job_id_from_url,
    extract_job_url_from_text,
)

JOB_HASH = "0123abcd4567ef0089ab4cde0123f456"
JOB_UUID = "0123abcd-4567-ef00-89ab-4cde0123f456"

APP_LINK = f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID}/report"
LEGACY_LINK = f"https://prover.certora.com/output/12345/{JOB_HASH}"


@pytest.mark.parametrize(
    "url",
    [
        APP_LINK,
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID}",
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID}/",
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_HASH}/report",
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID.upper()}/report",
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID}/report?tab=rules",
        f"https://prover.certora.com/orgs/some-org/jobs/{JOB_UUID}/report",
        f"https://vaas-stg.certora.com/orgs/some-org/jobs/{JOB_UUID}/report",
    ],
)
def test_app_link_parses_to_the_job_hash(url: str) -> None:
    assert extract_job_id_from_url(url) == JOB_HASH
    assert extract_job_hash_from_url(url) == JOB_HASH


def test_legacy_links_keep_their_ids() -> None:
    assert extract_job_id_from_url(LEGACY_LINK) == f"12345_{JOB_HASH}"
    assert extract_job_hash_from_url(LEGACY_LINK) == JOB_HASH
    hash_only = f"https://prover.certora.com/output/{JOB_HASH}"
    assert extract_job_id_from_url(hash_only) == JOB_HASH


@pytest.mark.parametrize(
    "url",
    [
        "https://app.certora.com/orgs/some-org/jobs",
        "https://app.certora.com/orgs/some-org/jobs/not-a-uuid/report",
        f"https://app.certora.com/orgs/some-org/jobs/{JOB_UUID}/report/extra",
        f"https://app.certora.com/orgs/some-org/mutations/{JOB_UUID}",
    ],
)
def test_non_job_links_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        extract_job_id_from_url(url)


def test_text_with_app_link_yields_the_link() -> None:
    stdout = (
        "Connecting to server...\n"
        f"Follow your job and see verification results at {APP_LINK}?tab=rules\n"
    )
    found = extract_job_url_from_text(stdout)
    assert found == APP_LINK
    assert extract_job_hash_from_url(found) == JOB_HASH


def test_text_without_report_suffix_yields_the_link() -> None:
    link = f"https://app.certora.com/orgs/some-org/jobs/{JOB_HASH}"
    assert extract_job_url_from_text(f"View the results at {link}\n") == link


def test_text_with_legacy_link_yields_the_link() -> None:
    stdout = f"Job is completed! View the results at {LEGACY_LINK}?anonymousKey=abc\n"
    assert extract_job_url_from_text(stdout) == LEGACY_LINK


def test_text_returns_the_last_link() -> None:
    stdout = f"first {LEGACY_LINK}\nthen {APP_LINK}\n"
    assert extract_job_url_from_text(stdout) == APP_LINK


def test_text_without_job_link_yields_none() -> None:
    assert extract_job_url_from_text("Cache warmup failed: https://app.certora.com/orgs/some-org/jobs\n") is None


def test_outputs_url_from_app_link() -> None:
    assert to_outputs_url(f"{APP_LINK}?anonymousKey=k") == (
        f"https://prover.certora.com/v1/domain/jobs/{JOB_HASH}/f/outputs?anonymousKey=k"
    )


def test_outputs_url_keeps_a_prover_host_app_link() -> None:
    link = f"https://vaas-stg.certora.com/orgs/some-org/jobs/{JOB_HASH}/report"
    assert to_outputs_url(link) == f"https://vaas-stg.certora.com/v1/domain/jobs/{JOB_HASH}/f/outputs"


def test_outputs_url_rejects_unknown_hosts() -> None:
    with pytest.raises(ValueError):
        to_outputs_url(f"https://other.certora.com/orgs/some-org/jobs/{JOB_HASH}/report")


def test_outputs_url_from_legacy_link() -> None:
    assert to_outputs_url(LEGACY_LINK) == f"https://prover.certora.com/v1/domain/jobs/{JOB_HASH}/f/outputs"


def test_outputs_url_rejects_other_links() -> None:
    with pytest.raises(ValueError):
        to_outputs_url("https://app.certora.com/orgs/some-org/jobs")
