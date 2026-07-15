"""Summarize a ``.asts.json`` dump through the typed models.

Usage::

    python -m certora_autosetup.solidity_ast <path/to/all_asts.json>

Prints, per source file: parse status, node counts, any unknown node types or
unmodeled fields, and the contracts with their resolved inheritance — a quick way
to see the typed models working against a real project's existing dump.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from .base import UnknownNode
from .declarations import ContractDefinition
from .loader import AstDump
from .traversal import find_all, walk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("dump", help="path to a .asts.json / all_asts.json file")
    args = parser.parse_args(argv)

    dump = AstDump.load(args.dump)
    n_sources = n_parsed = n_failed = 0
    for file_asts in dump.files.values():
        print(f"{file_asts.original_file}")
        id_to_name = {
            c.id: c.name
            for source in file_asts.sources.values()
            if source.root is not None
            for c in find_all(source.root, ContractDefinition)
        }
        for source in file_asts.sources.values():
            n_sources += 1
            if source.root is None:
                n_failed += source.raw_kind == "parse_failed"
                detail = f": {source.parse_error}" if source.parse_error else ""
                print(f"  {source.source_path}  [{source.raw_kind}]{detail}")
                continue
            n_parsed += 1
            nodes = list(walk(source.root))
            unknown = Counter(n.nodeType for n in nodes if isinstance(n, UnknownNode))
            extras = Counter(
                f"{type(n).__name__}.{key}" for n in nodes for key in (n.model_extra or {})
            )
            print(f"  {source.source_path}  [ok] {len(nodes)} nodes, {len(source.nodes)} with ids")
            if unknown:
                print(f"    unknown node types: {dict(unknown)}")
            if extras:
                print(f"    unmodeled fields: {dict(extras)}")
            for contract in find_all(source.root, ContractDefinition):
                bases = [
                    id_to_name.get(i, f"#{i}")
                    for i in contract.linearizedBaseContracts[1:]
                ]
                abstract = "abstract " if contract.abstract else ""
                inherits = f" is {', '.join(bases)}" if bases else ""
                print(f"    {abstract}{contract.contractKind} {contract.name}{inherits}")

    print(f"\n{n_parsed}/{n_sources} sources parsed with the typed models")
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
