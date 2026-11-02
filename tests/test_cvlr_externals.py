"""The Solana Prover's external-function warnings, picked out of the alert report."""

from composer.prover.results import AlertReport
from composer.spec.cvlr.externals import ExternalWarnings, external_warnings

_PREFIX = (
    "The following functions are neither inlined nor summarized. They are treated as external. "
    "This is likely to affect soundness. Consult logs and documentation for details on how to fix. "
)


def _warning(names: str) -> str:
    return f"{_PREFIX}[{names}]"


def test_warnings_are_the_distinct_alert_texts_sorted():
    both = _warning("solana_account_info::AccountInfo::resize, anchor_lang::system_program::transfer")
    resize = _warning("solana_account_info::AccountInfo::resize")
    alerts = AlertReport((both, resize, resize, "unrelated [not_a_function]"))

    assert external_warnings(alerts) == ExternalWarnings(tuple(sorted({both, resize})))


def test_a_warning_is_kept_whole():
    drop = "core::ptr::drop_in_place<core::result::Result<(),anchor_lang::error::Error>>"
    array = "core::ptr::drop_in_place<[solana_account_info::AccountInfo; 3]>"
    message = _warning(f"anchor_lang::error::ErrorCode::name, {drop}, {array}")

    assert external_warnings(AlertReport((message,))) == ExternalWarnings((message,))


def test_no_alerts_means_no_warnings():
    assert external_warnings(AlertReport()) == ExternalWarnings()
