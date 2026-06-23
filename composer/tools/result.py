from graphcore.tools.results import result_tool_generator

from composer.core.context import check_completion
from composer.core.state import AIComposerState, ResultStateSchema

code_result = result_tool_generator("generated_code", ResultStateSchema,
"""
Used to communicate when the generated code is complete and satisfies all of the rules in specification.
""",
    (AIComposerState, lambda s, _r, _id: check_completion(s))
)
