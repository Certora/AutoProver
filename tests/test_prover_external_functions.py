"""The Prover's warnings about functions it treated as external, read from its alert report.

An external call writes nothing but its return value, so these are the places the model can be
unsound without any verdict saying so. The alert text is the Prover's own
(`Reports/alertReport.json`, one `Summarization` alert per rule translated) and is passed on whole.
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


def test_warnings_are_the_distinct_alert_texts_sorted(tmp_path):
    both = _alert("solana_account_info::AccountInfo::resize, anchor_lang::system_program::transfer")
    resize = _alert("solana_account_info::AccountInfo::resize")
    root = _write(tmp_path, [
        both,
        resize,
        resize,
        {"type": "Other", "severity": "INFO", "message": "unrelated [not_a_function]"},
    ])
    assert external_functions(root) == tuple(sorted({both["message"], resize["message"]}))


def test_a_warning_is_kept_whole(tmp_path):
    drop = "core::ptr::drop_in_place<core::result::Result<(),anchor_lang::error::Error>>"
    array = "core::ptr::drop_in_place<[solana_account_info::AccountInfo; 3]>"
    message = _alert(f"anchor_lang::error::ErrorCode::name, {drop}, {array}")["message"]
    root = _write(tmp_path, [{"type": "Summarization", "message": message}])
    assert external_functions(root) == (message,)


def test_no_report_means_nothing_to_say(tmp_path):
    assert external_functions(tmp_path) == ()
    (tmp_path / ALERT_REPORT).parent.mkdir(parents=True)
    (tmp_path / ALERT_REPORT).write_text("not json")
    assert external_functions(tmp_path) == ()
