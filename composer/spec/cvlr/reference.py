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

from composer.cargo.features import CargoFeature
from composer.spec.cvlr.forks import ANCHOR_FORK, FIXED_FORK, ForkOverride


#: What keeps the Solana SDK's ``entrypoint!`` from installing its global allocator. The macro's
#: ``custom_heap_default!`` checks the calling crate's ``custom-heap`` feature, in
#: ``solana-program`` 2.x and in ``solana-program-entrypoint`` 3.x. The bump allocator writes the
#: heap at a fixed address. A rule that also allocates through ``__rust_alloc`` then fails with
#: Prover error 3309, "Conflicting allocation/usage of heap memory".
CUSTOM_HEAP_FEATURE = CargoFeature("custom-heap")


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
    #: A crate listed at several lines is on the generation at any of them: a framework whose
    #: account type kept its shape across releases is one generation over all of them.
    witnesses: tuple[CrateRequirement, ...]
    #: How this generation spells the paths in the starting tuning files.
    #: :mod:`composer.spec.cvlr.env_paths` applies these. Empty when this generation's spelling
    #: is already the one the starting layers use, which is the monolith's.
    path_aliases: tuple[PathAlias, ...] = ()
    #: The feature of the calling crate that keeps this generation's ``entrypoint!`` macro from
    #: installing its global allocator, or ``None`` when the macro has no such switch. The scaffold
    #: turns it on for a program that calls the macro and has no ``no-entrypoint`` feature
    #: (:func:`composer.spec.cvlr.scaffold._entrypoint_switch`).
    heap_opt_out: CargoFeature | None = None


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
class HarnessShim:
    """A module of the harness that binds the CVLR core to a framework's account type, written by
    the scaffold because no CVLR chain crate is published for the framework.

    The scaffold writes it from ``harness_files/<template>.template.rs`` into
    ``src/certora/<module>.rs``, beside ``specs``. It compiles against the program's own copy of
    the framework, so it follows whatever release the program resolves, which a published crate
    could not do for a framework that is distributed as git dependencies.
    """

    #: The module's name under ``src/certora/``: what a rule's ``use`` line names.
    module: str
    #: The stem of the module's template under ``harness_files/``. Generations of one framework
    #: share a module name and differ in what the module assumes of an account's header.
    template: str
    #: The crate the module's ``use`` lines name. The program must depend on it directly: a crate
    #: can only name its own dependencies.
    requires: str

    def describe(self) -> str:
        return f"the scaffold's {self.module} harness module"


#: What binds the CVLR core to a platform's account type: a published chain crate, or a module the
#: scaffold writes.
type ChainBinding = CrateRelease | HarnessShim


@dataclass(frozen=True)
class ChainReference:
    """What current CVLR means for one chain."""

    core: CrateRelease
    #: The cvlr chain crate every project on this generation declares, or the harness module that
    #: stands in for one.
    binding: ChainBinding
    platform: PlatformGeneration
    #: Certora's forks of crates this chain's programs depend on, which a target is redirected at
    #: when it resolves them (:mod:`composer.spec.cvlr.forks`). Empty for a chain with none.
    forks: tuple[ForkOverride, ...]
    #: Chain crates that model one on-chain program rather than the chain itself: the SPL token
    #: account model, the stake program's state. Narrower than :attr:`binding`, and still
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
        match self.binding:
            case CrateRelease(name=name, version=version):
                bound = f"{name} {version}"
            case HarnessShim() as shim:
                bound = shim.describe()
        return f"{self.core.name} {self.core.version} with {bound}"

    @property
    def shim(self) -> HarnessShim | None:
        """The harness module the scaffold writes for this generation, if its binding is one."""
        return self.binding if isinstance(self.binding, HarnessShim) else None

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set: the line this build supports."""
        chain = (self.binding,) if isinstance(self.binding, CrateRelease) else ()
        return (self.core, *chain, *self.specializations)

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

#: ``cvlr-solana`` 0.5.2, which reads account flags without branching (each nondet account used to
#: add 2^3 paths to a rule). The three crates of that repository are released together, at one
#: version.
SOLANA_V2 = ChainReference(
    core=_CORE,
    binding=CrateRelease("cvlr-solana", "0.5.2"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.5.2"),
        # The SPL token account model: nondet token accounts and mints, and the token instruction
        # summaries.
        CrateRelease("cvlr-spl-token", "0.5.2"),
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
        heap_opt_out=CUSTOM_HEAP_FEATURE,
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

#: ``cvlr-solana`` 0.6.1, for the split ``solana-*`` 3.x crates: 0.5.2 ported to them.
SOLANA_V3 = replace(
    SOLANA_V2,
    binding=CrateRelease("cvlr-solana", "0.6.1"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.6.1"),
        CrateRelease("cvlr-spl-token", "0.6.1"),
    ),
    platform=PlatformGeneration(
        # Every platform type lives in its own crate.
        label="solana-* 3.x",
        sdk_crates=(CrateRequirement("solana-account-info", "3"),),
        witnesses=(CrateRequirement("solana-account-info", "3.0"),),
        heap_opt_out=CUSTOM_HEAP_FEATURE,
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

#: ``cvlr-solana`` 0.4.7, for ``solana-program`` 1.18, with the same account-flag fix. It requires
#: ``cvlr-*`` ``>=0.4, <1``, so cargo resolves it onto the shared 0.6.2 core, one copy of each core
#: crate.
SOLANA_V1_18 = ChainReference(
    core=_CORE,
    binding=CrateRelease("cvlr-solana", "0.4.7"),
    # No specializations. The 0.4 line has no stake model, and its token helpers are in the chain
    # crate. It has no SPL Token CPI model either: a token CPI reaches ``invoke_signed_unchecked``,
    # which the core summaries make opaque.
    forks=(ANCHOR_FORK, FIXED_FORK),
    platform=PlatformGeneration(
        label="solana-program 1.18",
        sdk_crates=(CrateRequirement("solana-program", "1.18"),),
        witnesses=(CrateRequirement("solana-program", "1.18"),),
        heap_opt_out=CUSTOM_HEAP_FEATURE,
        # No aliases: the monolith's spelling is the one the starting tuning files use.
    ),
)

#: Programs written on igneous-labs' jiminy (https://github.com/igneous-labs/jiminy), a framework
#: that replaces the SDK: its own entrypoint, its own account type (``AccountHandle``, read through
#: the ``Abr`` borrow registry) and its own syscall bindings. It is distributed as git dependencies
#: only, and the ``jiminy`` crates on crates.io are an unrelated project. A published chain crate
#: could not name its types, so the account helpers are a module the scaffold writes against the
#: program's own copy, reached through ``jiminy-entrypoint``'s re-export of ``jiminy-account``.
SOLANA_JIMINY = ChainReference(
    core=_CORE,
    binding=HarnessShim(module="jiminy", template="jiminy", requires="jiminy-entrypoint"),
    forks=(FIXED_FORK,),
    platform=PlatformGeneration(
        label="jiminy",
        # jiminy is not published, so there is no requirement a probe crate could declare.
        sdk_crates=(),
        # Every jiminy program declares its entrypoint crate, which links `jiminy-account`.
        witnesses=(CrateRequirement("jiminy-entrypoint", "0.1"),),
        # jiminy's `entrypoint!` installs its allocator unconditionally.
        heap_opt_out=None,
    ),
)

#: Programs written on Pinocchio (https://github.com/anza-xyz/pinocchio), a framework that replaces
#: the SDK: its own entrypoint, account type and syscall bindings. It is on crates.io, but its
#: account type has changed shape across 0.x releases, so the account helpers are a module the
#: scaffold writes against the program's own copy rather than a chain crate pinned to one release.
#: Each generation below is a shape of the account header the module has to describe: the type,
#: and what the entrypoint leaves in the four bytes after the flags.
#:
#: 0.11 onwards: ``AccountView`` (``solana-account-view``). Under the ``account-resize`` feature the
#: entrypoint stores the original data length in those bytes, and nothing reads them otherwise.
SOLANA_PINOCCHIO_0_11 = ChainReference(
    core=_CORE,
    binding=HarnessShim(module="pinocchio", template="pinocchio_0_11", requires="pinocchio"),
    forks=(FIXED_FORK,),
    platform=PlatformGeneration(
        label="pinocchio 0.11+",
        sdk_crates=(),
        witnesses=(CrateRequirement("pinocchio", "0.12"), CrateRequirement("pinocchio", "0.11")),
        # Pinocchio's `entrypoint!` installs its allocator unconditionally.
        heap_opt_out=None,
    ),
)

#: 0.10: ``AccountView`` with a ``resize_delta`` the runtime hands over at zero.
SOLANA_PINOCCHIO_0_10 = ChainReference(
    core=_CORE,
    binding=HarnessShim(module="pinocchio", template="pinocchio_0_10", requires="pinocchio"),
    forks=(FIXED_FORK,),
    platform=PlatformGeneration(
        label="pinocchio 0.10",
        sdk_crates=(),
        witnesses=(CrateRequirement("pinocchio", "0.10"),),
        heap_opt_out=None,
    ),
)

#: 0.8 and 0.9: ``pinocchio::account_info::AccountInfo``, whose header is private to Pinocchio, so
#: the module builds it by layout. The four bytes are zero at entry: 0.8 reads them as an unset
#: original length, 0.9 as a resize delta.
SOLANA_PINOCCHIO_0_8 = ChainReference(
    core=_CORE,
    binding=HarnessShim(module="pinocchio", template="pinocchio_0_8", requires="pinocchio"),
    forks=(FIXED_FORK,),
    platform=PlatformGeneration(
        label="pinocchio 0.8-0.9",
        sdk_crates=(),
        witnesses=(CrateRequirement("pinocchio", "0.9"), CrateRequirement("pinocchio", "0.8")),
        heap_opt_out=None,
    ),
)

SOROBAN = ChainReference(
    core=_CORE,
    binding=CrateRelease("cvlr-soroban", "0.4.0"),
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
#: One reference set per platform generation, each framework's newest first. A chain crate works
#: only with its own generation's ``AccountInfo``, so a target is set up with the generation it is
#: on (:func:`composer.spec.cvlr.scaffold.select_generation`).
#:
#: A framework's generations come before the SDK's. A program that links a framework's entrypoint
#: takes that framework's account type in its handlers, whatever SDK crates its dependencies also
#: link.
REFERENCE_SET: dict[str, tuple[ChainReference, ...]] = {
    "solana": (
        SOLANA_JIMINY,
        SOLANA_PINOCCHIO_0_11,
        SOLANA_PINOCCHIO_0_10,
        SOLANA_PINOCCHIO_0_8,
        SOLANA_V3,
        SOLANA_V2,
        SOLANA_V1_18,
    ),
    "soroban": (SOROBAN,),
}


def generations(chain: str) -> tuple[ChainReference, ...]:
    """The reference sets for ``chain``, one per supported platform generation, in the order
    :data:`REFERENCE_SET` gives.

    Raises when ``chain`` has none. Callers need an answer to proceed, and a missing chain is a
    registration bug. The error names the chains that have a set. CVLR has no EVM line."""
    try:
        return REFERENCE_SET[chain]
    except KeyError:
        raise ValueError(
            f"no CVLR reference set for chain {chain!r} (have: {sorted(REFERENCE_SET)}). CVLR is "
            f"the Rust-side language, so EVM has none; a new Rust chain needs an entry here."
        ) from None
