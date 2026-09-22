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
can pair a new core with an old chain crate. A bump is an edit here.

A chain crate is bound to one platform generation, and each generation has its own ``AccountInfo``.
``cvlr-solana`` 0.4.x goes with ``solana-program`` 1.18, 0.5.0 with 2.2, and the unreleased 0.6
line with the ``solana-*`` v3 crates. A helper from one generation cannot be passed an account
from another, so :attr:`ChainReference.platform` is part of the reference, not a detail of one
target.

``solana-program`` stopped defining the platform types at 2.2, not at 3.0. 1.17 and 1.18 have a
real ``account_info`` module. 2.2.1, 2.3.0, and 3.0.0 re-export ``solana-account-info``. A path
written ``solana_program::account_info::AccountInfo`` then names a re-export. The path a demangled
symbol carries is ``solana_account_info::AccountInfo``. :class:`PathAlias` is that difference, for
the tuning files.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CrateRelease:
    """One crate at one published version."""

    name: str
    version: str

    def dependency_line(self) -> str:
        """A ``Cargo.toml`` dependency line. Exact (``=version``), not a caret range.

        The reference set says what was compiled, not which later releases are compatible.
        """
        return f'{self.name} = "={self.version}"'


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
class NamespacePattern:
    """A blanket over one crate's whole namespace, widened to the family that replaced that crate.

    The canonical spelling is ``<crate>::.*``, the pattern the starting layers use for a whole
    layer, as in ``#[inline(never)] ^solana_program::.*$``. After the monolith split, that layer
    lives in ``solana_account_info``, ``solana_pubkey``, ``solana_cpi``, and others, so the blanket
    matches almost nothing and the default stops applying.

    This is not a :class:`PathAlias`, for two reasons.

    It must not rewrite a path that merely starts with the crate.
    ``solana_program::instruction::get_stack_height`` is still a function in the monolith, and
    rewriting it would name a symbol that does not exist. The literal ``.*`` is what marks a
    blanket.

    It is also unconditional. A :class:`PathAlias` is dropped unless the target resolves the crate
    it names. This replacement matches crate names, so it covers the canonical spelling and stays
    correct on a target that predates the split, and it does not go stale when another crate is
    split out.
    """

    canonical: str
    actual: str


@dataclass(frozen=True)
class PlatformGeneration:
    """The chain-platform release line a CVLR chain crate is bound to.

    ``label`` is for people and for corpus provenance. ``crates`` is what a probe crate declares
    so it can name the platform types (``AccountInfo`` and the rest) the chain crate uses."""

    label: str
    crates: tuple[CrateRequirement, ...]
    #: Crates whose presence in a target's graph says which generation it is on, most specific
    #: first. The scaffold's platform gate uses the first one the target resolves.
    #:
    #: Separate from :attr:`crates`. That list is what this generation declares, so it can only
    #: name crates this generation has. A newer generation is recognized by a crate this one
    #: lacks. Solana v3 moved ``AccountInfo`` out of ``solana-program`` and stopped publishing
    #: that crate, so a v3 target resolves no ``solana-program``. A gate that only asked about
    #: ``solana-program`` would read the absence as "no opinion" and pin this generation's CVLR
    #: against it. The witness is the crate that still defines the type.
    witnesses: tuple[CrateRequirement, ...]
    #: How this generation spells the paths in the starting tuning files.
    #: :mod:`composer.spec.cvlr.env_paths` applies these. Empty when this generation's spelling
    #: is already the one the starting layers use, which is the monolith's.
    path_aliases: tuple[PathAlias | NamespacePattern, ...] = ()


@dataclass(frozen=True)
class UnpublishedCapability:
    """Something current practice uses that no published crate provides.

    Recorded so the corpus can say it is uncovered, and why. A reader who meets the capability
    in a project should see that it is outside the reference set.
    """

    #: Every name the capability has gone by. A rename is the case where searching for one
    #: name and finding nothing looks like absence.
    names: tuple[str, ...]
    #: What the corpus therefore does not cover.
    missing: str


@dataclass(frozen=True)
class ChainReference:
    """What current CVLR means for one chain."""

    core: CrateRelease
    #: The chain crate every project on this chain declares.
    chain: CrateRelease
    platform: PlatformGeneration
    #: Chain crates that model one on-chain program rather than the chain itself: the SPL token
    #: account model, the stake program's state. Narrower than :attr:`chain`, and still declared.
    #: :meth:`scaffold_crates` includes them.
    #:
    #: They are optional crates behind the ``certora`` feature, so a project that never calls them
    #: pays one extra compile. They are still pinned here. The scaffold is what writes
    #: dependencies, and it does not add one later. A dependency changes how the project builds
    #: for everyone, which the scaffold does not guess at. A specialization left out of this list
    #: is a crate the project cannot name.
    specializations: tuple[CrateRelease, ...] = ()
    unpublished: tuple[UnpublishedCapability, ...] = ()

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set. This is what the corpus was written against."""
        return (self.core, self.chain, *self.specializations)

    def scaffold_crates(self) -> tuple[CrateRelease, ...]:
        """What a fresh project declares in its ``Cargo.toml``.

        The same crates as :meth:`crates`. The two names are the two questions: what the corpus
        was compiled against, and what this project pins.
        """
        return self.crates()

    def cargo_dependencies(self) -> str:
        """A ``[dependencies]`` body pinning this reference set, for a probe or scaffold crate.

        The platform crates are included because the chain crate's public types come from them.
        Without them a probe cannot name what the helpers return."""
        lines = [c.dependency_line() for c in self.crates()]
        lines += [c.dependency_line() for c in self.platform.crates]
        return "\n".join(lines)


#: The core line, shared by every chain. ``cvlr-spec`` (``cvlr_spec!``, ``cvlr_rules!``,
#: ``cvlr_lemma!``) is a dependency of ``cvlr``, so a target names one crate and gets the
#: parametric-rule layer with it.
_CORE = CrateRelease("cvlr", "0.6.1")

SOLANA = ChainReference(
    core=_CORE,
    chain=CrateRelease("cvlr-solana", "0.5.0"),
    specializations=(
        CrateRelease("cvlr-solana-stake", "0.5.0"),
        # The SPL token account model: nondet token accounts and mints, and the token instruction
        # summaries. On crates.io at 0.5.0, the same version as the chain crate it was split from.
        CrateRelease("cvlr-spl-token", "0.5.0"),
    ),
    platform=PlatformGeneration(
        label="solana-program 2.x (the last monolithic line)",
        crates=(CrateRequirement("solana-program", "2.2"),),
        # ``solana-account-info`` first: it defines ``AccountInfo`` and exists on both 2.x and 3.x.
        # ``solana-program`` is the fallback for 1.18, which predates the split and defines the
        # type inside the monolith.
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
            # Not aliased: `solana_program::instruction::get_stack_height` is still a function in
            # the monolith on this generation, and `solana_program::poseidon` does not exist here
            # under any spelling. Rewriting either would hide a directive that does not apply.
            #
            # The blanket that sets the platform layer's never-inline default. ``solana_program::.*``
            # only matches what stayed in the monolith. The replacement covers the split crates too.
            NamespacePattern("solana_program::.*", "solana_[a-z0-9_]*::.*"),
        ),
    ),
)

SOROBAN = ChainReference(
    core=_CORE,
    chain=CrateRelease("cvlr-soroban", "0.4.0"),
    # The derive crate is a companion of the chain crate. A target uses it when it writes the
    # attribute macros. It is declared the same way as a specialization.
    specializations=(CrateRelease("cvlr-soroban-derive", "0.4.0"),),
    platform=PlatformGeneration(
        label="soroban-sdk 22.x",
        crates=(CrateRequirement("soroban-sdk", "22"),),
        # Soroban has one SDK crate, so the declared crate and the witness are the same. Spelled
        # out because that is a fact about this platform. On Solana the witness list names a crate
        # this generation does not declare.
        witnesses=(CrateRequirement("soroban-sdk", "22"),),
    ),
)

#: Keyed by ``composer.pipeline.ecosystem.ChainTag``, minus ``evm``. CVLR is the Rust-side
#: specification language and has no EVM line.
REFERENCE_SET: dict[str, ChainReference] = {"solana": SOLANA, "soroban": SOROBAN}


def reference_for(chain: str) -> ChainReference:
    """The reference set for ``chain``.

    Raises when ``chain`` has none. Callers need an answer to proceed, and a missing chain is a
    registration bug. The error names the chains that have a set. CVLR has no EVM line."""
    try:
        return REFERENCE_SET[chain]
    except KeyError:
        raise ValueError(
            f"no CVLR reference set for chain {chain!r} (have: {sorted(REFERENCE_SET)}). CVLR is "
            f"the Rust-side language, so EVM has none; a new Rust chain needs an entry here."
        ) from None
