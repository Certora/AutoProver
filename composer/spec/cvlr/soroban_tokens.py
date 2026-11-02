"""The SEP-41 tokens a Soroban contract calls, pointed at an in-process model under ``certora``.

The Soroban Prover does not follow a call into another contract: the call returns an arbitrary
value and changes nothing. Through the real token client, two reads of one balance are two
unrelated numbers, so no rule about what a transfer did can be proven. The scaffold writes a model
of the token into the harness (``harness_files/soroban_token_model.template.rs``) and points the
contract's client at it, in the two shapes contracts reach one:

* ``soroban_sdk::token`` named by a ``use``: ``use soroban_sdk::token;``,
  ``use soroban_sdk::{…, token, …};`` or ``use soroban_sdk::token::…;``. The ``use`` is gated on
  ``not(feature = "certora")`` and followed by a twin under ``certora`` that names the model
  instead. One-way-channel does this.
* A module whose only item is a ``contractimport!`` of a token's wasm, one whose module name or
  file names a token. The module is gated the same way, and its name is re-exported from the model
  under ``certora``. Phoenix generates its token client this way. A module the package names
  anything from besides its ``Client`` is left alone: the model has nothing else such a module
  generates.

Each edit only adds lines, so a build without the feature compiles what it compiled before. A site
already gated is recognized by the gate on the line above it, so a second run finds it done.
Anything else that reaches a token — ``soroban_sdk::token::Client`` spelled in full, a module that
is not recognized — still calls the real contract, and the plan says so in a note. Code in a
``#[cfg(test)]`` module is not read.
"""

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path

from composer.spec.cvlr.soroban_types import package_sources

#: The harness module holding the model (``harness_files/``).
TOKEN_MODEL = "soroban_token_model"

_MODEL_PATH = f"crate::certora::{TOKEN_MODEL}"
_GATE_OFF = '#[cfg(not(feature = "certora"))]'
_GATE_ON = '#[cfg(feature = "certora")]'
_OFF_NOTE = " // added by AutoProver: under `certora` the token model stands in for this"
_ON_NOTE = f" // added by AutoProver: the token model, src/certora/{TOKEN_MODEL}.rs"

#: What a ``contractimport!`` module generates that the model also has.
_IMPORT_NAMES = frozenset({"Client"})

_VIS = r"(?P<vis>pub(?:\s*\([^)]*\))?\s+)?"
_USE = re.compile(
    rf"^(?P<indent>[ \t]*){_VIS}use\s+(?:::)?soroban_sdk\s*::\s*(?P<tree>[^;]*);", re.MULTILINE
)
_MOD = re.compile(rf"^(?P<indent>[ \t]*){_VIS}mod\s+(?P<name>[A-Za-z_]\w*)\s*\{{", re.MULTILINE)
_TEST_MOD = re.compile(r"#\[[^\]]*\btest\b[^\]]*\]\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+\w+\s*\{")
_IMPORT_BODY = re.compile(
    r"\A\s*(?:soroban_sdk\s*::\s*)?contractimport\s*!\s*\(\s*file\s*=\s*\"(?P<file>[^\"]*)\""
    r"[^)]*\)\s*;?\s*\Z"
)
_TOKEN_PART = re.compile(r"\Atoken\b")
_FULL_PATH = re.compile(r"\bsoroban_sdk\s*::\s*token\s*::\s*(?P<item>\w+)")
_ATTRIBUTE_LINE = re.compile(r"\A[ \t]*#\[.*\][ \t]*\Z")
_RAW_STRING = re.compile(r'r(#*)"')
_CHAR = re.compile(r"'(?:\\[^']*|[^\\'\n])'")


@dataclasses.dataclass(frozen=True)
class TokenSite:
    """Where the contract names a token client's module, and what it calls that module."""

    path: Path
    line: int
    name: str
    #: Gated by an earlier run.
    done: bool


@dataclasses.dataclass(frozen=True)
class TokenNote:
    """Something that reaches a token and is left calling the real one."""

    path: Path
    line: int
    text: str


@dataclasses.dataclass(frozen=True)
class TokenEdits:
    """What pointing a package's token clients at the model takes."""

    sites: tuple[TokenSite, ...]
    #: Each file to edit, as it is and as it would be.
    edited: dict[Path, tuple[str, str]]
    notes: tuple[TokenNote, ...]


# ---------------------------------------------------------------------------------------------
# Reading Rust, as far as finding these takes


def _mask(src: str, *, strings: bool) -> str:
    """``src`` with every comment blanked to spaces, and with ``strings`` every string and
    character literal's contents too. Same length and same lines, so a position in it is a
    position in ``src``."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if src.startswith("//", i):
            end = src.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif (m := _RAW_STRING.match(src, i)) and (i == 0 or not _is_ident(src[i - 1])):
            close = '"' + m.group(1)
            end = src.find(close, m.end())
            end = n if end == -1 else end
            if strings:
                blank(m.end(), end)
            i = end + len(close)
        elif src[i] == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            if strings:
                blank(i + 1, j)
            i = j + 1
        elif m := _CHAR.match(src, i):
            if strings:
                blank(i + 1, m.end() - 1)
            i = m.end()
        else:
            i += 1
    return "".join(out)


def _is_ident(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _matching_brace(code: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top(s: str) -> list[str]:
    """``s`` split on commas outside any brace."""
    parts: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(s):
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(s[start:i].strip())
            start = i + 1
    parts.append(s[start:].strip())
    return [p for p in parts if p]


def _line_start(src: str, pos: int) -> int:
    return src.rfind("\n", 0, pos) + 1


def _line_end(src: str, pos: int) -> int:
    end = src.find("\n", pos)
    return len(src) if end == -1 else end


def _lines_above(text: str, line_start: int) -> list[str]:
    return text[:line_start].split("\n")[:-1]


def _attributes_above(text: str, line_start: int) -> list[str]:
    """The single-line attributes directly above the line at ``line_start``, in order."""
    attrs: list[str] = []
    for line in reversed(_lines_above(text, line_start)):
        if not _ATTRIBUTE_LINE.match(line):
            break
        attrs.insert(0, line.strip())
    return attrs


def _token_twin(part: str) -> tuple[str, str]:
    """The model's spelling of one ``token…`` part of a ``soroban_sdk`` use tree, and the name the
    part gives the module (``token`` unless renamed)."""
    rest = part[len("token") :].strip()
    if not rest:
        return f"{_MODEL_PATH} as token", "token"
    if rest.startswith("as "):
        return f"{_MODEL_PATH} {rest}", rest.removeprefix("as ").strip()
    tail = rest.removeprefix("::").strip()
    if tail.startswith("{") and tail.endswith("}"):
        items = ["self as token" if item == "self" else item for item in _split_top(tail[1:-1])]
        tail = "{" + ", ".join(items) + "}"
    return f"{_MODEL_PATH}::{tail}", "token"


def _sdk_twins(tree: str) -> tuple[list[str], str] | None:
    """The use trees a ``use soroban_sdk::<tree>;`` becomes under ``certora`` and the name it gives
    the token module, or ``None`` when it names no ``token``."""
    tree = " ".join(tree.split())
    parts = _split_top(tree[1:-1]) if tree.startswith("{") and tree.endswith("}") else [tree]
    tokens = [_token_twin(p) for p in parts if _TOKEN_PART.match(p)]
    if not tokens:
        return None
    others = [p for p in parts if not _TOKEN_PART.match(p)]
    twins = ([f"soroban_sdk::{{{', '.join(others)}}}"] if others else []) + [t for t, _ in tokens]
    return twins, tokens[0][1]


@dataclasses.dataclass
class _File:
    path: Path
    src: str
    #: ``src`` with comments blanked.
    uncommented: str
    #: ``src`` with comments and literals blanked, and every ``#[cfg(test)]`` module.
    code: str
    #: (position, text) to insert into ``src``.
    inserts: list[tuple[int, str]] = dataclasses.field(default_factory=list)

    @classmethod
    def read(cls, path: Path) -> "_File":
        src = path.read_text(errors="replace")
        code = _mask(src, strings=True)
        for m in reversed(list(_TEST_MOD.finditer(code))):
            close = _matching_brace(code, m.end() - 1) + 1
            if close:
                blanked = re.sub(r"[^\n]", " ", code[m.start() : close])
                code = code[: m.start()] + blanked + code[close:]
        return cls(path, src, _mask(src, strings=False), code)

    def line_of(self, pos: int) -> int:
        return self.src.count("\n", 0, pos) + 1

    def is_gated(self, start: int) -> bool:
        above = _lines_above(self.uncommented, _line_start(self.src, start))
        return bool(above) and above[-1].strip().startswith(_GATE_OFF)

    def gate(self, start: int, end: int, indent: str, vis: str, twins: Iterable[str]) -> None:
        """Gate the item from ``start`` to the line holding ``end`` off under ``certora``, and add
        ``twins`` after it, each a ``use`` under the feature carrying the item's attributes."""
        line_start = _line_start(self.src, start)
        attrs = "".join(f"\n{indent}{a}" for a in _attributes_above(self.uncommented, line_start))
        self.inserts.append((line_start, f"{indent}{_GATE_OFF}{_OFF_NOTE}\n"))
        self.inserts.append(
            (
                _line_end(self.src, end),
                "".join(
                    f"{attrs}\n{indent}{_GATE_ON}{_ON_NOTE}\n{indent}{vis}use {twin};"
                    for twin in twins
                ),
            )
        )

    def edited(self) -> str:
        out = self.src
        for pos, text in sorted(self.inserts, key=lambda p: p[0], reverse=True):
            out = out[:pos] + text + out[pos:]
        return out


# ---------------------------------------------------------------------------------------------
# Planning


def _import_modules(f: _File) -> list[tuple[re.Match[str], int, str]]:
    """Each module whose only item is a token's ``contractimport!``: the match, where it closes,
    and the imported file."""
    found = []
    for m in _MOD.finditer(f.code):
        close = _matching_brace(f.code, m.end() - 1)
        if close == -1:
            continue
        body = _IMPORT_BODY.match(f.uncommented[m.end() : close])
        if body is None:
            continue
        file = body.group("file")
        if "token" in m.group("name").lower() or "token" in Path(file).stem.lower():
            found.append((m, close, file))
    return found


def plan_token_edits(package_root: Path, skip: Path) -> TokenEdits:
    """The edits that point the package's token clients at the model, read from its non-test
    sources under ``src/`` outside ``skip`` (the harness)."""
    files = [_File.read(p) for p in package_sources(package_root / "src", skip=skip)]
    sites: list[TokenSite] = []
    notes: list[TokenNote] = []

    for f in files:
        for m, close, file in _import_modules(f):
            name = m.group("name")
            done = f.is_gated(m.start())
            named = {
                used.group(1)
                for g in files
                for used in re.finditer(rf"\b{name}\s*::\s*(\w+|\*)", g.code)
            }
            if not done and not named <= _IMPORT_NAMES:
                notes.append(
                    TokenNote(
                        f.path,
                        f.line_of(m.start()),
                        f"`{name}` is a token client generated from {file}, and the package "
                        f"also names {', '.join(sorted(named - _IMPORT_NAMES))} from it, which the "
                        f"token model does not have, so it still calls the real token",
                    )
                )
                continue
            sites.append(TokenSite(f.path, f.line_of(m.start()), name, done))
            if not done:
                f.gate(
                    m.start(), close, m.group("indent"), m.group("vis") or "",
                    [f"{_MODEL_PATH} as {name}"],
                )

        uses: list[tuple[int, int]] = []
        for m in _USE.finditer(f.code):
            uses.append((m.start(), m.end()))
            found = _sdk_twins(m.group("tree"))
            if found is None:
                continue
            twins, name = found
            done = f.is_gated(m.start())
            sites.append(TokenSite(f.path, f.line_of(m.start()), name, done))
            if not done:
                f.gate(m.start(), m.end() - 1, m.group("indent"), m.group("vis") or "", twins)

        for m in _FULL_PATH.finditer(f.code):
            if not any(a <= m.start() < b for a, b in uses):
                notes.append(
                    TokenNote(
                        f.path,
                        f.line_of(m.start()),
                        f"`soroban_sdk::token::{m.group('item')}` is spelled in full, so calls "
                        f"through it still go to the real token",
                    )
                )

    return TokenEdits(
        sites=tuple(sorted(sites, key=lambda s: (s.path, s.line))),
        edited={f.path: (f.src, f.edited()) for f in files if f.inserts},
        notes=tuple(notes),
    )


def modelled(package_root: Path, harness: Path) -> tuple[TokenSite, ...]:
    """The sites a scaffold already pointed at the model, when the model is in ``harness``."""
    if not (harness / f"{TOKEN_MODEL}.rs").is_file():
        return ()
    return tuple(s for s in plan_token_edits(package_root, skip=harness).sites if s.done)
