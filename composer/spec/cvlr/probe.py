"""Compiling one CVLR snippet against the reference set.

The gate for generated corpus content, and the standing lesson from the capture pass is *where* it
goes: moving the compile inside the generation retry loop — so the compiler's own message is fed back
as the next attempt's input — took that pass's Rust examples from 4 of 48 compiling to 35 of 35. A
gate that runs after generation only tells you how bad the output was.

That only works if a check is cheap, and here it is: a throwaway crate pinned to
:mod:`composer.spec.cvlr_reference`, warmed once, then ``cargo check`` per snippet — 4.6 s for the
first (it builds ``solana-program`` and its tree) and well under a second for every one after.

Two things this deliberately is not. It is **not** the authoring loop's fast tier: that one checks
generated rules against *the target project*, whose CVLR version and whose program are the point,
and it lives on :class:`~composer.cargo.session.CargoSession` already. And it is **not** an SBF
build — a host-target check catches the errors that matter here (an invented macro, a wrong
signature, a misused derive) at a fiftieth of the cost.

Confinement still applies. The snippet is LLM-authored, and Rust runs arbitrary code at compile time
— ``include_str!`` alone reads any path the process can — so the check goes through the session's
confined path like every other compile.
"""

import asyncio
import dataclasses
import logging
from pathlib import Path

from composer.cargo.session import CargoSession, CompileFailed, CompileRun
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr_reference import reference_for

_log = logging.getLogger(__name__)

#: The probe crate's manifest. ``edition 2021`` matches the reference set's own crates, and the
#: package name is deliberately unlike anything a snippet would refer to.
_MANIFEST = """\
[package]
name = "cvlr_reference_probe"
version = "0.0.0"
edition = "2021"

[dependencies]
{dependencies}
"""

#: Prepended when a snippet does not import for itself. Corpus examples are written to be read as
#: much as compiled, and a reader who has to be told to add the prelude is being told the wrong
#: thing — so an example that omits it is not wrong, it is abbreviated.
PRELUDE = "use cvlr::prelude::*;"


@dataclasses.dataclass(frozen=True)
class Attempt:
    """One way of presenting a snippet to the compiler, and what happened."""

    #: How the snippet was framed. Reported so a caller can tell "compiles as written" from
    #: "compiles only once we guessed at the missing scaffolding", which are different qualities of
    #: corpus entry even though both end in a green check.
    framing: str
    run: CompileRun


@dataclasses.dataclass(frozen=True)
class Compiles:
    """The snippet compiles. ``attempt`` says how it had to be framed to do so."""

    attempt: Attempt


@dataclasses.dataclass(frozen=True)
class DoesNotCompile:
    """Every framing failed.

    ``diagnostics`` is the *last* attempt's, not the first: the last framing is the most generous
    one, so its error is the one that is actually about the snippet rather than about missing
    scaffolding. Reporting the first attempt's error sends an author off to fix an import the gate
    had already added for them.

    A field rather than a property reading back through ``attempts``, because only the construction
    site knows the attempts all failed — a type that says "every one of these was rejected" would
    have to be a second near-copy of :class:`Attempt` to prove it."""

    attempts: tuple[Attempt, ...]
    diagnostics: str


type ProbeVerdict = Compiles | DoesNotCompile


def framings(snippet: str) -> tuple[tuple[str, str], ...]:
    """``(framing name, source)`` in increasing order of generosity.

    Ordered so the first success is the tightest one that works, which is what makes
    :attr:`Compiles.attempt` informative rather than incidental."""
    body = snippet.strip()
    candidates = [("as written", body)]
    if PRELUDE not in body:
        candidates.append(("with the prelude", f"{PRELUDE}\n\n{body}"))
    # A snippet that is a sequence of statements rather than items — the shape a "how do I call this"
    # example naturally takes — is not a module. Wrapping it is the last resort because a snippet
    # that needs it is one a reader would have to wrap too, and the entry should say so.
    candidates.append(
        ("wrapped in a function", f"{PRELUDE}\n\npub fn probe() {{\n{body}\n}}")
    )
    return tuple(candidates)


@dataclasses.dataclass
class ReferenceProbe:
    """A warm crate pinned to the reference set, compiling one snippet at a time.

    One crate rather than one per snippet: the dependency tree is the expensive part and it does not
    change between snippets. The cost is that ``src/lib.rs`` is a single shared slot, so checks are
    serialized — :attr:`_writing` is that, and it is why this is an object rather than a function.
    """

    session: CargoSession
    _writing: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    async def create(
        cls, workdir: Path, *, chain: str = "solana", sandbox: SandboxConfig | None = None
    ) -> "ReferenceProbe":
        """Write the probe crate under ``workdir`` and warm its dependency graph.

        The dependencies come from :mod:`composer.spec.cvlr_reference` rather than from anything
        here. That module is the one answer to "which CVLR does *current* mean", shared with the
        scaffold and the corpus's own gate, and a second copy here would drift silently — the failure
        being a corpus certified against a version nobody ships.
        """
        reference = reference_for(chain)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "Cargo.toml").write_text(
            _MANIFEST.format(dependencies=reference.cargo_dependencies())
        )
        source = workdir / "src"
        source.mkdir(exist_ok=True)
        (source / "lib.rs").write_text("")
        session = CargoSession(
            workdir=workdir, sandbox=sandbox if sandbox is not None else SandboxConfig.from_env()
        )
        warmed = await session.warm()
        _log.info("reference probe in %s: %s", workdir, type(warmed).__name__)
        return cls(session=session)

    async def check(self, snippet: str) -> ProbeVerdict:
        """Compile ``snippet``, trying each framing until one works."""
        attempts: list[Attempt] = []
        last = "no framing was attempted"
        async with self._writing:
            for framing, source in framings(snippet):
                (self.session.workdir / "src" / "lib.rs").write_text(source + "\n")
                run = await self.session.check()
                attempt = Attempt(framing=framing, run=run)
                if run.ok:
                    return Compiles(attempt)
                assert isinstance(run.verdict, CompileFailed)
                attempts.append(attempt)
                last = run.verdict.diagnostics
        return DoesNotCompile(tuple(attempts), last)
