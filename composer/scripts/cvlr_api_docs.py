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

**Documenting a working copy.** ``--crate-source`` points the probe at a local CVLR checkout:
every CVLR package that checkout defines replaces the crates.io release through
``[patch.crates-io]``, so the pin still decides *which* crates the corpus holds and the checkout
decides what is in them. That is how doc comments get read before they are published.

The rows it emits are byte-for-byte what the same crates would emit once released. That is
deliberate and worth keeping: a corpus built this way exists to be *tried* — to see what an author
agent does with documentation that is still in review — and a caveat stamped on every entry would
mean the run under observation was not the run that ships. Where the build came from is recorded
off to the side instead, in the manifest's ``source`` line and in the log, neither of which any
agent reads.

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
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from composer.rag.import_format import (
    EmbeddedBlock,
    EmbeddedBlockKind,
    EmbeddedGroup,
    ManualBlock,
    ManualBlockKind,
    ManualSection,
    RagManifest,
)
from composer.spec.cvlr.crates import CVLR_PREFIX
from composer.spec.cvlr.reference import ChainReference, reference_for
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
class Published:
    """The crate as crates.io publishes it, at the release the reference set pins."""

    def describe(self) -> str:
        return "the published release"


@dataclass(frozen=True)
class LocalCheckout:
    """A working copy documented in place of the published release.

    Two paths because they answer different questions. :attr:`root` is what the caller pointed at
    and what a reader recognizes; :attr:`crate_dir` is the package's own directory, which is what
    ``[patch.crates-io]`` has to name.
    """

    root: pathlib.Path
    crate_dir: pathlib.Path
    #: ``git describe`` of :attr:`root`, ``-dirty`` included. The only thing that distinguishes
    #: one build of a checkout from another, and uncommitted edits are the case that matters.
    revision: str

    def describe(self) -> str:
        return f"{self.root} @ {self.revision}"


#: Where one crate's documentation came from. A corpus mixes the two: a caller who points at the
#: ``cvlr`` checkout still gets ``cvlr-solana`` from crates.io. It reaches the manifest's source
#: line, the undocumented report and the log, and deliberately not the rows.
type Origin = Published | LocalCheckout


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
    origin: Origin = Published()
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
        case "proc_derive":
            return f"#[derive({name})]"
        case "proc_attribute":
            return f"#[{name}]"
        case "proc_macro":
            return f"{name}!"
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


def _item_kind(kind: str, inner: dict | str) -> str:
    """rustdoc's kind, with a procedural macro resolved to the three ways one is written.

    ``index`` calls all three ``proc_macro`` and puts the distinction inside; the difference is the
    whole of how a caller writes it — ``#[derive(Nondet)]``, ``#[rule]``, ``thing!()`` — so it is
    lifted out here, to the same spellings rustdoc's ``paths`` uses.
    """
    if kind == "proc_macro" and isinstance(inner, dict):
        match inner.get("kind"):
            case "derive":
                return "proc_derive"
            case "attr":
                return "proc_attribute"
            case _:
                return kind
    return kind


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


def _proc_macro_paths(docs: CrateDocs) -> dict[str, dict]:
    """``name -> paths entry`` for this crate's procedural macros.

    rustdoc files a proc macro's export under an id of its own, unrelated to the id of the item in
    ``index``, so the usual ``paths[item_id]`` lookup finds nothing and the walk reads it as an
    associated item and skips it. A proc-macro crate therefore contributed *no entries at all*:
    ``cvlr-derive``, whose whole surface is ``#[derive(Nondet)]`` and ``#[derive(CvlrLog)]``, was
    an empty crate in the corpus — and its "complete public surface" section said so.

    Name is a safe key only because these kinds are crate-level by construction: a procedural
    macro cannot be an associated item, so there is nothing for it to be confused with.
    """
    return {
        summary["path"][-1]: summary
        for summary in docs.paths.values()
        if summary.get("crate_id") == docs.local_crate_id
        and summary.get("kind") in ("proc_macro", "proc_attribute", "proc_derive")
        and summary.get("path")
    }


def _modules(docs: CrateDocs) -> tuple[dict[str, str], dict[str, bool]]:
    """``(module path per item id, is-public per module path)`` for this crate's own modules.

    Both halves answer the same question from different directions: which module an item was
    *declared* in, and whether that module can be named from outside the crate.
    """
    owner: dict[str, str] = {}
    public: dict[str, bool] = {}
    for item_id, item in docs.index.items():
        if item.get("crate_id") != docs.local_crate_id:
            continue
        inner = item.get("inner")
        if not isinstance(inner, dict) or "module" not in inner:
            continue
        summary = docs.paths.get(item_id)
        if summary is None:
            continue
        path = "::".join(summary["path"])
        public[path] = item.get("visibility") == "public" or len(summary["path"]) == 1
        for child in inner["module"].get("items", []):
            owner[str(child)] = path
    return owner, public


def _reachable(path: str, public: dict[str, bool]) -> bool:
    """Can ``path`` be written from outside the crate?

    Every module between the crate root and the item has to be public. rustdoc gives the
    *definition* path, and a crate that defines in ``mod layout;`` and re-exports with
    ``pub use layout::{...}`` has a definition path that does not compile — which is a third of
    the CVLR surface, including ``cvlr_deserialize_nondet_accounts``, the most-searched name in
    the census. The crate root itself is always reachable.
    """
    parts = path.split("::")
    return all(public.get("::".join(parts[: i + 1]), False) for i in range(1, len(parts) - 1))


def public_paths(docs: CrateDocs) -> dict[str, list[str]]:
    """Every path each of this crate's items can be written as, by item id, shallowest first.

    The definition path when it is reachable, plus every intra-crate ``pub use`` that re-exports
    the item from a module that is itself reachable. :func:`resolve_aliases` does this across
    crates and deliberately not within one — a facade alias is a fact about two crates — so this
    is the same inversion applied to the crate's own re-exports, which is where the paths that
    actually compile live.

    **Globs are expanded here and skipped there**, and the difference is knowledge rather than
    taste. Across crates a glob names no item this payload can enumerate, so reporting a path
    through one would be a guess. Within a crate the module's own contents are right here, so
    ``pub use log::*`` at the root *derives* ``cvlr_solana::<name>`` for every public item ``log``
    declares. That matters because the glob is the house style: ``cvlr-log`` re-exports its whole
    surface with two of them, and without this every ``log_*`` function has no writable path.
    """
    owner, public = _modules(docs)
    root = docs.name.replace("-", "_")
    found: dict[str, list[str]] = {}
    for item_id, summary in docs.paths.items():
        if summary.get("crate_id") == docs.local_crate_id and summary.get("path"):
            path = "::".join(summary["path"])
            if _reachable(path, public):
                found.setdefault(item_id, []).append(path)

    for item_id, item in docs.index.items():
        if item.get("crate_id") != docs.local_crate_id:
            continue
        kinds = _kind_of(item)
        if kinds is None or kinds[0] != "use" or not isinstance(kinds[1], dict):
            continue
        use = kinds[1]
        target, name, is_glob = use.get("id"), use.get("name"), bool(use.get("is_glob"))
        if target is None or (not name and not is_glob):
            continue
        if str(target) not in docs.index and str(target) not in docs.paths:
            continue
        # A `use` is declared inside some module; the path it creates hangs off that module.
        module = owner.get(item_id, root)
        if not public.get(module, module == root) or not _reachable(f"{module}::x", public):
            continue
        for target_id, exported in _exports(docs, str(target), name or "", is_glob):
            path = f"{module}::{exported}"
            if path not in found.get(target_id, []):
                found.setdefault(target_id, []).append(path)

    return {k: sorted(v, key=lambda p: (p.count("::"), len(p))) for k, v in found.items()}


def _exports(docs: CrateDocs, target: str, name: str, is_glob: bool) -> list[tuple[str, str]]:
    """``(item id, exported name)`` for what one ``use`` brings into its module.

    A plain ``use`` exports one item under ``name``, which is the rename when there is one. A glob
    exports every public item of the target module under its own name.
    """
    if not is_glob:
        return [(target, name)]
    item = docs.index.get(target)
    inner = item.get("inner") if item else None
    if not isinstance(inner, dict) or "module" not in inner:
        return []
    out: list[tuple[str, str]] = []
    for child_id in inner["module"].get("items", []):
        child = docs.index.get(str(child_id))
        if child and child.get("name") and child.get("visibility") == "public":
            out.append((str(child_id), child["name"]))
    return out


def items_of(docs: CrateDocs) -> list[Item]:
    """Every entry this crate contributes, in name order.

    Only items the crate itself defines: a rustdoc payload carries every dependency's items too,
    and attributing those here would file the same item under several crates.
    """
    traits, members = _impl_items(docs)
    proc_macros = _proc_macro_paths(docs)
    reachable = public_paths(docs)
    _, module_public = _modules(docs)
    found: list[Item] = []

    for item_id, item in docs.index.items():
        if item.get("crate_id") != docs.local_crate_id or not item.get("name"):
            continue
        kinds = _kind_of(item)
        if kinds is None or kinds[0] not in DOCUMENTED_KINDS:
            continue
        kind, inner = kinds
        kind = _item_kind(kind, inner)
        name = item["name"]
        summary = docs.paths.get(item_id) or proc_macros.get(name)
        if summary is None:
            # Not path-addressable: an associated item, reached through its type below.
            continue
        # The shallowest path that compiles, not the definition path. They differ for a third of
        # this family: `mod layout;` + `pub use layout::{...}` is the CVLR house style.
        # Prefer the path that spells the item's own name: `cvlr-solana` re-exports
        # `cvlr_deserialize_nondet_accounts` twice, once renamed, and the rename is shorter.
        # Two paths, and they are not interchangeable. `declared` is where rustdoc says the item
        # is defined, which is the key `resolve_aliases` files facade re-exports under. `path` is
        # what a caller writes, which for a third of this family is a re-export instead.
        declared = "::".join(summary["path"])
        writable = reachable.get(item_id) or []
        path = next((p for p in writable if p.rsplit("::", 1)[-1] == name), "")
        path = path or (writable[0] if writable else "")
        if not path and _reachable(declared, module_public):
            # A procedural macro's export is filed under an id of its own, so `public_paths` has
            # nothing under the index item's id. Its declaring module is the crate root.
            path = declared
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
                aliases=tuple(sorted(docs.aliases.get(declared, ()))),
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

    **The alias is not a nicety, it is usually the only path the caller can write.** The scaffold
    declares four crates — the core, the chain crate and its models — so the other eleven are
    transitive, and ``use cvlr_log::log_u64`` in a scaffolded project is
    ``E0433: use of undeclared crate``. 138 of 200 entries name such a crate.

    The facade reaches them with whole-crate globs, one public module each::

        pub mod asserts { pub use cvlr_asserts::*; }

    so the glob is expanded rather than skipped. The comment this replaces said a glob "names no
    item", which was true of a pass holding one payload; :func:`document_family` loads the whole
    family, so the target crate's own contents are right here to enumerate.
    """
    by_crate = {c.name: c for c in crates}
    #: rustdoc spells a crate name with underscores; cargo spells it with hyphens.
    by_module = {c.name.replace("-", "_"): c for c in crates}
    #: Which module each ``use`` is declared in, per crate: a glob's alias hangs off that module,
    #: and the facade puts every one of them in a module of its own.
    facade_modules = {c.name: _modules(c)[0] for c in crates}
    #: Computed once per crate: a glob asks the same question of the whole family.
    writable = {c.name: public_paths(c) for c in crates}
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
            if target_id is None:
                continue
            target = facade.paths.get(str(target_id))
            if target is None:
                continue
            owner = externals.get(str(target["crate_id"]), {}).get("name")
            defining = by_module.get(owner) if owner else None
            if defining is None or defining.name == facade.name:
                continue
            if use.get("is_glob"):
                module = facade_modules[facade.name].get(item_id)
                if module is None:
                    continue
                contents = _glob_contents(
                    defining, "::".join(target["path"]), writable[defining.name]
                )
                for declared, name in contents:
                    defining.aliases.setdefault(declared, []).append(f"{module}::{name}")
                continue
            alias_path = _alias_path(facade, item_id, use.get("name") or "")
            if alias_path is None:
                continue
            defining.aliases.setdefault("::".join(target["path"]), []).append(alias_path)
    for crate in by_crate.values():
        for target, paths in crate.aliases.items():
            crate.aliases[target] = sorted(set(paths))


def _glob_contents(
    defining: CrateDocs, module_path: str, writable: dict[str, list[str]]
) -> list[tuple[str, str]]:
    """``(defining path, name)`` for every item a whole-module glob brings in.

    Built from the defining crate's own writable paths rather than from the module's direct
    children, because the two globs compose: ``cvlr-log`` declares in ``mod core`` and re-exports
    with ``pub use crate::core::*``, and ``cvlr`` then re-exports the crate with
    ``pub mod log { pub use cvlr_log::*; }``. An item's name at ``module_path`` is what the outer
    glob carries, whichever module inside the crate declared it — and chasing only direct children
    left ``log_u64`` and the ``*_checked`` assert family with no path a project could write.
    """
    out: list[tuple[str, str]] = []
    for item_id, paths in writable.items():
        summary = defining.paths.get(item_id)
        if summary is None or summary.get("crate_id") != defining.local_crate_id:
            continue
        declared = "::".join(summary["path"])
        for path in paths:
            # Anything *under* the target, not only its direct children: a glob re-exports the
            # public submodules too, so `pub use cvlr_nondet::*` makes `havoc` reachable and
            # `cvlr::nondet::havoc::memhavoc` with it.
            if path.startswith(f"{module_path}::"):
                out.append((declared, path[len(module_path) + 2 :]))
                break
    return out


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


def _leaves(items: list[Item]) -> list[str]:
    """The header leaf for each item, disambiguated only where a name is not unique.

    Names collide: ``cvlr-log`` exports both the ``cvlr_log!`` macro and the ``cvlr_log`` function
    it expands into, and ``cvlr-spec`` does the same four times over. The leaf is the primary key
    of the manual product — the importer's ``parts_unique`` constraint is over the header parts —
    so a collision is an ingest that fails at the database with a message about no item in
    particular, and before that an entry that silently shadows another.

    Qualified by kind first, which is the distinction in every real case and reads as English, and
    by the defining path only when even that is shared. Nothing is renamed that does not have to
    be: an agent reaches these sections by searching for a bare identifier, and a uniform
    ``name (kind)`` scheme would put a parenthesis in the middle of every header in the corpus.
    """
    by_name = Counter(i.name for i in items)
    kinded = [f"{i.name} ({i.kind.replace('_', ' ')})" for i in items]
    by_kind = Counter(kinded)
    leaves = [
        item.name if by_name[item.name] == 1
        else kind_leaf if by_kind[kind_leaf] == 1
        else item.path or kind_leaf
        for item, kind_leaf in zip(items, kinded)
    ]
    if duplicated := [n for n, count in Counter(leaves).items() if count > 1]:
        raise RustdocUnavailable(
            f"{items[0].crate if items else '?'}: {', '.join(duplicated)} name more than one item "
            f"each, and not even the defining path tells them apart. The manual product keys on "
            f"the header path, so these entries would overwrite each other."
        )
    return leaves


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
        for item, leaf in zip(items, _leaves(items)):
            headers = [API_ROOT, docs.name, docs.version, leaf]
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


def cvlr_packages(metadata: dict, root: pathlib.Path) -> dict[str, pathlib.Path]:
    """The CVLR packages one checkout defines, as ``name -> the package's own directory``.

    Taken from ``cargo metadata`` rather than by reading manifests, because a workspace member
    inherits its name's neighbours — the version, the edition — from the root table, and cargo is
    the thing that knows how to follow that.

    A checkout that defines none is refused rather than ignored. It is the one outcome a caller
    cannot tell from success: the build would finish, and describe the published releases.
    """
    found = {
        package["name"]: pathlib.Path(package["manifest_path"]).parent
        for package in metadata.get("packages", [])
        if package["name"].startswith(CVLR_PREFIX)
    }
    if not found:
        offered = ", ".join(sorted(p["name"] for p in metadata.get("packages", []))) or "none"
        raise RustdocUnavailable(
            f"{root} defines no CVLR crate, so documenting it would describe the published "
            f"releases while looking like it had used the checkout. It defines: {offered}"
        )
    return found


def _revision(root: pathlib.Path) -> str:
    """``git describe`` of a checkout, or a statement that there is none.

    The ``-dirty`` suffix is the point: the common case for this flag is documentation that has
    not been committed anywhere yet, and a corpus built from it has no other way to be identified.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "describe", "--always", "--dirty", "--tags"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else (
        "no git revision"
    )


def crate_sources(roots: Sequence[pathlib.Path]) -> dict[str, LocalCheckout]:
    """Every CVLR crate the given checkouts define, by cargo package name.

    Two checkouts claiming one crate is refused rather than resolved by order: which of the two a
    corpus describes is not a thing to pick quietly, and the entries would not say which won.
    """
    sources: dict[str, LocalCheckout] = {}
    for root in roots:
        resolved = root.expanduser().resolve()
        if not (resolved / "Cargo.toml").is_file():
            raise RustdocUnavailable(f"{resolved} has no Cargo.toml, so it is not a checkout.")
        result = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1"],
            cwd=resolved, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RustdocUnavailable(
                f"cargo metadata failed in {resolved}:\n{result.stderr.strip()}"
            )
        revision = _revision(resolved)
        for name, crate_dir in cvlr_packages(json.loads(result.stdout), resolved).items():
            if (prior := sources.get(name)) is not None:
                raise RustdocUnavailable(
                    f"both {prior.root} and {resolved} define {name}. Pass one of them: which "
                    f"copy the corpus would describe is not something this can choose for you."
                )
            sources[name] = LocalCheckout(root=resolved, crate_dir=crate_dir, revision=revision)
    return sources


def _probe_crate(
    root: pathlib.Path, reference: ChainReference, sources: Mapping[str, LocalCheckout]
) -> None:
    """A crate whose only purpose is to make the reference set resolvable.

    The dependency block comes from the reference set itself, so the corpus is built against
    exactly the releases a run pins — which is what makes a bump one change rather than two
    (``docs/cvlr-api-docs-plan.md`` §2).

    A checkout enters as ``[patch.crates-io]`` rather than as a dependency, which is what keeps
    those two questions apart: the pin still says which crates resolve and at what version, and
    the patch only redirects where their source is read from. A checkout whose version has moved
    off the pin therefore fails here, at cargo, rather than producing a corpus quietly describing
    a release this build does not support.
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.rs").write_text("")
    patches = "".join(
        f'{name} = {{ path = "{source.crate_dir}" }}\n' for name, source in sorted(sources.items())
    )
    (root / "Cargo.toml").write_text(
        "[package]\n"
        f'name = "{PROBE_PACKAGE}"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n\n'
        "[dependencies]\n" + reference.cargo_dependencies() + "\n"
        + (f"\n[patch.crates-io]\n{patches}" if patches else "")
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


def load(
    path: pathlib.Path, name: str, version: str, origin: Origin = Published()
) -> CrateDocs:
    return CrateDocs(
        name=name, version=version, payload=json.loads(path.read_text()), origin=origin
    )


def document_family(
    reference: ChainReference,
    workdir: pathlib.Path,
    *,
    sources: Mapping[str, LocalCheckout] = MappingProxyType({}),
) -> list[CrateDocs]:
    """Build the probe, run rustdoc over the whole family, and resolve re-exports across it.

    Returns the parsed crates rather than a manifest: the walk has two consumers now, and which
    one runs is a flag rather than a second pass over cargo.
    """
    if shutil.which("cargo") is None:
        raise RustdocUnavailable("cargo is not on PATH, so there is nothing to document.")
    _probe_crate(workdir, reference, sources)
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
        origin = sources.get(name, Published())
        _log.info("rustdoc: %s %s from %s", name, version, origin.describe())
        crates.append(load(_rustdoc(workdir, name), name, version, origin))
    resolve_aliases(crates)
    return crates


def manifest_source(reference: ChainReference, sources: Mapping[str, LocalCheckout]) -> str:
    """What the manifest records about where it came from.

    The only durable trace that a checkout was read: the rows deliberately carry none, so this is
    all that someone looking at an ingested corpus and asking why it disagrees with crates.io has
    to go on.
    """
    pinned = ", ".join(f"{c.name} {c.version}" for c in reference.crates())
    base = f"rustdoc over the CVLR family pinned at {pinned}"
    if not sources:
        return base
    by_checkout: dict[str, list[str]] = {}
    for name, source in sorted(sources.items()):
        by_checkout.setdefault(source.describe(), []).append(name)
    read = "; ".join(f"{where}: {', '.join(names)}" for where, names in by_checkout.items())
    return f"{base}, reading these from local checkouts rather than the published releases — {read}"


def undocumented_report(crates: list[CrateDocs]) -> str:
    """Every corpus item with no prose documentation, grouped by crate.

    The corpus can only carry what the crates carry. rustdoc gives existence, signature, defining
    crate and feature gate with no doc comment at all — which is most of what the live run's tool
    census asked for — but *what an item is for* has to be written by someone, and this is the
    list of what nobody has written yet (``docs/cvlr-api-docs-plan.md`` §4.2).

    Regenerate rather than edit: the point of the list is to shrink.
    """
    lines: list[str] = []
    total = undocumented = 0
    summary: list[tuple[str, int, int]] = []
    for docs in sorted(crates, key=lambda c: c.name):
        items = items_of(docs)
        missing = [i for i in items if not i.docs]
        total += len(items)
        undocumented += len(missing)
        summary.append((f"{docs.name} {docs.version}", len(missing), len(items)))
        if not missing:
            continue
        lines.append(f"\n### {docs.name} {docs.version} — {len(missing)} of {len(items)}\n")
        for item in missing:
            gate = f"  *(feature `{item.gate}`)*" if item.gate else ""
            lines.append(f"- `{item.name}` — {item.kind.replace('_', ' ')}{gate}")
            lines.append(f"  - `{item.signature.splitlines()[0]}`")
    header = [
        f"{undocumented} of {total} items carry no prose documentation.\n",
        "| crate | undocumented | items |",
        "| --- | --- | --- |",
    ]
    header += [f"| `{name}` | {miss} | {count} |" for name, miss, count in summary]
    checkouts = sorted(
        {c.origin.describe() for c in crates if isinstance(c.origin, LocalCheckout)}
    )
    if checkouts:
        # Without this the list reads as a measurement of the published crates, and the whole
        # reason to run it over a checkout is to watch it shrink before the crates are published.
        header += [
            "",
            "Read from local checkouts rather than the published releases: "
            + "; ".join(checkouts)
            + ".",
        ]
    return "\n".join(header + lines) + "\n"


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
    parser.add_argument(
        "--crate-source", type=pathlib.Path, action="append", default=[], metavar="PATH",
        help="A local CVLR checkout to document instead of the published crates. Repeatable, and "
             "it may be a workspace root or a single crate directory. Every CVLR package the "
             "checkout defines replaces the crates.io release, so the pin still decides which "
             "crates the corpus holds and the checkout decides what is in them. The entries it "
             "emits are identical to the ones the same crates produce once published; the "
             "manifest's source line is what records that a checkout was read.",
    )
    parser.add_argument(
        "--undocumented", action="store_true",
        help="Instead of a manifest, write the list of items that carry no prose documentation. "
             "That list is the ask against the CVLR crates, and it is meant to shrink.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    reference = reference_for(args.chain)
    try:
        sources = crate_sources(args.crate_source)
        if args.keep is not None:
            args.keep.mkdir(parents=True, exist_ok=True)
            crates = document_family(reference, args.keep, sources=sources)
        else:
            with tempfile.TemporaryDirectory(prefix="cvlr-api-docs-") as tmp:
                crates = document_family(reference, pathlib.Path(tmp), sources=sources)
    except RustdocUnavailable as exc:
        raise SystemExit(f"cvlr_api_docs: {exc}") from exc

    if args.undocumented:
        args.output.write_text(undocumented_report(crates))
        _log.info("wrote %s", args.output)
        return
    manifest = build_manifest(crates, source=manifest_source(reference, sources))
    args.output.write_text(manifest.model_dump_json(indent=1))
    _log.info(
        "wrote %s: %d sections, %d embedded groups",
        args.output, len(manifest.manual_sections), len(manifest.embedded_groups),
    )


if __name__ == "__main__":
    sys.exit(main())
