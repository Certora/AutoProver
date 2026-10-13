"""Rust examples in Markdown, type-checked the way rustdoc's doctest runner would build them.

Two corpora hand an agent Rust it is likely to copy: the CVLR API corpus, whose examples are the
crates' own doc comments, and the hand-written CVLR recipes. Neither is produced by a compiler, so
each needs a check that can fail for a reason nobody had to notice. This is that check, and it is
deliberately rustdoc's contract rather than one of our own — the doc-comment examples were written
against it, and a recipe author can read it off any crate's documentation:

* **Which blocks are code** is the fence's info string, parsed as rustdoc parses it
  (:func:`classify`). A bare fence is Rust; ``ignore``, ``text`` or any other language is not
  compiled; ``compile_fail`` claims the opposite of compiling and is not checked here.
* **Hidden lines** (``# use foo;``) are compiled, ``##`` escapes a literal ``#``
  (:func:`reveal_hidden_lines`).
* **A block without ``fn main``** is the body of one (:func:`wrap`).

Checked, not run: ``cargo check`` on the host, ``--offline``, every block in one invocation. Each
block is its own module in its own file, so an error is attributed to a block by the file its span
— or the span of the macro call it expanded from — points into (:func:`attribute_errors`).
"""

import json
import re
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: rustdoc's own attribute words. Any other word makes a block not Rust unless ``rust`` is also
#: written, which is how ``text`` and ``toml`` are excluded and ``rust,ignore`` is not.
_RUST_WORDS = frozenset(
    {"rust", "should_panic", "no_run", "ignore", "compile_fail", "test_harness", "standalone_crate"}
)

#: The directory, under ``src/``, that holds one generated file per block. Errors are attributed by
#: the parent directory as well as the file name, so a span into anything else is never a block's.
_EXAMPLES_DIR = "doc_examples"

#: rustc's closing summary. It carries no span and says nothing about any one block.
_SUMMARY = re.compile(r"^aborting due to|^could not compile")

_MAIN = re.compile(r"^\s*(pub\s+)?(async\s+)?fn\s+main\s*\(", re.MULTILINE)

#: A cold check of the CVLR family plus ``solana-program`` is about a minute; a warm one, seconds.
CHECK_TIMEOUT_S = 900


class FenceClass(Enum):
    #: rustdoc would build it: bare, ``rust``, ``no_run``, ``should_panic``, ``test_harness``.
    COMPILED = "compiled"
    #: Rust that claims *not* to compile. Kept as a claim; this module does not check it.
    COMPILE_FAIL = "compile_fail"
    #: Rust that rustdoc skips: ``ignore``, ``ignore-<target>``.
    IGNORED = "ignored"
    #: Another language, or prose in a fence: ``text``, ``toml``, ``sh``.
    NOT_RUST = "not_rust"


def classify(info: str) -> FenceClass:
    """What rustdoc does with a fence whose info string is ``info``.

    Follows rustdoc's ``LangString::parse``: words split on commas and whitespace, an unknown word
    makes the block not Rust unless ``rust`` is also present, and ``edition20xx`` or an ``E0123``
    error code are attributes rather than languages.
    """
    words = [w for w in re.split(r"[,\s]+", info.strip()) if w]
    other = [
        w for w in words
        if w not in _RUST_WORDS
        and not w.startswith("ignore-")
        and not re.fullmatch(r"edition\d{4}", w)
        and not re.fullmatch(r"E\d{4}", w)
        and not w.startswith("{")
    ]
    if other and "rust" not in words:
        return FenceClass.NOT_RUST
    if "ignore" in words or any(w.startswith("ignore-") for w in words):
        return FenceClass.IGNORED
    if "compile_fail" in words:
        return FenceClass.COMPILE_FAIL
    return FenceClass.COMPILED


@dataclass(frozen=True)
class Fence:
    """One fenced code block, with where it sits in the text it was found in."""

    info: str
    #: The lines between the fences, exactly as written.
    body: str
    #: ``[start, end)`` offsets of the whole block, both fence lines included, in the source text.
    start: int
    end: int

    @property
    def kind(self) -> FenceClass:
        return classify(self.info)


_OPENING = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def fences(markdown: str) -> list[Fence]:
    """Every fenced code block in ``markdown``, in order.

    CommonMark's rules, as far as doc comments use them: an opening fence of three or more
    backticks or tildes indented at most three spaces, closed by a run of the same character at
    least as long, or by the end of the text. A backtick fence's info string cannot contain a
    backtick, which is what keeps an inline ```` ``` ```` in prose from opening one.
    """
    found: list[Fence] = []
    lines = markdown.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    i = 0
    while i < len(lines):
        opening = _OPENING.match(lines[i].rstrip("\r\n"))
        if opening is None or (opening.group(2)[0] == "`" and "`" in opening.group(3)):
            i += 1
            continue
        indent, marker, info = opening.groups()
        closing = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$")
        end = i + 1
        while end < len(lines) and not closing.match(lines[end].rstrip("\r\n")):
            end += 1
        body_lines = [_dedent(line, len(indent)) for line in lines[i + 1 : end]]
        found.append(
            Fence(
                info=info.strip(),
                body="".join(body_lines),
                start=offsets[i],
                end=offsets[min(end + 1, len(lines))],
            )
        )
        i = end + 1
    return found


def _dedent(line: str, width: int) -> str:
    stripped = len(line) - len(line.lstrip(" "))
    return line[min(stripped, width) :]


def reveal_hidden_lines(body: str) -> str:
    """The block as rustdoc compiles it: hidden lines shown, ``##`` unescaped.

    rustdoc hides a line whose trimmed text is ``#`` or starts with ``# ``, and renders ``##`` as a
    literal ``#``. ``#[attr]`` and ``#![attr]`` are neither and stay as written.
    """
    out: list[str] = []
    for line in body.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("##"):
            out.append(line.replace("##", "#", 1))
        elif trimmed.startswith("# "):
            out.append(trimmed[2:])
        elif trimmed == "#":
            out.append("")
        else:
            out.append(line)
    return "\n".join(out) + "\n" if out else ""


def wrap(source: str) -> str:
    """One block as a module file: leading inner attributes first, the rest inside ``fn main``.

    rustdoc's own wrapping, minus the crate-level parts a module cannot carry. A block that writes
    its own ``fn main`` is taken as written, which is rustdoc's rule too.
    """
    lines = source.splitlines()
    attrs: list[str] = []
    while lines and (lines[0].strip().startswith("#![") or not lines[0].strip()):
        attrs.append(lines.pop(0))
    rest = "\n".join(lines)
    body = rest if _MAIN.search(rest) else f"fn main() {{\n{rest}\n}}"
    return "\n".join([*attrs, body]) + "\n"


# ---------------------------------------------------------------------------------------------
# compiling


@dataclass(frozen=True)
class Passed:
    pass


@dataclass(frozen=True)
class Failed:
    """``diagnostics`` is rustc's rendered text for every error attributed to the block."""

    diagnostics: str


type Verdict = Passed | Failed


class GateUnavailable(RuntimeError):
    """The check could not run, or failed in a way no block accounts for.

    Distinct from a block failing: that is a verdict, and the caller decides what to do with it.
    This is the check itself being unable to say anything, which nobody should read as a pass.
    """


def _module(index: int) -> str:
    return f"example_{index:04}"


def _span_files(span: Mapping) -> Iterator[str]:
    """The span's file, then the file of each macro call it was expanded from, innermost first."""
    current: Mapping | None = span
    while current is not None:
        yield current.get("file_name", "")
        expansion = current.get("expansion")
        current = expansion.get("span") if isinstance(expansion, Mapping) else None


def _diagnostic_spans(message: Mapping) -> Iterator[Mapping]:
    spans = message.get("spans") or []
    yield from sorted(spans, key=lambda s: not s.get("is_primary"))
    for child in message.get("children") or []:
        yield from _diagnostic_spans(child)


def attribute_errors(
    messages: Iterable[Mapping], modules: Mapping[str, int]
) -> tuple[dict[int, list[str]], list[str]]:
    """``(rendered errors per block, errors no block accounts for)``.

    ``messages`` are cargo's ``--message-format json`` records; ``modules`` maps a block's module
    name to its index. An error belongs to the first block any of its spans — or the macro call a
    span was expanded from — lies in, which is how an error reported inside a CVLR macro's
    expansion lands on the example that called the macro.
    """
    per_block: dict[int, list[str]] = {}
    unattributed: list[str] = []
    for record in messages:
        if record.get("reason") != "compiler-message":
            continue
        message = record.get("message") or {}
        if message.get("level") not in ("error", "error: internal compiler error"):
            continue
        rendered = message.get("rendered") or message.get("message") or ""
        if not message.get("spans") and _SUMMARY.match(message.get("message", "")):
            continue
        owner = next(
            (
                modules[path.stem]
                for span in _diagnostic_spans(message)
                for path in map(Path, _span_files(span))
                if path.parent.name == _EXAMPLES_DIR and path.stem in modules
            ),
            None,
        )
        if owner is None:
            unattributed.append(rendered)
        else:
            per_block.setdefault(owner, []).append(rendered)
    return per_block, unattributed


def _write_crate_source(crate_dir: Path, sources: Mapping[int, str]) -> None:
    examples = crate_dir / "src" / _EXAMPLES_DIR
    examples.mkdir(parents=True, exist_ok=True)
    declarations = []
    for index, source in sorted(sources.items()):
        (examples / f"{_module(index)}.rs").write_text(wrap(source))
        declarations.append(
            f'#[path = "{_EXAMPLES_DIR}/{_module(index)}.rs"]\nmod {_module(index)};\n'
        )
    # Unused imports and dead code are the norm in an example, and a warning fails nothing.
    (crate_dir / "src" / "lib.rs").write_text("#![allow(warnings)]\n" + "".join(declarations))


def _check_once(
    crate_dir: Path, sources: Mapping[int, str], cargo: Sequence[str], timeout_s: int
) -> dict[int, list[str]]:
    _write_crate_source(crate_dir, sources)
    try:
        result = subprocess.run(
            [*cargo, "check", "--offline", "--quiet", "--message-format", "json"],
            cwd=crate_dir, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except FileNotFoundError as exc:
        raise GateUnavailable(f"{cargo[0]} is not on PATH, so no example can be checked.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateUnavailable(f"cargo check over the examples took over {timeout_s}s.") from exc
    if result.returncode == 0:
        return {}
    records = []
    for line in result.stdout.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    modules = {_module(index): index for index in sources}
    per_block, unattributed = attribute_errors(records, modules)
    if unattributed or not per_block:
        detail = "\n".join(unattributed) or result.stderr.strip()
        raise GateUnavailable(
            f"cargo check in {crate_dir} failed (exit {result.returncode}) with errors no example "
            f"accounts for, so the check cannot say which examples compile:\n{detail}"
        )
    return per_block


def check_examples(
    crate_dir: Path,
    sources: Sequence[str],
    *,
    cargo: Sequence[str] = ("cargo",),
    timeout_s: int = CHECK_TIMEOUT_S,
) -> list[Verdict]:
    """A verdict per source, in order, from ``cargo check`` of a crate holding all of them.

    ``crate_dir`` must already hold a ``Cargo.toml`` whose dependencies are what the examples may
    use; this writes ``src/lib.rs`` and one file per example under it, replacing any ``lib.rs``.
    ``sources`` are compiled as given: reveal hidden lines first.

    Failing examples are removed and the rest checked again until a build passes, so a
    :class:`Passed` is backed by a build that compiled every passing example together — rustc can
    stop short of type-checking one module after errors in another, and a single pass would then
    report it passing unchecked.
    """
    remaining = dict(enumerate(sources))
    verdicts: dict[int, Verdict] = {}
    while remaining:
        failures = _check_once(crate_dir, remaining, cargo, timeout_s)
        if not failures:
            break
        for index, errors in failures.items():
            verdicts[index] = Failed(diagnostics="\n".join(errors))
            del remaining[index]
    for index in remaining:
        verdicts[index] = Passed()
    return [verdicts[i] for i in range(len(sources))]
