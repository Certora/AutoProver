"""The timeout shape used when streaming a prover results archive down from the cloud.

Worth pinning rather than leaving to review: an overall `total` deadline on this request
is indistinguishable from a stall to the caller, but fails a download that is merely
large. Results archives reach hundreds of megabytes on a big scene, so the bound has to
be on inactivity — `sock_read`, rearmed per chunk — not on elapsed time.
"""

from composer.prover.cloud import _RESULTS_DOWNLOAD_TIMEOUT


def test_no_overall_deadline() -> None:
    # A `total` here aborts mid-stream on a slow-but-progressing transfer.
    assert _RESULTS_DOWNLOAD_TIMEOUT.total is None


def test_stalled_transfer_is_still_bounded() -> None:
    # Without this a dead connection would hang the phase indefinitely.
    assert _RESULTS_DOWNLOAD_TIMEOUT.sock_read is not None
    assert _RESULTS_DOWNLOAD_TIMEOUT.sock_read > 0


def test_connect_is_bounded() -> None:
    assert _RESULTS_DOWNLOAD_TIMEOUT.connect is not None
    assert _RESULTS_DOWNLOAD_TIMEOUT.connect > 0
