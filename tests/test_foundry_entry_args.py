"""Regression test for the console-foundry argument parser.

The entry point consumes model-tier options (``heavy_model``, ``lite_model``,
``tokens``, ...) from the parsed namespace. The parser used to register
``TieredModelOptions`` — a read-only ``@property`` protocol that contributes no
argparse options — so every console-foundry invocation crashed with
``AttributeError: 'Namespace' object has no attribute 'heavy_model'`` before
the pipeline started. The parser must register ``ExtendedModelOptions``, whose
``Arg`` annotations actually produce the options.
"""

from composer.foundry.entry import _build_parser


def test_foundry_parser_provides_model_tier_args() -> None:
    args = _build_parser().parse_args(["proj", "src/C.sol:C", "doc.md"])
    for attr in (
        "heavy_model",
        "lite_model",
        "tokens",
        "thinking_tokens",
        "memory_tool",
        "interleaved_thinking",
        "rag_db",
    ):
        assert hasattr(args, attr), f"parser is missing --{attr.replace('_', '-')}"
