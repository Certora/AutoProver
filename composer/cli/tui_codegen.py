"""Entry point for the codegen TUI workflow (``tui-codegen``)."""

import composer.bind as _

import asyncio
import sys

from composer.input.parsing import fresh_workflow_argument_parser, upload_input
from composer.llm.registry import get_provider_for, uploader_for
from composer.workflow.executor import execute_ai_composer_workflow
from composer.ui.codegen_rich import CodeGenRichApp
from composer.ui.ide_bridge import IDEBridge
from composer.ui.tool_display import tool_context


async def _main() -> int:
    parser = fresh_workflow_argument_parser()
    parser.add_argument(  # type: ignore[attr-defined]
        "--show-checkpoints",
        action="store_true",
        help="Show checkpoint IDs inline in the event log",
    )
    args = parser.parse_args()


    llm = get_provider_for(options=args)
    uploader = uploader_for(llm.provider)

    input_data = await upload_input(uploader, args)

    async with IDEBridge.connect() as ide:
        app = CodeGenRichApp(show_checkpoints=args.show_checkpoints, ide=ide) # type: ignore[attr-defined]

        async def work() -> None:
            app.result = await execute_ai_composer_workflow(
                handler=app,
                llm=llm,
                input=input_data,
                workflow_options=args,
            )

        app.set_work(work)
        with tool_context():
            await app.run_async()

        return app.exit_code


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
