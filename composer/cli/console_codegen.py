"""Entry point for the codegen console workflow (``console-codegen``).

Non-TUI driver: streams the workflow's progress to stdout via
``ConsoleHandler`` and drives proposal interrupts at the terminal. The
preferred entry for fake-LLM harnesses and scripted runs.
"""

import composer.bind as _

import asyncio
import sys

from composer.input.parsing import fresh_workflow_argument_parser, upload_input
from composer.llm.registry import get_provider_for, uploader_for
from composer.workflow.executor import execute_ai_composer_workflow
from composer.workflow.types import WorkflowSuccess
from composer.ui.console import ConsoleHandler
from composer.ui.tool_display import tool_context


async def _main() -> int:
    parser = fresh_workflow_argument_parser()
    args = parser.parse_args()

    llm = get_provider_for(options=args)
    input_data = await upload_input(uploader_for(llm.provider), args)

    handler = ConsoleHandler(capture_prover_output=args.prover_capture_output)
    with tool_context():
        result = await execute_ai_composer_workflow(
            handler=handler,
            llm=llm,
            input=input_data,
            workflow_options=args,
        )
    return 0 if isinstance(result, WorkflowSuccess) else 1


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
