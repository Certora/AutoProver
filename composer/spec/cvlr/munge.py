"""Verification-only edits to the program's own source, and applying them.

"Munge" is Certora's word for a modified copy of the code under verification. Where a CVL project
munges a copy of the contracts beside a separate spec, CVLR rules are compiled into the program's
own crate, so a munge here is an edit to that crate, switched on by a cargo feature.

The vocabulary is closed: a CVLR attribute on one function (:class:`FunctionMunge`), a function
split into a feature-gated pair (:class:`FunctionExtraction`), a type's derives moved behind the
feature (:class:`DeriveSwap`), a module compiled from a different file (:class:`ModuleRedirect`),
or an imported name pointed at a stand-in (:class:`ImportSwap`).
Each is a mechanical edit with a compile gate behind it, not an agent editing a program. A change
that would need a kind outside :data:`Munge` is the give-up boundary.

Every munge is gated on the cargo feature of the unit that recorded it, so it is a dormant line for
every other unit, and every munge is replayed onto the project's own copy of the file rather than
edited in place. :mod:`composer.spec.cvlr.tree` is what replays them.

Redirecting a dependency at a Certora-maintained fork is :mod:`composer.spec.cvlr.forks`.
"""

import hashlib
import posixpath
import textwrap
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.rust_source import (
    DERIVE_LIST,
    FunctionAmbiguous,
    FunctionNotFound,
    Signature,
    UseDeclaration,
    UseTree,
    attributes_above,
    derive_lists,
    find_signature,
    module_declarations,
    use_declarations,
)


# ---------------------------------------------------------------------------------------------
# attributes on the program's own functions
#
# Each kind is a CVLR attribute rather than a rewrite, which is what makes applying one a mechanical
# edit with a compile gate behind it instead of an agent editing a program.


@dataclass(frozen=True)
class EarlyPanic:
    """Every ``?`` in the function becomes ``.unwrap()``.

    Literally that — ``cvlr_early_panic`` walks the body and rewrites ``Expr::Try``. It is for a
    ``?`` inside the program, below the handler a rule calls, whose error construction the prover's
    pointer analysis refuses ([3308]). The most common munge in hand-written CVLR projects by a wide
    margin.

    What it cannot do is make an *acceptance* property statable. It removes the failure path rather
    than exposing one, so "this handler accepts a zero amount" is no more provable after it.
    """

    def attribute(self) -> str:
        return "cvlr::early_panic"

    def describe(self) -> str:
        return "`?` rewritten to `.unwrap()` throughout, so error paths panic and are pruned"


@dataclass(frozen=True)
class MockFn:
    """The function is replaced by a named stand-in.

    Differs from a summary (:mod:`composer.spec.cvlr.tuning`) in the direction that matters: a
    summary havocs the return, a mock *computes* one. So a property downstream of a mocked function
    still means something, where downstream of a summary it usually does not.
    """

    #: Path to the replacement, as the munged file must be able to spell it. The munged file is the
    #: program's own, so the path has to resolve from *outside* ``certora``, and the module holding
    #: the stand-in has to be ``pub``.
    stand_in: str

    def attribute(self) -> str:
        return f"cvlr::mock_fn(with = {self.stand_in})"

    def describe(self) -> str:
        return f"replaced by {self.stand_in}"


@dataclass(frozen=True)
class InlineNever:
    """The function is kept as its own frame rather than being inlined into its callers.

    Behaviour-preserving outright — ``inline`` is a hint about codegen and nothing else — which makes
    it the cheapest thing in the vocabulary to justify. It earns its place because a summary, an
    inlining directive and a counterexample's call stack all address a function *by symbol*, and a
    function the optimizer folded away has no symbol to address. Hand-written projects use it that
    way, often paired with ``early_panic``.
    """

    def attribute(self) -> str:
        return "inline(never)"

    def describe(self) -> str:
        return "kept out of line, so it keeps a symbol the prover can name"


@dataclass(frozen=True)
class HookOnEntry:
    """A call inserted as the function's first statement.

    ``cvlr::hook_on_entry`` (``cvlr-hook``, in the pinned reference set) rewrites the body to run
    ``call`` before anything else. Sound when the call only *observes* — records a flag, checks an
    invariant, logs; a call that mutates the program's state is a rewrite wearing an attribute and
    is outside the vocabulary.

    It is the vocabulary's only instrument for *observing* a point inside an execution. Everything
    else states a property at a function boundary.
    """

    call: str

    def attribute(self) -> str:
        return f"cvlr::hook_on_entry({self.call})"

    def describe(self) -> str:
        return f"calls {self.call} on entry"


@dataclass(frozen=True)
class HookOnExit:
    """A call inserted before the function returns. The exit half of :class:`HookOnEntry`."""

    call: str

    def attribute(self) -> str:
        return f"cvlr::hook_on_exit({self.call})"

    def describe(self) -> str:
        return f"calls {self.call} on exit"


#: The attribute kinds, as types. A change that would need a kind outside the vocabulary is the
#: give-up boundary rather than an edit budget.
#:
#: These are the attributes hand-written CVLR projects put on their programs' functions, less one
#: that cannot be had: making an item ``pub`` for verification has no counterpart in ``cvlr``, and
#: Rust cannot make visibility conditional on a feature without duplicating the item.
#:
#: Splitting a function is **not** an attribute and so is not in this union — see
#: :class:`FunctionExtraction`, a sibling of :class:`FunctionMunge` rather than one more entry here.
type MungeKind = EarlyPanic | MockFn | InlineNever | HookOnEntry | HookOnExit


@dataclass(frozen=True)
class FunctionMunge:
    """One verification-only attribute on one of the program's functions, and what turns it on.

    ``feature`` is the cargo feature the attribute is gated on, and it is what makes a shared
    working tree possible. A munge recorded by one unit is a
    **dormant** line for every other: with the feature off, ``cfg_attr`` contributes no attribute and
    the compiled function is the one the project shipped. So two units' munges of the same function
    coexist as two lines, and the union of every unit's munges is the single well-defined content of
    that file.

    Gating the whole ``cfg_attr`` rather than using ``cvlr::mock_fn``'s own ``when`` parameter,
    because it is one mechanism for every kind: ``cvlr::early_panic`` has no ``when``, and
    hand-written projects gate *it* by wrapping the ``cfg_attr`` condition anyway. The effect is
    identical and there is one thing to read.
    """

    #: The file, relative to the workspace root, so the record means the same thing in the report as
    #: on disk.
    path: str
    function: str
    kind: MungeKind
    #: Why the prover could not analyze the function as written, and why this attribute is sound for
    #: the properties in this batch. The only account anybody gets, exactly as with a summary.
    why_sound: str
    #: The cargo feature that activates it. Every munge this backend records names the recording
    #: unit's own feature; the default, the whole-harness feature, is for a record that names none.
    feature: str = DEFAULT_FEATURE

    @property
    def edit_id(self) -> str:
        """Identity for deduplication and for the report.

        The feature is part of it: two units munging the same function the same way are two distinct
        lines in the file, each dormant for the other, and collapsing them would drop one.
        """
        return f"{self.feature}:{self.kind.attribute()}@{self.path}::{self.function}"

    @property
    def subject(self) -> str:
        """The item this munge is about, for a report or a prompt.

        Every kind has one, because what renders a munge wants "what was edited" and the kinds do
        not all edit a function: a :class:`DeriveSwap` is about types, an :class:`ImportSwap` about
        a name.
        """
        return self.function

    def describe(self) -> str:
        return self.kind.describe()

    def attribute_line(self, indent: str) -> str:
        return f'{indent}#[cfg_attr(feature = "{self.feature}", {self.kind.attribute()})]'


@dataclass(frozen=True)
class Munged:
    """The edit was applied.

    ``line`` is where the edited item sits **in the returned source**, 1-indexed — so it is what a
    message about the munge should quote. For an attribute that is the function's signature, and the
    attribute is the line above it.
    """

    source: str
    line: int


@dataclass(frozen=True)
class AlreadyMunged:
    """The edit is already in the file, so it is unchanged."""

    #: What the munge is about, as :attr:`FunctionMunge.subject` names it.
    subject: str
    line: int


@dataclass(frozen=True)
class SourceDrifted:
    """The exact text a munge was recorded against is no longer in the file.

    Only the kinds located by captured text report this — :class:`FunctionExtraction` and
    :class:`DeriveSwap` — and it is the price of rewriting a region rather than inserting beside a
    named item. An attribute is *inserted* above a signature, so replaying it needs only the
    signature to still be there; a rewrite replaces a region and has to know the region is
    unchanged. Content addressing is what makes a moved region loud rather than a rewrite applied to
    code nobody read.
    """

    subject: str


@dataclass(frozen=True)
class DeriveNotFound:
    """A derive the swap names is not on the type, so the swap would be a silent no-op.

    Refused rather than skipped: the whole point of removing ``BorshSerialize`` is that the harness
    supplies it instead, and a swap that removed nothing leaves both impls absent or both present
    depending on which half was wrong. ``present`` is what the type actually derives.
    """

    type_name: str
    missing: tuple[str, ...]
    present: tuple[str, ...]


@dataclass(frozen=True)
class DeriveAttributeUnreadable:
    """The captured text has no single ``#[derive(..)]`` list to rewrite.

    Two ``derive`` attributes on one item, or none, or one this module's reader cannot parse. All
    three are refused for the same reason: the swap has to know exactly which derives survive, and
    a guess here silently drops one.
    """

    type_name: str
    why: str


@dataclass(frozen=True)
class ModuleNotFound:
    """No ``mod <name>;`` declaration in the file. ``nearby`` is what the file does declare.

    A *declaration* specifically — ``mod name { .. }`` with a body is not redirectable, since
    ``#[path]`` names the file a module is loaded from and an inline module is not loaded from one.
    Such a module is reported here rather than half-matched, because the attribute would compile and
    do nothing.
    """

    module: str
    nearby: tuple[str, ...]


@dataclass(frozen=True)
class ModuleOutsideCrateSource:
    """The declaring file is not under a crate's ``src/``, so the substitute has nowhere to go.

    The substitute's location is derived rather than supplied (:func:`mirror_path`), and the
    derivation needs the crate's source root to mirror *from*. A ``mod`` declared outside one —
    a ``build.rs``, a path this module misread — is refused rather than given an invented home.
    """

    path: str


@dataclass(frozen=True)
class ImportNotFound:
    """No ``use`` declaration in the file binds that name.

    ``nearby`` is every name the file's ``use`` lines do bind. A glob (``use foo::*;``) binds names
    it does not spell, so a name reaching the file that way lands here with ``nearby`` unable to
    show it — which is the honest report, since there is no declaration to rewrite either.
    """

    name: str
    nearby: tuple[str, ...]


@dataclass(frozen=True)
class ImportNotIsolable:
    """A declaration binds the name, and this module cannot rewrite it to leave the rest alone.

    Nested braces, specifically: ``use a::{b::{c, d}, e}`` is a tree, and dropping one leaf from it
    is a parse this module does not do. Refused rather than approximated, because the alternative
    failure is a ``use`` line that silently stops importing something else in the group.
    """

    name: str
    declaration: str


#: What applying an attribute can come to. ``FunctionNotFound`` is here and not in
#: :data:`ExtractionAttempt` because an attribute is located by name, where an extraction is located
#: by the text it captured — a name that has gone takes the text with it, so the extraction reports
#: the more specific :class:`SourceDrifted`.
type AttributeAttempt = Munged | FunctionNotFound | FunctionAmbiguous | AlreadyMunged

#: What applying an extraction can come to.
type ExtractionAttempt = Munged | FunctionAmbiguous | AlreadyMunged | SourceDrifted

#: What applying a derive swap can come to. It is located by captured text like an extraction,
#: so it reports :class:`SourceDrifted`; the two extra refusals are about the derive list itself.
type DeriveSwapAttempt = (
    Munged | AlreadyMunged | SourceDrifted | FunctionAmbiguous | DeriveNotFound
    | DeriveAttributeUnreadable
)

#: What applying a module redirect can come to. Located by declaration rather than by captured
#: text, so it reports :class:`ModuleNotFound` the way an attribute does rather than
#: :class:`SourceDrifted`; :class:`ModuleOutsideCrateSource` is about where the substitute would go.
type ModuleRedirectAttempt = (
    Munged | ModuleNotFound | FunctionAmbiguous | AlreadyMunged | ModuleOutsideCrateSource
)

#: What applying an import swap can come to. Located by declaration rather than by captured text, so
#: no :class:`SourceDrifted`: the feature-on half is derived from whatever the ``use`` line says at
#: replay time, so a line that has gained a name swaps correctly rather than drifting.
type ImportSwapAttempt = (
    Munged | ImportNotFound | ImportNotIsolable | FunctionAmbiguous | AlreadyMunged
)

type MungeAttempt = (
    AttributeAttempt | ExtractionAttempt | DeriveSwapAttempt | ModuleRedirectAttempt
    | ImportSwapAttempt
)


# ---------------------------------------------------------------------------------------------
# extraction: the one kind that is not an attribute
#
# Everything above attaches a line to a function that already exists, which is why applying one is a
# mechanical insert. Extraction *splits* a function so a rule can drive a piece of it directly, and
# there is no attribute for that — so it is a rewrite, and a rewrite needs two things the attribute
# kinds get for free: it has to be content-addressed, so a replay onto source that has moved fails
# loudly instead of quietly rewriting the wrong region; and it cannot be *inert by accident*, which
# in Rust means the gated-pair form below rather than a ``cfg_attr``.
#
# Finding where an item ends is the machinery that costs: an attribute needs only the signature
# line, a rewrite needs the whole definition. Both are found by `rust_source`, which says why.


def _reindent(text: str, indent: str) -> str:
    """Model-written Rust, re-laid-out to sit where the function it replaces sat."""
    return textwrap.indent(textwrap.dedent(text).strip("\n"), indent)


@dataclass(frozen=True)
class FunctionExtraction:
    """A function split into a feature-gated pair so a rule can drive a piece of it.

    For a property about a state transition buried inside a handler, which has no function boundary
    to attach to; no attribute creates one. :class:`HookOnEntry` reaches such a point to *observe*
    it; this is for the case where the rule has to **call** it.

    What replaces the original function is three items rather than one::

        #[cfg(not(feature = "unit_x"))]
        fn settle(..) { /* the original, untouched */ }
        #[cfg(feature = "unit_x")]
        fn settle(..) { settle_transition(..) }
        #[cfg(feature = "unit_x")]
        pub fn settle_transition(..) { /* what the rule descends to */ }

    Ugly, and it keeps every property the attribute kinds have. The deployed build compiles the
    original verbatim, because :attr:`original` is captured text rather than something a model
    retyped. Every sibling unit compiles it too, since the pair is gated on the requesting unit's
    own feature. And it cannot be inert by accident: :meth:`render` writes both ``cfg`` lines, so
    there is no spelling of this record in which the feature-off build sees anything new.

    :attr:`original` is also what makes replay safe. It is the drift detector the attribute kinds
    get from ``FunctionNotFound``: a rewrite has to know the region it replaces is still the region
    it was recorded against, so the text is stored and a replay that cannot find it reports
    :class:`SourceDrifted` rather than rewriting whatever is there now.

    One thing the pair does not carry across: attributes and doc comments already above the function
    stay above the ``#[cfg(not(..))]`` line, so they apply to the original half and not to the
    feature-on half. That is right for a ``#[cfg_attr]`` recorded by *another* unit — that unit
    builds with this feature off — and it is why :func:`replay` applies attributes first.
    """

    path: str
    function: str
    #: The new function's name, as :attr:`extracted` spells it and as the author's rule will call it.
    extracted_name: str
    #: The pristine definition of :attr:`function`, captured verbatim when the extraction was
    #: recorded. Not authored by anybody: retyping it is how the deployed half stops being the
    #: deployed half.
    original: str
    #: :attr:`function` as it reads under the feature — same signature, body delegating to the
    #: extracted item.
    replacement: str
    #: The extracted item itself, ``pub`` so the harness module can name it.
    extracted: str
    why_sound: str
    feature: str = DEFAULT_FEATURE

    def render(self) -> str:
        """The gated triple, indented to sit where the original sat."""
        indent = self.original[: len(self.original) - len(self.original.lstrip(" \t"))]
        gate_on = f'{indent}#[cfg(feature = "{self.feature}")]'
        return "\n".join(
            [
                f'{indent}#[cfg(not(feature = "{self.feature}"))]',
                self.original,
                gate_on,
                _reindent(self.replacement, indent),
                gate_on,
                _reindent(self.extracted, indent),
            ]
        )

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, for the report, and for what a review is approval *of*.

        Carries a digest of the rewrite itself, which the attribute kinds have no need of: an
        attribute's whole content is its name, so ``edit_id`` spells it out. Here the compiler sees
        two blocks of model-written Rust, and an id blind to them would let a re-recorded extraction
        inherit the previous one's review and the previous one's prover stamp.
        """
        digest = hashlib.sha256(self.render().encode()).hexdigest()[:8]
        return f"{self.feature}:extract[{self.extracted_name}:{digest}]@{self.path}::{self.function}"

    @property
    def subject(self) -> str:
        return self.function

    def describe(self) -> str:
        return f"split so `{self.extracted_name}` is a function a rule can drive"


# ---------------------------------------------------------------------------------------------
# the derive swap: the kind for code no function names
#
# Everything above addresses a *function* — annotate it, replace it, split it. A derived trait impl
# has no function to name: `#[derive(BorshSerialize)]` generates the code, and the only handle on it
# is the derive itself. That is the whole of why this kind exists, and why it is narrow: on every
# framework except native-plus-borsh the seam is a function or a method, which ``mock_fn`` already
# reaches.


@dataclass(frozen=True)
class SwappedDerive:
    """One type's derives, as they read before and as they should read under the feature."""

    type_name: str
    #: The ``#[derive(..)]`` line through the item's declaration line, verbatim. Both halves are
    #: needed: the derive is what gets rewritten, and the declaration is what makes the capture
    #: unique — a bare derive list recurs many times in a state module.
    original: str
    #: Derives that move behind ``cfg_attr(not(feature))``, because the harness supplies them.
    removed: tuple[str, ...]
    #: Derives added under the feature. ``Copy`` in practice, because an impl that assigns
    #: ``*self`` to a global needs it.
    added: tuple[str, ...] = ()

    def _list(self) -> tuple[str, ...] | DeriveAttributeUnreadable:
        match derive_lists(self.original):
            case (only,):
                return only
            case found:
                return DeriveAttributeUnreadable(
                    self.type_name,
                    f"expected exactly one #[derive(..)] in the captured text, found {len(found)}",
                )

    def render(self, feature: str) -> str | DeriveAttributeUnreadable | DeriveNotFound:
        """The captured text with its derive line replaced by the gated trio.

        Derives this swap does not name stay on an ungated ``derive``, so the deployed build sees
        exactly what it saw before — the two ``cfg_attr`` lines are additions, not a rewrite of what
        was there.
        """
        match self._list():
            case DeriveAttributeUnreadable() as bad:
                return bad
            case derives:
                pass
        if missing := tuple(d for d in self.removed if d not in derives):
            return DeriveNotFound(self.type_name, missing, derives)

        kept = tuple(d for d in derives if d not in self.removed)
        indent = self.original[: len(self.original) - len(self.original.lstrip(" \t"))]
        lines: list[str] = []
        if self.removed:
            lines.append(
                f'{indent}#[cfg_attr(not(feature = "{feature}"), '
                f'derive({", ".join(self.removed)}))]'
            )
        if self.added:
            lines.append(
                f'{indent}#[cfg_attr(feature = "{feature}", derive({", ".join(self.added)}))]'
            )
        rest = DERIVE_LIST.sub(
            f'#[derive({", ".join(kept)})]' if kept else "", self.original, count=1
        )
        # Dropping every derive leaves a blank line where the attribute was; the item still needs
        # to land immediately under the gates.
        lines.append(rest if kept else "\n".join(l for l in rest.splitlines() if l.strip()))
        return "\n".join(lines)

    def describe(self) -> str:
        parts = []
        if self.removed:
            parts.append(f"stops deriving {', '.join(self.removed)}")
        if self.added:
            parts.append(f"derives {', '.join(self.added)}")
        return f"`{self.type_name}` " + " and ".join(parts or ["is unchanged"])


@dataclass(frozen=True)
class DeriveSwap:
    """Derives moved behind the unit's feature so the harness can supply the impls instead.

    One record covers **every type in one file** the swap needs, because it cascades and the pieces
    stand or fall together: ``derive(Copy)`` on a struct requires every field type to be ``Copy``,
    so swapping ``StakePool`` alone does not compile until ``AccountType`` is swapped too. Reverting
    half of that leaves a program that does not build, which is why it is one reviewable unit rather
    than several.

    A cascade that crosses files is *not* expressible in one record — replay applies munges per
    file — and is recorded as one swap per file. The refusal for a half-applied cross-file cascade
    is the compiler's, which is loud and immediate, so this is a sharp edge rather than a hazard.

    Unlike :class:`FunctionExtraction` this does not gate an *item*, only its attributes, so the
    ``cfg_attr`` form is safe here: a derive list is one attribute, and gating it cannot leave two
    definitions of one name.
    """

    path: str
    swaps: tuple[SwappedDerive, ...]
    why_sound: str
    feature: str = DEFAULT_FEATURE

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, the report, and what a review approves.

        Digests the rendered result like an extraction's does, because the compiler sees a derive
        list this record computed rather than a name it spelled out.
        """
        material = "|".join(
            f"{sw.type_name}:{','.join(sw.removed)}:{','.join(sw.added)}" for sw in self.swaps
        )
        digest = hashlib.sha256(material.encode()).hexdigest()[:8]
        names = "+".join(sw.type_name for sw in self.swaps)
        return f"{self.feature}:derives[{names}:{digest}]@{self.path}"

    @property
    def subject(self) -> str:
        return ", ".join(sw.type_name for sw in self.swaps)

    def describe(self) -> str:
        return "; ".join(sw.describe() for sw in self.swaps)


# ---------------------------------------------------------------------------------------------
# the module redirect: the kind for methods
#
# ``mock_fn`` replaces an item with ``use <stand-in> as <name>;``, and a ``use`` inside an ``impl`` is
# not a method — so no attribute reaches an inherent or trait method, which is where a large share of
# Solana state logic lives. Hand-written CVLR projects answer that by redirecting the file the
# methods are *in*.


#: Where a redirected module's substitute file lives, relative to the crate's source root. Mirrors
#: the original module hierarchy, the convention hand-written CVLR projects follow. The ``#[path]``
#: reaches the file directly, so no ``mocks`` module is declared.
MOCKS_DIR = PurePosixPath("certora/mocks")


def mirror_path(declaring_file: str, module: str) -> PurePosixPath | None:
    """Where the substitute for ``module`` goes, given the file that declares it.

    ``programs/lending/src/invokes/mod.rs`` + ``liquidity_layer`` becomes
    ``programs/lending/src/certora/mocks/invokes/liquidity_layer.rs`` — the mocks tree mirrors the
    source tree beneath it. Derived rather than supplied because a substitute in the wrong place is
    a ``#[path]`` that resolves to nothing, and the compiler's message for that names the *module*,
    not the mistake.

    ``None`` when the declaring file is not under a crate's ``src/``, which the caller reports as
    :class:`ModuleOutsideCrateSource`.
    """
    parts = PurePosixPath(declaring_file).parts
    if "src" not in parts:
        return None
    cut = len(parts) - 1 - parts[::-1].index("src")
    src_root = PurePosixPath(*parts[: cut + 1])
    within = PurePosixPath(*parts[cut + 1 : -1])
    return src_root / MOCKS_DIR / within / f"{module}.rs"


def _path_attr_value(declaring_file: str, substitute: PurePosixPath) -> str:
    """The ``#[path]`` string, which Rust resolves relative to the declaring file's directory.

    Minimal rather than merely correct: climbing to the workspace root and back down resolves to the
    same file, and would break the moment the crate moved within the workspace. Both trees share the
    crate's ``src/``, so the common prefix is always at least that.
    """
    return posixpath.relpath(str(substitute), str(PurePosixPath(declaring_file).parent))


def forwards_feature(program_manifest: str, dependency: str, feature: str) -> bool:
    """Whether the program's ``feature`` enables the same feature in ``dependency``.

    The precondition for a cross-crate redirect, and worth checking rather than assuming, because
    its absence is silent: ``#[cfg_attr(feature = "certora", ..)]`` inside a dependency compiles
    perfectly and simply never activates, so the munge lands, the build succeeds, and the prover
    reports exactly what it reported before. :func:`~composer.spec.cvlr.scaffold._plan_feature_forwarding`
    writes the forward for a project it scaffolds from scratch, and cannot for one that already had
    a ``certora`` feature — nothing here overwrites a feature somebody else defined.
    """
    try:
        parsed = tomllib.loads(program_manifest)
    except tomllib.TOMLDecodeError:
        return False
    enables = parsed.get("features", {}).get(feature)
    return isinstance(enables, list) and f"{dependency}/{feature}" in enables


@dataclass(frozen=True)
class ModuleRedirect:
    """A whole module compiled from a different file behind the unit's feature.

    .. code-block:: rust

        #[cfg_attr(feature = "unit_x", path = "../certora/mocks/invokes/liquidity_layer.rs")]
        pub mod liquidity_layer;

    Nothing is aliased, so there is no name to resolve: a different file is compiled in the
    module's place, carrying its own types and ``impl`` blocks.

    **The substitute is arbitrary code, and that makes this the most dangerous kind.** The others
    are bounded — an attribute cannot change what a function computes, and an extraction and a
    derive swap both reproduce the original text verbatim in the deployed half. A substitute module
    is written from scratch, so a rule proved against one is a rule about *the substitute* unless
    what it stands for is stated. :attr:`why_sound` therefore carries a heavier burden here than
    elsewhere and the judge is told so.

    Where it goes is derived (:func:`mirror_path`), not supplied. The deployed build is untouched:
    with the feature off the ``mod`` declaration is exactly what the developer wrote, and the
    substitute file is never named by anything.
    """

    path: str
    module: str
    #: The substitute file's full contents.
    substitute: str
    why_sound: str
    feature: str = DEFAULT_FEATURE

    @property
    def substitute_path(self) -> PurePosixPath | None:
        return mirror_path(self.path, self.module)

    @property
    def created(self) -> dict[str, str]:
        """The file this munge adds, which no project file backs, by project-relative path.

        The only kind that adds one: every other munge is replayed onto the project's copy of a
        file, and there is no project copy of a substitute.
        """
        target = self.substitute_path
        return {} if target is None else {str(target): self.substitute}

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, the report, and what a review approves.

        Digests the substitute, because that is what the compiler sees and what a reviewer approved.
        Re-authoring the stand-in is a different edit and must not inherit the old approval.
        """
        digest = hashlib.sha256(self.substitute.encode()).hexdigest()[:8]
        return f"{self.feature}:module[{self.module}:{digest}]@{self.path}"

    @property
    def subject(self) -> str:
        return self.module

    def describe(self) -> str:
        return f"`{self.module}` is compiled from a stand-in module"


# ---------------------------------------------------------------------------------------------
# the import swap: the kind for code in a dependency the program only calls
#
# Every kind above edits the item it is about, which requires the item to be the program's. A CPI is
# the case where it is not: ``invoke`` is the SDK's (``solana_program``'s, or ``solana_cpi``'s on
# SDK 3), a munge may not touch a dependency,
# and the call the program makes to it is an expression rather than an item. What *is* the program's
# is the ``use`` line that puts the name in scope.


@dataclass(frozen=True)
class ImportSwap:
    """A name the file imports resolves to a stand-in behind the unit's feature.

    ``mock_fn`` replaces a definition with ``use <stand-in> as <name>;``. This replaces a ``use``
    with a ``use`` — the same mechanism, reached from the calling side, which is the only side
    available when the definition belongs to a dependency:

    .. code-block:: rust

        #[cfg(not(feature = "unit_x"))]
        use anchor_lang::solana_program::program::invoke;
        #[cfg(feature = "unit_x")]
        use crate::certora::specs::unit_x::invoke_transfer as invoke;

    Hand-written CVLR projects make this edit for exactly this purpose, and it is how a CPI wrapper
    gets a boundary in a program that did not factor one.

    **Scope is the file, not the call.** Every mention of the name below the declaration resolves to
    the stand-in, including calls this unit's property says nothing about. That is what
    :attr:`why_sound` has to be written against, and it is why the swap names an import rather than
    a call site: a munge that changed one expression and not its neighbour would be a rewrite of the
    program's behaviour wearing an edit's clothes.

    **What a CPI stand-in may claim.** The Prover's own model of a cross-program call already moves
    nothing, so a stand-in that returns ``Ok(())`` drops the *callee's* effect at no cost against
    it, and gets the rule past the call and the instruction-building loop no bound discharges. A
    property about the program's accounting is then provable and a property about the moved lamports
    is not. Which of those the batch holds is the author's to know, and :attr:`why_sound` is where it
    is written down.
    """

    path: str
    #: The name as the code spells it at the call site — the binding, not the path it came from. An
    #: aliased import (``use x::y as z``) binds ``z``, and ``z`` is what this names.
    name: str
    #: Path to the replacement, spelled as the munged file must spell it. Same constraint as
    #: :attr:`MockFn.stand_in`: the file is the program's own, so it has to resolve from outside
    #: ``certora``.
    stand_in: str
    why_sound: str
    feature: str = DEFAULT_FEATURE

    @property
    def edit_id(self) -> str:
        return f"{self.feature}:import[{self.name}={self.stand_in}]@{self.path}"

    @property
    def subject(self) -> str:
        return self.name

    @property
    def replacement(self) -> str:
        """The declaration that binds :attr:`name` to the stand-in, without the ``use`` keyword.

        ``as`` is omitted when the stand-in's own last segment is already the name: ``use x::y as y``
        compiles, and it reads as though something was renamed when nothing was. A munge diff is
        read by people.
        """
        tail = self.stand_in.rsplit("::", 1)[-1]
        return self.stand_in if tail == self.name else f"{self.stand_in} as {self.name}"

    def describe(self) -> str:
        return f"`{self.name}` resolves to {self.stand_in} throughout this file"


#: One edit to the program under verification: an attribute on a function, a function split in two,
#: a type's derives moved, a whole module compiled from elsewhere, or an imported name pointed at a
#: stand-in. All are gated on a cargo feature and all replay onto the pristine project, which is the
#: whole of what the rest of the backend needs to know about the difference.
type Munge = FunctionMunge | FunctionExtraction | DeriveSwap | ModuleRedirect | ImportSwap


def apply_munge(source: str, munge: Munge) -> MungeAttempt:
    """Apply one recorded edit to ``source``, or say why it did not take.

    The refusals are the drift detector for a resumed or replayed run. Replay happens against the
    *pristine* project, which can have moved since the edit was recorded; a function that is gone,
    has acquired a same-named sibling, or whose body no longer reads as it did is reported here
    rather than silently mis-applied.
    """
    match munge:
        case FunctionMunge():
            return apply_attribute(source, munge)
        case FunctionExtraction():
            return apply_extraction(source, munge)
        case DeriveSwap():
            return apply_derive_swap(source, munge)
        case ModuleRedirect():
            return apply_module_redirect(source, munge)
        case ImportSwap():
            return apply_import_swap(source, munge)


def apply_attribute(source: str, munge: FunctionMunge) -> AttributeAttempt:
    """Insert the attribute immediately above its function's signature.

    Above the signature puts it *below* any doc comment and any attribute already there — legal, and
    it keeps the insertion a single-line edit whose diff reads as one. The compile gate is what
    catches a signature this misjudged; these three refusals are the cases a compile would accept
    and a reader would not.
    """
    match find_signature(source, munge.function):
        case Signature() as signature:
            pass
        case refused:
            return refused
    lines = source.splitlines(keepends=True)
    index = signature.line - 1
    attribute = munge.attribute_line(signature.indent)
    if index > 0 and lines[index - 1].strip() == attribute.strip():
        return AlreadyMunged(munge.subject, signature.line)
    lines.insert(index, attribute + "\n")
    # The signature moved down by the line just inserted above it.
    return Munged("".join(lines), signature.line + 1)


def _occurrences(haystack: str, needle: str) -> tuple[int, ...]:
    found: list[int] = []
    at = haystack.find(needle)
    while at >= 0:
        found.append(at)
        at = haystack.find(needle, at + 1)
    return tuple(found)


def apply_extraction(source: str, edit: FunctionExtraction) -> ExtractionAttempt:
    """Replace the captured definition with the gated triple.

    The rendered block *contains* the original verbatim, so "already applied" has to be asked before
    "is the original still here" — otherwise a second replay would find the original inside the
    ``#[cfg(not(..))]`` half and nest one triple inside another.
    """
    rendered = edit.render()
    if (at := source.find(rendered)) >= 0:
        return AlreadyMunged(edit.subject, source.count("\n", 0, at) + 2)
    match _occurrences(source, edit.original):
        case ():
            return SourceDrifted(edit.subject)
        case (at,):
            pass
        case hits:
            return FunctionAmbiguous(
                edit.subject, tuple(source.count("\n", 0, h) + 1 for h in hits)
            )
    updated = source[:at] + rendered + source[at + len(edit.original) :]
    # One line below the `#[cfg(not(..))]` the triple opens with.
    return Munged(updated, updated.count("\n", 0, at) + 2)


def apply_derive_swap(source: str, edit: DeriveSwap) -> DeriveSwapAttempt:
    """Rewrite each captured derive line into its gated trio.

    All of the file's swaps land or none do: a cascade that applied halfway would leave a struct
    deriving ``Copy`` over a field type that does not, and the compiler's message for that names the
    field rather than the munge. So the refusals are checked against the *original* source first and
    the writes happen afterwards.
    """
    rendered: list[tuple[str, str]] = []
    for swap in edit.swaps:
        if _derives_already_gated(source, swap, edit.feature):
            continue
        match swap.render(edit.feature):
            case (DeriveNotFound() | DeriveAttributeUnreadable()) as refusal:
                return refusal
            case str(text):
                rendered.append((swap.original, text))

    if not rendered:
        first = edit.swaps[0]
        at = source.find(first.original.splitlines()[-1])
        return AlreadyMunged(edit.subject, source.count("\n", 0, max(at, 0)) + 1)

    for original, _ in rendered:
        match _occurrences(source, original):
            case ():
                return SourceDrifted(edit.subject)
            case (_,):
                pass
            case hits:
                return FunctionAmbiguous(
                    edit.subject,
                    tuple(source.count("\n", 0, h) + 1 for h in hits),
                )

    at = 0
    for original, text in rendered:
        at = source.find(original)
        source = source[:at] + text + source[at + len(original) :]
    return Munged(source, source.count("\n", 0, at) + 1)


def _derives_already_gated(source: str, swap: SwappedDerive, feature: str) -> bool:
    """Is this type's declaration already sitting under a gate naming this feature?

    Asked of the file's shape rather than by looking for a rendered string, because a swap that has
    landed can no longer render: its derives have moved behind the gate, so
    :meth:`SwappedDerive.render` would report them missing. Which is the same reason this is
    consulted *before* a refusal is believed.
    """
    at = source.find(swap.original.splitlines()[-1])
    return at >= 0 and any(
        f'feature = "{feature}"' in attribute for attribute in attributes_above(source, at)
    )


def apply_module_redirect(source: str, edit: ModuleRedirect) -> ModuleRedirectAttempt:
    """Put the ``cfg_attr`` immediately above the module's declaration.

    Located by declaration rather than by captured text, so a file that has grown or lost unrelated
    modules replays cleanly — the same property an attribute munge has, and for the same reason: the
    edit is an insertion above a line rather than a rewrite of a region.
    """
    target = edit.substitute_path
    if target is None:
        return ModuleOutsideCrateSource(path=edit.path)

    declarations = list(module_declarations(source))
    match [d for d in declarations if d.name == edit.module]:
        case []:
            return ModuleNotFound(module=edit.module, nearby=tuple(d.name for d in declarations))
        case [declaration]:
            pass
        case found:
            return FunctionAmbiguous(subject=edit.subject, lines=tuple(d.line for d in found))

    attribute = (
        f'#[cfg_attr(feature = "{edit.feature}", path = "{_path_attr_value(edit.path, target)}")]'
    )
    before = source[: declaration.start]
    if before.rstrip("\n").endswith(attribute):
        return AlreadyMunged(subject=edit.subject, line=declaration.line)
    return Munged(
        source=f"{before}{declaration.indent}{attribute}\n{source[declaration.start:]}",
        line=declaration.line + 1,
    )


def _landed_import(declaration: UseDeclaration, edit: ImportSwap) -> bool:
    """Whether ``declaration`` is the aliased one this swap writes, under this swap's gate."""
    return (
        declaration.gate is not None
        and not declaration.gate.negated
        and declaration.gate.feature == edit.feature
        and declaration.tree == UseTree(prefix="", items=((edit.name, edit.replacement),))
    )


def _dormant_here(declaration: UseDeclaration, feature: str) -> bool:
    """Whether a declaration is gated *on* some other unit's feature, so this build never sees it.

    The case is a second unit swapping an import a first unit already swapped: the shared tree
    replays the union onto the pristine file, so the second swap meets the first one's output. Its
    aliased line is dormant for everybody else and must not be mistaken for a second binding of the
    name; the original under ``#[cfg(not(..))]`` is still in force here and is the one to gate.
    """
    gate = declaration.gate
    return gate is not None and not gate.negated and gate.feature != feature


def apply_import_swap(source: str, edit: ImportSwap) -> ImportSwapAttempt:
    """Gate the declaration that binds the name, and put an aliased one beside it.

    The original is kept verbatim under ``#[cfg(not(..))]`` rather than rewritten, so the deployed
    build imports exactly what the developer wrote. That is the property :class:`FunctionExtraction`
    has to store its original text to get, and it is free here because the original stays on the
    page.

    ``line`` is where the aliased declaration lands in the returned source: the swapped name is what
    a message about this munge is about, and it is the only one of the three lines that is new.
    """
    declarations = list(use_declarations(source))
    if (landed := next((d for d in declarations if _landed_import(d, edit)), None)) is not None:
        return AlreadyMunged(subject=edit.subject, line=landed.line)

    bound: list[str] = []
    found: list[tuple[UseDeclaration, UseTree]] = []
    opaque: str | None = None
    for declaration in declarations:
        if declaration.tree is None:
            if opaque is None and declaration.mentions(edit.name):
                opaque = " ".join(declaration.text.split())
            continue
        names = declaration.tree.names()
        bound.extend(names)
        if edit.name in names and not _dormant_here(declaration, edit.feature):
            found.append((declaration, declaration.tree))

    match found:
        case []:
            if opaque is not None:
                return ImportNotIsolable(name=edit.name, declaration=opaque)
            return ImportNotFound(name=edit.name, nearby=tuple(bound))
        case [(declaration, tree)]:
            pass
        case _:
            return FunctionAmbiguous(subject=edit.subject, lines=tuple(d.line for d, _ in found))

    before = source[: declaration.start]
    indent = declaration.indent
    gate_off = f'{indent}#[cfg(not(feature = "{edit.feature}"))]'
    gate_on = f'{indent}#[cfg(feature = "{edit.feature}")]'
    keyword = f"{declaration.visibility} use" if declaration.visibility else "use"
    kept = [text for name, text in tree.items if name != edit.name]
    lines = [gate_off, declaration.text]
    if kept:
        lines += [gate_on, f'{indent}{keyword} {tree.prefix}{{{", ".join(kept)}}};']
    lines += [gate_on, f"{indent}{keyword} {edit.replacement};"]
    return Munged(
        source=before + "\n".join(lines) + source[declaration.end :],
        line=before.count("\n") + 1 + sum(line.count("\n") + 1 for line in lines[:-1]),
    )


def munge_history(munges: tuple[Munge, ...]) -> tuple[str, ...]:
    """The munges as ``version_history`` tokens, so a stamp predating one goes stale with it.

    Keyed on what the prover sees differently — the file, the function, and the attribute or the
    rewrite — and not on ``why_sound``, so correcting the wording of a justification does not cost a
    submission.
    """
    return tuple(f"munge:{m.edit_id}" for m in munges)


@dataclass(frozen=True)
class DropMunges:
    """A write that *removes* munges rather than adding them.

    A distinct type rather than a flag on the write, because the two operations carry different
    payloads and a list that sometimes meant "remove these" would be indistinguishable from one that
    meant "add these". It is what makes a munge undoable: the working tree is rebuilt from the
    pristine copy and replayed from this list on every build, so an ``edit_id`` that leaves the list
    leaves the file.
    """

    edit_ids: frozenset[str]


type MungeWrite = list[Munge] | DropMunges


def amended(munge: Munge, why_sound: str) -> Munge:
    """The same edit with a different account of itself.

    :attr:`edit_id` deliberately excludes ``why_sound`` — it keys on what the compiler and the prover see —
    so the result is the *same* munge by every identity this module defines. That is what makes a
    correction free: no file changes, :func:`munge_history` yields the same token, and no submission
    is invalidated.
    """
    return replace(munge, why_sound=why_sound)


def latest(munges: Iterable[Munge]) -> list[Munge]:
    """One record per :attr:`edit_id`, last write winning, in the order the ids first appeared.

    Two records sharing an id are the same edit described twice, so the later description is the
    live one. Position comes from the first appearance rather than the last: a re-worded
    justification is not a re-ordering of the edits, and a report that shuffled on a typo fix would
    read as though something moved.
    """
    by_id: dict[str, Munge] = {}
    for munge in munges:
        by_id[munge.edit_id] = munge
    return list(by_id.values())


def merge_munges(left: list[Munge], right: MungeWrite) -> list[Munge]:
    """State reducer for the munge list: append or replace by :attr:`edit_id`, or remove.

    A reducer because several tool calls can land in one graph step, and LangGraph refuses two
    writes to an unreduced key.

    A record whose id is already held **replaces** it rather than being dropped. Since the id covers
    everything the build sees, the two can differ only in ``why_sound``, and the later prose is a
    correction of the earlier — see :func:`amended`. Keeping the first would leave a landed munge's
    justification uncorrectable, and the stale sentence would go on to the report.
    """
    if isinstance(right, DropMunges):
        return [m for m in left if m.edit_id not in right.edit_ids]
    return latest([*left, *right])
