"""Summarize a ``.asts.json`` dump through the typed models.

Usage::

    python -m certora_autosetup.solidity_ast [--json] [--solc-version V] <path/to/all_asts.json>

Per source file: parse status, node counts, unknown node types, unmodeled fields,
and round-trip fidelity (does the typed tree re-serialize to the exact source
JSON?) — a quick way to validate the models against a real project's dump. The
dump is streamed one compilation unit at a time, so multi-GB dumps stay cheap.
``--json`` emits one machine-readable summary object instead of text.
"""

import argparse
import json
import sys
from collections import Counter
from typing import Any

from .base import UnknownNode
from .declarations import ContractDefinition
from .diagnostics import roundtrip_diffs
from .loader import AstDump, FileAsts, SourceAst
from .traversal import find_all, walk


def _source_report(source_ast: SourceAst) -> dict[str, Any]:
    if source_ast.root is None:
        return {
            "status": source_ast.raw_kind,
            "error": source_ast.parse_error,
            "raw_nodes": len(source_ast.raw),
        }
    nodes = list(walk(source_ast.root))
    unknown = Counter(n.nodeType for n in nodes if isinstance(n, UnknownNode))
    extras = Counter(
        f"{type(n).__name__}.{key}" for n in nodes for key in (n.model_extra or {})
    )
    diffs = roundtrip_diffs(source_ast)
    return {
        "status": "ok",
        "nodes": len(nodes),
        "indexed": len(source_ast.nodes),
        "unknown_node_types": dict(unknown),
        "unmodeled_fields": dict(extras),
        "roundtrip_diffs": diffs,
    }


def _print_unit_text(file_asts: FileAsts, per_file: dict[str, dict[str, Any]]) -> None:
    print(file_asts.original_file)
    id_to_name = {
        c.id: c.name
        for source in file_asts.sources.values()
        if source.root is not None
        for c in find_all(source.root, ContractDefinition)
    }
    for source in file_asts.sources.values():
        r = per_file[source.source_path]
        if r["status"] != "ok":
            detail = f": {r['error']}" if r.get("error") else ""
            print(f"  {source.source_path}  [{r['status']}]{detail}")
            continue
        print(f"  {source.source_path}  [ok] {r['nodes']} nodes, {r['indexed']} with ids")
        if r["unknown_node_types"]:
            print(f"    unknown node types: {r['unknown_node_types']}")
        if r["unmodeled_fields"]:
            print(f"    unmodeled fields: {r['unmodeled_fields']}")
        if r["roundtrip_diffs"]:
            print(f"    roundtrip diffs ({len(r['roundtrip_diffs'])}):")
            for d in r["roundtrip_diffs"][:10]:
                print(f"      {d}")
        assert source.root is not None
        for contract in find_all(source.root, ContractDefinition):
            bases = [
                id_to_name.get(i, f"#{i}") for i in contract.linearizedBaseContracts[1:]
            ]
            abstract = "abstract " if contract.abstract else ""
            inherits = f" is {', '.join(bases)}" if bases else ""
            print(f"    {abstract}{contract.contractKind} {contract.name}{inherits}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("dump", help="path to a .asts.json / all_asts.json file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--solc-version",
        default=None,
        help="compiler version that produced the dump; enables the VERSION_GATES "
        "check (absent gated fields fail the source instead of reading as None)",
    )
    args = parser.parse_args(argv)

    report: dict[str, dict[str, Any]] = {}
    for file_asts in AstDump.stream_units(args.dump, solc_version=args.solc_version):
        per_file = {
            source.source_path: _source_report(source)
            for source in file_asts.sources.values()
        }
        report[file_asts.original_file] = per_file
        if not args.json:
            _print_unit_text(file_asts, per_file)

    flat = [r for per_file in report.values() for r in per_file.values()]
    ok = [r for r in flat if r["status"] == "ok"]
    clean = [
        r
        for r in ok
        if not r["unknown_node_types"] and not r["unmodeled_fields"] and not r["roundtrip_diffs"]
    ]
    summary = {
        "sources": len(flat),
        "parsed": len(ok),
        "vyper": sum(r["status"] == "vyper" for r in flat),
        "parse_failed": sum(r["status"] == "parse_failed" for r in flat),
        "fully_clean": len(clean),
    }

    if args.json:
        json.dump({"summary": summary, "files": report}, sys.stdout, indent=1)
        print()
    else:
        print(
            f"\n{summary['parsed']}/{summary['sources']} sources parsed, "
            f"{summary['fully_clean']} fully clean (no unknowns, no unmodeled fields, "
            f"round-trip exact); vyper: {summary['vyper']}, failed: {summary['parse_failed']}"
        )

    return 0 if summary["parse_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
