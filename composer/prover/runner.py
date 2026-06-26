from typing import Optional, List, Callable, override
from pathlib import Path

from langgraph.config import get_stream_writer
from langgraph.runtime import get_runtime

from composer.diagnostics.stream import (
    ProverRun, ProverResult, RuleAnalysisResult, CEXAnalysisStart,
    ProverOutputEvent, CloudPollingEvent,
)
from composer.prover.ptypes import RuleResult
from composer.prover.core import (
    ProverReport, ProverOptions as CoreProverOptions,
    ProverCallbacks, run_prover,
)
from composer.core.state import AIComposerState
from composer.core.context import AIComposerContext

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

async def certora_prover(
    source_files: List[str],
    target_contract: str,
    compiler_version: str,
    loop_iter: int,
    rule: Optional[str],
    state: AIComposerState,
    tool_call_id: str,
    use_working_spec: bool
) -> ProverReport | str:
    if use_working_spec and not state["working_spec"]:
        return "No working spec written."
    runtime = get_runtime(AIComposerContext)
    ctxt = runtime.context
    writer = get_stream_writer()

    with ctxt.vfs_materializer.materialize(state, debug=ctxt.prover_opts.keep_folder) as temp_dir:
        if use_working_spec:
            ws = state["working_spec"]
            assert ws is not None
            (Path(temp_dir) / "rules.spec").write_text(ws)
        try:
            args = source_files.copy()
            args.extend([
                "--verify",
                f"{target_contract}:./rules.spec",
                "--optimistic_loop",
                "--optimistic_hashing",
                "--loop_iter",
                str(loop_iter),
                "--solc", compiler_version,
                "--solc_via_ir",
                "--strict_solc_optimizer",
                "--prover_args",
                "-timeoutCracker true",
            ])
            if rule is not None:
                args.extend(["--rule", rule])

            return await run_prover(
                Path(temp_dir),
                args,
                tool_call_id,
                CoreProverOptions(extra_args=ctxt.prover_opts.extra_args),
                ProverEventCallbacks(writer, tool_call_id),
                ctxt.cex_handler,
            )
        except Exception as e:
            print(e)
            import traceback
            traceback.print_exc()
            raise e
