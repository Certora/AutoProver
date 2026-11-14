"""Which published CVLR releases count as current, for each chain.

CVLR is published as several crates, in layers. The core crate, ``cvlr``, is the part that does
not depend on any chain: the specification language, plus the parametric-rule macros it gets from
``cvlr-spec``. A chain crate (``cvlr-solana``, ``cvlr-soroban``) binds the core to one chain and
supplies the helpers that work with that chain's platform types. Specializations are narrower
crates that go with one chain crate. Most of them model a single on-chain program rather than the
whole chain: ``cvlr-spl-token`` models SPL token accounts and ``cvlr-solana-stake`` models the
stake program. Soroban's derive-macro crate is a companion rather than a model: it is declared
the same way and models no program, which is what makes it un-withholdable. A project declares the
core, its chain's crate, and that chain's models and companions. A platform generation is the release line of
the chain's own SDK that a chain crate is built against, such as ``solana-program`` 2.x or
``soroban-sdk`` 22.x.

Exact versions, not ranges. The core and the chain crates are versioned separately, so "latest"
can pair a new core with an old chain crate. A bump is an edit here, and it moves every project
this build sets up: what is named here is the one CVLR line supported per platform generation, the
way a Prover release ships one CVL. That is why :mod:`composer.spec.cvlr.scaffold` refuses a project
already on a different line rather than deferring to it. A bump also means rebuilding the
``cvlr_api_kb`` corpus (``scripts/populate_cvlr_rag.sh``), which is generated from the releases
named here and describes nothing else.

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
from enum import StrEnum

from composer.cargo.features import CargoFeature
from composer.spec.cvlr.forks import ANCHOR_FORK, FIXED_FORK, ForkOverride


#: What keeps the Solana SDK's ``entrypoint!`` from installing its global allocator. The macro's
#: ``custom_heap_default!`` checks the *calling* crate's ``custom-heap`` feature, in ``solana-program``
#: 2.x and ``solana-program-entrypoint`` 3.x alike. The program's bump allocator writes the heap at a
#: fixed address, and a rule that also allocates through ``__rust_alloc`` fails with Prover error
#: 3309, "Conflicting allocation/usage of heap memory".
CUSTOM_HEAP_FEATURE = CargoFeature("custom-heap")


@dataclass(frozen=True)
class GitCommit:
    """The commit a release was cut from, read in place of crates.io until the release is published.

    The project still declares the crates.io spelling (``=version``), so the pin gate reads it as
    it reads any release. A ``[patch.crates-io]`` entry redirects where cargo fetches it. Cargo
    applies a patch only when the patched crate's version meets the requirement, so a commit at
    any other version fails the build instead of standing in for the release.

    A commit, not a tag or a branch: either can be moved, and the reference set names what was
    compiled.
    """

    repo: str
    rev: str

    def patch_body(self) -> tuple[tuple[str, str], ...]:
        """The body of the crate's ``[patch.crates-io.<crate>]`` table."""
        return (("git", self.repo), ("rev", self.rev))

    def patch_line(self, crate: str) -> str:
        """The crate's entry in a ``[patch.crates-io]`` table, as one line."""
        return f'{crate} = {{ git = "{self.repo}", rev = "{self.rev}" }}'


@dataclass(frozen=True)
class CrateRelease:
    """One crate at one released version."""

    name: str
    version: str
    #: Where cargo reads the release from while crates.io does not have it. ``None`` for a
    #: release on crates.io.
    unpublished: GitCommit | None = None

    def dependency_line(self) -> str:
        """A ``Cargo.toml`` dependency line. Exact (``=version``), not a caret range.

        The reference set says what was compiled, not which later releases are compatible.
        """
        return f'{self.name} = "={self.version}"'


@dataclass(frozen=True)
class CommitPatch:
    """One crate cargo reads from a commit: an entry in a ``[patch.crates-io]`` table."""

    name: str
    version: str
    commit: GitCommit


def commit_patches(crates: Iterable[CrateRelease]) -> tuple[CommitPatch, ...]:
    """The patches that read these releases from their commits. Empty for releases that are all on
    crates.io."""
    return tuple(
        CommitPatch(crate.name, crate.version, crate.unpublished)
        for crate in crates
        if crate.unpublished is not None
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
    #: The feature of the *calling* crate that keeps this generation's ``entrypoint!`` macro from
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
class ProgramModel:
    """A crate that models one named on-chain program's own instructions.

    Separate from a plain :class:`CrateRelease` because only this kind can be withheld, and only
    for one reason: a run whose target *is* :attr:`program` would be handed a behavioural model of
    the very instructions it is meant to specify — an answer key rather than a model of a
    dependency (``docs/stake-benchmark.md``). A crate that models no program cannot be an answer
    key for any target, so there is no run that would want it withheld.

    That is the whole of what withholding means, and having it in the type rather than in a
    convention is what lets everything downstream ignore the setting: a withheld crate is always
    *this target's own behaviour*, never a piece of CVLR the author needs and cannot have.
    """

    release: CrateRelease
    #: The program it models, named as a reader would name it: "the stake program".
    program: str


class AuthorSection(StrEnum):
    """A section of the author's system prompt that says how a rule reaches one framework's
    handlers, by the template that holds it. Each defines the macros the prompt's "Reaching a
    handler" section calls in place of its Anchor-only parts."""

    JIMINY = "cvlr_reaching_jiminy_handler.j2"
    PINOCCHIO_ACCOUNT_VIEW = "cvlr_reaching_pinocchio_view_handler.j2"
    PINOCCHIO_ACCOUNT_INFO = "cvlr_reaching_pinocchio_info_handler.j2"


@dataclass(frozen=True)
class HarnessShim:
    """A module of the harness that binds the CVLR core to a framework's account type, written by
    the scaffold because no CVLR chain crate is published for the framework.

    The scaffold writes it from ``harness_files/<template>.template.rs`` into
    ``src/certora/<module>.rs``, beside ``specs``. It compiles against the program's own copy of the framework, so it follows whatever
    release the program resolves, which a published crate could not do for a framework that is
    distributed as git dependencies.
    """

    #: The module's name under ``src/certora/``: what a rule's ``use`` line names.
    module: str
    #: The stem of the module's template under ``harness_files/``. Generations of one framework
    #: share a module name and differ in what the module assumes of an account's header.
    template: str
    #: The crate the module's ``use`` lines name. The program must depend on it directly: a crate
    #: can only name its own dependencies.
    requires: str
    #: How a rule reaches one of this framework's handlers, in the author's system prompt.
    author_section: AuthorSection

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
    #: declared. :meth:`scaffold_crates` includes them unless :attr:`withheld` names them.
    #:
    #: They are optional crates behind the ``certora`` feature, so a project that never calls them
    #: pays one extra compile. They are still pinned here. The scaffold is what writes
    #: dependencies, and it does not add one later. A dependency changes how the project builds
    #: for everyone, which the scaffold does not guess at. A model left out of this list is a
    #: crate the project cannot name.
    models: tuple[ProgramModel, ...] = ()
    #: Crates declared beside the chain crate that model no program — Soroban's derive macros are
    #: the case. They are pinned exactly like a :class:`ProgramModel`'s release and differ in one
    #: respect: :meth:`withholding` cannot name them, because there is no target they could be an
    #: answer key for.
    companions: tuple[CrateRelease, ...] = ()
    unpublished: tuple[UnpublishedCapability, ...] = ()
    #: What an author on this generation must be told because the CVLR manual says otherwise. The
    #: manual is written for the newest line, so this is empty there and grows on older ones.
    #: Stated in the author's and the judge's system prompts.
    caveats: tuple[str, ...] = ()
    #: Program models this run will not offer the project, by crate name. Empty for every ordinary
    #: run; :meth:`withholding` is what sets it, and :class:`ProgramModel` is what bounds it.
    #:
    #: Per crate rather than all-or-nothing, because the models have nothing to do with each
    #: other: the same tuple carries the SPL token model, which a run verifying the stake program
    #: still wants.
    withheld: frozenset[str] = frozenset()


    def line(self) -> str:
        """The supported line as a refusal names it to a project author: core and chain crate."""
        match self.binding:
            case CrateRelease(name=name, version=version):
                bound = f"{name} {version}"
            case HarnessShim() as shim:
                bound = shim.describe()
        return f"{self.core.name} {self.core.version} with {bound}"

    def _binding_name(self) -> str:
        match self.binding:
            case CrateRelease(name=name):
                return name
            case HarnessShim() as shim:
                return shim.describe()

    @property
    def shim(self) -> HarnessShim | None:
        """The harness module the scaffold writes for this generation, if its binding is one."""
        return self.binding if isinstance(self.binding, HarnessShim) else None

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set: the line this build supports.

        Deliberately not narrowed by :attr:`withheld`. Withholding a crate from one project says
        nothing about which line this build is on, and narrowing here would stop
        :meth:`composer.spec.cvlr.crates.CvlrSources.gaps` — and so
        :func:`composer.spec.cvlr.scaffold._check_pins` — noticing a real version disagreement on
        a crate the project declares for itself.
        """
        chain = (self.binding,) if isinstance(self.binding, CrateRelease) else ()
        return (self.core, *chain, *(m.release for m in self.models), *self.companions)

    def scaffold_crates(self) -> tuple[CrateRelease, ...]:
        """What a fresh project declares in its ``Cargo.toml``.

        :meth:`crates` less anything :attr:`withheld`. Two names because they answer two
        questions, asked in different places: :func:`composer.spec.cvlr.scaffold._check_pins`
        gates on which releases this build supports, and the manifest planning writes which of
        them a project is given. They returned the same tuple until a run needed to verify a
        program one of the specializations models, which is the case where the answers differ.
        """
        return tuple(c for c in self.crates() if c.name not in self.withheld)

    def withholding(self, *names: str) -> "ChainReference":
        """This reference set with ``names`` not offered to the project.

        Only a :class:`ProgramModel` can be named, and only for the one reason that type exists:
        this run's target *is* the program it models. Nothing else is withholdable — not the core,
        not the chain crate, not a companion — because nothing else can be an answer key, and a
        setting that could withhold arbitrary CVLR would make every consumer of the reference set
        ask what this run is missing.

        Refuses a name that is not a model. A withheld crate that was never in the set changes
        nothing, and "changed nothing" is the one outcome a caller cannot tell apart from success —
        so a typo here would silently leave the model in the project it was meant to be kept out of.
        """
        known = {m.release.name: m.program for m in self.models}
        if unknown := set(names) - set(known):
            offered = ", ".join(f"{n} ({p})" for n, p in sorted(known.items())) or "none"
            raise ValueError(
                f"not program models of {self._binding_name()}: {', '.join(sorted(unknown))}. "
                f"Withholding is only for a target that *is* the modelled program; this chain "
                f"models: {offered}"
            )
        return replace(self, withheld=self.withheld | frozenset(names))

    def cargo_dependencies(self) -> str:
        """A ``[dependencies]`` body pinning this reference set, for a probe crate.

        The platform crates are included because the chain crate's public types come from them.
        Without them a probe cannot name what the helpers return."""
        lines = [c.dependency_line() for c in self.crates()]
        lines += [c.dependency_line() for c in self.platform.sdk_crates]
        return "\n".join(lines)

    def from_commits(self) -> tuple[CommitPatch, ...]:
        """The crates of :meth:`crates` that cargo reads from a commit rather than crates.io."""
        return commit_patches(self.crates())


#: The core line, shared by every chain. ``cvlr-spec`` (``cvlr_spec!``, ``cvlr_rules!``,
#: ``cvlr_lemma!``) is a dependency of ``cvlr``, so a target names one crate and gets the
#: parametric-rule layer with it. 0.6.2 is the first release with the public API documented and
#: with ``#[rule]`` recording each rule in the ``.cvlr_rules`` section.
_CORE = CrateRelease("cvlr", "0.6.2")

#: ``cvlr-solana`` 0.5.2: 0.5.0 with the public API documented, the ``rt`` feature fixed, and
#: account flags read without branching (each nondet account used to add 2^3 paths to a rule). The
#: three crates of that repository are released together, at one version.
SOLANA_V2 = ChainReference(
    core=_CORE,
    binding=CrateRelease("cvlr-solana", "0.5.2"),
    models=(
        ProgramModel(
            CrateRelease("cvlr-solana-stake", "0.5.2"), "the stake program"
        ),
        # The SPL token account model: nondet token accounts and mints, and the token instruction
        # summaries. Split out of the chain crate, and released with it.
        ProgramModel(
            CrateRelease("cvlr-spl-token", "0.5.2"), "the SPL token program"
        ),
    ),
    forks=(ANCHOR_FORK, FIXED_FORK),
    platform=PlatformGeneration(
        # The last line published as one ``solana-program`` crate. v3 is only the split crates.
        label="solana-program 2.x",
        sdk_crates=(CrateRequirement("solana-program", "2.2"),),
        # ``solana-account-info`` first: it defines ``AccountInfo`` and exists on both 2.x and 3.x.
        # ``solana-program`` is the fallback for 1.x, which predates the split and defines the
        # type inside the monolith, so a 1.x target reads as off this generation, not as on none.
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
            # Not aliased: `solana_program::instruction::get_stack_height` is still a function in
            # the monolith on this generation, and `solana_program::poseidon` does not exist here
            # under any spelling. Rewriting either would hide a directive that does not apply.
            #
            # Nor is the never-inline blanket `solana_program::.*`. It matches only what stayed in
            # the monolith, so the split crates are analyzed. That is what projects verified on
            # this generation run, and what their specs depend on: a non-inlined function is
            # external, returning an arbitrary value and writing nothing, so
            # `Sysvar::from_account_info` never returns `Ok` and the program's domain interface
            # crates (`solana_stake_interface`) become oracles.
        ),
    ),
)

#: ``cvlr-solana`` 0.6.1, the SDK 3 line: 0.5.2 ported to the split ``solana-*`` 3.x crates, with
#: both program models.
SOLANA_V3 = replace(
    SOLANA_V2,
    binding=CrateRelease("cvlr-solana", "0.6.1"),
    models=(
        ProgramModel(CrateRelease("cvlr-solana-stake", "0.6.1"), "the stake program"),
        ProgramModel(CrateRelease("cvlr-spl-token", "0.6.1"), "the SPL token program"),
    ),
    platform=PlatformGeneration(
        # No `solana-program` at all: every platform type is in its own crate.
        label="solana-* 3.x",
        sdk_crates=(CrateRequirement("solana-account-info", "3"),),
        witnesses=(CrateRequirement("solana-account-info", "3.0"),),
        heap_opt_out=CUSTOM_HEAP_FEATURE,
        # Read off a stake `main` build's demangled symbols (`docs/cvlr-sdk3-support.md`, A2).
        # Most are the 2.x spellings. Three are not:
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
            # No monolith, so no second spelling: the facade's own `invoke_signed_unchecked`
            # is gone with it.
            PathAlias(
                "solana_program::program::invoke_signed_unchecked",
                ("solana_cpi::invoke_signed_unchecked",),
            ),
            # `invoke`, `invoke_signed` and `set_return_data` were real functions in the 2.x
            # monolith. They are `solana-cpi`'s now.
            PathAlias("solana_program::program", ("solana_cpi",)),
            PathAlias(
                "solana_program::instruction::get_stack_height",
                ("solana_instruction::syscalls::get_stack_height",),
            ),
            PathAlias("solana_program::poseidon", ("solana_poseidon",)),
            # The never-inline blanket `solana_program::.*` is not aliased, as on 2.x. Here it
            # matches nothing, so the whole SDK is analyzed. `solana_get_sysvar::get_sysvar`, the
            # generic sysvar read `StakeHistorySysvar` goes through, needs no tuning: the syscall
            # under it is modelled.
            #
            # Not a platform crate: Anchor 1.1.0 moved `anchor_lang::error` into the
            # `anchor-lang-error` crate. The anchor blanket `^.*anchor_lang.*$` still matches it,
            # so without this the `Error` conversions are opaque. An opaque conversion on a
            # constructor's error path leaves the `Ok` payload's pointers without provenance, and
            # the first dereference fails with [3003]. Anchor 1.0.x has no such crate and keeps
            # the canonical spelling.
            PathAlias("anchor_lang::error::", ("anchor_lang_error::",)),
        ),
    ),
)

#: ``cvlr-solana`` 0.4.7, the release for ``solana-program`` 1.18 (branch ``v0.4``): 0.4.5 with
#: the public API documented, the ``rt`` feature fixed, and account flags read without branching. It requires ``cvlr-*`` ``>=0.4, <1``,
#: so it resolves onto the shared 0.6.2 core with one copy of each core crate.
SOLANA_V1_18 = ChainReference(
    core=_CORE,
    binding=CrateRelease("cvlr-solana", "0.4.7"),
    # None. The 0.4 line has no stake model, and its token helpers are in the chain crate. It has
    # no SPL Token CPI model either: a token CPI reaches ``invoke_signed_unchecked``, which the
    # core summaries make opaque.
    models=(),
    forks=(ANCHOR_FORK, FIXED_FORK),
    caveats=(
        "This build is on `cvlr-solana` 0.4, the line for `solana-program` 1.18. The CVLR manual "
        "is written for 0.5 and later. Where it names `cvlr_spl_token` or `cvlr_solana_stake`, "
        "those crates do not exist on this line, and nothing substitutes token CPIs "
        "automatically.",
        "A token CPI here reaches `invoke_signed_unchecked`, which the prover treats as opaque: "
        "it moves no balance and returns an arbitrary result. When a property needs the transfer "
        "itself, have the stand-in for the program's token call (the `swap_import` route) call "
        "the matching summary in `cvlr_solana::token` — `spl_token_transfer`, "
        "`spl_token_2022_transfer`, `spl_mint_to`, `spl_burn` or `spl_close_account`. Each one "
        "moves the amounts in the token accounts' data, and the `spl_token_account_get_amount` "
        "and `spl_mint_get_supply` helpers read them back.",
    ),
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
    binding=HarnessShim(
        module="jiminy",
        template="jiminy",
        requires="jiminy-entrypoint",
        author_section=AuthorSection.JIMINY,
    ),
    models=(),
    forks=(FIXED_FORK,),
    caveats=(
        "This program is written on jiminy, not on the Solana SDK, so this build gives it the "
        "`cvlr` core and no chain crate. Nothing in `cvlr_solana`, `cvlr_spl_token` or "
        "`cvlr_solana_stake` applies: their helpers take and return the SDK's `AccountInfo`, which "
        "a jiminy handler cannot take. Where the CVLR manual reaches for them, use the "
        "`crate::certora::jiminy` module the harness already has: `cvlr_jiminy_abr()`, "
        "`cvlr_jiminy_account()` and `cvlr_jiminy_accounts()`.",
        "A jiminy account is an `AccountHandle`, read and written through the `Abr`, never "
        "directly. Handles are `Copy` and own nothing, so the advice about putting an "
        "`AccountInfo` array in a `Box` and ending with `core::mem::forget` does not apply.",
        "A CPI through `jiminy_cpi` reaches the `sol_invoke_signed_c` syscall, which the prover "
        "models as returning an arbitrary status and writing nothing. No token model applies: "
        "`cvlr_spl_token`'s summaries replace the SDK's invoke functions, which a jiminy program "
        "never calls.",
    ),
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

def _pinocchio_caveats(account_type: str) -> tuple[str, ...]:
    return (
        "This program is written on Pinocchio, not on the Solana SDK, so this build gives it the "
        "`cvlr` core and no chain crate. Nothing in `cvlr_solana`, `cvlr_spl_token` or "
        "`cvlr_solana_stake` applies: their helpers take and return the SDK's `AccountInfo`, which "
        f"a Pinocchio handler cannot take. Pinocchio's account type is `{account_type}`. Where the "
        "CVLR manual reaches for those crates, use the `crate::certora::pinocchio` module the "
        "harness already has: `cvlr_pinocchio_account()` and `cvlr_pinocchio_accounts()`.",
        f"A Pinocchio `{account_type}` is `Copy` and owns nothing, so the advice about putting an "
        "`AccountInfo` array in a `Box` and ending with `core::mem::forget` does not apply.",
        "A CPI through `pinocchio::cpi` (and so through `pinocchio-token` or `pinocchio-system`) "
        "reaches the `sol_invoke_signed_c` syscall, which the prover models as returning an "
        "arbitrary status and writing nothing. No token model applies: `cvlr_spl_token`'s summaries "
        "replace the SDK's invoke functions, which a Pinocchio program never calls.",
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
    binding=HarnessShim(
        module="pinocchio",
        template="pinocchio_0_11",
        requires="pinocchio",
        author_section=AuthorSection.PINOCCHIO_ACCOUNT_VIEW,
    ),
    models=(),
    forks=(FIXED_FORK,),
    caveats=_pinocchio_caveats("AccountView"),
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
    binding=HarnessShim(
        module="pinocchio",
        template="pinocchio_0_10",
        requires="pinocchio",
        author_section=AuthorSection.PINOCCHIO_ACCOUNT_VIEW,
    ),
    models=(),
    forks=(FIXED_FORK,),
    caveats=_pinocchio_caveats("AccountView"),
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
    binding=HarnessShim(
        module="pinocchio",
        template="pinocchio_0_8",
        requires="pinocchio",
        author_section=AuthorSection.PINOCCHIO_ACCOUNT_INFO,
    ),
    models=(),
    forks=(FIXED_FORK,),
    caveats=_pinocchio_caveats("AccountInfo"),
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
    # attribute macros. It is declared and pinned like a model's release, and models no program,
    # so it is not withholdable.
    companions=(CrateRelease("cvlr-soroban-derive", "0.4.0"),),
    forks=(),
    platform=PlatformGeneration(
        label="soroban-sdk 22.x",
        sdk_crates=(CrateRequirement("soroban-sdk", "22"),),
        # Soroban has one SDK crate, so the declared crate and the witness are the same. Spelled
        # out because that is a fact about this platform. On Solana the witness list names a crate
        # this generation does not declare.
        witnesses=(CrateRequirement("soroban-sdk", "22"),),
    ),
)

#: Keyed by ``composer.pipeline.ecosystem.ChainTag``, minus ``evm``. CVLR is the Rust-side
#: specification language and has no EVM line.
#:
#: One reference set per platform generation, newest first. A chain crate works only with its own
#: generation's ``AccountInfo``, so a target is set up with the generation it is on
#: (:func:`composer.spec.cvlr.scaffold.select_generation`).
#:
#: A framework's generation comes before the SDK's. A program that links a framework's entrypoint
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
