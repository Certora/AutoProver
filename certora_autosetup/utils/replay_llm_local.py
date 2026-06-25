#!/usr/bin/env python3
"""
Replay LLM analysis calls through a local model and compare against Claude outputs.

Reads .in.json / .out.json pairs produced by the LLM debug dumper (llm_debug.py),
replays each input through the local LLM backend, and generates a comparison report
showing schema validity, categorical field agreement, and text similarity.

Usage:
    python -m certora_autosetup.utils.replay_llm_local path/to/llm_input_dumps/ [options]

Prerequisites:
    - openai package installed: pip install -e ".[local-llm]"
    - Ollama running: ollama serve
    - Model pulled: ollama pull qwen2.5-coder:32b
"""

import argparse
import difflib
import fnmatch
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, get_origin

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pydantic import BaseModel, ValidationError

from certora_autosetup.utils.constants import (
    DEFAULT_LOCAL_LLM_BASE_URL,
    DEFAULT_LOCAL_LLM_MODEL,
    LLM_BACKEND_ENV,
    LOCAL_LLM_BASE_URL_ENV,
    LOCAL_LLM_MODEL_ENV,
)

# Default system prompt — PreAudit can override this via SYSTEM_PROMPT_OVERRIDE
_SYSTEM_PROMPT: Optional[str] = None
SYSTEM_PROMPT_OVERRIDE: Optional[str] = None


def _get_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        if SYSTEM_PROMPT_OVERRIDE is not None:
            _SYSTEM_PROMPT = SYSTEM_PROMPT_OVERRIDE
        else:
            _SYSTEM_PROMPT = (
                "You are analyzing smart contract verification results from the Certora Prover. "
                "Be concrete, note uncertainties, and focus on security-relevant details."
            )
    return _SYSTEM_PROMPT


def _build_output_type_registry() -> dict[str, type[BaseModel]]:
    """Build the output type registry with AutoSetup-native types.

    PreAudit extends this registry with its own types (IterationResult, SynthesisResult, etc.)
    by calling register_output_types() at startup.
    """
    registry: dict[str, type[BaseModel]] = {}

    # AutoSetup-native types only
    _imports: list[tuple[str, str, str]] = [
        ("MatchAnalysis", "certora_autosetup.setup.setup_summaries", "MatchAnalysis"),
    ]

    import importlib

    for name, module_path, attr in _imports:
        try:
            mod = importlib.import_module(module_path)
            registry[name] = getattr(mod, attr)
        except (ImportError, AttributeError, SystemExit):
            pass

    return registry


OUTPUT_TYPE_REGISTRY: dict[str, type[BaseModel]] = _build_output_type_registry()


def register_output_types(types: dict[str, type[BaseModel]]) -> None:
    """Register additional output types into the registry (called by PreAudit)."""
    OUTPUT_TYPE_REGISTRY.update(types)

# Fields set externally (not by LLM) — skip during comparison
EXTERNALLY_SET_FIELDS: dict[str, set[str]] = {
    "SynthesisResult": {
        "computed_confidence_score",
        "computed_classification",
        "is_single_iteration",
        "source_location",
        "contracts",
        "methods_by_contract",
    },
    "JudgementResult": {
        "recomputed_score",
        "recomputed_classification",
        "classification_changed",
    },
}

# Max chars for text similarity to keep SequenceMatcher fast
TEXT_SIMILARITY_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FieldComparison:
    field_name: str
    field_kind: Literal["categorical", "text", "numeric", "skipped"]
    claude_value: Any = None
    local_value: Any = None
    match: Optional[bool] = None  # for categorical/numeric
    similarity: Optional[float] = None  # for text


@dataclass
class OutputComparison:
    schema_valid: bool
    schema_error: Optional[str] = None
    field_comparisons: list[FieldComparison] = field(default_factory=list)
    overall_categorical_match: Optional[bool] = None
    average_text_similarity: Optional[float] = None


@dataclass
class ReplayResult:
    input_file: str
    function: str
    rule: str
    output_type_name: Optional[str]
    replay_time_s: float
    exit_code: int
    analysis_text: str
    error: Optional[str] = None
    comparison: Optional[OutputComparison] = None


# ---------------------------------------------------------------------------
# Message reconstruction
# ---------------------------------------------------------------------------


def build_replay_messages(dump: dict) -> tuple[list[Any], list[Any]]:
    """Reconstruct system + messages arrays from .in.json dump.

    Mirrors SimpleViolationAnalyzer._build_messages() without cache_control
    (irrelevant for local backend, stripped by _anthropic_messages_to_openai anyway).
    """
    initial_prompt = dump["initial_prompt"]
    input_messages = dump["input_messages"]

    system_content: list[Any] = [
        {"type": "text", "text": _get_system_prompt()},
        {"type": "text", "text": initial_prompt},
    ]

    user_content = [{"type": "text", "text": msg} for msg in input_messages]
    messages: list[Any] = [{"role": "user", "content": user_content}]

    return system_content, messages


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_single(
    in_path: Path,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    verbose: bool = False,
) -> ReplayResult:
    """Replay a single .in.json through the local LLM backend."""
    from certora_autosetup.utils.llm_util import call_llm_messages, call_llm_messages_structured

    with open(in_path) as f:
        dump = json.load(f)

    function_name = dump.get("function", "unknown")
    rule = dump.get("args", {}).get("rule", "unknown")
    output_type_name = dump.get("output_type")

    output_type = None
    if output_type_name is not None:
        output_type = OUTPUT_TYPE_REGISTRY.get(output_type_name)
        if output_type is None:
            return ReplayResult(
                input_file=in_path.name,
                function=function_name,
                rule=rule,
                output_type_name=output_type_name,
                replay_time_s=0.0,
                exit_code=1,
                analysis_text="",
                error=f"Unknown output_type: {output_type_name}. Known: {list(OUTPUT_TYPE_REGISTRY.keys())}",
            )

    system_content, messages = build_replay_messages(dump)

    start = time.monotonic()
    try:
        if output_type is not None:
            result = call_llm_messages_structured(
                system=system_content,
                messages=messages,
                output_type=output_type,
                max_tokens=max_tokens,
                temperature=temperature,
                verbose=verbose,
            )
            analysis_text = result.model_dump_json(indent=2)
        else:
            analysis_text = call_llm_messages(
                system=system_content,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                verbose=verbose,
            )
        elapsed = time.monotonic() - start
        return ReplayResult(
            input_file=in_path.name,
            function=function_name,
            rule=rule,
            output_type_name=output_type_name,
            replay_time_s=round(elapsed, 2),
            exit_code=0,
            analysis_text=analysis_text,
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        return ReplayResult(
            input_file=in_path.name,
            function=function_name,
            rule=rule,
            output_type_name=output_type_name,
            replay_time_s=round(elapsed, 2),
            exit_code=1,
            analysis_text="",
            error=f"{type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _is_categorical_field(model_class: type[BaseModel], field_name: str) -> bool:
    """Check if a Pydantic field is categorical (Literal[...] or bool)."""
    info = model_class.model_fields[field_name]
    annotation = info.annotation
    if annotation is bool:
        return True
    return get_origin(annotation) is Literal


def text_similarity(a: str, b: str) -> float:
    """Return 0.0-1.0 similarity ratio between two strings."""
    a = a[:TEXT_SIMILARITY_LIMIT]
    b = b[:TEXT_SIMILARITY_LIMIT]
    return difflib.SequenceMatcher(None, a, b).ratio()


def compare_structured(
    claude_text: str,
    local_text: str,
    output_type: type[BaseModel],
    output_type_name: str,
) -> OutputComparison:
    """Field-level comparison for structured outputs."""
    # Parse local output
    try:
        local_obj = output_type.model_validate_json(local_text)
    except (ValidationError, json.JSONDecodeError) as e:
        return OutputComparison(schema_valid=False, schema_error=str(e))

    # Parse Claude output
    try:
        claude_obj = output_type.model_validate_json(claude_text)
    except (ValidationError, json.JSONDecodeError):
        # Claude output can't parse either — fall back to text-only comparison
        sim = text_similarity(claude_text, local_text)
        return OutputComparison(
            schema_valid=True,
            schema_error=None,
            average_text_similarity=sim,
        )

    skip_fields = EXTERNALLY_SET_FIELDS.get(output_type_name, set())
    comparisons: list[FieldComparison] = []

    for name in output_type.model_fields:
        if name in skip_fields:
            comparisons.append(FieldComparison(field_name=name, field_kind="skipped"))
            continue

        claude_val = getattr(claude_obj, name)
        local_val = getattr(local_obj, name)

        if _is_categorical_field(output_type, name):
            comparisons.append(
                FieldComparison(
                    field_name=name,
                    field_kind="categorical",
                    claude_value=claude_val,
                    local_value=local_val,
                    match=claude_val == local_val,
                )
            )
        elif isinstance(claude_val, str):
            sim = text_similarity(str(claude_val), str(local_val))
            comparisons.append(
                FieldComparison(
                    field_name=name,
                    field_kind="text",
                    claude_value=str(claude_val)[:200],
                    local_value=str(local_val)[:200],
                    similarity=round(sim, 4),
                )
            )
        elif isinstance(claude_val, (int, float)):
            comparisons.append(
                FieldComparison(
                    field_name=name,
                    field_kind="numeric",
                    claude_value=claude_val,
                    local_value=local_val,
                    match=claude_val == local_val,
                )
            )
        else:
            comparisons.append(FieldComparison(field_name=name, field_kind="skipped"))

    categorical = [c for c in comparisons if c.field_kind == "categorical"]
    text_fields = [c for c in comparisons if c.field_kind == "text"]

    all_cat_match = all(c.match for c in categorical) if categorical else None
    text_sims = [c.similarity for c in text_fields if c.similarity is not None]
    avg_sim = round(sum(text_sims) / len(text_sims), 4) if text_sims else None

    return OutputComparison(
        schema_valid=True,
        field_comparisons=comparisons,
        overall_categorical_match=all_cat_match,
        average_text_similarity=avg_sim,
    )


def compare_outputs(
    claude_text: str,
    local_text: str,
    output_type_name: Optional[str],
) -> OutputComparison:
    """Compare Claude output vs local LLM output."""
    if output_type_name is None or output_type_name not in OUTPUT_TYPE_REGISTRY:
        sim = text_similarity(claude_text, local_text)
        return OutputComparison(schema_valid=True, average_text_similarity=round(sim, 4))

    output_type = OUTPUT_TYPE_REGISTRY[output_type_name]
    return compare_structured(claude_text, local_text, output_type, output_type_name)


# ---------------------------------------------------------------------------
# Verdict change analysis
# ---------------------------------------------------------------------------


@dataclass
class VerdictChangeStats:
    """Tracks is_defect and confidence changes across results."""

    total: int = 0  # results that have both is_defect and confidence fields
    is_defect_changed: int = 0  # is_defect differs
    is_defect_yes_to_no: int = 0  # Claude said YES, local said NO
    is_defect_no_to_yes: int = 0  # Claude said NO, local said YES
    confidence_changed_only: int = 0  # is_defect same, confidence differs
    confidence_high_to_low: int = 0  # Claude said HIGH, local said LOW
    confidence_low_to_high: int = 0  # Claude said LOW, local said HIGH
    both_same: int = 0  # both fields agree


def compute_verdict_changes(results: list[ReplayResult]) -> VerdictChangeStats:
    """Count is_defect and confidence changes across all compared results."""
    stats = VerdictChangeStats()

    for r in results:
        if not r.comparison or not r.comparison.field_comparisons:
            continue

        fields_by_name = {fc.field_name: fc for fc in r.comparison.field_comparisons}
        defect_fc = fields_by_name.get("is_defect")
        confidence_fc = fields_by_name.get("confidence")

        if defect_fc is None or defect_fc.field_kind != "categorical":
            continue
        if confidence_fc is None or confidence_fc.field_kind != "categorical":
            continue

        stats.total += 1

        if not defect_fc.match:
            stats.is_defect_changed += 1
            if defect_fc.claude_value == "YES" and defect_fc.local_value == "NO":
                stats.is_defect_yes_to_no += 1
            elif defect_fc.claude_value == "NO" and defect_fc.local_value == "YES":
                stats.is_defect_no_to_yes += 1
        elif not confidence_fc.match:
            stats.confidence_changed_only += 1
            if confidence_fc.claude_value == "HIGH" and confidence_fc.local_value == "LOW":
                stats.confidence_high_to_low += 1
            elif confidence_fc.claude_value == "LOW" and confidence_fc.local_value == "HIGH":
                stats.confidence_low_to_high += 1
        else:
            stats.both_same += 1

    return stats


# ---------------------------------------------------------------------------
# Local output storage
# ---------------------------------------------------------------------------


def write_local_output(in_path: Path, result: ReplayResult, model: str) -> Path:
    """Write .local.out.json alongside the original files."""
    out_path = in_path.with_suffix("").with_suffix(".local.out.json")
    data = {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "exit_code": result.exit_code,
        "analysis_text": result.analysis_text,
        "model": model,
        "replay_time_s": result.replay_time_s,
    }
    if result.error:
        data["error"] = result.error
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return out_path


def read_claude_output(in_path: Path) -> Optional[str]:
    """Read the Claude .out.json corresponding to an .in.json."""
    out_path = in_path.with_suffix("").with_suffix(".out.json")
    if not out_path.exists():
        return None
    with open(out_path) as f:
        data = json.load(f)
    if data.get("exit_code", 1) != 0:
        return None
    return data.get("analysis_text")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_json_report(results: list[ReplayResult], model: str) -> dict:
    """Generate the machine-readable JSON comparison report."""
    total = len(results)
    successful = [r for r in results if r.comparison is not None]
    structured = [r for r in successful if r.comparison and r.comparison.field_comparisons]
    errors = [r for r in results if r.error is not None]

    schema_valid_count = sum(1 for r in successful if r.comparison and r.comparison.schema_valid)
    cat_match_count = sum(1 for r in structured if r.comparison and r.comparison.overall_categorical_match is True)
    cat_total = sum(1 for r in structured if r.comparison and r.comparison.overall_categorical_match is not None)
    text_sims = [
        r.comparison.average_text_similarity
        for r in successful
        if r.comparison and r.comparison.average_text_similarity is not None
    ]
    total_time = sum(r.replay_time_s for r in results)

    verdict = compute_verdict_changes(results)

    summary = {
        "schema_valid_count": schema_valid_count,
        "schema_valid_total": len(successful),
        "schema_valid_pct": round(100 * schema_valid_count / len(successful), 1) if successful else 0.0,
        "categorical_match_count": cat_match_count,
        "categorical_match_total": cat_total,
        "categorical_match_pct": round(100 * cat_match_count / cat_total, 1) if cat_total else 0.0,
        "average_text_similarity": round(sum(text_sims) / len(text_sims), 4) if text_sims else 0.0,
        "replay_errors": len(errors),
        "total_replay_time_s": round(total_time, 1),
        "verdict_total": verdict.total,
        "verdict_is_defect_changed": verdict.is_defect_changed,
        "verdict_is_defect_yes_to_no": verdict.is_defect_yes_to_no,
        "verdict_is_defect_no_to_yes": verdict.is_defect_no_to_yes,
        "verdict_confidence_changed_only": verdict.confidence_changed_only,
        "verdict_confidence_high_to_low": verdict.confidence_high_to_low,
        "verdict_confidence_low_to_high": verdict.confidence_low_to_high,
        "verdict_both_agree": verdict.both_same,
    }

    # Per output_type breakdown
    by_type: dict[str, dict[str, Any]] = {}
    for r in successful:
        key = r.output_type_name or "null"
        if key not in by_type:
            by_type[key] = {"count": 0, "schema_valid": 0, "field_summaries": {}}
        by_type[key]["count"] += 1
        if r.comparison and r.comparison.schema_valid:
            by_type[key]["schema_valid"] += 1
        if r.comparison:
            for fc in r.comparison.field_comparisons:
                if fc.field_kind == "skipped":
                    continue
                fs = by_type[key]["field_summaries"]
                if fc.field_name not in fs:
                    fs[fc.field_name] = {"values": [], "kind": fc.field_kind}
                if fc.field_kind == "categorical" and fc.match is not None:
                    fs[fc.field_name]["values"].append(fc.match)
                elif fc.field_kind == "text" and fc.similarity is not None:
                    fs[fc.field_name]["values"].append(fc.similarity)

    # Aggregate field summaries
    for type_info in by_type.values():
        for fname, fdata in type_info["field_summaries"].items():
            vals = fdata.pop("values")
            if fdata["kind"] == "categorical" and vals:
                fdata["match_count"] = sum(vals)
                fdata["match_pct"] = round(100 * sum(vals) / len(vals), 1)
            elif fdata["kind"] == "text" and vals:
                fdata["avg_similarity"] = round(sum(vals) / len(vals), 4)

    # Per-file results
    per_file = []
    for r in results:
        entry: dict[str, Any] = {
            "input_file": r.input_file,
            "function": r.function,
            "rule": r.rule,
            "output_type": r.output_type_name,
            "replay_time_s": r.replay_time_s,
            "error": r.error,
        }
        if r.comparison:
            entry["schema_valid"] = r.comparison.schema_valid
            entry["categorical_match"] = r.comparison.overall_categorical_match
            entry["avg_text_similarity"] = r.comparison.average_text_similarity
            fields = {}
            for fc in r.comparison.field_comparisons:
                if fc.field_kind == "skipped":
                    continue
                fd: dict[str, Any] = {}
                if fc.field_kind == "categorical":
                    fd = {"claude": fc.claude_value, "local": fc.local_value, "match": fc.match}
                elif fc.field_kind == "text":
                    fd = {"similarity": fc.similarity}
                elif fc.field_kind == "numeric":
                    fd = {"claude": fc.claude_value, "local": fc.local_value, "match": fc.match}
                fields[fc.field_name] = fd
            entry["fields"] = fields
        per_file.append(entry)

    return {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "local_model": model,
        "total_files": total,
        "summary": summary,
        "by_output_type": by_type,
        "results": per_file,
    }


def generate_markdown_report(report: dict) -> str:
    """Generate a human-readable Markdown report from the JSON report."""
    s = report["summary"]
    lines = [
        "# Local LLM Comparison Report",
        "",
        f"**Model:** {report['local_model']}  ",
        f"**Date:** {report['timestamp'][:19]}  ",
        f"**Files:** {report['total_files']}  ",
        f"**Total replay time:** {s['total_replay_time_s']}s  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Schema valid | {s['schema_valid_count']}/{s['schema_valid_total']} ({s['schema_valid_pct']}%) |",
        f"| Categorical match | {s['categorical_match_count']}/{s['categorical_match_total']} ({s['categorical_match_pct']}%) |",
        f"| Avg text similarity | {s['average_text_similarity']} |",
        f"| Replay errors | {s['replay_errors']} |",
        "",
    ]

    # Per-type breakdown
    for type_name, info in report["by_output_type"].items():
        lines.append(f"## {type_name}")
        lines.append("")
        lines.append(f"**Count:** {info['count']} | **Schema valid:** {info['schema_valid']}")
        lines.append("")
        if info["field_summaries"]:
            lines.append("| Field | Kind | Result |")
            lines.append("|-------|------|--------|")
            for fname, fdata in info["field_summaries"].items():
                kind = fdata["kind"]
                if kind == "categorical":
                    lines.append(
                        f"| {fname} | categorical | {fdata.get('match_count', 0)}/{info['count']}"
                        f" ({fdata.get('match_pct', 0)}%) |"
                    )
                elif kind == "text":
                    lines.append(f"| {fname} | text | avg similarity {fdata.get('avg_similarity', 0)} |")
            lines.append("")

    # Per-file details
    lines.append("## Per-file Results")
    lines.append("")
    for r in report["results"]:
        status = "ERROR" if r.get("error") else ("VALID" if r.get("schema_valid", False) else "INVALID")
        cat = r.get("categorical_match")
        cat_str = "yes" if cat is True else ("no" if cat is False else "n/a")
        sim = r.get("avg_text_similarity")
        sim_str = f"{sim}" if sim is not None else "n/a"
        lines.append(
            f"- **{r['input_file']}** [{r.get('output_type', 'text')}] "
            f"schema={status} cat_match={cat_str} text_sim={sim_str} "
            f"time={r['replay_time_s']}s"
        )
        if r.get("error"):
            lines.append(f"  - Error: {r['error']}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Replay LLM calls through local model and compare against Claude outputs"
    )
    parser.add_argument("dump_dir", help="Directory containing .in.json / .out.json pairs")
    parser.add_argument("--output-dir", help="Where to write reports (default: dump_dir)")
    parser.add_argument("--model", help=f"Override PREAUDIT_LOCAL_LLM_MODEL (default: {DEFAULT_LOCAL_LLM_MODEL})")
    parser.add_argument("--base-url", help=f"Override PREAUDIT_LOCAL_LLM_BASE_URL (default: {DEFAULT_LOCAL_LLM_BASE_URL})")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens for local LLM (default: 4096)")
    parser.add_argument("--temperature", type=float, default=0.0, help="Temperature (default: 0.0)")
    parser.add_argument("--filter", dest="filter_pattern", help="Only replay files matching glob (e.g. '*someRule*')")
    parser.add_argument("--skip-existing", action="store_true", help="Skip files that already have .local.out.json")
    parser.add_argument("--json-only", action="store_true", help="Only output JSON report, skip Markdown")
    parser.add_argument("--verbose", action="store_true", help="Detailed progress")
    args = parser.parse_args()

    dump_dir = Path(args.dump_dir)
    if not dump_dir.is_dir():
        print(f"ERROR: Not a directory: {dump_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else dump_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Force local backend
    os.environ[LLM_BACKEND_ENV] = "local"
    if args.model:
        os.environ[LOCAL_LLM_MODEL_ENV] = args.model
    if args.base_url:
        os.environ[LOCAL_LLM_BASE_URL_ENV] = args.base_url

    model = os.environ.get(LOCAL_LLM_MODEL_ENV, DEFAULT_LOCAL_LLM_MODEL)
    base_url = os.environ.get(LOCAL_LLM_BASE_URL_ENV, DEFAULT_LOCAL_LLM_BASE_URL)

    # Find .in.json files
    in_files = sorted(dump_dir.glob("*.in.json"))
    if args.filter_pattern:
        in_files = [f for f in in_files if fnmatch.fnmatch(f.name, args.filter_pattern)]
    if args.skip_existing:
        in_files = [f for f in in_files if not f.with_suffix("").with_suffix(".local.out.json").exists()]

    if not in_files:
        print("No .in.json files found to replay.", file=sys.stderr)
        sys.exit(0)

    print(f"Replaying {len(in_files)} files through {model} @ {base_url}", file=sys.stderr)

    results: list[ReplayResult] = []
    for i, in_path in enumerate(in_files):
        label = in_path.name
        print(f"  [{i + 1}/{len(in_files)}] {label} ...", end="", file=sys.stderr, flush=True)

        result = replay_single(in_path, args.max_tokens, args.temperature, args.verbose)

        # Write .local.out.json
        write_local_output(in_path, result, model)

        # Compare against Claude output if available
        claude_text = read_claude_output(in_path)
        if claude_text is not None and result.exit_code == 0:
            result.comparison = compare_outputs(claude_text, result.analysis_text, result.output_type_name)

        results.append(result)

        status = "OK" if result.exit_code == 0 else "ERR"
        print(f" {status} ({result.replay_time_s}s)", file=sys.stderr)

    # Generate reports
    report = generate_json_report(results, model)

    json_path = output_dir / "comparison_report.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON report: {json_path}", file=sys.stderr)

    if not args.json_only:
        md_path = output_dir / "comparison_report.md"
        with open(md_path, "w") as f:
            f.write(generate_markdown_report(report))
        print(f"Markdown report: {md_path}", file=sys.stderr)

    # Print summary to stdout
    s = report["summary"]
    verdict = compute_verdict_changes(results)

    print(f"\n{'=' * 60}")
    print(f"  Schema valid:      {s['schema_valid_count']}/{s['schema_valid_total']} ({s['schema_valid_pct']}%)")
    print(f"  Categorical match: {s['categorical_match_count']}/{s['categorical_match_total']} ({s['categorical_match_pct']}%)")
    print(f"  Avg text sim:      {s['average_text_similarity']}")
    print(f"  Errors:            {s['replay_errors']}")
    print(f"  Total time:        {s['total_replay_time_s']}s")
    if verdict.total > 0:
        print(f"  {'─' * 56}")
        print(f"  Verdict analysis ({verdict.total} results with is_defect + confidence):")
        print(
            f"    is_defect changed:    {verdict.is_defect_changed}/{verdict.total} "
            f"[YES->NO: {verdict.is_defect_yes_to_no}, NO->YES: {verdict.is_defect_no_to_yes}]"
        )
        print(
            f"    confidence changed:   {verdict.confidence_changed_only}/{verdict.total} (is_defect same) "
            f"[HIGH->LOW: {verdict.confidence_high_to_low}, LOW->HIGH: {verdict.confidence_low_to_high}]"
        )
        print(f"    both agree:           {verdict.both_same}/{verdict.total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
