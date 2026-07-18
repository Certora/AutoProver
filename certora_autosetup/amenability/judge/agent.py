"""LLM judge (phase 2): one structured call per project.

Design choices, in order of importance:
- Everything the judge needs is pre-assembled into the prompt (static report +
  ±N-line excerpts around every evidence item), so a single request suffices —
  no tool loop, which keeps cost bounded and the verdict reproducible-ish.
- The verdict is CLAMPED in code to at most one level away from the static
  provisional, and only moves at all with >= 2 concrete citations. The judge
  refines, it does not override.
- The output records model / rubric version+sha / prompt version and the request
  usage+cost, so fleet runs can budget and audits can reproduce.
"""

import hashlib
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import AmenabilityReport, Level

JUDGE_MODEL = "claude-opus-4-8"
# USD per 1M tokens (input, output, cache-read) — used for fleet budget tracking.
PRICE_INPUT_PER_MTOK = 5.00
PRICE_OUTPUT_PER_MTOK = 25.00
PRICE_CACHE_READ_PER_MTOK = 0.50
PROMPT_TEMPLATE_VERSION = "1"
EXCERPT_RADIUS = 10  # lines around each evidence line
MAX_EXCERPTS = 40
MAX_OUTPUT_TOKENS = 2000

RUBRIC_DIR = Path(__file__).parent / "rubric"

LEVEL_ORDER = [Level.LOW, Level.MEDIUM, Level.HIGH]


class JudgeCitation(BaseModel):
    file: str
    line: int
    quote: str
    signal: str


class JudgeVerdict(BaseModel):
    level: Level
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    citations: list[JudgeCitation]


class JudgeError(Exception):
    pass


def load_rubric(version: Optional[str] = None) -> tuple[str, str, str]:
    """(version, sha256, text) of the requested (default: latest) rubric."""
    rubrics = sorted(RUBRIC_DIR.glob("rubric_v*.md"))
    if not rubrics:
        raise JudgeError(f"no rubric files under {RUBRIC_DIR}")
    if version:
        path = RUBRIC_DIR / f"rubric_v{version}.md"
        if not path.is_file():
            raise JudgeError(f"rubric version {version} not found")
    else:
        path = rubrics[-1]
    text = path.read_text()
    match = re.search(r"rubric_v(\w+)\.md", path.name)
    ver = match.group(1) if match else path.stem
    return ver, hashlib.sha256(text.encode()).hexdigest(), text


def build_excerpts(ctx: AnalysisContext, report: AmenabilityReport) -> str:
    """±EXCERPT_RADIUS-line excerpts around each evidence item, deduplicated."""
    seen: set[tuple[str, int]] = set()
    chunks: list[str] = []
    for e in report.evidence:
        key = (e.file, e.line // (EXCERPT_RADIUS * 2))
        if e.line == 0 or key in seen:
            continue
        seen.add(key)
        source = ctx.project_root / e.file
        if not source.is_file():
            continue
        lines = source.read_text(errors="replace").splitlines()
        lo = max(0, e.line - 1 - EXCERPT_RADIUS)
        hi = min(len(lines), e.line + EXCERPT_RADIUS)
        body = "\n".join(f"{i + 1:5d}| {lines[i]}" for i in range(lo, hi))
        chunks.append(f"--- {e.file}:{e.line} [{e.signal}] ---\n{body}")
        if len(chunks) >= MAX_EXCERPTS:
            break
    return "\n\n".join(chunks)


def _clamp(static_level: Level, judged: Level, citations: int) -> tuple[Level, bool]:
    """Enforce the guardrail: >= 2 citations to move, and at most one step."""
    if judged == static_level:
        return judged, False
    if citations < 2:
        return static_level, True
    si, ji = LEVEL_ORDER.index(static_level), LEVEL_ORDER.index(judged)
    if abs(ji - si) > 1:
        ji = si + (1 if ji > si else -1)
        return LEVEL_ORDER[ji], True
    return judged, False


def judge_report(
    report: AmenabilityReport,
    ctx: AnalysisContext,
    rubric_version: Optional[str] = None,
    client=None,
) -> dict:
    """Run the judge over a static report; returns the report's `judge` dict."""
    import anthropic  # deferred: --no-llm paths must not require the SDK

    ver, sha, rubric_text = load_rubric(rubric_version)
    if client is None:
        client = anthropic.Anthropic()

    static_json = report.model_dump_json(
        include={"level", "static", "evidence", "contracts_analyzed"}, indent=1
    )
    excerpts = build_excerpts(ctx, report)

    system = (
        f"{rubric_text}\n\n"
        "Return your verdict via the structured output schema. `level` is your "
        "judged amenability level; `citations` must reference concrete file:line "
        "locations from the provided evidence/excerpts with a short quote each. "
        "You may move at most one level away from the static provisional level, "
        "and only with at least two citations justifying the move."
    )
    user = (
        f"## Static analysis report\n\n{static_json}\n\n"
        f"## Code excerpts (around each evidence item)\n\n{excerpts or '(no excerpts available)'}"
    )

    response = client.messages.parse(
        model=JUDGE_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=JudgeVerdict,
    )
    verdict = response.parsed_output
    if not isinstance(verdict, JudgeVerdict):
        raise JudgeError(f"judge returned no parseable verdict (stop_reason={response.stop_reason})")

    final_level, clamped = _clamp(report.level, verdict.level, len(verdict.citations))

    usage = response.usage
    cost = (
        usage.input_tokens * PRICE_INPUT_PER_MTOK
        + usage.output_tokens * PRICE_OUTPUT_PER_MTOK
        + (usage.cache_read_input_tokens or 0) * PRICE_CACHE_READ_PER_MTOK
    ) / 1_000_000

    return {
        "level": final_level.value,
        "raw_level": verdict.level.value,
        "clamped": clamped,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "citations": [c.model_dump() for c in verdict.citations],
        "disagrees_with_static": final_level != report.level,
        "model": JUDGE_MODEL,
        "rubric_version": ver,
        "rubric_sha256": sha,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
            "cost_usd": round(cost, 6),
        },
    }
