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
  ``paths`` — it lives inside an ``impl``. :meth:`CrateDocs.impl_members` is why inherent impls
  are walked.
* **The facade re-exports almost everything.** ``clog`` resolves to ``cvlr_log::cvlr_log``, and an
  agent handed only the facade finds a re-export and stops. :func:`resolve_aliases` inverts the
  ``use`` items so each entry names the crate that *defines* it and lists the paths it is
  reachable by.
* **Trait-impl methods are mechanical.** Twenty ``Add``/``Sub``/``Mul`` impls make twenty entries
  that answer a question nobody asks while burying the ones people do. Their methods are not
  emitted; the type's entry names the traits it implements instead.

**Every example that claims to compile has compiled.** The doc comments' code blocks reach the
corpus inside an item's prose, and an author copies them. :func:`gate_examples` type-checks every
block rustdoc would build — ``cargo check``, host target, offline, in one invocation — against the
whole pinned family, which is what a doctest of any one crate can name. A block that fails is
dropped from the corpus and named in the log and in the ``--undocumented`` report; one that
rustdoc would not build (``ignore``, ``text``, ``compile_fail``) is kept under a label saying it
was not compiled, so nothing in the corpus reads as checked that was not. Upstream examples may
lean on doctest context this does not reproduce, which is why a failure drops the block rather than
the build; the build fails only when the check itself cannot run.

**Documenting a working copy.** ``--crate-source`` points the probe at a local CVLR checkout:
every CVLR package that checkout defines replaces the crates.io release through
``[patch.crates-io]``, so the pin still decides *which* crates the corpus holds and the checkout
decides what is in them. That is how doc comments get read before they are published.

The rows it emits are what the same crates emit once released: a checkout corpus exists to be
tried by an author agent, so it carries no caveat the released one would not. The manifest's
``source`` line and the log record where the build came from.

Run it through ``scripts/populate_cvlr_rag.sh``, or directly::

    uv run --no-sync python -m composer.scripts.cvlr_api_docs --chain solana \
        --output cvlr_api_kb.rag.json

rustdoc runs under :data:`PINNED_NIGHTLY` unless ``--toolchain`` names another, since
``--output-format json`` is nightly-only and :mod:`composer.cargo.rustdoc` reads one schema. It needs a
populated cargo registry and passes ``--offline``, so a warm cache needs no network. The example
check runs under the default toolchain.
"""

import argparse
import asyncio
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from pydantic import JsonValue

from composer.cargo import rustdoc

from composer.cargo.metadata import CratePackage, Workspace
from composer.cargo.doctest import (
    Failed,
    FenceClass,
    GateUnavailable,
    Verdict,
    check_examples,
    fences,
    reveal_hidden_lines,
)
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
from composer.spec.cvlr.reference import ChainReference, GitCommit, generations
from composer.rag.cvlr_api import SURFACE_HEADING, ApiPath

_log = logging.getLogger(__name__)

#: A dated nightly whose rustdoc emits :data:`composer.cargo.rustdoc.FORMAT_VERSION`, and the
#: toolchain rustdoc runs under unless the caller names another. Moving one means moving the other.
PINNED_NIGHTLY = "nightly-2026-07-06"

#: The probe crate's own name. The family is the CVLR packages the probe resolves, which excludes the
#: probe itself as a workspace member (:func:`_family_packages`).
PROBE_PACKAGE = "cvlr-api-probe"
#: The crate the examples are compiled in. Kept apart from the probe so the probe's dependency
#: block stays exactly the reference set.
EXAMPLES_PACKAGE = "cvlr-api-examples"


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
    #: The crate's version in the checkout, as cargo reports it. A generation that pins the crate
    #: at another version does not take this checkout (:func:`sources_for`).
    version: str
    #: ``git describe`` of :attr:`root`, ``-dirty`` included. The only thing that distinguishes
    #: one build of a checkout from another, and uncommitted edits are the case that matters.
    revision: str

    def describe(self) -> str:
        return f"{self.root} @ {self.revision}"


@dataclass(frozen=True)
class ReleaseCommit:
    """A release the reference set reads from the commit it was cut from, because crates.io does
    not have it yet (:class:`~composer.spec.cvlr.reference.GitCommit`)."""

    commit: GitCommit

    def describe(self) -> str:
        return f"{self.commit.repo} @ {self.commit.rev[:12]}"


#: Where one crate's documentation came from. A corpus mixes them: a caller who points at the
#: ``cvlr`` checkout still gets ``cvlr-solana`` from wherever the reference set reads it. It
#: reaches the manifest's source line, the undocumented report and the log, and deliberately not
#: the rows.
type Origin = Published | LocalCheckout | ReleaseCommit


class ItemKind(StrEnum):
    """What a corpus entry is. The three procedural-macro kinds are told apart because each is
    written differently: ``thing!()``, ``#[rule]``, ``#[derive(Nondet)]``."""

    FUNCTION = "function"
    MACRO = "macro"
    PROC_MACRO = "proc_macro"
    PROC_ATTRIBUTE = "proc_attribute"
    PROC_DERIVE = "proc_derive"
    STRUCT = "struct"
    ENUM = "enum"
    UNION = "union"
    TRAIT = "trait"
    TRAIT_ALIAS = "trait_alias"
    TYPE_ALIAS = "type_alias"
    CONSTANT = "constant"
    STATIC = "static"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


@dataclass(frozen=True)
class FeatureGate:
    """``#[cfg(feature = "...")]``: one cargo feature, and nothing else, decides it."""

    feature: str

    def describe(self) -> str:
        return f"Behind the `{self.feature}` cargo feature; without it this item is absent."

    def label(self) -> str:
        return f"feature `{self.feature}`"


@dataclass(frozen=True)
class CfgGate:
    """Any other ``cfg`` predicate, given as written: ``any(...)``, ``all(...)``, a target."""

    predicate: str

    def describe(self) -> str:
        return f"Present only when `cfg({self.predicate})` holds."

    def label(self) -> str:
        return f"`cfg({self.predicate})`"


type Gate = FeatureGate | CfgGate


@dataclass(frozen=True)
class Item:
    """One entry in the corpus: what a caller needs in order to write the call."""

    #: The name a caller writes, ``cvlr_assert_le`` or ``NativeIntU64::u64_max``.
    name: str
    kind: ItemKind
    #: The crate that *defines* it, in cargo's spelling.
    crate: str
    #: The shallowest path that compiles, ``cvlr_asserts::cvlr_assert_le``. ``None`` for an
    #: associated item, whose type's path is the addressable one.
    path: str | None
    signature: str
    docs: str | None = None
    #: The ``cfg`` this item is compiled under, when rustdoc records one. A helper that exists but
    #: is gated is a compile error that reads like a missing item.
    gate: Gate | None = None
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
            ("text", f"`{self.name}` — {self.kind.label}, defined in `{self.crate}`.")
        ]
        out.append(("code", self.signature))
        if self.docs:
            out.append(("text", self.docs))
        if self.path is not None:
            out.append(("text", f"Path: `{self.path}`"))
        if self.aliases:
            reachable = ", ".join(f"`{a}`" for a in self.aliases)
            out.append(("text", f"Also reachable as {reachable}."))
        if self.traits:
            out.append(("text", f"Implements: {', '.join(f'`{t}`' for t in self.traits)}."))
        if self.gate is not None:
            out.append(("text", self.gate.describe()))
        if self.deprecated:
            out.append(("text", f"Deprecated: {self.deprecated}"))
        return out


@dataclass
class CrateDocs:
    """One crate's rustdoc, and the walk over it."""

    name: str
    version: str
    crate: rustdoc.Crate
    #: Facade paths pointing into this crate, by the defining path they resolve to. Filled by
    #: :func:`resolve_aliases` once every crate has been loaded, since a re-export is a fact about
    #: two crates.
    aliases: dict[str, list[str]] = field(default_factory=dict)

    @property
    def paths(self) -> dict[rustdoc.Id, rustdoc.ItemSummary]:
        return self.crate.paths

    @property
    def local_crate_id(self) -> int:
        return self.crate.index[self.crate.root].crate_id

    def own_items(self) -> Iterator[tuple[rustdoc.Id, rustdoc.Item]]:
        """This crate's items. A payload carries every dependency's items too, and attributing
        those here would file the same item under several crates."""
        local = self.local_crate_id
        return ((i, item) for i, item in self.crate.index.items() if item.crate_id == local)

    def own_summaries(self) -> Iterator[tuple[rustdoc.Id, rustdoc.ItemSummary]]:
        local = self.local_crate_id
        return ((i, s) for i, s in self.paths.items() if s.crate_id == local and s.path)

    def items(self) -> list[Item]:
        """Every entry this crate contributes, in name order."""
        traits, members = self.impl_members()
        proc_macros = self.proc_macro_paths()
        reachable = self.public_paths()
        _, module_public = self.modules()
        found: list[Item] = []

        for item_id, item in self.own_items():
            if item.name is None:
                continue
            documented = _kind_and_signature(item.name, item.inner)
            if documented is None:
                continue
            kind, signature = documented
            summary = self.paths.get(item_id) or proc_macros.get(item.name)
            if summary is None:
                # Not path-addressable: an associated item, reached through its type below.
                continue
            # `declared` is where rustdoc says the item is defined, the key `resolve_aliases`
            # files facade re-exports under. `path` is the shallowest path that compiles, which
            # under the CVLR house style (`mod layout;` + `pub use layout::{...}`) is a re-export.
            # It prefers the path that spells the item's own name: `cvlr-solana` re-exports
            # `cvlr_deserialize_nondet_accounts` twice, once under a shorter rename.
            declared = "::".join(summary.path)
            writable = reachable.get(item_id) or []
            path = next((p for p in writable if p.rsplit("::", 1)[-1] == item.name), None)
            if path is None and writable:
                path = writable[0]
            if path is None and _reachable(declared, module_public):
                # A procedural macro's export is filed under an id of its own, so `public_paths`
                # has nothing under the index item's id. Its declaring module is the crate root.
                path = declared
            found.append(
                Item(
                    name=item.name,
                    kind=kind,
                    crate=self.name,
                    path=path,
                    signature=signature,
                    docs=item.docs,
                    gate=gate_of(item.attrs),
                    deprecated=_deprecation_of(item.deprecation),
                    aliases=tuple(sorted(self.aliases.get(declared, ()))),
                    traits=tuple(sorted(set(traits.get(item.name, ())))),
                )
            )

        for type_name, type_members in members.items():
            for member_name, member in type_members:
                documented = _kind_and_signature(member_name, member.inner)
                if documented is None:
                    continue
                kind, signature = documented
                found.append(
                    Item(
                        name=f"{type_name}::{member_name}",
                        kind=kind,
                        crate=self.name,
                        path=None,
                        signature=signature,
                        docs=member.docs,
                        gate=gate_of(member.attrs),
                        deprecated=_deprecation_of(member.deprecation),
                    )
                )

        return sorted(found, key=lambda i: i.name)

    def impl_members(
        self,
    ) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, rustdoc.Item]]]]:
        """``(traits per type, inherent members per type)``.

        Trait impls contribute only their trait's name to the type they are for: twenty ``Add``
        impls are twenty entries nobody searches for. Inherent impls contribute their members,
        because that is where the methods a caller actually writes live and nothing else
        addresses them.
        """
        traits: dict[str, list[str]] = {}
        members: dict[str, list[tuple[str, rustdoc.Item]]] = {}
        for _, item in self.own_items():
            if not isinstance(item.inner, rustdoc.ImplInner):
                continue
            block = item.inner.impl
            for_type = render_type(block.for_)
            if block.trait_ is not None:
                traits.setdefault(for_type, []).append(_render_trait(block.trait_))
                continue
            for member_id in block.items:
                member = self.crate.index.get(member_id)
                if member is None or not member.name:
                    continue
                members.setdefault(for_type, []).append((member.name, member))
        return traits, members

    def proc_macro_paths(self) -> dict[str, rustdoc.ItemSummary]:
        """``name -> paths entry`` for this crate's procedural macros.

        rustdoc files a proc macro's export under an id of its own, unrelated to the id of the
        item in ``index``, so the usual ``paths[item_id]`` lookup finds nothing and would read it
        as an associated item. Without this, ``cvlr-derive``, whose whole surface is
        ``#[derive(Nondet)]`` and ``#[derive(CvlrLog)]``, would have no entries.

        Name is a safe key only because these kinds are crate-level by construction: a procedural
        macro cannot be an associated item, so there is nothing for it to be confused with.
        """
        return {
            summary.path[-1]: summary
            for _, summary in self.own_summaries()
            if summary.kind in ("proc_macro", "proc_attribute", "proc_derive")
        }

    def modules(self) -> tuple[dict[rustdoc.Id, str], dict[str, bool]]:
        """``(module path per item id, is-public per module path)`` for this crate's own modules.

        Both halves answer the same question from different directions: which module an item was
        *declared* in, and whether that module can be named from outside the crate.
        """
        owner: dict[rustdoc.Id, str] = {}
        public: dict[str, bool] = {}
        for item_id, item in self.own_items():
            if not isinstance(item.inner, rustdoc.ModuleInner):
                continue
            summary = self.paths.get(item_id)
            if summary is None:
                continue
            path = "::".join(summary.path)
            public[path] = item.is_public or len(summary.path) == 1
            for child in item.inner.module.items:
                owner[child] = path
        return owner, public

    def public_paths(self) -> dict[rustdoc.Id, list[str]]:
        """Every path each of this crate's items can be written as, by item id, shallowest first.

        The definition path when it is reachable, plus every intra-crate ``pub use`` that
        re-exports the item from a module that is itself reachable. :func:`resolve_aliases` does
        this across crates and deliberately not within one — a facade alias is a fact about two
        crates — so this is the same inversion applied to the crate's own re-exports, which is
        where the paths that actually compile live.

        **Globs are expanded here and skipped there**, and the difference is knowledge rather than
        taste. Across crates a glob names no item this payload can enumerate, so reporting a path
        through one would be a guess. Within a crate the module's own contents are right here, so
        ``pub use log::*`` at the root *derives* ``cvlr_solana::<name>`` for every public item
        ``log`` declares. That matters because the glob is the house style: ``cvlr-log``
        re-exports its whole surface with two of them, and without this every ``log_*`` function
        has no writable path.
        """
        owner, public = self.modules()
        root = self.name.replace("-", "_")
        found: dict[rustdoc.Id, list[str]] = {}
        for item_id, summary in self.own_summaries():
            path = "::".join(summary.path)
            if _reachable(path, public):
                found.setdefault(item_id, []).append(path)

        for item_id, item in self.own_items():
            if not isinstance(item.inner, rustdoc.UseInner):
                continue
            use = item.inner.use
            target = use.id
            if target is None or (not use.name and not use.is_glob):
                continue
            if target not in self.crate.index and target not in self.paths:
                continue
            # A `use` is declared inside some module; the path it creates hangs off that module.
            module = owner.get(item_id, root)
            if not public.get(module, module == root) or not _reachable(f"{module}::x", public):
                continue
            for target_id, exported in self._exports(target, use):
                path = f"{module}::{exported}"
                if path not in found.get(target_id, []):
                    found.setdefault(target_id, []).append(path)

        return {k: sorted(v, key=lambda p: (p.count("::"), len(p))) for k, v in found.items()}

    def _exports(self, target: rustdoc.Id, use: rustdoc.Use) -> list[tuple[rustdoc.Id, str]]:
        """``(item id, exported name)`` for what one ``use`` brings into its module.

        A plain ``use`` exports one item under its name, which is the rename when there is one. A
        glob exports every public item of the target module under its own name.
        """
        if not use.is_glob:
            return [(target, use.name)]
        module = self.crate.index.get(target)
        if module is None or not isinstance(module.inner, rustdoc.ModuleInner):
            return []
        out: list[tuple[rustdoc.Id, str]] = []
        for child_id in module.inner.module.items:
            child = self.crate.index.get(child_id)
            if child is not None and child.name and child.is_public:
                out.append((child_id, child.name))
        return out


# ---------------------------------------------------------------------------------------------
# rendering types
#
# rustdoc gives a structured type tree rather than source text. Rendering it back is a bounded
# job for the shapes CVLR uses, and every unhandled shape falls through to a marker rather than
# to a plausible-looking wrong type.


def render_type(node: rustdoc.TypeNode) -> str:
    """A Rust spelling of one rustdoc type node.

    Falls back to ``_`` for a shape this does not model. A wrong type is worse than an elided one:
    the caller is about to write it into Rust that has to compile.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict) or len(node) != 1:
        return "_"
    (kind, body), = node.items()
    if not isinstance(body, dict):
        match kind, body:
            case ("primitive" | "generic", str()):
                return body
            case ("slice", _):
                return f"[{render_type(body)}]"
            case ("tuple", list()):
                return f"({', '.join(render_type(t) for t in body)})" if body else "()"
            case ("impl_trait", list()):
                return f"impl {' + '.join(_render_bound(b) for b in body)}"
            case _:
                return "_"
    match kind:
        case "resolved_path":
            return f"{body.get('path', '_')}{_render_args(body.get('args'))}"
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
        case "dyn_trait":
            traits = body.get("traits")
            bounds = [
                render_type({"resolved_path": t.get("trait")})
                for t in (traits if isinstance(traits, list) else [])
                if isinstance(t, dict)
            ]
            return f"dyn {' + '.join(bounds)}"
        case "qualified_path":
            return f"<{render_type(body.get('self_type'))}>::{body.get('name')}"
        case "function_pointer":
            sig = body.get("sig")
            sig = sig if isinstance(sig, dict) else {}
            inputs = sig.get("inputs")
            args = ", ".join(
                render_type(t[1])
                for t in (inputs if isinstance(inputs, list) else [])
                if isinstance(t, list) and len(t) == 2
            )
            out = sig.get("output")
            return f"fn({args})" + (f" -> {render_type(out)}" if out else "")
        case _:
            return "_"


def _render_args(args: rustdoc.TypeNode) -> str:
    if not isinstance(args, dict):
        return ""
    if isinstance(angle := args.get("angle_bracketed"), dict):
        listed = angle.get("args")
        rendered = [_render_generic_arg(a) for a in (listed if isinstance(listed, list) else [])]
        shown = [r for r in rendered if r]
        return f"<{', '.join(shown)}>" if shown else ""
    if isinstance(paren := args.get("parenthesized"), dict):
        listed = paren.get("inputs")
        inputs = ", ".join(render_type(t) for t in (listed if isinstance(listed, list) else []))
        out = paren.get("output")
        return f"({inputs})" + (f" -> {render_type(out)}" if out else "")
    return ""


def _render_generic_arg(arg: rustdoc.TypeNode) -> str:
    if not isinstance(arg, dict):
        return ""
    if (life := arg.get("lifetime")) is not None:
        return str(life)
    if (typ := arg.get("type")) is not None:
        return render_type(typ)
    if isinstance(const := arg.get("const"), dict):
        return str(const.get("expr", ""))
    return ""


def _render_bound(bound: rustdoc.TypeNode) -> str:
    if isinstance(bound, dict) and (tb := bound.get("trait_bound")) is not None:
        trait = tb.get("trait") if isinstance(tb, dict) else None
        return render_type({"resolved_path": trait})
    if isinstance(bound, dict) and (life := bound.get("outlives")) is not None:
        return str(life)
    return "_"


def _render_trait(trait: rustdoc.TraitPath) -> str:
    return f"{trait.path}{_render_args(trait.args)}"


def _render_generics(generics: rustdoc.Generics) -> str:
    """The ``<T, 'a>`` clause, lifetimes included. Where-clauses are elided: they constrain, and
    the caller needs the shape of the call rather than the full bound set."""
    names = [p.name for p in generics.params if p.name != "Self"]
    return f"<{', '.join(names)}>" if names else ""


def _function_signature(name: str, function: rustdoc.Function) -> str:
    header = function.header
    prefix = ("const " if header.is_const else "") + ("unsafe " if header.is_unsafe else "")
    args = ", ".join(
        arg_name if arg_name == "self" and typ == {"generic": "Self"} else
        f"{arg_name}: {render_type(typ)}"
        for arg_name, typ in function.sig.inputs
    )
    out = function.sig.output
    ret = f" -> {render_type(out)}" if out is not None else ""
    return f"{prefix}fn {name}{_render_generics(function.generics)}({args}){ret}"


def _kind_and_signature(name: str, inner: rustdoc.Inner) -> tuple[ItemKind, str] | None:
    """The entry an item makes, or ``None`` for a kind that is structure rather than surface.

    ``impl`` and ``use`` are walked for what they say about other items, and modules, fields,
    variants and associated items are parts of an item already emitted. A ``macro``'s declaration
    is passed through verbatim: rustdoc hands over the matchers with the bodies elided, which is the
    call signature and none of the expansion.
    """
    match inner:
        case rustdoc.MacroInner(macro=declaration):
            return ItemKind.MACRO, declaration
        case rustdoc.ProcMacroInner(proc_macro=rustdoc.ProcMacro(kind="derive")):
            return ItemKind.PROC_DERIVE, f"#[derive({name})]"
        case rustdoc.ProcMacroInner(proc_macro=rustdoc.ProcMacro(kind="attr")):
            return ItemKind.PROC_ATTRIBUTE, f"#[{name}]"
        case rustdoc.ProcMacroInner(proc_macro=rustdoc.ProcMacro(kind="bang")):
            return ItemKind.PROC_MACRO, f"{name}!"
        case rustdoc.FunctionInner(function=function):
            return ItemKind.FUNCTION, _function_signature(name, function)
        case rustdoc.StructInner(struct=d):
            return ItemKind.STRUCT, f"struct {name}{_render_generics(d.generics)}"
        case rustdoc.UnionInner(union=d):
            return ItemKind.UNION, f"union {name}{_render_generics(d.generics)}"
        case rustdoc.EnumInner(enum=d):
            return ItemKind.ENUM, f"enum {name}{_render_generics(d.generics)}"
        case rustdoc.TraitInner(trait_=d):
            return ItemKind.TRAIT, f"trait {name}{_render_generics(d.generics)}"
        case rustdoc.TraitAliasInner(trait_alias=d):
            return ItemKind.TRAIT_ALIAS, f"trait {name}{_render_generics(d.generics)}"
        case rustdoc.TypeAliasInner(type_alias=alias):
            return ItemKind.TYPE_ALIAS, f"type {name} = {render_type(alias.type_)}"
        case rustdoc.ConstantInner(constant=value):
            return ItemKind.CONSTANT, f"const {name}: {render_type(value.type_)}"
        case rustdoc.StaticInner(static=value):
            return ItemKind.STATIC, f"static {name}: {render_type(value.type_)}"
        case _:
            return None


# ---------------------------------------------------------------------------------------------
# what an item's attributes say


#: rustdoc hands an attribute over as its source text. ``doc(cfg(...))`` is how a crate tells
#: rustdoc about a gate it documents with every feature on.
_CFG_ATTRS = (
    re.compile(r"#\[cfg\((?P<predicate>.*)\)\]", re.DOTALL),
    re.compile(r"#\[doc\(cfg\((?P<predicate>.*)\)\)\]", re.DOTALL),
)
_SINGLE_FEATURE = re.compile(r'feature\s*=\s*"(?P<feature>[^"]+)"')


def gate_of(attrs: Sequence[str | dict[str, JsonValue]]) -> Gate | None:
    """The ``cfg`` an item is compiled under, from its ``cfg`` or ``doc(cfg)`` attributes.

    A predicate that is exactly one feature is a :class:`FeatureGate`; anything else is reported
    whole, since naming one feature out of ``any(...)`` or ``all(...)`` would misstate it.
    """
    predicates = [
        match["predicate"].strip()
        for attr in attrs
        for pattern in _CFG_ATTRS
        if (match := pattern.fullmatch(_attr_text(attr).strip())) is not None
    ]
    unique = list(dict.fromkeys(predicates))
    if not unique:
        return None
    if len(unique) == 1 and (feature := _SINGLE_FEATURE.fullmatch(unique[0])) is not None:
        return FeatureGate(feature["feature"])
    return CfgGate(unique[0] if len(unique) == 1 else f"all({', '.join(unique)})")


def _attr_text(attr: str | dict[str, JsonValue]) -> str:
    """An attribute's source text: a bare string, or the ``other`` arm of the attribute enum."""
    if isinstance(attr, str):
        return attr
    other = attr.get("other")
    return other if isinstance(other, str) else ""


def _deprecation_of(deprecation: rustdoc.Deprecation | None) -> str | None:
    if deprecation is None:
        return None
    note = deprecation.note or "no reason given"
    since = f" (since {deprecation.since})" if deprecation.since else ""
    return f"{note}{since}"


# ---------------------------------------------------------------------------------------------
# paths


def _reachable(path: str, public: dict[str, bool]) -> bool:
    """Can ``path`` be written from outside the crate?

    Every module between the crate root and the item has to be public. rustdoc gives the
    *definition* path, and a crate that defines in ``mod layout;`` and re-exports with
    ``pub use layout::{...}`` has a definition path that does not compile, which is the CVLR house
    style. The crate root itself is always reachable.
    """
    parts = path.split("::")
    return all(public.get("::".join(parts[: i + 1]), False) for i in range(1, len(parts) - 1))


@dataclass(frozen=True)
class DocumentedCrate:
    """One crate as the corpus describes it: the walk's output, and everything after the walk
    reads this rather than rustdoc."""

    name: str
    version: str
    items: tuple[Item, ...]
    origin: Origin = Published()


def resolve_aliases(crates: list[CrateDocs]) -> None:
    """Fill each crate's :attr:`CrateDocs.aliases` from every other crate's ``use`` items.

    This is the re-export trap recorded rather than taught. ``cvlr`` re-exports almost its whole
    surface, so ``clog`` is reached as ``cvlr::clog`` and *defined* as ``cvlr_log::cvlr_log``; an
    entry that named only one of the two would send a caller to a path that does not resolve or to
    a crate they cannot search.

    **The alias is usually the only path the caller can write.** The scaffold declares the core,
    the chain crate and its models, so the rest of the family is transitive, and
    ``use cvlr_log::log_u64`` in a scaffolded project is ``E0433: use of undeclared crate``.

    The facade reaches those crates with whole-crate globs, one public module each::

        pub mod asserts { pub use cvlr_asserts::*; }

    A glob is expanded because :func:`document_family` loads the whole family, so the target
    crate's own contents are here to enumerate.
    """
    #: rustdoc spells a crate name with underscores; cargo spells it with hyphens.
    by_module = {c.name.replace("-", "_"): c for c in crates}
    #: Which module each ``use`` is declared in, per crate: a glob's alias hangs off that module,
    #: and the facade puts every one of them in a module of its own.
    facade_modules = {c.name: c.modules()[0] for c in crates}
    #: Computed once per crate: a glob asks the same question of the whole family.
    writable = {c.name: c.public_paths() for c in crates}
    for facade in crates:
        for item_id, item in facade.own_items():
            if not isinstance(item.inner, rustdoc.UseInner) or item.inner.use.id is None:
                continue
            use = item.inner.use
            target = facade.paths.get(item.inner.use.id)
            if target is None:
                continue
            owner = facade.crate.external_crates.get(target.crate_id)
            defining = by_module.get(owner.name) if owner is not None else None
            if defining is None or defining.name == facade.name:
                continue
            if use.is_glob:
                module = facade_modules[facade.name].get(item_id)
                if module is None:
                    continue
                contents = _glob_contents(
                    defining, "::".join(target.path), writable[defining.name]
                )
                for declared, name in contents:
                    defining.aliases.setdefault(declared, []).append(f"{module}::{name}")
                continue
            alias_path = _alias_path(facade, item_id, use.name)
            if alias_path is None:
                continue
            defining.aliases.setdefault("::".join(target.path), []).append(alias_path)
    for crate in crates:
        for target_path, paths in crate.aliases.items():
            crate.aliases[target_path] = sorted(set(paths))


def _glob_contents(
    defining: CrateDocs, module_path: str, writable: dict[rustdoc.Id, list[str]]
) -> list[tuple[str, str]]:
    """``(defining path, name)`` for every item a whole-module glob brings in.

    Built from the defining crate's own writable paths rather than from the module's direct
    children, because the two globs compose: ``cvlr-log`` declares in ``mod core`` and re-exports
    with ``pub use crate::core::*``, and ``cvlr`` then re-exports the crate with
    ``pub mod log { pub use cvlr_log::*; }``. An item's name at ``module_path`` is what the outer
    glob carries, whichever module inside the crate declared it.
    """
    out: list[tuple[str, str]] = []
    for item_id, paths in writable.items():
        summary = defining.paths.get(item_id)
        if summary is None or summary.crate_id != defining.local_crate_id:
            continue
        declared = "::".join(summary.path)
        for path in paths:
            # Anything *under* the target, not only its direct children: a glob re-exports the
            # public submodules too, so `pub use cvlr_nondet::*` makes `havoc` reachable and
            # `cvlr::nondet::havoc::memhavoc` with it.
            if path.startswith(f"{module_path}::"):
                out.append((declared, path[len(module_path) + 2 :]))
                break
    return out


def _alias_path(facade: CrateDocs, item_id: rustdoc.Id, name: str) -> str | None:
    """The path a re-export is reachable by, from the facade's own ``paths`` where it has one.

    A ``use`` that rustdoc gives no path of its own is reported at the crate root, which is where
    a ``pub use`` in ``lib.rs`` puts it and is the spelling a caller will try first.
    """
    summary = facade.paths.get(item_id)
    if summary is not None:
        return "::".join(summary.path)
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
    kinded = [f"{i.name} ({i.kind.label})" for i in items]
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


def _surface_section(docs: DocumentedCrate) -> ManualSection:
    """The closed-world listing: every item this crate exports, in one block.

    This is the only row that supports "does X exist?". A vector search always returns its nearest
    rows, so an empty result cannot tell absence from an oddly phrased query; a caller who reads
    this list can say a name is not in it.
    """
    items = docs.items
    lines = [f"`{i.name}` — {i.kind.label}" for i in items]
    listing = "\n".join(f"- {line}" for line in lines) or "- (this crate exports nothing)"
    return ManualSection(
        headers=ApiPath(docs.name, docs.version, SURFACE_HEADING).headers(),
        blocks=[
            ManualBlock(
                kind=ManualBlockKind.TEXT,
                body=(
                    f"Everything `{docs.name}` {docs.version} exports, in full — "
                    f"{len(items)} items. This list is closed: a name that is not on it is not in "
                    f"`{docs.name}` {docs.version}.\n\n{listing}"
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------------------------
# the examples


def _compiled_source(body: str) -> str:
    """What a block is compiled as, and so what the corpus shows once it has compiled.

    Hidden lines are shown rather than stripped: they are the setup the example needs, and an
    example the reader can copy whole is the one the check actually vouches for.
    """
    return reveal_hidden_lines(body)


def gated_sources(crates: Sequence[DocumentedCrate]) -> dict[str, list[str]]:
    """Every distinct example rustdoc would build, by compiled source, with the items carrying it.

    Keyed by source rather than by item because the family repeats itself — ``cvlr_lemma!`` and
    ``CvlrLemma`` share an example — and the same source compiles the same way wherever it is.
    """
    found: dict[str, list[str]] = {}
    for docs in sorted(crates, key=lambda c: c.name):
        for item in docs.items:
            for fence in fences(item.docs or ""):
                if fence.kind is FenceClass.COMPILED:
                    found.setdefault(_compiled_source(fence.body), []).append(
                        f"{docs.name}::{item.name}"
                    )
    return found


#: Put ahead of a block that was not compiled, so a reader cannot take it for checked Rust.
_NOT_COMPILED = (
    "_Not compiled (marked `{info}` upstream); read it as a sketch, not as checked Rust:_"
)
_COMPILED = "_Compiled against the pinned crates:_"
_DROPPED = "_An example here did not compile against the pinned crates and has been removed._"


@dataclass(frozen=True)
class ExampleGate:
    """The verdict on every compiled example, and how an item's prose is rewritten from them."""

    #: By compiled source (:func:`gated_sources`'s keys).
    verdicts: Mapping[str, Verdict]
    #: The items each source appears under, for the report. Empty where nobody needs it.
    owners: Mapping[str, Sequence[str]] = MappingProxyType({})

    def render(self, docs: str) -> str:
        """``docs`` with each code block labelled by what was done with it.

        A block that compiled is shown as compiled, hidden lines included; one that failed is
        replaced by a line saying so, which keeps the prose around it from pointing at nothing; one
        rustdoc would not build keeps its own fence under a label saying it was not compiled.
        """
        out: list[str] = []
        cursor = 0
        for fence in fences(docs):
            out.append(docs[cursor : fence.start])
            cursor = fence.end
            trailing = "\n" if docs[fence.start : fence.end].endswith("\n") else ""
            match fence.kind:
                case FenceClass.COMPILED:
                    source = _compiled_source(fence.body)
                    verdict = self.verdicts.get(source)
                    if verdict is None:
                        raise GateUnavailable(
                            f"an example reached the corpus without being compiled:\n{source}"
                        )
                    if isinstance(verdict, Failed):
                        out.append(_DROPPED + trailing)
                    else:
                        out.append(f"{_COMPILED}\n```rust\n{source}```{trailing}")
                case FenceClass.COMPILE_FAIL | FenceClass.IGNORED | FenceClass.NOT_RUST:
                    label = _NOT_COMPILED.format(info=fence.info)
                    out.append(f"{label}\n```{fence.info}\n{fence.body}```{trailing}")
        out.append(docs[cursor:])
        return "".join(out)

    @staticmethod
    def combined(gates: Sequence["ExampleGate"]) -> "ExampleGate":
        """One gate over several generations' compiles.

        A crate the generations share carries its examples into each, and the corpus holds it
        once, so an example is kept only if it compiled in every generation that checked it.
        """
        verdicts: dict[str, Verdict] = {}
        owners: dict[str, list[str]] = {}
        for gate in gates:
            for source, verdict in gate.verdicts.items():
                if not isinstance(verdicts.get(source), Failed):
                    verdicts[source] = verdict
            for source, items in gate.owners.items():
                seen = owners.setdefault(source, [])
                seen += [i for i in items if i not in seen]
        return ExampleGate(verdicts=verdicts, owners=owners)

    def failures(self) -> list[tuple[str, Failed]]:
        return [(s, v) for s, v in self.verdicts.items() if isinstance(v, Failed)]

    def summary(self) -> str:
        failed = len(self.failures())
        return (
            f"examples: {len(self.verdicts)} compiled, {len(self.verdicts) - failed} passed, "
            f"{failed} dropped"
        )

    def failure_report(self) -> list[str]:
        """Markdown lines naming each dropped example, where it is, and rustc's first complaint."""
        lines: list[str] = []
        for source, verdict in self.failures():
            where = ", ".join(f"`{o}`" for o in self.owners.get(source, ())) or "(unknown item)"
            first = next((ln for ln in verdict.diagnostics.splitlines() if ln.strip()), "")
            lines.append(f"- {where}: `{first.strip()}`")
        return lines


def _examples_crate(
    root: pathlib.Path,
    reference: ChainReference,
    family: Sequence[tuple[str, str]],
    sources: Mapping[str, LocalCheckout],
) -> None:
    """The crate the examples compile in: the reference set plus every family crate, direct.

    A doctest can name its own crate whichever it is, so an example in ``cvlr-macros`` may write
    ``use cvlr_macros::...``. Declaring the whole family at the versions the probe resolved is
    that context for every crate at once. It is wider than a scaffolded project's, which declares
    only the reference set and reaches the rest through the facade.
    """
    pinned = {c.name for c in reference.crates()}
    extra = "".join(f'{name} = "={version}"\n' for name, version in family if name not in pinned)
    _write_manifest(
        root, EXAMPLES_PACKAGE, reference.cargo_dependencies() + "\n" + extra, reference, sources
    )


def gate_examples(
    crates: Sequence[DocumentedCrate],
    reference: ChainReference,
    workdir: pathlib.Path,
    *,
    sources: Mapping[str, LocalCheckout] = MappingProxyType({}),
) -> ExampleGate:
    """Compile every example the corpus would carry; :func:`document_family` must have run here.

    The probe's lockfile is copied so the examples compile against the releases rustdoc read.
    """
    owners = gated_sources(crates)
    root = workdir / "example-check"
    _examples_crate(root, reference, [(c.name, c.version) for c in crates], sources)
    shutil.copyfile(workdir / "Cargo.lock", root / "Cargo.lock")
    ordered = list(owners)
    verdicts = check_examples(root, ordered)
    gate = ExampleGate(verdicts=dict(zip(ordered, verdicts)), owners=owners)
    _log.info(gate.summary())
    for line in gate.failure_report():
        _log.warning("example dropped: %s", line.removeprefix("- "))
    return gate


def build_manifest(
    crates: Sequence[DocumentedCrate], source: str, examples: ExampleGate
) -> RagManifest:
    """Both retrieval products for the whole family.

    The two want opposite things and are laid out separately rather than derived from each other:
    the manual product keeps every item addressable, because an exact lookup of
    ``NativeIntU64::u64_max`` has to find it; the embedded product is what a natural-language
    question lands on. Trait-impl methods are collapsed by :meth:`CrateDocs.impl_members` before either sees
    them, which is the grouping judgement made as a rule rather than a guess.
    """
    manual: list[ManualSection] = []
    embedded: list[EmbeddedGroup] = []
    for docs in sorted(crates, key=lambda c: c.name):
        manual.append(_surface_section(docs))
        for item, leaf in zip(docs.items, _leaves(list(docs.items))):
            headers = ApiPath(docs.name, docs.version, leaf).headers()
            body = replace(item, docs=examples.render(item.docs) if item.docs else None).body()
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


def cvlr_packages(checkout: Workspace) -> tuple[CratePackage, ...]:
    """The CVLR packages a checkout defines.

    Taken from ``cargo metadata`` rather than by reading manifests, because a workspace member
    inherits its version and edition from the root table, and cargo is the thing that knows how to
    follow that.

    A checkout that defines none is refused rather than ignored. It is the one outcome a caller
    cannot tell from success: the build would finish, and describe the published releases.
    """
    found = tuple(m for m in checkout.members if m in checkout.family(CVLR_PREFIX))
    if not found:
        offered = ", ".join(sorted(m.name for m in checkout.members)) or "none"
        raise RustdocUnavailable(
            f"{checkout.root} defines no CVLR crate, so documenting it would describe the "
            f"published releases while looking like it had used the checkout. It defines: "
            f"{offered}"
        )
    return found


def _read_workspace(root: pathlib.Path, *, no_deps: bool) -> Workspace:
    workspace = asyncio.run(Workspace.read(root, offline=True, no_deps=no_deps))
    if not isinstance(workspace, Workspace):
        raise RustdocUnavailable(f"cargo metadata failed in {root}: {workspace.describe()}")
    return workspace


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
        revision = _revision(resolved)
        for package in cvlr_packages(_read_workspace(resolved, no_deps=True)):
            if (prior := sources.get(package.name)) is not None:
                raise RustdocUnavailable(
                    f"both {prior.root} and {resolved} define {package.name}. Pass one of them: "
                    f"which copy the corpus would describe is not something this can choose for "
                    f"you."
                )
            sources[package.name] = LocalCheckout(
                root=resolved, crate_dir=package.root, version=package.version, revision=revision
            )
    return sources


def sources_for(
    reference: ChainReference, sources: Mapping[str, LocalCheckout]
) -> dict[str, LocalCheckout]:
    """The checkouts one generation can take: all but those of a crate it pins at another version.

    A ``cvlr-solana`` checkout is one release line, and the generations pin different ones.
    Patched into the other generation's probe it would fail there at cargo, so it is left to the
    generation it belongs to. A crate the reference set does not pin, such as ``cvlr-asserts``
    under the core, is taken by every generation.
    """
    pinned = {c.name: c.version for c in reference.crates()}
    return {
        name: source
        for name, source in sources.items()
        if pinned.get(name, source.version) == source.version
    }


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
    a release this build does not support. An unpublished release enters the same way, from its
    commit, unless a checkout replaces it.
    """
    _write_manifest(root, PROBE_PACKAGE, reference.cargo_dependencies(), reference, sources)


def _write_manifest(
    root: pathlib.Path,
    package: str,
    dependencies: str,
    reference: ChainReference,
    sources: Mapping[str, LocalCheckout],
) -> None:
    """An empty library crate with these dependencies, patched to the reference set's unpublished
    releases and to the checkouts."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.rs").write_text("")
    entries = {
        patch.name: patch.commit.patch_line(patch.name) for patch in reference.from_commits()
    }
    entries |= {name: f'{name} = {{ path = "{source.crate_dir}" }}' for name, source in sources.items()}
    patches = "".join(f"{entries[name]}\n" for name in sorted(entries))
    (root / "Cargo.toml").write_text(
        "[package]\n"
        f'name = "{package}"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n\n'
        "[dependencies]\n" + dependencies.rstrip("\n") + "\n"
        + (f"\n[patch.crates-io]\n{patches}" if patches else "")
    )


def _family_packages(root: pathlib.Path) -> list[tuple[str, str]]:
    """``(name, version)`` for every CVLR crate the probe resolved.

    The family is not declared anywhere — ``cvlr`` pulls in ``cvlr-asserts``, ``cvlr-log`` and the
    rest as ordinary dependencies — so it is recognized by name, exactly as
    :mod:`composer.spec.cvlr.crates` does for a target.
    """
    probe = _read_workspace(root, no_deps=False)
    return sorted(
        {(p.name, p.version) for p in probe.family(CVLR_PREFIX) if p not in probe.members}
    )


def _rustdoc(root: pathlib.Path, package: str, toolchain: str) -> pathlib.Path:
    result = subprocess.run(
        [
            "cargo", f"+{toolchain}", "rustdoc", "-q", "--offline",
            "-p", package, "-Z", "unstable-options", "--output-format", "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    out = root / "target" / "doc" / f"{package.replace('-', '_')}.json"
    if result.returncode != 0 or not out.is_file():
        raise RustdocUnavailable(
            f"cargo +{toolchain} rustdoc failed for {package} (exit {result.returncode}). rustdoc "
            f"JSON needs a nightly toolchain and a warm cargo registry.\n{result.stderr.strip()}"
        )
    return out


def load(path: pathlib.Path, name: str, version: str) -> CrateDocs:
    try:
        crate = rustdoc.parse_crate(path.read_text())
    except rustdoc.UnreadableRustdoc as exc:
        raise RustdocUnavailable(f"{name} {version}: {exc}") from exc
    return CrateDocs(name=name, version=version, crate=crate)


def document_family(
    reference: ChainReference,
    workdir: pathlib.Path,
    *,
    sources: Mapping[str, LocalCheckout] = MappingProxyType({}),
    toolchain: str = PINNED_NIGHTLY,
    fetch: bool = False,
) -> list[DocumentedCrate]:
    """Build the probe, run rustdoc over the whole family, resolve re-exports across it, and walk
    each crate.

    ``fetch`` resolves and fetches the probe's graph over the network first, for a cold registry.
    Without it the build is offline unless a release is read from a commit.
    """
    if shutil.which("cargo") is None:
        raise RustdocUnavailable("cargo is not on PATH, so there is nothing to document.")
    _probe_crate(workdir, reference, sources)
    commits = _commits(reference, sources)
    # A commit cargo has not fetched is not in the registry cache, so resolving one needs the
    # network, and so does the rest of a graph the cache has never held. Fetching it once lets every
    # rustdoc below stay offline.
    online = fetch or bool(commits)
    locked = subprocess.run(
        ["cargo", "generate-lockfile", "-q", *(() if online else ("--offline",))],
        cwd=workdir, capture_output=True, text=True, check=False,
    )
    if locked.returncode != 0 or not (workdir / "Cargo.lock").is_file():
        raise RustdocUnavailable(
            f"cargo could not resolve the reference set (exit {locked.returncode}). Without "
            f"--fetch or a release read from its commit this build never touches the network, so "
            f"the pinned crates must already be in the cargo registry: pass --fetch, or run "
            f"`cargo fetch` once in a crate depending on\n{reference.cargo_dependencies()}\n"
            f"{locked.stderr.strip()}"
        )
    if online:
        subprocess.run(
            ["cargo", "fetch", "-q"], cwd=workdir, capture_output=True, text=True, check=False
        )
    packages = _family_packages(workdir)
    if not packages:
        raise RustdocUnavailable(
            f"the probe crate resolved no CVLR packages. Its dependencies were:\n"
            f"{reference.cargo_dependencies()}"
        )
    crates: list[tuple[CrateDocs, Origin]] = []
    for name, version in packages:
        origin: Origin = sources.get(name) or commits.get(name) or Published()
        _log.info("rustdoc: %s %s from %s", name, version, origin.describe())
        crates.append((load(_rustdoc(workdir, name, toolchain), name, version), origin))
    resolve_aliases([docs for docs, _ in crates])
    declared = {c.name.replace("-", "_") for c in reference.scaffold_crates()}
    documented: list[DocumentedCrate] = []
    for docs, origin in crates:
        items = docs.items()
        writable = tuple(i for i in items if writable_from(i, declared))
        if len(writable) < len(items):
            _log.info(
                "%s: %d items have no path through a crate the scaffold declares, and are left out",
                docs.name, len(items) - len(writable),
            )
        documented.append(DocumentedCrate(docs.name, docs.version, writable, origin))
    return documented


def writable_from(item: Item, declared: set[str]) -> bool:
    """Whether a project declaring the crates ``declared`` names (in module spelling) can write
    one of ``item``'s paths.

    A crate the scaffold does not declare is reachable only through the facade's re-exports, so an
    item it defines that nothing re-exports is one the corpus would list and the author could not
    use. An associated item has no path of its own and is reached through its type.
    """
    if item.path is None:
        return True
    return any(p.split("::")[0] in declared for p in (item.path, *item.aliases))


def _commits(
    reference: ChainReference, sources: Mapping[str, LocalCheckout]
) -> dict[str, ReleaseCommit]:
    """The unpublished releases the probe reads from their commits: those no checkout replaces."""
    return {
        patch.name: ReleaseCommit(patch.commit)
        for patch in reference.from_commits()
        if patch.name not in sources
    }


def document_generations(
    references: Sequence[ChainReference],
    workdir: pathlib.Path,
    sources: Mapping[str, LocalCheckout],
    *,
    toolchain: str = PINNED_NIGHTLY,
    fetch: bool = False,
) -> tuple[list[DocumentedCrate], ExampleGate]:
    """Every generation's crates, each documented and its examples compiled in a probe of its
    own, a crate the generations share (the core) once.

    One probe per generation, because the generations cannot share one: their chain crates are
    bound to different platform crates, which is the reason there are two.
    """
    crates: dict[tuple[str, str], DocumentedCrate] = {}
    gates: list[ExampleGate] = []
    for n, reference in enumerate(references):
        probe = workdir / f"generation-{n}"
        probe.mkdir(parents=True, exist_ok=True)
        generation_sources = sources_for(reference, sources)
        family = document_family(
            reference, probe, sources=generation_sources, toolchain=toolchain, fetch=fetch
        )
        gates.append(gate_examples(family, reference, probe, sources=generation_sources))
        for docs in family:
            crates.setdefault((docs.name, docs.version), docs)
    return list(crates.values()), ExampleGate.combined(gates)


def documented_generations(chain: str) -> tuple[ChainReference, ...]:
    """The generations whose CVLR includes a chain crate.

    A harness-shim generation (jiminy, Pinocchio) pins the core alone and stands in a module of
    its own for the chain crate, so it has nothing to document that the SDK generations, which pin
    the same core, do not.
    """
    documented = tuple(r for r in generations(chain) if r.shim is None)
    if not documented:
        raise SystemExit(f"cvlr_api_docs: no {chain} generation pins a CVLR chain crate.")
    return documented


def manifest_source(reference: ChainReference, sources: Mapping[str, LocalCheckout]) -> str:
    """What the manifest records about where it came from.

    The only durable trace that a checkout was read: the rows deliberately carry none, so this is
    all that someone looking at an ingested corpus and asking why it disagrees with crates.io has
    to go on.
    """
    pinned = ", ".join(f"{c.name} {c.version}" for c in reference.crates())
    base = f"rustdoc over the CVLR family pinned at {pinned}"
    if commits := _commits(reference, sources):
        by_commit: dict[str, list[str]] = {}
        for name, commit in sorted(commits.items()):
            by_commit.setdefault(commit.describe(), []).append(name)
        read = "; ".join(f"{where}: {', '.join(names)}" for where, names in by_commit.items())
        base += f", reading these unpublished releases from their release commits — {read}"
    if not sources:
        return base
    by_checkout: dict[str, list[str]] = {}
    for name, source in sorted(sources.items()):
        by_checkout.setdefault(source.describe(), []).append(name)
    read = "; ".join(f"{where}: {', '.join(names)}" for where, names in by_checkout.items())
    return f"{base}, reading these from local checkouts rather than the published releases — {read}"


def undocumented_report(crates: Sequence[DocumentedCrate], examples: ExampleGate) -> str:
    """Every corpus item with no prose documentation, grouped by crate, then every example that
    did not compile.

    The corpus can only carry what the crates carry. rustdoc gives existence, signature, defining
    crate and feature gate with no doc comment at all, but *what an item is for* has to be written
    by someone, and this is the list of what nobody has written yet
    (``docs/cvlr-api-docs-plan.md`` §4.2).

    An example that does not compile is the same kind of ask: the corpus drops it, so the item
    reads as having no example at all.

    Regenerate rather than edit: the point of the list is to shrink.
    """
    lines: list[str] = []
    total = undocumented = 0
    summary: list[tuple[str, int, int]] = []
    for docs in sorted(crates, key=lambda c: c.name):
        items = docs.items
        missing = [i for i in items if not i.docs]
        total += len(items)
        undocumented += len(missing)
        summary.append((f"{docs.name} {docs.version}", len(missing), len(items)))
        if not missing:
            continue
        lines.append(f"\n### {docs.name} {docs.version} — {len(missing)} of {len(items)}\n")
        for item in missing:
            gate = f"  *({item.gate.label()})*" if item.gate is not None else ""
            lines.append(f"- `{item.name}` — {item.kind.label}{gate}")
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
    if failures := examples.failure_report():
        lines += [
            f"\n### Examples that do not compile — {len(failures)} of {len(examples.verdicts)}\n",
            *failures,
        ]
    return "\n".join(header + lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", "-o", type=pathlib.Path, default=pathlib.Path("cvlr_api_kb.rag.json"),
        help="Where to write the manifest. Feed it to composer.scripts.rag_import.",
    )
    parser.add_argument(
        "--chain", required=True,
        help="Which chain to document (solana, soroban): every platform generation it binds "
             "through a chain crate.",
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
        "--toolchain", default=PINNED_NIGHTLY,
        help=f"The rustup toolchain rustdoc runs under (default {PINNED_NIGHTLY}). rustdoc JSON "
             f"is nightly-only and this parser speaks format {rustdoc.FORMAT_VERSION} only. The example "
             "check runs under the default toolchain, whatever `cargo` resolves to.",
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="Resolve and fetch each generation's crates over the network before documenting "
             "them, for a cold cargo registry. Without it the build is offline, except for a "
             "release the reference set reads from a commit.",
    )
    parser.add_argument(
        "--undocumented", action="store_true",
        help="Instead of a manifest, write the list of items that carry no prose documentation. "
             "That list is the ask against the CVLR crates, and it is meant to shrink.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    references = documented_generations(args.chain)
    try:
        sources = crate_sources(args.crate_source)
        if args.keep is not None:
            args.keep.mkdir(parents=True, exist_ok=True)
            crates, examples = document_generations(
                references, args.keep, sources, toolchain=args.toolchain, fetch=args.fetch
            )
        else:
            with tempfile.TemporaryDirectory(prefix="cvlr-api-docs-") as tmp:
                crates, examples = document_generations(
                    references, pathlib.Path(tmp), sources, toolchain=args.toolchain,
                    fetch=args.fetch,
                )
    except (RustdocUnavailable, GateUnavailable) as exc:
        raise SystemExit(f"cvlr_api_docs: {exc}") from exc

    if args.undocumented:
        args.output.write_text(undocumented_report(crates, examples))
        _log.info("wrote %s", args.output)
        return
    source = " | ".join(
        f"{r.platform.label}: {manifest_source(r, sources_for(r, sources))}" for r in references
    )
    manifest = build_manifest(crates, source, examples)
    args.output.write_text(manifest.model_dump_json(indent=1))
    _log.info(
        "wrote %s: %d sections, %d embedded groups",
        args.output, len(manifest.manual_sections), len(manifest.embedded_groups),
    )


if __name__ == "__main__":
    sys.exit(main())
