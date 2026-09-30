"""The functions a Prover job treated as external, read from its alert report.

An external call writes nothing but its return value, so these are the places the model can be
unsound without any verdict saying so. The alert text is the Prover's own
(`Reports/alertReport.json`, one `Summarization` alert per rule translated).
"""

import json
from pathlib import Path

from composer.prover.results import ALERT_REPORT, external_functions

_PREFIX = (
    "The following functions are neither inlined nor summarized. They are treated as external. "
    "This is likely to affect soundness. Consult logs and documentation for details on how to fix. "
)


def _write(root: Path, alerts: object) -> Path:
    (root / ALERT_REPORT).parent.mkdir(parents=True, exist_ok=True)
    (root / ALERT_REPORT).write_text(json.dumps(alerts))
    return root


def _alert(names: str) -> dict[str, str]:
    return {"type": "Summarization", "severity": "WARNING", "message": f"{_PREFIX}[{names}]"}


def test_names_are_the_union_across_alerts_sorted_and_deduplicated(tmp_path):
    root = _write(tmp_path, [
        _alert("solana_account_info::AccountInfo::resize, anchor_lang::system_program::transfer"),
        _alert("solana_account_info::AccountInfo::resize"),
        {"type": "Other", "severity": "INFO", "message": "unrelated [not_a_function]"},
    ])
    assert external_functions(root) == (
        "anchor_lang::system_program::transfer",
        "solana_account_info::AccountInfo::resize",
    )


def test_a_generic_argument_list_is_not_split_on_its_own_commas(tmp_path):
    drop = "core::ptr::drop_in_place<core::result::Result<(),anchor_lang::error::Error>>"
    root = _write(tmp_path, [_alert(f"{drop}, solana_account_info::AccountInfo::resize")])
    assert external_functions(root) == (drop, "solana_account_info::AccountInfo::resize")


def test_a_name_with_an_array_type_keeps_the_names_before_it(tmp_path):
    drop = "core::ptr::drop_in_place<[solana_account_info::AccountInfo; 3]>"
    root = _write(tmp_path, [_alert(f"anchor_lang::error::ErrorCode::name, {drop}")])
    assert external_functions(root) == ("anchor_lang::error::ErrorCode::name", drop)


def test_no_report_means_nothing_to_say(tmp_path):
    assert external_functions(tmp_path) == ()
    (tmp_path / ALERT_REPORT).parent.mkdir(parents=True)
    (tmp_path / ALERT_REPORT).write_text("not json")
    assert external_functions(tmp_path) == ()
