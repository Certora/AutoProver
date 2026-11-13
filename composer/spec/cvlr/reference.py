"""Which published CVLR releases count as current, for each chain.

CVLR is published as several crates, in layers. The core crate, ``cvlr``, is the part that does
not depend on any chain: the specification language, plus the parametric-rule macros it gets from
``cvlr-spec``. A chain crate (``cvlr-solana``, ``cvlr-soroban``) binds the core to one chain and
supplies the helpers that work with that chain's platform types. Specializations are narrower
crates that go with one chain crate. Most of them model a single on-chain program rather than the
whole chain: ``cvlr-spl-token`` models SPL token accounts and ``cvlr-solana-stake`` models the
stake program. Soroban's derive-macro crate is counted here as well. A project declares the core,
its chain's crate, and that chain's specializations. A platform generation is the release line of
the chain's own SDK that a chain crate is built against, such as ``solana-program`` 2.x or
``soroban-sdk`` 22.x.

Exact versions, not ranges. The core and the chain crates are versioned separately, so "latest"
can pair a new core with an old chain crate. A bump is an edit here, and it moves every project
this build sets up. Each platform generation has one CVLR line.
:mod:`composer.spec.cvlr.scaffold` refuses a project that is already on a different line.

A chain crate is bound to one platform generation, and each generation has its own ``AccountInfo``.
``cvlr-solana`` 0.4.x goes with ``solana-program`` 1.18, 0.5.x with 2.2, and 0.6.x with the
``solana-*`` v3 crates. A helper from one generation cannot be passed an account from another, so
:attr:`ChainReference.platform` is part of the reference, not a detail of one target, and a chain
has one reference set per generation it supports (:data:`REFERENCE_SET`).

``solana-program`` stopped defining the platform types at 2.2, not at 3.0. 1.17 and 1.18 have a
real ``account_info`` module. 2.2.1, 2.3.0, and 3.0.0 re-export ``solana-account-info``. A path
written ``solana_program::account_info::AccountInfo`` then names a re-export. The path a demangled
symbol carries is ``solana_account_info::AccountInfo``. :class:`PathAlias` is that difference, for
the tuning files.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace

from composer.spec.cvlr.forks import ANCHOR_FORK, FIXED_FORK, ForkOverride


@dataclass(frozen=True)
class GitCommit:
    """A commit cargo fetches in place of a crates.io release.

    The project still declares the crates.io requirement (``=version``). A ``[patch.crates-io]``
    entry points the fetch at this commit. Cargo applies the patch only when the commit's crate
    version meets that requirement. The revision is a commit hash, because a tag or a branch can
    move.
    """

    repo: str
    rev: str

    def patch_body(self) -> tuple[tuple[str, str], ...]:
        return (("git", self.repo), ("rev", self.rev))

    def patch_line(self, crate: str) -> str:
        return f'{crate} = {{ git = "{self.repo}", rev = "{self.rev}" }}'


@dataclass(frozen=True)
class CrateRelease:
    name: str
    version: str
    #: ``None`` when the release is on crates.io. Otherwise the commit cargo fetches.
    commit: GitCommit | None = None

    def dependency_line(self) -> str:
        """A ``Cargo.toml`` dependency line. Exact (``=version``), not a caret range.

        The reference set says what was compiled, not which later releases are compatible.
        """
        return f'{self.name} = "={self.version}"'


@dataclass(frozen=True)
class CommitPatch:
    """One ``[patch.crates-io]`` entry."""

    name: str
    version: str
    commit: GitCommit


def commit_patches(crates: Iterable[CrateRelease]) -> tuple[CommitPatch, ...]:
    """Patches for the releases that name a commit. Empty when every release is on crates.io."""
    return tuple(
        CommitPatch(crate.name, crate.version, crate.commit)
        for crate in crates
        if crate.commit is not None
    )


@dataclass(frozen=True)
class CrateRequirement:
    """A crate at a version line, which is how a platform generation is named.

    A CVLR release is the exact crate that was compiled. The platform is a generation, and the
    patch level belongs to the target. An exact pin here would claim a patch that was never compiled.
    """

    name: str
    line: str

    def dependency_line(self) -> str:
        return f'{self.name} = "{self.line}"'


@dataclass(frozen=True)
class PathAlias:
    """A path prefix as the canonical tuning files spell it, and this generation's spellings of it.

    Matched as a literal substring of a directive's pattern, so a concept is renamed wherever it
    appears — several starting directives name two or three of them in one regex.

    ``actual`` is a tuple because a split is not always a rename. ``solana-program`` kept its own
    ``invoke_signed_unchecked``, and the one on the call path is ``solana-cpi``'s, so a summary of
    the concept is emitted under both spellings. A spelling whose crate the target does not
    resolve is dropped, which is what makes these safe on a target that predates the split.
    """

    canonical: str
    actual: tuple[str, ...]


@dataclass(frozen=True)
class PlatformGeneration:
    """The chain-platform release line a CVLR chain crate is bound to.

    The CVLR crates the reference set pins work only with this generation. :attr:`witnesses` is
    how a target is checked to be on it, :attr:`path_aliases` is how it spells the paths in the
    tuning files, and :attr:`sdk_crates` is what a crate built against it declares.

    ``label`` is for people: the platform refusal names it, and so does the tuning-path log line.
    ``sdk_crates`` are the chain platform's own crates (``solana-program``, ``soroban-sdk``).
    """

    label: str
    sdk_crates: tuple[CrateRequirement, ...]
    #: The platform crates that define the types the chain crate's API uses, such as Solana's
    #: ``AccountInfo``. When those types have moved between crates across generations, every
    #: crate they have lived in is listed, most recent first.
    #:
    #: These are read from a target's resolved graph, never declared. The scaffold's platform gate
    #: takes the first one the target resolves and compares its version against this generation.
    witnesses: tuple[CrateRequirement, ...]
    #: How this generation spells the paths in the starting tuning files.
    #: :mod:`composer.spec.cvlr.env_paths` applies these. Empty when this generation's spelling
    #: is already the one the starting layers use, which is the monolith's.
    path_aliases: tuple[PathAlias, ...] = ()


@dataclass(frozen=True)
class UnpublishedCapability:
    """Something current practice uses that no published crate provides.

    Recorded here rather than left out, so a reader who meets the capability in a project can see
    that it is outside what this build can pin: nothing publishes it, so there is no release to
    name. Deliberately not a :class:`~composer.spec.cvlr.crates.Mismatched` — that is a
    disagreement about *which* release, and this is the absence of one to disagree about.
    """

    #: Every name the capability has gone by. A rename is the case where searching for one
    #: name and finding nothing looks like absence.
    names: tuple[str, ...]
    #: What a project reaching for it therefore finds nothing for.
    missing: str


@dataclass(frozen=True)
class ChainReference:
    """What current CVLR means for one chain."""

    core: CrateRelease
    #: The cvlr chain crate every project on this chain declares.
    chain_crate: CrateRelease
    platform: PlatformGeneration
    #: Certora's forks of crates this chain's programs depend on, which a target is redirected at
    #: when it resolves them (:mod:`composer.spec.cvlr.forks`). Empty for a chain with none.
    forks: tuple[ForkOverride, ...]
    #: Chain crates that model one on-chain program rather than the chain itself: the SPL token
    #: account model, the stake program's state. Narrower than :attr:`chain_crate`, and still
    #: declared. :meth:`scaffold_crates` includes them.
    #:
    #: They are optional crates behind the ``certora`` feature, so a project that never calls them
    #: pays one extra compile. They are still pinned here. The scaffold is what writes
    #: dependencies, and it does not add one later. A dependency changes how the project builds
    #: for everyone, which the scaffold does not guess at. A specialization left out of this list
    #: is a crate the project cannot name.
    specializations: tuple[CrateRelease, ...] = ()
    unpublished: tuple[UnpublishedCapability, ...] = ()

    def line(self) -> str:
        """The supported line as a refusal names it to a project author: core and chain crate."""
        return (
            f"{self.core.name} {self.core.version} with "
            f"{self.chain_crate.name} {self.chain_crate.version}"
        )

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set: the line this build supports."""
        return (self.core, self.chain_crate, *self.specializations)

    def scaffold_crates(self) -> tuple[CrateRelease, ...]:
        """What a fresh project declares in its ``Cargo.toml``.

        The same crates as :meth:`crates` today. Two names because they answer two questions, and
        the questions are asked in different places: :func:`composer.spec.cvlr.scaffold._check_pins`
        gates on which releases this build supports, and the manifest planning writes which of them
        a project is given.
        """
        return self.crates()

    def cargo_dependencies(self) -> str:
        """A ``[dependencies]`` body pinning this reference set, for a probe crate.

        The platform crates are included because the chain crate's public types come from them.
        Without them a probe cannot name what the helpers return."""
        lines = [c.dependency_line() for c in self.crates()]
        lines += [c.dependency_line() for c in self.platform.sdk_crates]
        return "\n".join(lines)

    def from_commits(self) -> tuple[CommitPatch, ...]:
        return commit_patches(self.crates())


#: The core line, shared by every chain. ``cvlr-spec`` (``cvlr_spec!``, ``cvlr_rules!``,
#: ``cvlr_lemma!``) is a dependency of ``cvlr``, so a target names one crate and gets the
#: parametric-rule layer with it.
_CORE = CrateRelease("cvlr", "0.6.2")

#: ``cvlr-solana`` 0.5.1. The three crates of that repository are released together, at one version.
SOLANA_V2 = ChainReference(
    core=_CORE,
    chain_crate=CrateRelease("cvlr-solana", "0.5.1"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.5.1"),
        # The SPL token account model: nondet token accounts and mints, and the token instruction
        # summaries.
        CrateRelease("cvlr-spl-token", "0.5.1"),
    ),
    forks=(ANCHOR_FORK, FIXED_FORK),
    platform=PlatformGeneration(
        # The last line published as one ``solana-program`` crate. v3 is only the split crates.
        label="solana-program 2.x",
        sdk_crates=(CrateRequirement("solana-program", "2.2"),),
        # ``solana-account-info`` comes first: it defines ``AccountInfo``, and both 2.x and 3.x have
        # the crate. ``solana-program`` covers a target that still defines the type in the monolith.
        # A 1.x copy is another generation of that witness, so the platform gate refuses the 2.x line.
        witnesses=(
            CrateRequirement("solana-account-info", "2.3"),
            CrateRequirement("solana-program", "2.2"),
        ),
        # Checked against a demangled symbol table. ``solana-program`` is a partial facade, so
        # which side of the split a symbol lives on is per symbol, not per module.
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
            # `program` is the partial facade. `invoke`, `invoke_signed`, and `set_return_data`
            # are real functions there and stay under the canonical spelling. Only the symbol
            # that moved is aliased, and it is aliased to both: `solana-program` still defines
            # one of that name, and the one on the call path is `solana-cpi`'s.
            PathAlias(
                "solana_program::program::invoke_signed_unchecked",
                (
                    "solana_program::program::invoke_signed_unchecked",
                    "solana_cpi::invoke_signed_unchecked",
                ),
            ),
            # `solana_program::instruction::get_stack_height` stays in the monolith on this
            # generation, and `solana_program::poseidon` does not exist here, so neither is aliased.
            # Rewriting either would point a directive at a symbol that is not there.
            #
            # The never-inline blanket `solana_program::.*` matches only what stayed in the
            # monolith, so the split crates are analyzed. A non-inlined function is external: an
            # arbitrary return value, and no writes. Widening the blanket made
            # `Sysvar::from_account_info` never return `Ok`, and made `solana_stake_interface`
            # an oracle.
        ),
    ),
)

#: ``cvlr-solana`` 0.6.0, for the split ``solana-*`` 3.x crates.
SOLANA_V3 = replace(
    SOLANA_V2,
    chain_crate=CrateRelease("cvlr-solana", "0.6.0"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.6.0"),
        CrateRelease("cvlr-spl-token", "0.6.0"),
    ),
    platform=PlatformGeneration(
        # Every platform type lives in its own crate.
        label="solana-* 3.x",
        sdk_crates=(CrateRequirement("solana-account-info", "3"),),
        witnesses=(CrateRequirement("solana-account-info", "3.0"),),
        path_aliases=(
            PathAlias("solana_program::account_info", ("solana_account_info",)),
            # `Pubkey` is `solana-address`'s `Address`, which `solana-pubkey` re-exports. The
            # symbol alias first: the module alias it sits inside would otherwise rewrite it to a
            # type that does not exist.
            PathAlias("solana_program::pubkey::Pubkey", ("solana_address::Address",)),
            PathAlias("solana_program::pubkey", ("solana_address",)),
            PathAlias("solana_program::program_error", ("solana_program_error",)),
            PathAlias("solana_program::program_pack", ("solana_program_pack",)),
            PathAlias("solana_program::rent", ("solana_rent",)),
            PathAlias("solana_program::clock", ("solana_clock",)),
            PathAlias("solana_program::sysvar", ("solana_sysvar",)),
            PathAlias("solana_program::hash", ("solana_hash",)),
            PathAlias("solana_program::system_program", ("solana_sdk_ids::system_program",)),
            PathAlias("solana_program::incinerator", ("solana_sdk_ids::incinerator",)),
            # Only `solana_cpi::invoke_signed_unchecked` exists.
            PathAlias(
                "solana_program::program::invoke_signed_unchecked",
                ("solana_cpi::invoke_signed_unchecked",),
            ),
            # `invoke`, `invoke_signed`, and `set_return_data` live in `solana-cpi`.
            PathAlias("solana_program::program", ("solana_cpi",)),
            PathAlias(
                "solana_program::instruction::get_stack_height",
                ("solana_instruction::syscalls::get_stack_height",),
            ),
            PathAlias("solana_program::poseidon", ("solana_poseidon",)),
            # The never-inline blanket `solana_program::.*` is left as written. It matches nothing
            # on this generation, so the split crates are analyzed. `solana_get_sysvar::get_sysvar`,
            # which `StakeHistorySysvar` calls, needs no alias: the syscall under it is modelled.
            #
            # Anchor 1.1.0 moved `anchor_lang::error` into the `anchor-lang-error` crate. The
            # anchor blanket `^.*anchor_lang.*$` still matches that crate, so the `Error`
            # conversions stay opaque without this alias. An opaque conversion on a constructor's
            # error path leaves the `Ok` payload's pointers without provenance, and the first
            # dereference fails with [3003]. Anchor 1.0.x has no `anchor-lang-error` crate, so the
            # canonical spelling stays.
            PathAlias("anchor_lang::error::", ("anchor_lang_error::",)),
        ),
    ),
)

#: ``cvlr-solana`` 0.4.6, for ``solana-program`` 1.18. It requires ``cvlr-*`` ``>=0.4, <1``, so
#: cargo resolves it onto the shared 0.6.2 core, one copy of each core crate.
SOLANA_V1_18 = ChainReference(
    core=_CORE,
    chain_crate=CrateRelease("cvlr-solana", "0.4.6"),
    # No specializations. The 0.4 line has no stake model, and its token helpers are in the chain
    # crate. It has no SPL Token CPI model either: a token CPI reaches ``invoke_signed_unchecked``,
    # which the core summaries make opaque.
    forks=(ANCHOR_FORK, FIXED_FORK),
    platform=PlatformGeneration(
        label="solana-program 1.18",
        sdk_crates=(CrateRequirement("solana-program", "1.18"),),
        witnesses=(CrateRequirement("solana-program", "1.18"),),
        # No aliases: the monolith's spelling is the one the starting tuning files use.
    ),
)

SOROBAN = ChainReference(
    core=_CORE,
    chain_crate=CrateRelease("cvlr-soroban", "0.4.0"),
    # The derive crate is a companion of the chain crate. A target uses it when it writes the
    # attribute macros. It is declared the same way as a specialization.
    specializations=(CrateRelease("cvlr-soroban-derive", "0.4.0"),),
    forks=(),
    platform=PlatformGeneration(
        label="soroban-sdk 22.x",
        sdk_crates=(CrateRequirement("soroban-sdk", "22"),),
        # One SDK crate, so the declared crate and the witness are the same.
        witnesses=(CrateRequirement("soroban-sdk", "22"),),
    ),
)

#: Keyed by ``composer.pipeline.ecosystem.ChainTag``, minus ``evm``. CVLR is the Rust-side
#: specification language and has no EVM line.
#:
#: One reference set per platform generation, newest first. A chain crate works only with its own
#: generation's ``AccountInfo``, so a target is set up with the generation it is on
#: (:func:`composer.spec.cvlr.scaffold.select_generation`).
REFERENCE_SET: dict[str, tuple[ChainReference, ...]] = {
    "solana": (SOLANA_V3, SOLANA_V2, SOLANA_V1_18),
    "soroban": (SOROBAN,),
}


def generations(chain: str) -> tuple[ChainReference, ...]:
    """The reference sets for ``chain``, one per supported platform generation, newest first.

    Raises when ``chain`` has none. Callers need an answer to proceed, and a missing chain is a
    registration bug. The error names the chains that have a set. CVLR has no EVM line."""
    try:
        return REFERENCE_SET[chain]
    except KeyError:
        raise ValueError(
            f"no CVLR reference set for chain {chain!r} (have: {sorted(REFERENCE_SET)}). CVLR is "
            f"the Rust-side language, so EVM has none; a new Rust chain needs an entry here."
        ) from None
