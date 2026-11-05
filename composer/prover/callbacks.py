"""Stream-event prover callbacks shared by the codegen prover tool and the
source-pipeline buffer prover jobs.

``ProverEventCallbacks`` translates the ``ProverCallbacks`` lifecycle into the
custom stream events the UI renders, keyed by tool_call_id. Both prover entry
points subclass it (the source tool adds its own timing / prover-link handling).
Every override calls ``super()``, so it combines with other callbacks through
multiple inheritance.
"""

from typing import Callable, override

from composer.diagnostics.stream import (
    ProverRun, ProverLink, ProverResult, RuleAnalysisResult, CEXAnalysisStart,
    ProverOutputEvent, CloudPollingEvent,
)
from composer.prover.ptypes import RuleResult
from composer.prover.core import ProverCallbacks


type ProverEvents = (
    ProverOutputEvent | CloudPollingEvent | ProverRun | ProverLink | ProverResult
    | RuleAnalysisResult | CEXAnalysisStart
)


class ProverEventCallbacks(ProverCallbacks):
    def __init__(
        self,
        writer: Callable[[ProverEvents], None],
        tool_call_id: str,
        config: dict | None = None,
    ) -> None:
        """``config``, when given, rides on the ``prover_run`` event."""
        super().__init__()
        self._writer = writer
        self._tool_call_id = tool_call_id
        self._config = config

    @override
    async def on_stdout_line(self, line: str) -> None:
        evt: ProverOutputEvent = {
            "type": "prover_output",
            "tool_call_id": self._tool_call_id,
            "line": line,
        }
        self._writer(evt)
        await super().on_stdout_line(line)

    @override
    async def on_cloud_poll(self, status: str, message: str) -> None:
        evt: CloudPollingEvent = {
            "type": "cloud_polling",
            "tool_call_id": self._tool_call_id,
            "status": status,
            "message": message,
        }
        self._writer(evt)
        await super().on_cloud_poll(status, message)

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        evt: ProverRun = {
            "type": "prover_run",
            "args": args,
            "tool_call_id": self._tool_call_id,
        }
        if self._config is not None:
            evt["config"] = self._config
        self._writer(evt)
        await super().on_prover_run(args)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        evt: ProverResult = {
            "type": "prover_result",
            "tool_call_id": self._tool_call_id,
            "status": {k: v.status for k, v in results.items()},
        }
        self._writer(evt)
        await super().on_prover_result(results)

    @override
    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        evt: RuleAnalysisResult = {
            "type": "rule_analysis",
            "tool_call_id": self._tool_call_id,
            "rule": rule.path.pprint(),
            "analysis": explanation,
        }
        self._writer(evt)
        await super().on_analysis_complete(rule, explanation)

    @override
    async def on_analysis_start(self, rule: RuleResult) -> None:
        evt: CEXAnalysisStart = {
            "type": "cex_analysis",
            "tool_call_id": self._tool_call_id,
            "rule_name": rule.name,
        }
        self._writer(evt)
        await super().on_analysis_start(rule)
