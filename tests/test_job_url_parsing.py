"""Job link parsing in certora_autosetup: legacy /output/ links and app report links.

certora-cli prints the app report link (/orgs/ORG/jobs/UUID/report) when the server sends one,
and the /output/[NUMBER/]HASH link otherwise. Both name the same job, so both must parse to
the same HASH. All ids here are made up.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from certora_autosetup.utils.certora_wget import to_outputs_url
from certora_autosetup.utils.cloud_runner import _conf_has_split_rules
from certora_autosetup.utils.job_utils import (
    extract_group_id_from_text,
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
        f"https://prover.certora.com/orgs/other-org/jobs/{JOB_UUID}/report",
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
    link = f"https://prover.certora.com/orgs/some-org/jobs/{JOB_HASH}/report"
    assert to_outputs_url(link) == f"https://prover.certora.com/v1/domain/jobs/{JOB_HASH}/f/outputs"


def test_outputs_url_rejects_unknown_hosts() -> None:
    with pytest.raises(ValueError):
        to_outputs_url(f"https://other.certora.com/orgs/some-org/jobs/{JOB_HASH}/report")


def test_outputs_url_from_legacy_link() -> None:
    assert to_outputs_url(LEGACY_LINK) == f"https://prover.certora.com/v1/domain/jobs/{JOB_HASH}/f/outputs"


def test_outputs_url_rejects_other_links() -> None:
    with pytest.raises(ValueError):
        to_outputs_url("https://app.certora.com/orgs/some-org/jobs")


def test_group_id_parses_from_split_rules_output() -> None:
    """A --split_rules run prints the group link; extract_group_id_from_text finds its id even
    with the per-rule output links also present in the stdout."""
    group = "49d3e90d-d61b-4f29-bc11-63a9b50ea692"
    out = (
        "See all jobs generated from splitting the rules at "
        f"https://vaas-dev.certora.com/?groupIds={group}\n"
        f"Follow your job at https://vaas-dev.certora.com/output/22224/{JOB_HASH}\n"
    )
    assert extract_group_id_from_text(out) == group


def test_no_group_id_for_an_unsplit_run() -> None:
    assert extract_group_id_from_text(f"Follow your job at {LEGACY_LINK}") is None


def _spec_for(conf_path: Path) -> SimpleNamespace:
    return SimpleNamespace(config_file=SimpleNamespace(path=conf_path))


def test_split_rules_gate_reads_the_conf(tmp_path: Path) -> None:
    """The group handling keys off the conf's split_rules, not a groupIds link (which can also
    appear for unrelated execution-level grouping)."""
    split = tmp_path / "split.conf"
    split.write_text(json.dumps({"split_rules": ["ruleA", "ruleB"], "files": ["A.sol"]}))
    assert _conf_has_split_rules(_spec_for(split)) is True

    plain = tmp_path / "plain.conf"
    plain.write_text(json.dumps({"files": ["A.sol"]}))
    assert _conf_has_split_rules(_spec_for(plain)) is False

    empty = tmp_path / "empty.conf"
    empty.write_text(json.dumps({"split_rules": []}))
    assert _conf_has_split_rules(_spec_for(empty)) is False

    assert _conf_has_split_rules(_spec_for(tmp_path / "missing.conf")) is False
