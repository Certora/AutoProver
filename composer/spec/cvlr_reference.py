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
line (0.4.x → ``solana-program`` 1.18, 0.5.0 → 2.2, the unreleased 0.6 line → the split
``solana-*`` v3 crates), and each generation has its *own* ``AccountInfo`` type. Two crates that
disagree do not merely warn — a helper from one cannot be passed to a handler from the other, so
:attr:`ChainReference.platform` is part of the reference set rather than a detail of the target.

This module deliberately imports nothing: a script that only needs to know which version to write
into a probe crate should not pay for the pipeline (importing ``ChainTag``'s home costs ~2.5s and
pulls the whole model layer). It therefore repeats the chain vocabulary as plain strings, the same
trade ``composer.rustapp.descriptor`` makes for the same reason, and
``tests/test_cvlr_reference.py`` pins the two against each other so they cannot drift apart.
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class CrateRelease:
    """One crate at one exact published version."""

    name: str
    version: str

    def dependency_line(self) -> str:
        """The ``Cargo.toml`` line for this crate — an exact version, not a caret range: the
        reference set is a statement about what was compiled, not a compatibility claim."""
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
    #: Chain crates that model one specific on-chain program rather than the chain itself, so a
    #: target declares them only if it verifies that program. Separate from :attr:`chain` because
    #: the two answer different questions: the corpus is compiled against all of them, while a
    #: scaffold that declared all of them would add a dependency nobody uses.
    specializations: tuple[CrateRelease, ...] = ()
    unpublished: tuple[UnpublishedCapability, ...] = ()

    def crates(self) -> tuple[CrateRelease, ...]:
        """Every CVLR crate in the reference set — what the corpus was written against."""
        return (self.core, self.chain, *self.specializations)

    def scaffold_crates(self) -> tuple[CrateRelease, ...]:
        """What a fresh project declares in its ``Cargo.toml``."""
        return (self.core, self.chain)

    def cargo_dependencies(self) -> str:
        """A ``[dependencies]`` body pinning this reference set, for a probe or scaffold crate.

        The platform crates are included because the CVLR chain crate's public types come from
        them: omitting them leaves a probe unable to *name* what the helpers return."""
        lines = [c.dependency_line() for c in self.crates()]
        lines += [c.dependency_line() for c in self.platform.crates]
        return "\n".join(lines)


#: The core line, shared by every chain. ``cvlr-spec`` (the ``cvlr_spec!`` / ``cvlr_rules!`` /
#: ``cvlr_lemma!`` machinery) is a dependency of ``cvlr`` rather than a separate declaration, so a
#: target names one crate and gets the parametric-rule layer with it.
_CORE = CrateRelease("cvlr", "0.6.1")

SOLANA = ChainReference(
    core=_CORE,
    chain=CrateRelease("cvlr-solana", "0.5.0"),
    specializations=(CrateRelease("cvlr-solana-stake", "0.5.0"),),
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
    ),
    unpublished=(
        UnpublishedCapability(
            names=("cvlr-spl-token", "cvlr-solana-token"),
            missing=(
                "the SPL Token account model — nondet token accounts and mints, and the token "
                "instruction summaries. It was factored out of cvlr-solana on the unreleased 0.6 "
                "line and published under neither name, so entries needing it must model the "
                "token account themselves. Revisit the corpus if it is ever published."
            ),
        ),
    ),
)

SOROBAN = ChainReference(
    core=_CORE,
    chain=CrateRelease("cvlr-soroban", "0.4.0"),
    # The derive crate is a companion rather than a specialization, but it is declared the same
    # way: a target reaches for it only when it writes the attribute macros.
    specializations=(CrateRelease("cvlr-soroban-derive", "0.4.0"),),
    platform=PlatformGeneration(
        label="soroban-sdk 22.x",
        crates=(CrateRequirement("soroban-sdk", "22"),),
        # Soroban ships one SDK crate rather than a family, so declaring it and witnessing it are
        # the same crate. Spelled out rather than defaulted: they coincide here as a fact about
        # this platform, not as a rule, and Solana is the proof that the two can diverge.
        witnesses=(CrateRequirement("soroban-sdk", "22"),),
    ),
)

#: Keyed by the chain vocabulary of ``composer.pipeline.ecosystem.ChainTag``, minus ``evm`` — CVLR
#: is the Rust-side specification language and has no EVM line.
REFERENCE_SET: dict[str, ChainReference] = {"solana": SOLANA, "soroban": SOROBAN}


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
