"""
LLM input/output debug dumper.

Dumps inputs and outputs of LLM analysis calls to .certora_internal/llm_input_dumps/
for debugging and replay purposes. Each call produces a paired .in.json / .out.json
file with the same prefix, making it easy to correlate inputs with outputs.

Replay: use src/utils/replay_llm.py to re-execute a call from a .in.json file.
"""

import json
import re
from datetime import datetime
from typing import Any, Optional

from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils.constants import DIR_CERTORA_INTERNAL, DIR_LLM_INPUT_DUMPS


def _debug_dir() -> str:
    return cache_path(DIR_CERTORA_INTERNAL, DIR_LLM_INPUT_DUMPS)


def _sanitize_name(name: str) -> str:
    """Replace filesystem-unsafe characters with underscores."""
    return re.sub(r'[/\\:*?"<>| ]', "_", name)


def _serialize_args(args: Any) -> dict:
    """Extract all AnalysisArgs Protocol fields into a plain dict."""
    return {
        "folder": args.folder,
        "rule": args.rule,
        "method": args.method,
        "quiet": args.quiet,
        "recursion_limit": args.recursion_limit,
        "thread_id": args.thread_id,
        "checkpoint_id": args.checkpoint_id,
        "thinking_tokens": args.thinking_tokens,
        "tokens": args.tokens,
        "ecosystem": args.ecosystem,
        "rag_db": args.rag_db,
    }


def dump_llm_input(
    function_name: str,
    args: Any,
    input_messages: list[str],
    initial_prompt: str,
    output_type: Optional[type] = None,
) -> str:
    """
    Dump LLM call inputs to a JSON file.

    Args:
        function_name: "analyze_with_calltraces" or "analyze"
        args: The AnalysisArgs-compatible object
        input_messages: Messages passed to analyze_with_calltraces
        initial_prompt: Initial prompt passed to analyze_with_calltraces
        output_type: Optional Pydantic model type for structured output

    Returns:
        The filename prefix (used to correlate with the output dump)
    """
    fs = get_fs()
    base = _debug_dir()
    fs.mkdirs(base, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_rule = _sanitize_name(args.rule)
    prefix = f"{ts}_{safe_rule}"

    data = {
        "version": 1,
        "function": function_name,
        "timestamp": datetime.now().isoformat(),
        "input_messages": input_messages,
        "initial_prompt": initial_prompt,
        "output_type": output_type.__name__ if output_type is not None else None,
        "args": _serialize_args(args),
    }

    in_path = base + f"/{prefix}.in.json"
    with fs.open(in_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    return prefix


def dump_llm_output(
    prefix: str,
    exit_code: int,
    analysis_text: str,
) -> None:
    """
    Dump LLM call outputs to a JSON file.

    Args:
        prefix: The filename prefix returned by dump_llm_input
        exit_code: The return code (0 = success)
        analysis_text: The analysis result text
    """
    fs = get_fs()
    base = _debug_dir()
    fs.mkdirs(base, exist_ok=True)

    data = {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "exit_code": exit_code,
        "analysis_text": analysis_text,
    }

    out_path = base + f"/{prefix}.out.json"
    with fs.open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
