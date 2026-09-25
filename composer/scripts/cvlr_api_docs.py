"""Producer: the CVLR crates' rustdoc → the ``cvlr_api_kb`` manifest.

``docs/cvlr-api-docs-plan.md`` §4.2 is the argument for this living here. The short form: the
compiler emits the item list, the signatures, the doc comments and the re-export targets, so this
is cargo plus a JSON walk — no model, no API key, and testable by the ordinary suite. It sits
beside :mod:`composer.scripts.ragbuild` (the manual's producer) and emits the same manifest format
(``docs/rag-import-format.md``), so :mod:`composer.scripts.rag_import` ingests it with no changes.

**What it emits, and what it deliberately does not.** Entries carry a *contract*: the item's
signature, the crate that defines it, its feature gate, its deprecation, the facade paths it is
reachable by, and whatever the crate documents. Not its implementation. rustdoc agrees on the one
case where the distinction is sharp — a ``macro`` item carries the macro's *matchers* with the
body elided as ``{ ... }``, which is the call signature and nothing more. An expansion is an
implementation detail, and a corpus the author treats as authoritative is the wrong place for one:
it invites a dependency on a binding name that is nobody's promise.

**Three things the walk has to get right**, each of which a naive pass gets wrong:

* **Methods are not path-addressable.** ``NativeIntU64::u64_max`` has no entry in rustdoc's
  ``paths`` — it lives inside an ``impl`` — and it was the single most-searched name in the live
  run's tool census. :func:`_impl_items` is why inherent impls are walked.
* **The facade re-exports almost everything.** ``clog`` resolves to ``cvlr_log::cvlr_log``, and an
  agent handed only the facade finds a re-export and stops. :func:`_aliases` inverts the ``use``
  items so each entry names the crate that *defines* it and lists the paths it is reachable by.
* **Trait-impl methods are mechanical.** Twenty ``Add``/``Sub``/``Mul`` impls make twenty entries
  that answer a question nobody asks while burying the ones people do. Their methods are not
  emitted; the type's entry names the traits it implements instead.

Run it through :mod:`composer.scripts.populate_cvlr_rag`'s wrapper, or directly::

    uv run --no-sync python -m composer.scripts.cvlr_api_docs --output cvlr_api_kb.rag.json

It needs a nightly toolchain for ``--output-format json`` and a populated cargo registry; it
passes ``--offline`` so a warm cache needs no network.
"""

import argparse
import json
import logging
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from composer.rag.import_format import (
    EmbeddedBlock,
    EmbeddedBlockKind,
    EmbeddedGroup,
    ManualBlock,
    ManualBlockKind,
    ManualSection,
    RagManifest,
)
from composer.spec.cvlr_reference import ChainReference, reference_for
from composer.tools.cvlr_api_rag import API_ROOT, SURFACE_HEADING

_log = logging.getLogger(__name__)

#: The rustdoc JSON schema this parser speaks. It is nightly-only and explicitly unstable, so a
#: mismatch is refused loudly rather than half-parsed: a walk that silently skipped every item it
#: no longer recognized would emit a thin corpus that looks like a successful build.
FORMAT_VERSION = 60

#: Item kinds worth an entry. Everything else rustdoc emits is structure rather than surface —
#: ``impl`` and ``use`` are walked for what they tell us about other items, and ``module``,
#: ``variant``, ``struct_field`` and the ``assoc_*`` kinds are parts of an item already emitted.
DOCUMENTED_KINDS = frozenset(
    {
        "function",
        "macro",
        "proc_macro",
        "proc_attribute",
        "proc_derive",
        "struct",
        "enum",
        "union",
        "trait",
        "trait_alias",
        "type_alias",
        "constant",
        "static",
    }
)


#: The probe crate's own name. It matches :data:`composer.spec.cvlr.crates.CVLR_PREFIX` — which is
#: how the family is recognized — so it has to be excluded by name or the corpus documents the
#: empty crate this producer wrote to resolve the family.
PROBE_PACKAGE = "cvlr-api-probe"


class RustdocUnavailable(RuntimeError):
    """cargo could not produce rustdoc JSON, so there is no corpus to emit."""


@dataclass(frozen=True)
class Item:
    """One entry in the corpus: what a caller needs in order to write the call."""

    #: The name a caller writes, ``cvlr_assert_le`` or ``NativeIntU64::u64_max``.
    name: str
    kind: str
    #: The crate that *defines* it, in cargo's spelling.
    crate: str
    #: The full defining path, ``cvlr_asserts::cvlr_assert_le``. Empty for an associated item,
    #: whose type's path is the addressable one.
    path: str
    signature: str
    docs: str | None = None
    #: The cargo feature this item is gated behind, when rustdoc records one. A helper that exists
    #: but is gated is a compile error that reads like a missing item.
    gate: str | None = None
    deprecated: str | None = None
    #: Paths this item is also reachable by, through a facade re-export.
    aliases: tuple[str, ...] = ()
    #: For a type: the traits implemented for it. Named rather than given an entry each.
    traits: tuple[str, ...] = ()

    def body(self) -> list[tuple[str, str]]:
        """``(kind, text)`` pairs, where kind is ``"text"`` or ``"code"``.

        One shape for both products; each converts it to its own block type.
        """
        out: list[tuple[str, str]] = [
            ("text", f"`{self.name}` — {self.kind.replace('_', ' ')}, defined in `{self.crate}`.")
        ]
        out.append(("code", self.signature))
        if self.docs:
            out.append(("text", self.docs))
        if self.path:
            out.append(("text", f"Path: `{self.path}`"))
        if self.aliases:
            reachable = ", ".join(f"`{a}`" for a in self.aliases)
            out.append(("text", f"Also reachable as {reachable}."))
        if self.traits:
            out.append(("text", f"Implements: {', '.join(f'`{t}`' for t in self.traits)}."))
        if self.gate:
            out.append(
                ("text", f"Behind the `{self.gate}` cargo feature; without it this item is absent.")
            )
        if self.deprecated:
            out.append(("text", f"Deprecated: {self.deprecated}"))
        return out


@dataclass
class CrateDocs:
    """One crate's rustdoc JSON, already parsed.

    Constructed from a file by :func:`load`, or in a test from a literal payload — which is the
    whole reason the walk takes this rather than a path.
    """

    name: str
    version: str
    payload: dict
    #: Facade paths pointing into this crate, by the defining path they resolve to. Filled by
    #: :func:`resolve_aliases` once every crate has been loaded, since a re-export is a fact about
    #: two crates.
    aliases: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        found = self.payload.get("format_version")
        if found != FORMAT_VERSION:
            raise RustdocUnavailable(
                f"{self.name}: rustdoc JSON format_version {found}, but this parser speaks "
                f"{FORMAT_VERSION}. The format is nightly-only and unstable; update "
                f"composer/scripts/cvlr_api_docs.py against the new schema rather than ingesting "
                f"a partial walk."
            )

    @property
    def index(self) -> dict[str, dict]:
        return self.payload["index"]

    @property
    def paths(self) -> dict[str, dict]:
        return self.payload["paths"]

    @property
    def local_crate_id(self) -> int:
        return self.index[str(self.payload["root"])]["crate_id"]


# ---------------------------------------------------------------------------------------------
# rendering types
#
# rustdoc gives a structured type tree rather than source text. Rendering it back is a bounded
# job for the shapes CVLR uses, and every unhandled shape falls through to a marker rather than
# to a plausible-looking wrong type.


def render_type(node: object) -> str:
    """A Rust spelling of one rustdoc type node.

    Falls back to ``_`` for a shape this does not model. A wrong type is worse than an elided one:
    the caller is about to write it into Rust that has to compile.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict) or len(node) != 1:
        return "_"
    (kind, body), = node.items()
    match kind:
        case "primitive" | "generic":
            return str(body)
        case "resolved_path":
            path = body.get("path", "_")
            return f"{path}{_render_args(body.get('args'))}"
        case "borrowed_ref":
            life = f"{body['lifetime']} " if body.get("lifetime") else ""
            mut = "mut " if body.get("is_mutable") else ""
            return f"&{life}{mut}{render_type(body.get('type'))}"
        case "raw_pointer":
            return f"*{'mut' if body.get('is_mutable') else 'const'} {render_type(body.get('type'))}"
        case "slice":
            return f"[{render_type(body)}]"
        case "array":
            return f"[{render_type(body.get('type'))}; {body.get('len')}]"
        case "tuple":
            return f"({', '.join(render_type(t) for t in body)})" if body else "()"
        case "impl_trait":
            return f"impl {' + '.join(_render_bound(b) for b in body)}"
        case "dyn_trait":
            traits = body.get("traits", [])
            return f"dyn {' + '.join(render_type({'resolved_path': t['trait']}) for t in traits)}"
        case "qualified_path":
            return f"<{render_type(body.get('self_type'))}>::{body.get('name')}"
        case "function_pointer":
            sig = body.get("signature", {})
            args = ", ".join(render_type(t) for _n, t in sig.get("inputs", []))
            out = sig.get("output")
            return f"fn({args})" + (f" -> {render_type(out)}" if out else "")
        case "infer":
            return "_"
        case _:
            return "_"


def _render_args(args: object) -> str:
    if not isinstance(args, dict):
        return ""
    if (angle := args.get("angle_bracketed")) is not None:
        rendered = [_render_generic_arg(a) for a in angle.get("args", [])]
        shown = [r for r in rendered if r]
        return f"<{', '.join(shown)}>" if shown else ""
    if (paren := args.get("parenthesized")) is not None:
        inputs = ", ".join(render_type(t) for t in paren.get("inputs", []))
        out = paren.get("output")
        return f"({inputs})" + (f" -> {render_type(out)}" if out else "")
    return ""


def _render_generic_arg(arg: object) -> str:
    if not isinstance(arg, dict):
        return ""
    if (life := arg.get("lifetime")) is not None:
        return str(life)
    if (typ := arg.get("type")) is not None:
        return render_type(typ)
    if (const := arg.get("const")) is not None:
        return str(const.get("expr", ""))
    return ""


def _render_bound(bound: object) -> str:
    if isinstance(bound, dict) and (tb := bound.get("trait_bound")) is not None:
        return render_type({"resolved_path": tb["trait"]})
    if isinstance(bound, dict) and (life := bound.get("outlives")) is not None:
        return str(life)
    return "_"


def _render_generics(generics: dict) -> str:
    """The ``<T, 'a>`` clause, lifetimes included. Where-clauses are elided: they constrain, and
    the caller needs the shape of the call rather than the full bound set."""
    names = [p["name"] for p in generics.get("params", []) if p.get("name") not in (None, "Self")]
    return f"<{', '.join(names)}>" if names else ""


def _function_signature(name: str, inner: dict) -> str:
    sig = inner.get("sig", {})
    header = inner.get("header", {})
    prefix = "".join(
        part
        for part, on in (("const ", header.get("is_const")), ("unsafe ", header.get("is_unsafe")))
        if on
    )
    args = ", ".join(
        arg_name if typ == {"generic": "Self"} and arg_name in ("self",) else
        f"{arg_name}: {render_type(typ)}"
        for arg_name, typ in sig.get("inputs", [])
    )
    out = sig.get("output")
    ret = f" -> {render_type(out)}" if out is not None else ""
    return f"{prefix}fn {name}{_render_generics(inner.get('generics', {}))}({args}){ret}"


def _signature(name: str, kind: str, inner: dict | str) -> str:
    """The declaration a caller reads, per kind.

    A ``macro``'s inner *is* its declaration — rustdoc hands over the matchers with the body
    elided — so it is passed through verbatim.
    """
    match kind:
        case "macro":
            return str(inner)
        case "proc_macro" | "proc_attribute" | "proc_derive":
            return f"#[{name}]" if kind == "proc_attribute" else f"{name}!"
        case "function" if isinstance(inner, dict):
            return _function_signature(name, inner)
        case "struct" | "union" if isinstance(inner, dict):
            return f"{kind} {name}{_render_generics(inner.get('generics', {}))}"
        case "enum" | "trait" if isinstance(inner, dict):
            return f"{kind} {name}{_render_generics(inner.get('generics', {}))}"
        case "type_alias" if isinstance(inner, dict):
            return f"type {name} = {render_type(inner.get('type'))}"
        case "constant" if isinstance(inner, dict):
            return f"const {name}: {render_type(inner.get('type'))}"
        case "static" if isinstance(inner, dict):
            return f"static {name}: {render_type(inner.get('type'))}"
        case _:
            return name


# ---------------------------------------------------------------------------------------------
# the walk


def _kind_of(item: dict) -> tuple[str, dict | str] | None:
    inner = item.get("inner")
    if not isinstance(inner, dict) or len(inner) != 1:
        return None
    (kind, body), = inner.items()
    return kind, body


def _gate_of(item: dict) -> str | None:
    """The cargo feature an item is behind, read from its ``cfg`` attribute.

    Matched as a substring rather than parsed: rustdoc renders the attribute as source text, and
    the only question anyone asks of it is which feature name to turn on.
    """
    for attr in item.get("attrs") or []:
        text = attr if isinstance(attr, str) else str(attr)
        if "feature" in text and (start := text.find('feature = "')) >= 0:
            rest = text[start + len('feature = "') :]
            end = rest.find('"')
            if end > 0:
                return rest[:end]
    return None


def _deprecation_of(item: dict) -> str | None:
    dep = item.get("deprecation")
    if not isinstance(dep, dict):
        return None
    note = dep.get("note") or "no reason given"
    since = f" (since {dep['since']})" if dep.get("since") else ""
    return f"{note}{since}"


def _impl_items(docs: CrateDocs) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, dict]]]]:
    """``(traits per type, inherent members per type)``.

    Trait impls contribute only their trait's name to the type they are for: twenty ``Add`` impls
    are twenty entries nobody searches for. Inherent impls contribute their members, because that
    is where the methods a caller actually writes live and nothing else addresses them.
    """
    traits: dict[str, list[str]] = {}
    members: dict[str, list[tuple[str, dict]]] = {}
    for item in docs.index.values():
        if item.get("crate_id") != docs.local_crate_id:
            continue
        found = _kind_of(item)
        if found is None or found[0] != "impl" or not isinstance(found[1], dict):
            continue
        block = found[1]
        for_type = render_type(block.get("for"))
        if block.get("trait") is not None:
            name = render_type({"resolved_path": block["trait"]})
            traits.setdefault(for_type, []).append(name)
            continue
        for member_id in block.get("items", []):
            member = docs.index.get(str(member_id))
            if member is None or not member.get("name"):
                continue
            members.setdefault(for_type, []).append((member["name"], member))
    return traits, members


def items_of(docs: CrateDocs) -> list[Item]:
    """Every entry this crate contributes, in name order.

    Only items the crate itself defines: a rustdoc payload carries every dependency's items too,
    and attributing those here would file the same item under several crates.
    """
    traits, members = _impl_items(docs)
    found: list[Item] = []

    for item_id, item in docs.index.items():
        if item.get("crate_id") != docs.local_crate_id or not item.get("name"):
            continue
        kinds = _kind_of(item)
        if kinds is None or kinds[0] not in DOCUMENTED_KINDS:
            continue
        kind, inner = kinds
        summary = docs.paths.get(item_id)
        if summary is None:
            # Not path-addressable: an associated item, reached through its type below.
            continue
        path = "::".join(summary["path"])
        name = item["name"]
        found.append(
            Item(
                name=name,
                kind=kind,
                crate=docs.name,
                path=path,
                signature=_signature(name, kind, inner),
                docs=item.get("docs"),
                gate=_gate_of(item),
                deprecated=_deprecation_of(item),
                aliases=tuple(sorted(docs.aliases.get(path, ()))),
                traits=tuple(sorted(set(traits.get(name, ())))),
            )
        )

    for type_name, type_members in members.items():
        for member_name, member in type_members:
            kinds = _kind_of(member)
            if kinds is None or kinds[0] not in DOCUMENTED_KINDS:
                continue
            kind, inner = kinds
            found.append(
                Item(
                    name=f"{type_name}::{member_name}",
                    kind=kind,
                    crate=docs.name,
                    path="",
                    signature=_signature(member_name, kind, inner),
                    docs=member.get("docs"),
                    gate=_gate_of(member),
                    deprecated=_deprecation_of(member),
                )
            )

    return sorted(found, key=lambda i: i.name)


def resolve_aliases(crates: list[CrateDocs]) -> None:
    """Fill each crate's :attr:`CrateDocs.aliases` from every other crate's ``use`` items.

    This is the re-export trap recorded rather than taught. ``cvlr`` re-exports almost its whole
    surface, so ``clog`` is reached as ``cvlr::clog`` and *defined* as ``cvlr_log::cvlr_log``; an
    entry that named only one of the two would send a caller to a path that does not resolve or to
    a crate they cannot search.
    """
    by_crate = {c.name: c for c in crates}
    #: rustdoc spells a crate name with underscores; cargo spells it with hyphens.
    by_module = {c.name.replace("-", "_"): c for c in crates}
    for facade in crates:
        externals = facade.payload.get("external_crates", {})
        for item_id, item in facade.index.items():
            if item.get("crate_id") != facade.local_crate_id:
                continue
            kinds = _kind_of(item)
            if kinds is None or kinds[0] != "use" or not isinstance(kinds[1], dict):
                continue
            use = kinds[1]
            target_id = use.get("id")
            if target_id is None or use.get("is_glob"):
                continue
            target = facade.paths.get(str(target_id))
            if target is None:
                continue
            owner = externals.get(str(target["crate_id"]), {}).get("name")
            defining = by_module.get(owner) if owner else None
            if defining is None or defining.name == facade.name:
                continue
            alias_path = _alias_path(facade, item_id, use.get("name") or "")
            if alias_path is None:
                continue
            defining.aliases.setdefault("::".join(target["path"]), []).append(alias_path)
    for crate in by_crate.values():
        for target, paths in crate.aliases.items():
            crate.aliases[target] = sorted(set(paths))


def _alias_path(facade: CrateDocs, item_id: str, name: str) -> str | None:
    """The path a re-export is reachable by, from the facade's own ``paths`` where it has one.

    A ``use`` that rustdoc gives no path of its own is reported at the crate root, which is where
    a ``pub use`` in ``lib.rs`` puts it and is the spelling a caller will try first.
    """
    summary = facade.paths.get(item_id)
    if summary is not None:
        return "::".join(summary["path"])
    if not name:
        return None
    return f"{facade.name.replace('-', '_')}::{name}"


# ---------------------------------------------------------------------------------------------
# the manifest


def _surface_section(docs: CrateDocs, items: list[Item]) -> ManualSection:
    """The closed-world listing: every item this crate exports, as one atomic block.

    This is the only row that supports "does X exist?". A vector search always returns its nearest
    rows, so an empty result cannot tell absence from an oddly phrased query; a caller who reads
    this list can say a name is not in it.
    """
    lines = [f"`{i.name}` — {i.kind.replace('_', ' ')}" for i in items]
    listing = "\n".join(f"- {line}" for line in lines) or "- (this crate exports nothing)"
    return ManualSection(
        headers=[API_ROOT, docs.name, docs.version, SURFACE_HEADING],
        blocks=[
            ManualBlock(
                kind=ManualBlockKind.TEXT,
                body=(
                    f"Everything `{docs.name}` {docs.version} exports, in full — "
                    f"{len(items)} items. This list is closed: a name that is not on it is not in "
                    f"this crate at the release this build pins.\n\n{listing}"
                ),
            )
        ],
    )


def build_manifest(crates: list[CrateDocs], source: str) -> RagManifest:
    """Both retrieval products for the whole family.

    The two want opposite things and are laid out separately rather than derived from each other:
    the manual product keeps every item addressable, because an exact lookup of
    ``NativeIntU64::u64_max`` has to find it; the embedded product is what a natural-language
    question lands on. Trait-impl methods are collapsed by :func:`_impl_items` before either sees
    them, which is the grouping judgement made as a rule rather than a guess.
    """
    manual: list[ManualSection] = []
    embedded: list[EmbeddedGroup] = []
    for docs in sorted(crates, key=lambda c: c.name):
        items = items_of(docs)
        manual.append(_surface_section(docs, items))
        for item in items:
            headers = [API_ROOT, docs.name, docs.version, item.name]
            body = item.body()
            manual.append(
                ManualSection(
                    headers=headers,
                    blocks=[
                        ManualBlock(
                            kind=ManualBlockKind.CODE if k == "code" else ManualBlockKind.TEXT,
                            body=text,
                        )
                        for k, text in body
                    ],
                )
            )
            embedded.append(
                EmbeddedGroup(
                    headers=headers,
                    blocks=[
                        EmbeddedBlock(
                            kind=EmbeddedBlockKind.CODE if k == "code"
                            else EmbeddedBlockKind.PARAGRAPH,
                            body=text,
                        )
                        for k, text in body
                    ],
                )
            )
    return RagManifest(
        knowledge_base="cvlr_api_kb",
        source=source,
        manual_sections=manual,
        embedded_groups=embedded,
    )


# ---------------------------------------------------------------------------------------------
# driving cargo


def _probe_crate(root: pathlib.Path, reference: ChainReference) -> None:
    """A crate whose only purpose is to make the reference set resolvable.

    The dependency block comes from the reference set itself, so the corpus is built against
    exactly the releases a run pins — which is what makes a bump one change rather than two
    (``docs/cvlr-api-docs-plan.md`` §2).
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.rs").write_text("")
    (root / "Cargo.toml").write_text(
        "[package]\n"
        f'name = "{PROBE_PACKAGE}"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n\n'
        "[dependencies]\n" + reference.cargo_dependencies() + "\n"
    )


def _family_packages(root: pathlib.Path) -> list[tuple[str, str]]:
    """``(name, version)`` for every CVLR crate the probe resolved, from its lockfile.

    The family is not declared anywhere — ``cvlr`` pulls in ``cvlr-asserts``, ``cvlr-log`` and the
    rest as ordinary dependencies — so it is recognized by name, exactly as
    :mod:`composer.spec.cvlr.crates` does for a target.
    """
    lock = (root / "Cargo.lock").read_text()
    found: list[tuple[str, str]] = []
    name: str | None = None
    for line in lock.splitlines():
        if line.startswith('name = "'):
            name = line.split('"')[1]
        elif line.startswith('version = "') and name is not None:
            if name.startswith("cvlr") and name != PROBE_PACKAGE:
                found.append((name, line.split('"')[1]))
            name = None
    return sorted(set(found))


def _rustdoc(root: pathlib.Path, package: str) -> pathlib.Path:
    result = subprocess.run(
        [
            "cargo", "+nightly", "rustdoc", "-q", "--offline",
            "-p", package, "-Z", "unstable-options", "--output-format", "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    out = root / "target" / "doc" / f"{package.replace('-', '_')}.json"
    if result.returncode != 0 or not out.is_file():
        raise RustdocUnavailable(
            f"cargo rustdoc failed for {package} (exit {result.returncode}). rustdoc JSON needs a "
            f"nightly toolchain and a warm cargo registry.\n{result.stderr.strip()}"
        )
    return out


def load(path: pathlib.Path, name: str, version: str) -> CrateDocs:
    return CrateDocs(name=name, version=version, payload=json.loads(path.read_text()))


def generate(reference: ChainReference, workdir: pathlib.Path) -> RagManifest:
    """Build the probe, run rustdoc over the whole family, and walk the result."""
    if shutil.which("cargo") is None:
        raise RustdocUnavailable("cargo is not on PATH, so there is nothing to document.")
    _probe_crate(workdir, reference)
    subprocess.run(
        ["cargo", "generate-lockfile", "-q", "--offline"],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    packages = _family_packages(workdir)
    if not packages:
        raise RustdocUnavailable(
            f"the probe crate resolved no CVLR packages. Its dependencies were:\n"
            f"{reference.cargo_dependencies()}"
        )
    crates: list[CrateDocs] = []
    for name, version in packages:
        _log.info("rustdoc: %s %s", name, version)
        crates.append(load(_rustdoc(workdir, name), name, version))
    resolve_aliases(crates)
    pinned = ", ".join(f"{c.name} {c.version}" for c in reference.crates())
    return build_manifest(crates, source=f"rustdoc over the CVLR family pinned at {pinned}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", "-o", type=pathlib.Path, default=pathlib.Path("cvlr_api_kb.rag.json"),
        help="Where to write the manifest. Feed it to composer.scripts.rag_import.",
    )
    parser.add_argument(
        "--chain", default="solana", help="Which reference set to document (solana, soroban)."
    )
    parser.add_argument(
        "--keep", type=pathlib.Path, default=None,
        help="Build the probe crate here and leave it, instead of in a temporary directory.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    reference = reference_for(args.chain)
    try:
        if args.keep is not None:
            args.keep.mkdir(parents=True, exist_ok=True)
            manifest = generate(reference, args.keep)
        else:
            with tempfile.TemporaryDirectory(prefix="cvlr-api-docs-") as tmp:
                manifest = generate(reference, pathlib.Path(tmp))
    except RustdocUnavailable as exc:
        raise SystemExit(f"cvlr_api_docs: {exc}") from exc

    args.output.write_text(manifest.model_dump_json(indent=1))
    _log.info(
        "wrote %s: %d sections, %d embedded groups",
        args.output, len(manifest.manual_sections), len(manifest.embedded_groups),
    )


if __name__ == "__main__":
    sys.exit(main())
