"""Tests for the preprocessing watchdog state machine.

The watchdog probes the prover's treeview (written with a non-empty `rules` list only once
rule checking begins) to detect jobs stuck in preprocessing. This file covers the pure state
machine; the early-stop layer that wraps it (PreprocessingCheck) and the poll-loop
integration live in test_early_stop.py.
"""

from unittest.mock import MagicMock

from prover_output_utility.exceptions import JobNotFoundError

from certora_autosetup.utils.preprocessing_watchdog import (
    PreprocessingWatchdog,
    WatchdogVerdict,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_watchdog(clock, probe, budget=1800, interval=90, max_errors=3):
    return PreprocessingWatchdog(
        budget_seconds=budget,
        probe_interval_seconds=interval,
        probe_treeview=probe,
        log=lambda *a, **k: None,
        max_consecutive_probe_errors=max_errors,
        clock=clock,
    )


class TestWatchdogStateMachine:
    def test_queue_time_never_starts_the_clock(self):
        clock = FakeClock()
        probe = MagicMock()
        wd = make_watchdog(clock, probe)
        for _ in range(10):
            assert wd.observe(is_running=False) is WatchdogVerdict.WAITING
            clock.advance(600)  # far beyond budget while queued
        probe.assert_not_called()

    def test_first_probe_is_one_interval_into_running(self):
        # A job that has only just entered RUNNING cannot have a treeview yet, so the
        # first probe waits out one interval instead of firing on the first tick.
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, interval=90)
        for _ in range(9):  # 9 ticks x 10s = 80s of RUNNING
            assert wd.observe(is_running=True) is WatchdogVerdict.WAITING
            clock.advance(10)
        probe.assert_not_called()
        clock.advance(10)
        wd.observe(is_running=True)
        assert probe.call_count == 1

    def test_probe_cadence_rate_limited(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, interval=90)
        wd.observe(is_running=True)  # starts the clock; first probe is one interval out
        clock.advance(90)
        wd.observe(is_running=True)
        assert probe.call_count == 1
        for _ in range(8):  # 8 ticks x 10s = 80s after the first probe
            clock.advance(10)
            wd.observe(is_running=True)
        assert probe.call_count == 1  # next probe only allowed at +90s
        clock.advance(10)
        wd.observe(is_running=True)
        assert probe.call_count == 2

    def test_treeview_with_rules_makes_watchdog_dormant(self):
        clock = FakeClock()
        probe = MagicMock(return_value={"rules": [{"name": "sanity"}]})
        wd = make_watchdog(clock, probe, interval=90)
        assert wd.observe(is_running=True) is WatchdogVerdict.WAITING
        clock.advance(90)
        assert wd.observe(is_running=True) is WatchdogVerdict.PREPROCESSING_DONE
        clock.advance(10_000)
        assert wd.observe(is_running=True) is WatchdogVerdict.PREPROCESSING_DONE
        assert probe.call_count == 1  # dormant: no further probes ever

    def test_empty_rules_treeview_is_still_preprocessing(self):
        # Verified live: the cloud serves treeViewStatus.json with rules: [] for the
        # whole preprocessing phase — existence alone must NOT count as done.
        clock = FakeClock()
        probe = MagicMock(return_value={"rules": [], "contract": "Vault"})
        wd = make_watchdog(clock, probe, budget=1800, interval=90)
        verdict = wd.observe(is_running=True)
        while verdict is WatchdogVerdict.WAITING and clock.now < 1000 + 3600:
            clock.advance(90)
            verdict = wd.observe(is_running=True)
        assert verdict is WatchdogVerdict.PREPROCESSING_TIMEOUT

    def test_budget_exceeded_without_treeview(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, budget=1800, interval=90)
        verdict = wd.observe(is_running=True)
        while verdict is WatchdogVerdict.WAITING and clock.now < 1000 + 3600:
            clock.advance(90)
            verdict = wd.observe(is_running=True)
        assert verdict is WatchdogVerdict.PREPROCESSING_TIMEOUT

    def test_not_found_is_not_an_error(self):
        clock = FakeClock()
        probe = MagicMock(side_effect=JobNotFoundError("not yet"))
        wd = make_watchdog(clock, probe, interval=0, max_errors=3)
        for _ in range(10):
            assert wd.observe(is_running=True) is not WatchdogVerdict.DISABLED
            clock.advance(1)

    def _drain(self, wd, clock, ticks):
        verdicts = []
        for _ in range(ticks):
            verdicts.append(wd.observe(is_running=True))
            clock.advance(1)
        return verdicts

    def test_disable_only_after_consecutive_errors(self):
        clock = FakeClock()
        # two transport errors, then a not-found (resets the counter), then three more
        probe = MagicMock(side_effect=[
            RuntimeError("boom"), RuntimeError("boom"), JobNotFoundError("not yet"),
            RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"),
        ])
        wd = make_watchdog(clock, probe, interval=0, max_errors=3)
        verdicts = self._drain(wd, clock, 7)[1:]  # first tick starts the clock
        assert verdicts[:5] == [WatchdogVerdict.WAITING] * 5
        assert verdicts[5] is WatchdogVerdict.DISABLED
        # disabled is terminal
        assert wd.observe(is_running=True) is WatchdogVerdict.DISABLED
        assert probe.call_count == 6

    def test_answered_probe_resets_the_error_count(self):
        # An endpoint that answers is not a broken endpoint: errors scattered between
        # good probes must not accumulate into a permanent self-disable over a long run.
        clock = FakeClock()
        probe = MagicMock(side_effect=[
            RuntimeError("blip"), {"rules": []}, RuntimeError("blip"), {"rules": []},
            RuntimeError("blip"), {"rules": []}, RuntimeError("blip"),
        ])
        wd = make_watchdog(clock, probe, interval=0, max_errors=3)
        verdicts = self._drain(wd, clock, 8)[1:]
        assert WatchdogVerdict.DISABLED not in verdicts
        assert probe.call_count == 7


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
