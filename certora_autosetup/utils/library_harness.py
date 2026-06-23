"""Solidity source parsing + harness assembly for the missing-library workaround.

This module produces the ``<Consumer>Harness.sol`` source that
``CompilationWorkaroundManager._apply_missing_library_harness_to_config`` writes
to disk when the Certora wrapper reports "Failed to find a dependency library
while building the constructor bytecode of <Consumer>". The harness:

- ``is <Consumer>`` so the verifier sees the same interface as the wrapped
  contract.
- Imports each named library and emits one ``__certoraDummyUse_<Lib>_<Fn>``
  forwarder per library (calling one representative external/public function),
  so solc emits the library's deployable bytecode in this compilation unit and
  the linker can resolve it.
- Forwards the consumer's constructor parameters to the wrapped ``super(...)``
  call so solc doesn't error on "no matching constructor for parent contract".
- Copies each library's own ``import`` statements (rewriting relative paths
  for the harness's new location) so custom parameter types like interfaces
  and structs resolve in the dummy-use signatures.

Parsing is regex-based rather than AST-based because this code runs *inside*
the compilation-analysis retry loop — solc has failed to link, so the
prover's ``.asts.json`` / ``all_methods.json`` artifacts don't exist yet.
The Solidity surface parsed here (function declarations, constructor, imports)
is small and syntactically stable; failure modes are local (harness doesn't
compile → next retry surfaces the error).
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from certora_autosetup.utils.contract_linker import render_wrapper_contract
from certora_autosetup.utils.logger import logger


@dataclass
class LibraryFunction:
    """One external/public function declared in a library source file.

    ``params_source`` and ``returns_source`` hold the raw Solidity text from the
    library file (e.g. ``"uint256 _assets, IShareToken _tok"``) so the harness
    can re-emit the signature verbatim — preserves custom types without our
    needing to resolve them.
    """

    name: str
    params_source: str
    returns_source: str  # Empty string if the function has no `returns` clause.


@dataclass
class LibrarySpec:
    """A library the harness must use, supplied by the caller of
    ``build_consumer_harness_source``.

    The caller has already read the library source from disk; we keep
    ``file_path`` around so relative ``import`` statements in the source can be
    rewritten for the harness's new location.
    """

    name: str
    source_text: str
    file_path: Path


# =========================================================================
# Internal parsing primitives
# =========================================================================

_IMPORT_RE = re.compile(r"^\s*import\s[^;]*;", re.MULTILINE)
_FUNCTION_HEAD_RE = re.compile(r"\bfunction\s+(\w+)\s*\(")
_VISIBILITY_RE = re.compile(r"\b(external|public)\b")
_RETURNS_RE = re.compile(r"\breturns\s*\(")
_CONSTRUCTOR_HEAD_RE = re.compile(r"\bconstructor\s*\(")


def _strip_solidity_comments(src: str) -> str:
    """Replace ``//`` and ``/* */`` comments with whitespace of equal length.

    Byte offsets are preserved so the function-signature scanner can index into
    the stripped source and still hand back substrings that match the original.
    String literals are deliberately *not* stripped — import statements need
    their path strings intact, and ``function`` keywords inside string literals
    are vanishingly rare in real Solidity files.
    """
    out = list(src)
    i = 0
    n = len(src)
    while i < n:
        if i + 1 < n and src[i] == "/" and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if i + 1 < n and src[i] == "/" and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _find_balanced(src: str, open_pos: int) -> int:
    """Given index of an opening ``(`` in ``src``, return the index just past
    the matching ``)``. Returns -1 if unbalanced."""
    depth = 1
    i = open_pos + 1
    while i < len(src):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _extract_imports(src: str) -> List[str]:
    """Return every ``import …;`` statement from the (comment-stripped) source.

    Order is preserved. Each entry is the full statement text including the
    trailing semicolon, ready to splice into the harness verbatim.
    """
    return [m.group(0).strip() for m in _IMPORT_RE.finditer(src)]


def _find_external_function(src: str) -> Optional[LibraryFunction]:
    """Return the first body-bearing ``external``/``public`` function declared
    in ``src``, or None if there is none.

    Skips internal/private functions (they're inlined by solc and don't need linking)
    and abstract declarations (terminated by ``;`` not ``{``).
    """
    pos = 0
    while True:
        m = _FUNCTION_HEAD_RE.search(src, pos)
        if not m:
            return None
        name = m.group(1)
        open_paren = m.end() - 1
        close_paren = _find_balanced(src, open_paren)
        if close_paren == -1:
            return None  # Malformed; bail rather than infinite-loop.
        params_source = src[open_paren + 1: close_paren - 1].strip()

        # The modifier block runs from right after the params to either '{' (body)
        # or ';' (abstract). Whichever comes first is the boundary.
        brace = src.find("{", close_paren)
        semi = src.find(";", close_paren)
        if brace == -1 and semi == -1:
            return None
        end = brace if (brace != -1 and (semi == -1 or brace < semi)) else semi
        modifiers = src[close_paren:end]
        pos = end + 1

        if not _VISIBILITY_RE.search(modifiers):
            continue
        if end == semi:
            continue  # Abstract — no body, no bytecode to force.

        rm = _RETURNS_RE.search(modifiers)
        returns_source = ""
        if rm:
            # Re-anchor onto src so we can balanced-paren-match the returns list.
            ret_open = close_paren + rm.end() - 1
            ret_close = _find_balanced(src, ret_open)
            if ret_close != -1:
                returns_source = src[ret_open + 1: ret_close - 1].strip()

        return LibraryFunction(
            name=name, params_source=params_source, returns_source=returns_source
        )


def _split_top_level(s: str) -> List[str]:
    """Split a comma-separated list at depth-0 commas (so tuple types like
    ``(uint256, address) memory _t`` stay intact)."""
    if not s.strip():
        return []
    parts: List[str] = []
    depth = 0
    last = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(s[last:i])
            last = i + 1
    parts.append(s[last:])
    return [p.strip() for p in parts if p.strip()]


def _param_name(param: str, idx: int) -> str:
    """Pull the parameter name out of a declaration like ``uint256 calldata _x``.

    Returns the trailing identifier; if there's only a type, synthesizes
    ``_a<idx>`` so the harness still has something to pass through to the
    library call.
    """
    tokens = param.split()
    if tokens and re.match(r"^\w+$", tokens[-1]) and tokens[-1] not in {
        "memory", "calldata", "storage", "indexed", "payable",
    }:
        # Heuristic: a bare type like ``uint256`` is also a single identifier,
        # so disambiguate by checking that the token before it (if any) looks
        # like a real type keyword. Cheap rule: a name follows at least one
        # other token.
        if len(tokens) >= 2:
            return tokens[-1]
    return f"_a{idx}"


def _strip_return_names(returns_source: str) -> str:
    """Drop parameter names from a ``returns`` list so the harness's outer
    signature stays minimal — e.g. ``uint256 assets, uint256 shares`` becomes
    ``uint256, uint256``."""
    parts = _split_top_level(returns_source)
    cleaned: List[str] = []
    for p in parts:
        tokens = p.split()
        if len(tokens) >= 2 and re.match(r"^\w+$", tokens[-1]) and tokens[-1] not in {
            "memory", "calldata", "storage", "payable",
        }:
            cleaned.append(" ".join(tokens[:-1]))
        else:
            cleaned.append(p)
    return ", ".join(cleaned)


def _extract_constructor_params(src: str) -> Optional[str]:
    """Return the raw parameter list of the contract's constructor, or ``None``
    if no constructor is declared.

    Returns the substring between the parentheses (e.g.
    ``"address _owner, uint256 _cap"``). Empty string is a valid return — it
    signals a present-but-no-args constructor; callers can compare against
    ``None`` to detect absence.
    """
    m = _CONSTRUCTOR_HEAD_RE.search(src)
    if not m:
        return None
    open_paren = m.end() - 1
    close_paren = _find_balanced(src, open_paren)
    if close_paren == -1:
        return None
    return src[open_paren + 1: close_paren - 1].strip()


def _ensure_named_params(params_source: str) -> Tuple[str, List[str]]:
    """Take a parameter list source and return a (named_params, arg_names) pair.

    ``named_params`` is the parameter list with synthetic names added for any
    bare-type entries (e.g. ``"uint256"`` becomes ``"uint256 _a0"``).
    ``arg_names`` is the ordered list of names callers should use when
    forwarding to the wrapped function. Together they let an emitter produce
    both the outer signature and the inner call site in one pass.
    """
    parts = _split_top_level(params_source)
    named_parts: List[str] = []
    arg_names: List[str] = []
    for i, p in enumerate(parts):
        name = _param_name(p, i)
        tokens = p.split()
        has_name = (
            len(tokens) >= 2
            and re.match(r"^\w+$", tokens[-1])
            and tokens[-1] not in {"memory", "calldata", "storage", "payable"}
        )
        named_parts.append(p if has_name else f"{p} {name}")
        arg_names.append(name)
    return ", ".join(named_parts), arg_names


def _rewrite_import(import_stmt: str, lib_dir: Path, harness_dir: Path) -> str:
    """Rewrite relative-path imports so they resolve from the harness's location.

    Solidity imports of the forms ``"./foo.sol"`` / ``"../foo.sol"`` are
    anchored to the importing file's directory. When we copy them into the
    harness — which lives elsewhere — we have to recompute the relative path.
    Remapping-style imports (``"silo-core/..."``) and absolute paths are left
    alone.
    """
    m = re.search(r'(["\'])([^"\']+)\1', import_stmt)
    if not m:
        return import_stmt
    quote, path = m.group(1), m.group(2)
    if not (path.startswith("./") or path.startswith("../")):
        return import_stmt
    resolved = (lib_dir / path).resolve()
    new_path = os.path.relpath(resolved, harness_dir.resolve())
    return import_stmt[: m.start()] + f"{quote}{new_path}{quote}" + import_stmt[m.end():]


def _build_dummy_use_forwarder(lib_name: str, fn: LibraryFunction) -> str:
    """Emit one dummy-use method that calls ``<lib_name>.<fn.name>(args...)``.

    The method is named ``__certoraDummyUse_<lib_name>_<fn.name>`` so it
    cannot collide with anything the parent contract already exposes — the
    harness inherits the consumer's entire surface, so a forwarder called
    ``transfer`` would accidentally override the parent's ``transfer``.

    The outer signature preserves the library's parameter types verbatim;
    parameter names are stripped from the outer ``returns`` clause to keep the
    wrapper minimal — solc still has to emit the library because the body
    calls ``lib_name.fn.name``.
    """
    outer_params_src, arg_names = _ensure_named_params(fn.params_source)

    returns_clause = ""
    return_keyword = ""
    if fn.returns_source.strip():
        returns_clause = f" returns ({_strip_return_names(fn.returns_source)})"
        return_keyword = "return "

    method_name = f"__certoraDummyUse_{lib_name}_{fn.name}"
    return (
        f"    function {method_name}({outer_params_src}) external{returns_clause} {{\n"
        f"        {return_keyword}{lib_name}.{fn.name}({', '.join(arg_names)});\n"
        f"    }}"
    )


# =========================================================================
# Public API
# =========================================================================


def pragma_for_solc(target_solc: str) -> str:
    """Best-effort ``pragma solidity ^X.Y.Z;`` line for a harness compiled by
    ``target_solc``.

    Accepts either Certora (``"solc8.28"``) or solc-select (``"solc-0.8.28"``)
    naming. Returns an empty string if the version can't be parsed — the
    harness then inherits the pragma rules from the imported library file,
    which is also fine since the library already constrains the compatible
    compiler set.
    """
    m = re.match(r"^solc(\d+)\.(\d+)$", target_solc)
    if m:
        return f"pragma solidity ^0.{m.group(1)}.{m.group(2)};"
    m = re.match(r"^solc-(\d+)\.(\d+)\.(\d+)$", target_solc)
    if m:
        return f"pragma solidity ^{m.group(1)}.{m.group(2)}.{m.group(3)};"
    return ""


def build_consumer_harness_source(
    consumer_name: str,
    consumer_source_text: str,
    consumer_file_abs: Path,
    libraries: List[LibrarySpec],
    harness_dir: Path,
    harness_name: str,
    pragma_line: str,
) -> str:
    """Assemble the full ``<Consumer>Harness.sol`` source as a string.

    Pure function — the caller is responsible for writing the returned string
    to disk under ``harness_dir`` and for any conf-side updates (registering
    the harness in ``files``, renaming ``compiler_map`` keys, etc.).

    Behavior:

    - Inherits from ``consumer_name`` via a named ``import {Consumer} from "..."``
      relative to ``harness_dir`` so the harness sees the consumer's interface.
    - For each library in ``libraries``, parses its source for external/public
      function declarations and copies its own ``import`` statements (rewriting
      relative paths for the harness's location; we need it to have
      types used in the library function signature pass the type checking)
      A single dummy-use forwarder per library is emitted — calling one
      representative external function is enough to force solc to emit the
      whole library's bytecode. Deduplication of copied imports is by
      verbatim text.
    - If the consumer declares a constructor with parameters, the harness
      forwards them to the wrapped ``super(...)`` call.
    - A library named in a missing-dependency error always has at least one
      external/public function (internal-only libraries are inlined, never
      linked), so finding none is a parse miss: warn and emit the harness
      without a forwarder for that library — the recurring error then hits the
      caller's retry guard, which gives up cleanly.
    """
    consumer_src_stripped = _strip_solidity_comments(consumer_source_text)
    ctor_params_src = _extract_constructor_params(consumer_src_stripped)
    ctor_forward = _ensure_named_params(ctor_params_src) if ctor_params_src else None

    # Gather library imports + external functions, deduplicating imports by
    # their full text so the same `import {X} from "..."` doesn't appear twice.
    all_library_imports: List[str] = []
    seen_imports: Set[str] = set()
    library_blocks: List[str] = []
    library_named_import_lines: List[str] = []
    for lib in libraries:
        lib_src_stripped = _strip_solidity_comments(lib.source_text)
        for stmt in _extract_imports(lib_src_stripped):
            rewritten = _rewrite_import(stmt, lib.file_path.parent, harness_dir)
            if rewritten not in seen_imports:
                seen_imports.add(rewritten)
                all_library_imports.append(rewritten)

        external_fn = _find_external_function(lib_src_stripped)
        if external_fn is None:
            logger.warning(
                f"Parser found no external/public functions in {lib.file_path}, but the "
                f"linker named '{lib.name}' as a missing dependency, which implies it has "
                f"at least one — likely a parse miss. Emitting the harness without a "
                f"forwarder for it; the retry loop gives up if the error recurs."
            )
            continue

        rel_lib_import = os.path.relpath(lib.file_path, harness_dir)
        library_named_import_lines.append(
            f'import {{{lib.name}}} from "{rel_lib_import}";'
        )
        # One forwarder per library, not per function. A library deploys as a
        # single contract at one address; solc emits its deployable bytecode if
        # the compilation unit contains an external/public call to *any one* of
        # its functions. So a single representative call forces emission and lets
        # the linker resolve the constructor — emitting a forwarder for every
        # external function would only add decompilation work and pollute the
        # harness's external surface (each becomes a parametric-rule target).
        library_blocks.append(_build_dummy_use_forwarder(lib.name, external_fn))

    rel_consumer_import = os.path.relpath(consumer_file_abs, harness_dir)
    consumer_import_line = f'import {{{consumer_name}}} from "{rel_consumer_import}";'

    return render_wrapper_contract(
        harness_name=harness_name,
        parent_name=consumer_name,
        pragma_line=pragma_line,
        import_lines=[*all_library_imports, consumer_import_line, *library_named_import_lines],
        ctor_forward=ctor_forward,
        body_blocks=library_blocks,
        header_comment_lines=[
            f"// Consumer-wrapping harness: extends {consumer_name} and adds dummy uses of",
            "// each library it needs, so solc emits those libraries' deployable",
            "// bytecode in the same compilation unit and the Certora linker can",
            f"// resolve them when building {consumer_name}'s constructor.",
        ],
    )
