from typing import Annotated, Optional
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from graphcore.graph import tool_return
from graphcore.tools.schemas import WithInjectedId, WithAsyncDependencies

from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.prebuilt import InjectedState
from langgraph.runtime import get_runtime
from langgraph.types import Command

from composer.core.state import AIComposerState
from composer.core.context import AIComposerContext, ProverOptions, stamp
from composer.core.validation import ProverValidation
from composer.prover.core import ProverReport, CexHandler, ProverOptions as CoreProverOptions, run_prover
from composer.prover.callbacks import ProverEventCallbacks
from composer.ui.tool_display import tool_display


@dataclass
class ProverDeps:
    """Per-run dependencies injected into the prover tool — the CEX-analysis
    strategy and the prover options — bound at workflow setup (see the codegen
    executor) rather than read off the runtime context."""
    cex_handler: CexHandler
    prover_opts: ProverOptions


@tool_display(
    lambda p: (
        "Running prover: " + p.get("target_contract", "")
        + (f" — rule {p['rule']}" if p.get("rule") else "")
    ),
    None,
)
class CertoraProverTool(WithAsyncDependencies[Command, ProverDeps], WithInjectedId):
    """
    Invoke the Certora Prover, a powerful symbolic reasoning tool for verifying the correctness of smart contracts.

    The Certora Prover operates on one or more smart contracts files, and a specification for their behavior, written in a domain
    specific language called CVL, for which you have the documentation. A specification for the code you are generating
    has been provided for you, and is composed of multiple `rules`. Each rule defines the acceptable behavior
    of the smart contract in terms of assertions; a violated assertion means the smart contract's behavior is
    unacceptable.

    The Certora Prover will automatically check whether a smart contract instance (the "contract under verifiction")
    satisfies the provided specification on a per rule basis.

    For each rule, the prover will give one of the following result:
    1. VERIFIED: the smart contract satisfies the rule for all possible inputs
    2. VIOLATED: The smart contract violates the specification. As part of this result, the prover will provide
    a concrete counter example for the input/states which lead to the violation
    3. TIMEOUT: The automated reasoning used by the prover timed out before giving a response either way
    4. SANITY_FAIL: The rule succeeded, but was vacuously true, perhaps due to too specific requirements
    5. ERROR/other: There was some internal error within the prover

    When there are large numbers of failures, these result may be truncated. If this occurs, a summary of results from
    the prover will be provided.
    """

    source_files: list[str] = Field(description="""
      The (relative) filenames to verify. These files MUST have been put into the virtual filesystem with
      prior invocations of the PutFile tool.

      IMPORTANT: Only the files containing the contracts to be verified need to be passed as this
      argument. Thus, if verifying `MyContract`, only `src/MyContract.sol` needs to be provided here.
      HOWEVER: any files necessary to the compilation of `MyContract` must also have been placed on the
      virtual filesystem before calling the prover.
    """)

    target_contract: str = Field(description="""
       The name of the contract to check the specification against. This contract should be
       the main "entry point" for the functionality being synthesized. NB: the name of this
       contract should match one of the names provided in `source_files` according to that input's rules.
    """)

    compiler_version: str = \
        Field(description="The compiler to use when compiling the Solidity source code."
              "This parameter is specified as 'solcX.Y' where X.Y indicate solidity version 0.X.Y, or simply `solc`, which uses the system "
              "default. For example, solc8.29 uses the Solidity compiler for version 0.8.29, solc7.0 uses 0.7.0, etc." \
              "When possible, use specific compiler versions, falling back on the system default `solc` only as a last resort." \
              "Attempt to use the most recent compiler version if you can, which is solc8.29.")

    loop_iter: int = \
        Field(description="The Certora Prover uses bounded verification for looping code; "
              "any statically unbounded loops are unrolled a fixed number time, and then the loop condition is *assumed*. "
              "You should set this number as low as possible so that non-trivial looping behavior is observed. "
              "While values above 3 are technically supported, performance becomes exponentially worse, and thus"
              "should be avoided whenever possible.")

    rule: Optional[str] = \
        Field(description="The specific rule to check from the `spec_file`. If unspecified,"
              "all rules are run. Before delivering the finished code to the user, ensure that all rules pass on the most"
              "up to date version of the code. However, when iteratively developing code, it may be useful to focus on a"
              "single, 'problematic' rule.")
    
    use_working_spec : bool = Field(description="Use the working copy of the spec instead of the master copy.")

    state: Annotated[AIComposerState, InjectedState]

    async def run(self) -> Command:
        with self.tool_deps() as deps:
            result = await _run_certora_prover(self, deps.cex_handler, deps.prover_opts)
        match result:
            case str():
                return tool_return(tool_call_id=self.tool_call_id, content=result)
            case ProverReport():
                # The handler already rendered the full report into result_str
                # (including any volume summarization it chose to do); there's no
                # separate truncated/summarized shape to branch on anymore.
                if result.all_verified and not self.use_working_spec and not self.rule:
                    return Command(
                        update={
                            "messages": [
                                ToolMessage(
                                    tool_call_id=self.tool_call_id,
                                    content=result.result_str
                                )
                            ],
                            **stamp(ProverValidation(), self.state),
                        }
                    )
                return tool_return(tool_call_id=self.tool_call_id, content=result.result_str)


async def _run_certora_prover(
    tool: CertoraProverTool,
    cex_handler: CexHandler,
    prover_opts: ProverOptions,
) -> ProverReport | str:
    """Codegen-specific prover invocation: materialize the VFS, assemble the
    fixed codegen ``certoraRun`` args from the tool's fields, and run. Reads
    everything off ``tool`` (its args map ~1:1 onto a prover run); the only
    runtime-context read is ``vfs_materializer``."""
    if tool.use_working_spec and not tool.state["working_spec"]:
        return "No working spec written."
    ctxt = get_runtime(AIComposerContext).context
    writer = get_stream_writer()

    with ctxt.vfs_materializer.materialize(tool.state, debug=prover_opts.keep_folder) as temp_dir:
        if tool.use_working_spec:
            ws = tool.state["working_spec"]
            assert ws is not None
            (Path(temp_dir) / "rules.spec").write_text(ws)
        try:
            args = tool.source_files.copy()
            args.extend([
                "--verify",
                f"{tool.target_contract}:./rules.spec",
                "--optimistic_loop",
                "--optimistic_hashing",
                "--loop_iter",
                str(tool.loop_iter),
                "--solc", tool.compiler_version,
                "--solc_via_ir",
                "--strict_solc_optimizer",
                "--prover_args",
                "-timeoutCracker true",
            ])
            if tool.rule is not None:
                args.extend(["--rule", tool.rule])

            return await run_prover(
                Path(temp_dir),
                args,
                tool.tool_call_id,
                CoreProverOptions(extra_args=prover_opts.extra_args),
                ProverEventCallbacks(writer, tool.tool_call_id),
                cex_handler,
            )
        except Exception as e:
            print(e)
            import traceback
            traceback.print_exc()
            raise e
