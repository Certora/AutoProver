"""The CVLR reference set — which crate releases "current CVLR" resolves to.

Three things need one answer to that question and would otherwise each invent their own: the
acceptance gate that compiles every code-bearing corpus entry, the generated crate reference, and
the scaffold the backend writes into a target's ``Cargo.toml``. See
``docs/cvlr-capture-plan.md`` §4.7.2 for the survey this encodes.

**Published releases only, pinned exactly, never resolved as "latest".** The CVLR lines version
independently — the core is published well ahead of the chain crates — so "latest" would pair a
current core with a stale chain crate and still look right. Recording exact releases means a bump
is a visible edit here, with the compile gate as its test.

**A chain crate implies a platform generation.** ``cvlr-solana`` is pinned to one Solana platform
line (0.4.x → ``solana-program`` 1.18, 0.5.0 → 2.2, the unreleased 0.6 line → the ``solana-*`` v3
crates), and each generation has its *own* ``AccountInfo`` type. Two crates that disagree do not
merely warn — a helper from one cannot be passed to a handler from the other, so
:attr:`ChainReference.platform` is part of the reference set rather than a detail of the target.

**The split is not where the major version is.** ``solana-program`` stopped *defining* the platform
types at **2.2**, not at 3.0: 1.17 and 1.18 carry a real ``account_info`` module, while 2.2.1, 2.3.0
and 3.0.0 all re-export ``solana-account-info``. So the generation pinned above is already
post-split, and a path written as ``solana_program::account_info::AccountInfo`` names a re-export
whose *defining* path — the one a demangled symbol carries — is ``solana_account_info::AccountInfo``.
:class:`PathAlias` is how that difference reaches the tuning files; ``docs/cvlr-backend-plan.md``
§7.5.6 is what it cost to find out.

This module deliberately imports nothing: a script that only needs to know which version to write
into a probe crate should not pay for the pipeline (importing ``ChainTag``'s home costs ~2.5s and
pulls the whole model layer). It therefore repeats the chain vocabulary as plain strings, the same
trade ``composer.rustapp.descriptor`` makes for the same reason, and
``tests/test_cvlr_reference.py`` pins the two against each other so they cannot drift apart.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class GitPin:
    """A branch of a CVLR repository, for a crate whose releases trail what a chain needs.

    Soroban's whole set is in this state: no published ``cvlr`` builds a Soroban contract, and the
    fixes live on ``main`` unreleased. A branch moves under us, which is exactly what the reference
    set exists to prevent — so :attr:`observed` records the commit the set was checked against, and
    a bump is an edit here rather than a silent resolution change.
    """

    repo: str
    branch: str
    #: The commit ``branch`` pointed at when this entry was last verified.
    observed: str


@dataclasses.dataclass(frozen=True)
class CrateRelease:
    """One crate at one exact published version, or on a branch when none carries what is needed."""

    name: str
    version: str
    #: Set when the pin is a branch. The version stays: it is what the branch's manifest says, and
    #: what a resolved graph will report, so version gaps still read the same.
    git: GitPin | None = None

    def dependency_line(self) -> str:
        """The ``Cargo.toml`` line for this crate — an exact version, not a caret range: the
        reference set is a statement about what was compiled, not a compatibility claim."""
        if self.git is not None:
            return f'{self.name} = {{ git = "{self.git.repo}", branch = "{self.git.branch}" }}'
        return f'{self.name} = "={self.version}"'


@dataclasses.dataclass(frozen=True)
class CrateRequirement:
    """A crate at a version *line* rather than a release — how the platform is named.

    Distinct from :class:`CrateRelease` because the two say different things: a CVLR release is
    the exact thing we compiled, while the platform is a generation whose patch level is the
    target's business. Rendering both the same way would claim a precision we do not have."""

    name: str
    line: str

    def dependency_line(self) -> str:
        return f'{self.name} = "{self.line}"'


@dataclasses.dataclass(frozen=True)
class PathAlias:
    """A path prefix as the canonical tuning files spell it, and this generation's spellings of it.

    Matched as a literal substring of a directive's pattern, so a concept is renamed wherever it
    appears — several upstream directives name two or three of them in one regex.

    ``actual`` is a tuple because a platform split is not always a rename. ``solana-program`` kept a
    real ``invoke_signed_unchecked`` of its own while the one that ends up on the call path is
    ``solana-cpi``'s, so a summary that must cover the concept has to be emitted under both
    spellings. A spelling whose crate the target does not resolve is dropped, which is what keeps
    these safe to declare against a target that predates the split.
    """

    canonical: str
    actual: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class NamespacePattern:
    """A blanket over one crate's whole namespace, widened to the family that replaced that crate.

    The canonical spelling is ``<crate>::.*`` — the pattern upstream writes to set a default for a
    whole layer, as in ``#[inline(never)] ^solana_program::.*$``. On a generation that split the
    monolith into a family, that blanket covers almost nothing: the layer moved to
    ``solana_account_info``, ``solana_pubkey``, ``solana_cpi`` and a dozen more, so the default it was
    setting silently stopped applying to them.

    Two things make this its own type rather than a :class:`PathAlias`.

    It must not touch a path that merely *starts* with the crate:
    ``solana_program::instruction::get_stack_height`` names a function that still lives in the
    monolith, and rewriting it would point a directive at a symbol that does not exist. The literal
    ``.*`` in the canonical spelling is what separates the blanket from every other directive.

    And it is unconditional, where a :class:`PathAlias` is dropped unless the target resolves the
    crate it names. The replacement matches crate *names* rather than naming one crate, so it is a
    superset of the canonical spelling and stays correct on a target that predates the split — which
    is also why it cannot go stale when the next crate is split out.
    """

    canonical: str
    actual: str


@dataclasses.dataclass(frozen=True)
class PlatformGeneration:
    """The chain-platform release line a CVLR chain crate is bound to.

    ``label`` is for humans and for corpus provenance; ``crates`` is what a probe crate must
    declare to name the platform types (``AccountInfo`` and friends) the chain crate expects."""

    label: str
    crates: tuple[CrateRequirement, ...]
    #: The crates whose presence in a *target's* graph reveals which generation it is already on,
    #: most specific first — the scaffold's platform gate resolves the first one it finds and
    #: compares generations.
    #:
    #: A separate list from :attr:`crates` because the two roles disagree at exactly the moment
    #: that matters. :attr:`crates` names what *this* generation declares, so it can only ever
    #: mention crates this generation has; but a target on a *newer* generation is detected
    #: precisely by the crate this one lacks. Solana's v3 split moved ``AccountInfo`` out of
    #: ``solana-program`` and stopped publishing that crate, so a v3 target resolves no
    #: ``solana-program`` at all — and a gate that only asked about ``solana-program`` read that
    #: absence as "the project has no opinion" and waved the target through. Naming the crate that
    #: actually carries the type, and that survived the split, is what makes the answer legible
    #: across it.
    witnesses: tuple[CrateRequirement, ...]
    #: The lowest patch release within this generation the CVLR chain crate accepts, when it asks
    #: for one. ``cvlr-soroban``'s SDK-22 branch requires ``soroban-sdk ^22.0.8``, so a project
    #: *locked* to 22.0.7 is the same generation and still unresolvable — cargo reports that as a
    #: version-selection conflict naming neither side's reason, which is worth pre-empting.
    minimum: str | None = None
    #: How this generation spells the paths the canonical tuning files name, for
    #: :mod:`composer.spec.cvlr.env_paths` to emit. Empty for a generation whose spelling *is* the
    #: canonical one — the files are vendored verbatim from upstream and upstream writes them in the
    #: monolith's spelling, so "no aliases" means "upstream's paths are already right here".
    path_aliases: tuple[PathAlias | NamespacePattern, ...] = ()


@dataclasses.dataclass(frozen=True)
class UnpublishedCapability:
    """Something current practice uses that no published crate provides.

    Recorded rather than silently omitted: the corpus has to be able to say "this is not covered,
    and here is why", and a reader who meets the capability in a real project needs to know it is
    outside the reference set instead of concluding the corpus is merely incomplete."""

    #: Every name the capability has gone by. A rename is exactly the case where searching for one
    #: name and finding nothing reads as "does not exist".
    names: tuple[str, ...]
    #: What is therefore missing from the corpus.
    missing: str


@dataclasses.dataclass(frozen=True)
class ChainReference:
    """What "current CVLR" means for one chain."""

    core: CrateRelease
    #: The chain crate every project on this chain declares.
    chain: CrateRelease
    platform: PlatformGeneration
    #: Chain crates that model one specific on-chain program rather than the chain itself — the SPL
    #: token account model, the stake program's state. Separate from :attr:`chain` because they
    #: answer a narrower question, not because a project gets fewer of them: :meth:`scaffold_crates`
    #: declares all of them.
    #:
    #: **That is the reverse of what this said, and the reason is that nothing can add one later.**
    #: The original argument was that scaffolding every specialization "would add a dependency
    #: nobody uses" — true, and the cost is one compile of a small optional crate behind the
    #: ``certora`` feature. What it weighed that against was wrong: the alternative is not
    #: declaring it on demand, because there is no demand-time. The author writes spec code and has
    #: no manifest-editing tool (deliberately — a dependency changes how the project builds for
    #: everyone, which the scaffold refuses to guess at), and the munge editor's vocabulary is
    #: attributes on program source. So a specialization the scaffold omits is a capability the run
    #: cannot reach at all, and the first target that needed one — a stake pool, whose invariants
    #: are pool-token supply against staked lamports — would have been handed neither of the two
    #: libraries that model what it does.
    specializations: tuple[CrateRelease, ...] = ()
    unpublished: tuple[UnpublishedCapability, ...] = ()

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set — what the corpus was written against."""
        return (self.core, self.chain, *self.specializations)

    def scaffold_crates(self) -> tuple[CrateRelease, ...]:
        """What a fresh project declares in its ``Cargo.toml`` — every crate in the reference set.

        Identical to :meth:`crates` today. Kept as its own method because the two are asking
        different questions — "what was the corpus compiled against" and "what does this project
        pin" — and a future reference set that names a crate no project should declare would need
        them to differ again.
        """
        return self.crates()

    def cargo_dependencies(self) -> str:
        """A ``[dependencies]`` body pinning this reference set, for a probe or scaffold crate.

        The platform crates are included because the CVLR chain crate's public types come from
        them: omitting them leaves a probe unable to *name* what the helpers return."""
        lines = [c.dependency_line() for c in self.crates()]
        lines += [c.dependency_line() for c in self.platform.crates]
        return "\n".join(lines)


#: The core line Solana is pinned to. ``cvlr-spec`` (the ``cvlr_spec!`` / ``cvlr_rules!`` /
#: ``cvlr_lemma!`` machinery) is a dependency of ``cvlr`` rather than a separate declaration, so a
#: target names one crate and gets the parametric-rule layer with it. Soroban pins its own — see
#: :data:`SOROBAN`.
_CORE = CrateRelease("cvlr", "0.6.1")

SOLANA = ChainReference(
    core=_CORE,
    chain=CrateRelease("cvlr-solana", "0.5.0"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.5.0"),
        # The SPL token account model — nondet token accounts and mints, and the token instruction
        # summaries. This was recorded as an *unpublished* capability, on the strength of a real
        # project reaching it through a `[patch.crates-io]` git redirect. That project's own comment
        # says why ("use git dependency until v0.5 ... is released"), and the release happened: it
        # is on crates.io at 0.5.0, the same version as the chain crate it was factored out of.
        CrateRelease("cvlr-spl-token", "0.5.0"),
    ),
    platform=PlatformGeneration(
        label="solana-program 2.x (the last monolithic line)",
        crates=(CrateRequirement("solana-program", "2.2"),),
        # ``solana-account-info`` first: it defines ``AccountInfo`` itself and exists on both 2.x
        # and 3.x, so it answers the question across the split that ``solana-program`` cannot.
        # ``solana-program`` remains as the fallback for the 1.18 line, which predates the split
        # and defines the type inside the monolith.
        witnesses=(
            CrateRequirement("solana-account-info", "2.3"),
            CrateRequirement("solana-program", "2.2"),
        ),
        # Every entry here was checked against a demangled symbol table, not against the crates'
        # documentation: `solana-program` is a *partial* facade, so which side of the split a symbol
        # lives on is a per-symbol fact and reading it off the module was wrong twice.
        path_aliases=(
            # Modules that became whole-crate aliases (`pub use solana_x as x`), so every path
            # under them moved together.
            PathAlias("solana_program::account_info", ("solana_account_info",)),
            PathAlias("solana_program::pubkey", ("solana_pubkey",)),
            PathAlias("solana_program::program_error", ("solana_program_error",)),
            PathAlias("solana_program::program_pack", ("solana_program_pack",)),
            PathAlias("solana_program::rent", ("solana_rent",)),
            PathAlias("solana_program::clock", ("solana_clock",)),
            PathAlias("solana_program::sysvar", ("solana_sysvar",)),
            PathAlias("solana_program::hash", ("solana_hash",)),
            # These two went to one crate that is not named after either of them.
            PathAlias("solana_program::system_program", ("solana_sdk_ids::system_program",)),
            PathAlias("solana_program::incinerator", ("solana_sdk_ids::incinerator",)),
            # `program` is the partial facade. `invoke`, `invoke_signed` and `set_return_data` are
            # real functions there and keep the canonical spelling — they are in the symbol table
            # under it — so only the symbol that moved is aliased, and it is aliased to *both*:
            # `solana-program` still defines one of that name, and the one that ends up on the call
            # path is `solana-cpi`'s.
            PathAlias(
                "solana_program::program::invoke_signed_unchecked",
                (
                    "solana_program::program::invoke_signed_unchecked",
                    "solana_cpi::invoke_signed_unchecked",
                ),
            ),
            # Deliberately absent: `solana_program::instruction::get_stack_height`, a real function
            # in the monolith on this generation, and `solana_program::poseidon`, which the
            # generation does not have under any spelling — no rewrite makes an absent symbol
            # present, and pretending otherwise would hide that the directive is inapplicable.
            #
            # Last, and the one that matters most: the blanket that gives the whole platform layer
            # its never-inline default. It matched two symbols on the first real target this backend
            # was pointed at, because the layer had moved out from under it.
            NamespacePattern("solana_program::.*", "solana_[a-z0-9_]*::.*"),
        ),
    ),
)

#: The CVLR core, for Soroban. ``main`` rather than a release because no published ``cvlr`` builds a
#: Soroban contract — ``cvlr-spec`` is ``#![no_std]`` only on ``main``, and its ``std``
#: ``panic_impl`` collides with ``soroban-sdk``'s in the wasm build. ``main`` also declares
#: ``wasm_import_module = "env"`` on the ``CVT_*`` externs, which a release does not. The core is
#: free of ``soroban-sdk``, so one line serves every SDK generation: what the facade decides is the
#: *language* the author writes, and that is the same everywhere.
_CVLR_MAIN = GitPin(
    repo="https://github.com/Certora/cvlr",
    branch="main",
    observed="f1e3e08bf9c5525f5f8096a52ffe0d69bbe510c7",
)

#: ``cvlr-soroban`` is branched per SDK generation, because its helpers return the SDK's own types
#: and each generation defines its own. So the chain crate — and only the chain crate — is chosen by
#: the project's SDK. A generation with no branch is refused rather than paired with a neighbour:
#: ``Address`` from 23 and ``Address`` from 26 are different types, and mixing them does not warn.
_SOROBAN_CHAIN_BRANCHES: dict[str, str] = {
    "22": "soroban-22.0.8",
    "25": "soroban-25.1.1",
    "26": "main",
}

#: What each branch's own ``soroban-sdk`` requirement demands, at the patch level.
_SOROBAN_MINIMUM: dict[str, str] = {"22": "22.0.8", "25": "25.1.1", "26": "26.1.0"}

_SOROBAN_OBSERVED: dict[str, str] = {
    "22": "d00c0ab7499407bab4be0993176cbaf96d183461",
    "25": "faf7fb826f395cc0573a0ce674b7e4099cdf6f57",
    "26": "70a9ddfcc4fd4ca49ba5c8866a863f0f37b4b61b",
}

_CVLR_SOROBAN_REPO = "https://github.com/Certora/cvlr-soroban"


def _soroban_line(generation: str) -> ChainReference:
    """The reference set for a Soroban project on one SDK generation."""
    pin = GitPin(
        repo=_CVLR_SOROBAN_REPO,
        branch=_SOROBAN_CHAIN_BRANCHES[generation],
        observed=_SOROBAN_OBSERVED[generation],
    )
    return ChainReference(
        core=CrateRelease("cvlr", "0.6.1", git=_CVLR_MAIN),
        chain=CrateRelease("cvlr-soroban", "0.4.0", git=pin),
        # The derive crate is a companion rather than a specialization, but it is declared the same
        # way: a target reaches for it only when it writes the attribute macros.
        specializations=(CrateRelease("cvlr-soroban-derive", "0.4.0", git=pin),),
        platform=PlatformGeneration(
            label=f"soroban-sdk {generation}.x",
            crates=(CrateRequirement("soroban-sdk", generation),),
            # Soroban ships one SDK crate rather than a family, so declaring it and witnessing it
            # are the same crate. Spelled out rather than defaulted: they coincide here as a fact
            # about this platform, not as a rule, and Solana is the proof that the two can diverge.
            witnesses=(CrateRequirement("soroban-sdk", generation),),
            minimum=_SOROBAN_MINIMUM[generation],
        ),
    )


#: One reference set per supported SDK generation, newest last.
SOROBAN_LINES: dict[str, ChainReference] = {
    generation: _soroban_line(generation) for generation in sorted(_SOROBAN_CHAIN_BRANCHES)
}

#: The newest line, for callers with no project in hand — the corpus builders and the crate
#: reference, which describe "current CVLR" rather than what one target resolves.
SOROBAN = SOROBAN_LINES[max(SOROBAN_LINES, key=int)]

#: Keyed by the chain vocabulary of ``composer.pipeline.ecosystem.ChainTag``, minus ``evm`` — CVLR
#: is the Rust-side specification language and has no EVM line.
REFERENCE_SET: dict[str, ChainReference] = {"solana": SOLANA, "soroban": SOROBAN}


def soroban_line_for(sdk_version: str | None) -> ChainReference:
    """The Soroban reference set for a project resolving ``sdk_version``.

    ``None`` — a project that does not resolve ``soroban-sdk`` at all — gets the newest line, and
    the scaffold's platform gate has nothing to disagree with. An unsupported generation raises
    here rather than being paired with a neighbouring branch, because the types differ silently.
    """
    if sdk_version is None:
        return SOROBAN
    generation = sdk_version.split(".", maxsplit=1)[0]
    try:
        return SOROBAN_LINES[generation]
    except KeyError:
        raise ValueError(
            f"no cvlr-soroban branch pairs with soroban-sdk {sdk_version}: the supported "
            f"generations are {', '.join(sorted(SOROBAN_LINES, key=int))}. The chain crate returns "
            f"the SDK's own types, so a neighbouring branch would compile against a different "
            f"`Address` — move the project to a supported generation, or ask for a branch cut "
            f"against this one."
        ) from None


def reference_for(chain: str) -> ChainReference:
    """The reference set for ``chain``, or a message naming the chains that have one.

    Raises rather than returning ``None``: every caller (compile gate, crate reference, scaffold)
    needs an answer to proceed, and a missing chain is a registration bug, not a runtime state."""
    try:
        return REFERENCE_SET[chain]
    except KeyError:
        raise ValueError(
            f"no CVLR reference set for chain {chain!r} (have: {sorted(REFERENCE_SET)}). CVLR is "
            f"the Rust-side language, so EVM has none; a new Rust chain needs an entry here."
        ) from None
