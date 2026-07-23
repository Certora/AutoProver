"""AST-dump acquisition.

Compiling is the amenability floor: there is no degraded raw-source scoring
path. The resolution order is (1) dumps passed explicitly, (2) dumps a previous
certoraRun/autosetup run left under .certora_internal, (3) a distinct
"cannot score" error telling the caller exactly what to run.
"""

from dataclasses import dataclass
from pathlib import Path

from certora_autosetup.solidity_ast import AstDump

DUMP_BASENAMES = ("all_asts.json", ".asts.json")


class CannotScoreError(Exception):
    def __init__(self, error: str, detail: str):
        super().__init__(detail)
        self.error = error
        self.detail = detail


@dataclass
class DumpResolution:
    dumps: list[AstDump]
    dump_paths: list[Path]


def discover_dumps(project_root: Path) -> list[Path]:
    """Newest-first AST dumps a previous certoraRun left in the project."""
    internal = project_root / ".certora_internal"
    if not internal.is_dir():
        return []
    candidates = [
        p for name in DUMP_BASENAMES for p in internal.rglob(name) if p.is_file()
    ]
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_dumps(project_root: Path, ast_dump_args: list[Path]) -> DumpResolution:
    if ast_dump_args:
        missing = [p for p in ast_dump_args if not p.is_file()]
        if missing:
            raise CannotScoreError(
                "no-ast-dump", f"--ast-dump path(s) not found: {', '.join(map(str, missing))}"
            )
        return DumpResolution(
            dumps=[AstDump.load(p) for p in ast_dump_args], dump_paths=list(ast_dump_args)
        )

    discovered = discover_dumps(project_root)
    if discovered:
        newest = discovered[0]
        return DumpResolution(dumps=[AstDump.load(newest)], dump_paths=[newest])

    raise CannotScoreError(
        "does-not-compile",
        "No AST dump found. The project must compile before it can be scored — run "
        "`certoraRun <files...> --compilation_steps_only --dump_asts` (or a "
        "certora-autosetup compilation analysis) in the project first, or pass an "
        "existing dump via --ast-dump.",
    )
