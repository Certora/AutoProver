"""``certora-findings-check``: checks findings files against the ``certora-autoprover-findings/1``
format, and prints the format's JSON Schema.

A file is checked in two steps. First its shape: the fields, their types and the values they may
take. Then what the shape alone cannot express: ids are unique, every id a finding or property
names exists, a finding's evidence repeats rule results of the property it names, and every rule
is under the run's ``certora_dir``. These are errors.

The text of findings and properties is also searched for a run's internal vocabulary (judge
rounds, unit finding ids, ``CEX``, ...). The patterns can also match ordinary text, e.g. "round 2"
of a fee schedule, so a match is a warning.
"""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from composer.findings.model import FindingsFile, RuleResult

#: Internal vocabulary of a run, each with what to write instead.
VOCABULARY: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), hint) for p, hint in (
        (r"\bCEXs?\b", "write 'counterexample'"),
        (r"\bjudge\b", "say what was checked, without the step's name"),
        (r"\bround[- ]?\d", "drop the run's history and keep the outcome"),
        (r"\brun ?\d", "drop the run's history and keep the outcome"),
        (r"\bscene\b", "write 'verification setup'"),
        (r"\brank \d", "name the property or finding it points to"),
        (r"\b[A-Za-z]{2,12}-F\d+\b", "a unit finding id: record it in merged_from, or name the "
                                       "finding"),
    )
)


@dataclass(frozen=True)
class Problem:
    severity: Literal["error", "warning"]
    where: str
    message: str


def _loc(parts: Sequence[str | int]) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out


def _shape_errors(e: ValidationError) -> list[Problem]:
    return [Problem("error", _loc(err["loc"]), err["msg"]) for err in e.errors()]


def _duplicates(ids: Sequence[str]) -> list[str]:
    return sorted(i for i, n in Counter(ids).items() if n > 1)


def _rule_key(r: RuleResult) -> tuple[str, str, str]:
    return (r.rule, r.spec, r.conf)


def _texts(data: FindingsFile) -> Iterator[tuple[str, str]]:
    for i, f in enumerate(data.findings):
        at = f"findings[{i}]"
        j = f.justification
        for name, text in (
            ("title", f.title), ("description", f.description), ("impact", f.impact),
            ("trusted_precondition", f.trusted_precondition),
            ("recommendation", f.recommendation),
            ("justification.pre_state", j.pre_state),
            ("justification.reachability", j.reachability),
            ("justification.not_a_model_artefact", j.not_a_model_artefact),
            ("justification.configuration", j.configuration),
            ("severity_justification.reasoning", f.severity_justification.reasoning),
        ):
            if text:
                yield f"{at}.{name}", text
        for k, step in enumerate(j.steps):
            yield f"{at}.justification.steps[{k}]", step
        for k, ev in enumerate(f.evidence):
            yield f"{at}.evidence[{k}].how", ev.how
    for i, p in enumerate(data.properties):
        yield f"properties[{i}].description", p.description
        if p.status_note:
            yield f"properties[{i}].status_note", p.status_note


def check_consistency(data: FindingsFile) -> list[Problem]:
    """The errors in ``data`` that its shape does not rule out."""
    problems: list[Problem] = []

    def error(where: str, message: str) -> None:
        problems.append(Problem("error", where, message))

    for kind, ids in (("findings", [f.id for f in data.findings]),
                      ("properties", [p.id for p in data.properties])):
        for dup in _duplicates(ids):
            error(kind, f"id {dup!r} appears more than once")

    finding_ids = {f.id for f in data.findings}
    properties = {p.id: p for p in data.properties}

    for i, p in enumerate(data.properties):
        for k, fid in enumerate(p.findings):
            if fid not in finding_ids:
                error(f"properties[{i}].findings[{k}]", f"no finding has id {fid!r}")

    for i, f in enumerate(data.findings):
        for j, ev in enumerate(f.evidence):
            at = f"findings[{i}].evidence[{j}]"
            prop = properties.get(ev.property)
            if prop is None:
                error(f"{at}.property", f"no property has id {ev.property!r}")
                continue
            if f.id not in prop.findings:
                error(f"{at}.property", f"property {prop.id!r} does not list finding {f.id!r} "
                                        "in its findings")
            own = {_rule_key(r): r for r in prop.rules}
            for k, r in enumerate(ev.rules):
                match = own.get(_rule_key(r))
                if match is None:
                    error(f"{at}.rules[{k}]", f"rule {r.rule!r} ({r.conf}) is not among the "
                                              f"rules of property {prop.id!r}")
                elif match != r:
                    error(f"{at}.rules[{k}]", f"rule {r.rule!r} ({r.conf}) has another result "
                                              f"or job_url in property {prop.id!r}")

    root = data.run.results.certora_dir.rstrip("/") + "/"
    for i, p in enumerate(data.properties):
        for j, r in enumerate(p.rules):
            at = f"properties[{i}].rules[{j}]"
            for name, path in (("spec", r.spec), ("conf", r.conf)):
                if not path.startswith(root):
                    error(f"{at}.{name}", f"{path!r} is not under run.results.certora_dir {root!r}")

    return problems


def check_vocabulary(data: FindingsFile) -> list[Problem]:
    """A warning for each passage of ``data``'s text that uses a run's internal vocabulary."""
    problems: list[Problem] = []
    for where, text in _texts(data):
        for pattern, hint in VOCABULARY:
            for m in pattern.finditer(text):
                problems.append(Problem("warning", where, f"{m.group(0)!r}: {hint}"))
    return problems


def check(raw: object) -> list[Problem]:
    """Every problem of a findings file, given as parsed JSON."""
    try:
        data = FindingsFile.model_validate(raw)
    except ValidationError as e:
        return _shape_errors(e)
    return check_consistency(data) + check_vocabulary(data)


def schema() -> dict[str, object]:
    """The JSON Schema of the format."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **FindingsFile.model_json_schema(by_alias=True),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="certora-findings-check",
        description="Check findings files (certora-autoprover-findings/1), or print the format's "
        "JSON Schema.",
    )
    parser.add_argument("files", nargs="*", type=Path, metavar="FILE")
    parser.add_argument("--schema", action="store_true",
                        help="Print the JSON Schema, with every field's description, and exit.")
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(schema(), indent=2))
        return 0
    if not args.files:
        parser.error("give at least one FILE, or --schema")

    errors = 0
    for path in args.files:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: {path}: {e}", file=sys.stderr)
            errors += 1
            continue
        problems = check(raw)
        for p in problems:
            print(f"{p.severity}: {path}: {p.where}: {p.message}", file=sys.stderr)
        n_err = sum(p.severity == "error" for p in problems)
        errors += n_err
        print(f"{path}: {n_err} errors, {len(problems) - n_err} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
