"""Per-buffer completion tracking: each buffer is judged complete over its own runs and digest."""

from composer.prover.ptypes import RulePath
from composer.spec.source.prover import (
    NagMarker,
    ProverHistoryItem,
    ProverRunLog,
    _history_for_buffer,
    buffer_is_complete,
)


def _run(buffer: str, digest: str, results: list[tuple[str, str]]) -> ProverRunLog:
    return ProverRunLog(
        tool_call_id="t",
        prover_results=[(RulePath(rule=r), s) for r, s in results],  # type: ignore[misc]
        rules=None,
        spec_digest="h",
        sort="run",
        declared_rules=[r for r, _ in results],
        state_digest=digest,
        buffer=buffer,
    )


def _complete(history, buffer, digest, all_rules, *, expected_to_fail: set[str] | frozenset[str] = frozenset()):
    return buffer_is_complete(
        history, buffer=buffer, curr_digest=digest,
        expected_to_fail=set(expected_to_fail), curr_status=[], all_rules=all_rules,
    )


def test_complete_when_all_owned_verified():
    h: list[ProverHistoryItem] = [_run("A", "dA", [("r1", "VERIFIED"), ("r2", "VERIFIED")])]
    assert _complete(h, "A", "dA", ["r1", "r2"]) is True


def test_incomplete_when_a_rule_not_verified():
    h: list[ProverHistoryItem] = [_run("A", "dA", [("r1", "VERIFIED"), ("r2", "TIMEOUT")])]
    assert _complete(h, "A", "dA", ["r1", "r2"]) is False


def test_other_buffers_runs_do_not_affect_this_one():
    # B's interleaved (newer) run at a different digest would truncate A's streak without the filter.
    h: list[ProverHistoryItem] = [
        _run("A", "dA", [("r1", "VERIFIED")]),
        _run("B", "dB", [("r2", "VIOLATED")]),
    ]
    assert _complete(h, "A", "dA", ["r1"]) is True
    assert _complete(h, "B", "dB", ["r2"]) is False


def test_stale_digest_does_not_count():
    # The buffer (or an import) was edited: the old run's digest no longer matches.
    h: list[ProverHistoryItem] = [_run("A", "OLD", [("r1", "VERIFIED")])]
    assert _complete(h, "A", "NEW", ["r1"]) is False


def test_expected_to_fail_is_covered():
    h: list[ProverHistoryItem] = [_run("A", "dA", [("r1", "VIOLATED")])]
    assert _complete(h, "A", "dA", ["r1"], expected_to_fail={"r1"}) is True


def test_history_for_buffer_keeps_nag_and_matching_runs():
    nag = NagMarker(sort="nag", nagged_rules=[])
    h: list[ProverHistoryItem] = [
        _run("A", "dA", [("r1", "VERIFIED")]),
        nag,
        _run("B", "dB", [("r2", "VERIFIED")]),
    ]
    kept = _history_for_buffer(h, "A")
    assert nag in kept
    assert all(it["sort"] != "run" or it.get("buffer") == "A" for it in kept)
    assert not any(it["sort"] == "run" and it.get("buffer") == "B" for it in kept)
