"""Stream-event prover callbacks shared by the codegen prover tool and the
source-pipeline ``verify_spec`` tool.

``ProverEventCallbacks`` translates the ``ProverCallbacks`` lifecycle into the
custom stream events the UI renders, keyed by tool_call_id. Both prover entry
points subclass it (the source tool adds its own timing / prover-link handling).
"""

from typing import Callable, override

from composer.diagnostics.stream import (
    ProverRun, ProverResult, RuleAnalysisResult, CEXAnalysisStart,
    ProverOutputEvent, CloudPollingEvent,
)
from composer.prover.ptypes import RuleResult
from composer.prover.core import ProverCallbacks


type ProverEvents = ProverOutputEvent | CloudPollingEvent | ProverRun | ProverResult | RuleAnalysisResult | CEXAnalysisStart


class ProverEventCallbacks(ProverCallbacks):
    def __init__(self, writer: Callable[[ProverEvents], None], tool_call_id: str) -> None:
        self._writer = writer
        self._tool_call_id = tool_call_id

    @override
    async def on_stdout_line(self, line: str) -> None:
        evt: ProverOutputEvent = {
            "type": "prover_output",
            "tool_call_id": self._tool_call_id,
            "line": line,
        }
        self._writer(evt)

    @override
    async def on_cloud_poll(self, status: str, message: str) -> None:
        evt: CloudPollingEvent = {
            "type": "cloud_polling",
            "tool_call_id": self._tool_call_id,
            "status": status,
            "message": message,
        }
        self._writer(evt)

    @override
    async def on_prover_run(self, args: list[str]) -> None:
        evt: ProverRun = {
            "type": "prover_run",
            "args": args,
            "tool_call_id": self._tool_call_id,
        }
        self._writer(evt)

    @override
    async def on_prover_result(self, results: dict[str, RuleResult]) -> None:
        evt: ProverResult = {
            "type": "prover_result",
            "tool_call_id": self._tool_call_id,
            "status": {k: v.status for k, v in results.items()},
        }
        self._writer(evt)

    @override
    async def on_analysis_complete(self, rule: RuleResult, explanation: str) -> None:
        evt: RuleAnalysisResult = {
            "type": "rule_analysis",
            "tool_call_id": self._tool_call_id,
            "rule": rule.path.pprint(),
            "analysis": explanation,
        }
        self._writer(evt)

    @override
    async def on_analysis_start(self, rule: RuleResult) -> None:
        evt: CEXAnalysisStart = {
            "type": "cex_analysis",
            "tool_call_id": self._tool_call_id,
            "rule_name": rule.name,
        }
        self._writer(evt)
