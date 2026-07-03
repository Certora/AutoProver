#!/usr/bin/env python3
"""Static prune-pass for unresolvable CVL summary entries.

Curated summary specs (e.g. ``FixedPointMathLib.spec``) are bundled as *union*
templates that declare more method entries than any single project actually has —
for instance both the solmate naming (``divWadDown``/``mulWadDown``/...) and the
solady naming (``mulDiv``/``fullMulDiv``/``mulWad``/``divWad``) of the same library.
When such a spec is emitted verbatim, CVL hard-errors on every ``methods{}`` entry
whose method does not exist at the declared receiver, and AutoSetup's reactive
``TypecheckerLoop`` can only repair some of those (it has no handler for the
"defined in another contract, use that receiver" case), so the whole run aborts
before any checker is submitted.

This module resolves the problem *proactively*: after the summary specs are written
but before the typechecker runs, every internal-method summary entry is validated
against the compiled scene (``all_methods.json``) and any entry that does not resolve
is commented out. The policy is **drop-only** — we never rewrite a receiver; if a
method genuinely lives under a different contract, the dedicated per-library summary
for that contract (e.g. ``OZ_Math`` for ``Math.mulDiv``) is the one that should cover
it. The ``TypecheckerLoop`` remains in place as a backstop.

Parsing of the ``methods{}`` block is done with the official CVL parser
(``ASTExtraction.jar syntax-check``), not regex: it runs standalone (no compiled
scene), emits a JSON AST, and gives each entry's receiver, method id, parameter
types, visibility, target kind (specific vs wildcard ``_``), and source line range.

The signature database (``signature_state/signature_database.json``) is *not* usable
here: it is keyed on 4-byte selectors and skips every method with ``sighash == 0``,
i.e. exactly the internal library methods these summaries target. ``all_methods.json``
is the only source that carries internal methods with their defining ``contractName``
and ``fullSignature`` parameter types.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from certora_cli.Shared.certoraUtils import find_jar

from certora_autosetup.parsers.method_parser import MethodParser

# A logger callable matching SummarySetup.log(message, level).
LogFn = Callable[[str, str], None]

_AST_EXTRACTION_JAR = "ASTExtraction.jar"

# CVL `import "...";` lines are rejected by ASTExtraction's standalone syntax-check
# ("'import' declarations of .spec files are unsupported"). We blank them out before
# parsing — replacing each with an empty line preserves the line numbering so the AST
# `range` values still index the original file.
_IMPORT_LINE_RE = re.compile(r'^\s*import\s+"')

_VISIBILITY_INTERNAL = "INTERNAL"
_WILDCARD_TARGET = "WildcardTarget"


@dataclass
class MethodEntry:
    """One internal-method summary entry parsed out of a ``methods{}`` block.

    The position fields come straight from the AST ``range``: ``start`` is inclusive,
    ``end`` is exclusive (just past the trailing ``;``). Lines are 0-based; columns are
    byte offsets from the start of their line. They delimit the entry's exact span — so
    it can be disabled precisely without any "one entry per line" assumption.
    """

    receiver: str
    name: str
    param_types: List[str]
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    @property
    def arity(self) -> int:
        return len(self.param_types)

    @property
    def key(self) -> Tuple[str, str, Tuple[str, ...]]:
        # Dedup key. Both sides come from the same parser, so the type spellings are
        # self-consistent across specs (unlike a spec-vs-all_methods comparison).
        return (self.receiver, self.name, tuple(self.param_types))


def _vmtype_to_str(vmtype: dict) -> str:
    """Canonicalize an AST ``vmType`` node into a Solidity-ish type string.

    Handles array types (``ArrayType`` carries a nested ``base`` and *no* top-level
    ``id``) and contract-qualified struct/enum types. Used for the dedup key and for
    human-readable logging — the resolve decision itself only relies on arity (see
    ``entry_resolves``), so an imperfect rendering here cannot cause a false drop.
    """
    if "ArrayType" in vmtype.get("type", ""):
        return _vmtype_to_str(vmtype["base"]) + "[]"
    base = vmtype.get("id", "")
    contract = vmtype.get("contract")
    if contract:
        cname = contract.get("name") if isinstance(contract, dict) else contract
        if cname:
            return f"{cname}.{base}"
    return base


def _parse_ast_payload(stdout: str, returncode: int, stderr: str) -> Optional[dict]:
    """Parse the JSON AST payload from ``ASTExtraction.jar`` stdout.

    The jar prints ``Warning: …`` / ``Error: …``
    diagnostic lines to stdout *before* the JSON payload, which it then prints
    exactly once as the final output (on both the success and syntax-error paths).
    So we drop the leading diagnostic lines and parse the remainder — rather than
    ``json.loads`` the whole stream, which fails at char 0 on the first warning.
    Returns the decoded payload, or ``None`` when the jar produced no AST
    (``ast`` is null — a hard syntax error left for the TypecheckerLoop backstop).
    """
    lines = stdout.splitlines()
    # Only LEADING diagnostic lines precede the JSON; never strip inside the payload.
    while lines and lines[0].startswith(("Warning: ", "Error: ")):
        lines.pop(0)
    body = "\n".join(lines).strip()
    # Empty stdout (or stdout that was nothing but diagnostics) means the jar gave us
    # no payload — surface that with its stderr rather than an opaque JSONDecodeError.
    if not body:
        raise RuntimeError(f"{_AST_EXTRACTION_JAR} produced no JSON (exit {returncode}): {stderr.strip()}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{_AST_EXTRACTION_JAR} output was not JSON after stripping diagnostics "
            f"(exit {returncode}): {exc}; stdout_head={stdout!r} stderr={stderr.strip()!r}"
        )
    return payload if payload.get("ast") else None


def _ast_extraction(blanked_source: str) -> Optional[dict]:
    """Run ``ASTExtraction.jar syntax-check`` on CVL source, return the parsed JSON.

    Feeds the (import-blanked) source on stdin via ``--raw``. Returns the decoded
    payload, or ``None`` if the jar could not produce an AST (``ast`` is null) — which
    means a hard syntax error the caller should leave for the TypecheckerLoop backstop.
    Non-fatal diagnostics (e.g. "no reason provided for assumption") still yield an AST
    and are ignored here.
    """
    # find_jar only computes a path; it does not verify the jar is present. Fail loud
    # here rather than letting `java -jar <missing>` die with an opaque JSONDecodeError.
    jar = find_jar(_AST_EXTRACTION_JAR)
    if not Path(jar).is_file():
        raise FileNotFoundError(f"{_AST_EXTRACTION_JAR} not found at {jar} — cannot parse CVL summaries")
    result = subprocess.run(
        ["java", "-jar", str(jar), "syntax-check", "--raw"],
        input=blanked_source,
        capture_output=True,
        text=True,
    )
    return _parse_ast_payload(result.stdout, result.returncode, result.stderr)


def parse_methods_entries(spec_path: Path, log: Optional[LogFn] = None) -> List[MethodEntry]:
    """Parse a spec's ``methods{}`` block into internal-method entries via the CVL parser.

    Only entries that are ``INTERNAL`` *and* target a specific contract receiver are
    returned — wildcard (``_``) and external entries are out of scope and left untouched.
    Returns ``[]`` if the spec has no parseable AST (logged as a warning).
    """
    lines = spec_path.read_text().splitlines(keepends=True)
    blanked = "".join("\n" if _IMPORT_LINE_RE.match(line) else line for line in lines)
    payload = _ast_extraction(blanked)
    if payload is None:
        if log:
            log(f"summary_resolver: could not parse {spec_path.name}; leaving it to the typechecker", "WARNING")
        return []

    entries: List[MethodEntry] = []
    for method in payload["ast"].get("importedMethods", []):
        if method.get("qualifiers", {}).get("visibility") != _VISIBILITY_INTERNAL:
            continue
        if _WILDCARD_TARGET in method.get("target", {}).get("type", ""):
            continue
        sig = method["methodParameterSignature"]
        qualified = sig.get("qualifiedMethodName")
        if not qualified or not qualified.get("host"):
            continue
        receiver = qualified["host"]["name"]
        name = qualified["methodId"]
        param_types = [_vmtype_to_str(p.get("vmType", {})) for p in sig.get("params", [])]
        rng = method["range"]
        entries.append(
            MethodEntry(
                receiver,
                name,
                param_types,
                rng["start"]["line"],
                rng["start"]["charByteOffset"],
                rng["end"]["line"],
                rng["end"]["charByteOffset"],
            )
        )
    return entries


class MethodIndex:
    """Membership index of methods in the compiled scene, built from all_methods.json.

    Keyed on ``(contractName, name)`` → list of parameter-type tuples (one per
    overload). ``contractName`` is the *defining* contract/library, which is the
    receiver CVL expects for an internal-method summary entry.
    """

    def __init__(self, methods: Sequence[Dict]):
        self._overloads: Dict[Tuple[str, str], List[Tuple[str, ...]]] = {}
        for m in methods:
            key = (m["contractName"], m["name"])
            self._overloads.setdefault(key, []).append(tuple(m.get("fullSignature", [])))

    @classmethod
    def from_file(cls, all_methods_path: Path) -> "MethodIndex":
        return cls(MethodParser(str(all_methods_path)).get_all_methods())

    def overloads(self, receiver: str, name: str) -> List[Tuple[str, ...]]:
        """Parameter-type tuples of every overload of ``name`` defined in ``receiver``
        (empty if the name isn't defined there)."""
        return self._overloads.get((receiver, name), [])


def entry_resolves(index: MethodIndex, entry: MethodEntry) -> bool:
    """Decide whether a summary entry resolves in the scene (drop-only policy).

    The decision is based on ``(receiver, name, arity)`` only — DROP iff the method
    name is absent at the declared receiver, or no overload there has the entry's
    arity; otherwise KEEP. This is deliberately *not* an exact parameter-type match:
    type spellings differ between the AST (``uint256[]``, unqualified ``Rounding``,
    ...) and ``all_methods.json`` (``uint256[]`` vs static arrays, qualified
    ``Math.Rounding``), and a spelling mismatch must never cause a valid summary to be
    dropped. Arity is enough to catch the real cases (wrong receiver / nonexistent
    name / wrong arity) and is robust across both sources. A same-arity-but-different-
    type phantom overload, if it ever occurs, is left to the TypecheckerLoop backstop.
    """
    # KEEP iff some overload of this name at this receiver has the entry's arity.
    # An empty overload list means the name is absent at the receiver (e.g. solady
    # ``mulDiv`` on solmate ``FixedPointMathLib``) → DROP.
    return any(len(o) == entry.arity for o in index.overloads(entry.receiver, entry.name))


def _insert_at(line: str, byte_offset: int, text: str) -> str:
    """Insert ``text`` into ``line`` at a UTF-8 ``byte_offset`` (AST columns are byte
    offsets). Offsets land on token boundaries, so we never split a multibyte char."""
    raw = line.encode("utf-8")
    return (raw[:byte_offset] + text.encode("utf-8") + raw[byte_offset:]).decode("utf-8")


def _disable_entry(lines: List[str], entry: MethodEntry, reason: str) -> None:
    """Disable a summary entry in place by wrapping its exact parsed span in a CVL block
    comment ``/* AUTO-RESOLVED (reason): … */``.

    Uses only the AST positions — inclusive start, exclusive end — so multi-line entries
    (and, in principle, multiple entries sharing a line) are handled with no structural
    assumption. When the span is on a single line, the closing marker is inserted first
    so it doesn't shift the start byte offset.
    """
    close = " */"
    open_ = f"/* AUTO-RESOLVED ({reason}): "
    if entry.start_line == entry.end_line:
        line = lines[entry.start_line]
        line = _insert_at(line, entry.end_col, close)
        lines[entry.start_line] = _insert_at(line, entry.start_col, open_)
    else:
        lines[entry.end_line] = _insert_at(lines[entry.end_line], entry.end_col, close)
        lines[entry.start_line] = _insert_at(lines[entry.start_line], entry.start_col, open_)


def resolve_spec_file(
    spec_path: Path,
    index: MethodIndex,
    owned_keys: Optional[Set[Tuple[str, str, Tuple[str, ...]]]] = None,
    log: Optional[LogFn] = None,
) -> List[Tuple[str, str, Tuple[str, ...]]]:
    """Prune unresolvable internal-method entries in a single summary spec, in place.

    Args:
        spec_path: The emitted ``.spec`` file to rewrite.
        index: Scene method index.
        owned_keys: ``(receiver, name, param_types)`` keys already claimed by a
            higher-precedence spec. Entries matching one of these are dropped as
            duplicates (the dedup safeguard); ``None`` disables dedup.
        log: Optional ``(message, level)`` logger.

    Returns:
        The list of ``(receiver, name, param_types)`` keys this file *keeps* — used by
        the caller to seed ``owned_keys`` for lower-precedence specs.
    """
    entries = parse_methods_entries(spec_path, log=log)
    if not entries:
        return []

    lines = spec_path.read_text().splitlines(keepends=True)
    kept: List[Tuple[str, str, Tuple[str, ...]]] = []
    dropped_missing = 0
    dropped_dup = 0
    # Disable entries from the bottom up so earlier entries' positions stay valid after
    # later ones are edited.
    for entry in sorted(entries, key=lambda e: (e.start_line, e.start_col), reverse=True):
        if not entry_resolves(index, entry):
            _disable_entry(lines, entry, f"not in scene at {entry.receiver}")
            dropped_missing += 1
        elif owned_keys is not None and entry.key in owned_keys:
            _disable_entry(lines, entry, "duplicate, owned by higher-precedence spec")
            dropped_dup += 1
        else:
            kept.append(entry.key)

    if dropped_missing or dropped_dup:
        spec_path.write_text("".join(lines))
        if log:
            log(
                f"summary_resolver: {spec_path.name}: dropped {dropped_missing} "
                f"unresolvable + {dropped_dup} duplicate entr(ies)",
                "INFO",
            )
    return kept


def resolve_summary_specs(
    ordered_spec_files: Sequence[Path],
    all_methods_path: Path,
    log: Optional[LogFn] = None,
) -> None:
    """Run the prune-pass over a precedence-ordered list of emitted summary specs.

    Files earlier in ``ordered_spec_files`` win duplicate ownership: a
    ``(receiver, name, param_types)`` kept by an earlier file is dropped from later files.
    Non-existent / wrong-receiver entries are dropped from every file regardless of
    order.
    """
    index = MethodIndex.from_file(all_methods_path)
    owned: Set[Tuple[str, str, Tuple[str, ...]]] = set()
    for spec_path in ordered_spec_files:
        if not spec_path.exists():
            continue
        kept = resolve_spec_file(spec_path, index, owned_keys=owned, log=log)
        owned.update(kept)
