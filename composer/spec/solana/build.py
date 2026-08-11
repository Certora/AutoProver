"""Build a Solana program to sBPF (and, optionally, its IDL).

The shared "Solana build capability" (``docs/crucible-application.md (PR3)`` §5.1):
``source → .so [+ IDL]``. It is deliberately backend-agnostic — the Crucible
backend calls it in *no-munge* mode (build the program as-is), and a future
Certora-Prover/CVLR backend will call it in *munge-and-rebuild* mode (rewrite the
source first). Both route through the same :func:`run_local_command` choke point
the ``RunCommand`` effect uses, so phase-6 sandboxing (§7.4) wraps one path.

:func:`prepare_workspace` is this chain's
:class:`~composer.rustapp.toolchain.WorkspaceToolchain` — the seam by which a Rust wheel's declared
``workspace_prep`` plan reaches these helpers. The framework registers no implementation of its own
(see that module); everything here is knowledge about the *analyzed* program's toolchain, which is
why it lands with the application that first needs it rather than with the framework.
"""

import json
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from composer.rustapp.adapter import confined_target
from composer.rustapp.wire import AuthorInput, WorkspacePrep
from composer.sandbox.command import CommandResult, run_local_command
from composer.sandbox.config import SandboxConfig
from composer.spec.cargo import ProgramCrate, resolve_program_crate
from composer.spec.context import SourceFields
from composer.sandbox.recipes import (
    CARGO_REGISTRY_PROTOCOL,
    CARGO_REGISTRY_PROTOCOL_VAR,
    sandbox_cargo_home,
)

_log = logging.getLogger(__name__)

DEFAULT_BUILD_TIMEOUT_S = 600

#: Which `Anchor.toml` cluster's address to prefer. A harness runs the program in a local VM
#: (LiteSVM), so a `localnet` id is the most faithful; the rest are fallbacks for a manifest that
#: only declares a deployed cluster.
_CLUSTER_PREFERENCE = ("localnet", "mainnet", "devnet", "testnet")

#: `declare_id!("<base58>")` — the program id in the source, used only when it is unambiguous.
_DECLARE_ID = re.compile(r"""declare_id!\s*\(\s*["']([1-9A-HJ-NP-Za-km-z]{32,44})["']""")


class BuildError(RuntimeError):
    """A build step (``cargo-build-sbf`` / ``anchor idl``) failed."""

    def __init__(self, step: str, result: CommandResult):
        self.step = step
        self.result = result
        # Keep the tail of stderr — the actionable part of a cargo error.
        super().__init__(f"{step} failed (exit {result.exit_code}):\n{result.stderr[-2000:]}")


@dataclass(frozen=True)
class BuiltProgram:
    """Where the build left its outputs."""

    program: str
    so_path: Path
    idl_path: Path | None


def resolve_program_id(project_root: str | Path, crate: ProgramCrate | None) -> str | None:
    """The program's on-chain address, from ``Anchor.toml``'s ``[programs.<cluster>]`` table keyed by
    any of the crate's names, else a lone ``declare_id!`` in its source.

    ``crate`` is the resolved :class:`~composer.spec.cargo.ProgramCrate`, or ``None`` when the layout
    yielded none — then there are no names to match against and only the root's own sources to scan.
    Returns ``None`` when there is no manifest entry and the source declares several ids (a real
    program's ``staging`` id was feature-gated behind the real one) — guessing between them would
    produce a harness that calls a program that isn't there.
    """
    root = Path(project_root)
    names = crate.program_names() if crate is not None else set()
    if found := _anchor_toml_program_id(root / "Anchor.toml", names):
        return found

    crate_dir = crate.dir if crate is not None else "."
    sources = sorted((root / crate_dir / "src").rglob("*.rs"))
    ids = {m for f in sources for m in _DECLARE_ID.findall(_read_or_empty(f))}
    if len(ids) == 1:
        return ids.pop()
    if ids:
        _log.warning("%s declares %d program ids %s; not guessing", crate_dir, len(ids), sorted(ids))
    return None


def _anchor_toml_program_id(manifest: Path, names: set[str]) -> str | None:
    """``[programs.<cluster>]`` address for any of ``names``, preferring a local-validator entry."""
    try:
        programs = tomllib.loads(manifest.read_text()).get("programs") or {}
    except (OSError, ValueError):
        return None
    ordered = [c for c in _CLUSTER_PREFERENCE if c in programs] + [
        c for c in programs if c not in _CLUSTER_PREFERENCE
    ]
    for cluster in ordered:
        for key, value in (programs.get(cluster) or {}).items():
            if key.replace("-", "_") not in names:
                continue
            # Either `name = "<address>"` or `name = { address = "<address>", ... }`.
            address = value.get("address") if isinstance(value, dict) else value
            if isinstance(address, str) and address:
                return address
    return None


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def idl_with_program_id(
    idl_text: str, *, project_root: str | Path, crate: ProgramCrate | None
) -> str:
    """``idl_text`` with the program's address filled in, if it carries none.

    An IDL must name the program it describes — a harness derives the id it sends instructions to
    from it. The 0.30+ spec keeps it in the top-level ``address``, the older layout in
    ``metadata.address``, and **`anchor idl build` under 0.29 writes neither** (only the deploy flow
    filled it in), so the file that command produces is unusable as-is: the type generator rejects
    it with "Program id missing in `idl.metadata.address` field". Fill it from the project instead
    of making that the operator's problem.

    Raises :class:`ValueError` when the id can't be established — better than emitting an IDL that
    would fuzz the wrong address.
    """
    try:
        idl = json.loads(idl_text)
    except ValueError as exc:
        raise ValueError(f"IDL is not valid JSON: {exc}") from exc
    if not isinstance(idl, dict):
        raise ValueError("IDL is not a JSON object")
    raw_metadata = idl.get("metadata")
    metadata: dict = raw_metadata if isinstance(raw_metadata, dict) else {}
    if idl.get("address") or metadata.get("address"):
        return idl_text

    program_id = resolve_program_id(project_root, crate)
    if program_id is None:
        raise ValueError(
            "the IDL does not name the program's address (neither `address` nor "
            "`metadata.address`), and it could not be resolved from the project: add a "
            "[programs.<cluster>] entry to Anchor.toml for this program, or pass --program-idl "
            "with an IDL that carries its address."
        )
    idl["metadata"] = {**metadata, "address": program_id}
    _log.info("filled in the IDL's program id: %s", program_id)
    return json.dumps(idl)


#: The rustup toolchain ``cargo-build-sbf`` links to the platform-tools Rust it will build
#: with — it re-runs ``rustup toolchain link solana <platform-tools>/rust`` on every
#: invocation. Warming through ``cargo +solana`` therefore uses *the same cargo as the
#: build*, without having to locate ``~/.cache/solana/v<X.YZ>/`` ourselves.
SBF_TOOLCHAIN = "solana"


async def warm_cargo_cache(
    manifest_dir: str | Path,
    *,
    cargo_binary: str = "cargo",
    cargo_home: str | Path | None = None,
    toolchain: str | None = None,
    timeout_s: int = DEFAULT_BUILD_TIMEOUT_S,
) -> CommandResult:
    """Populate ``CARGO_HOME`` with the deps declared in ``manifest_dir/Cargo.toml``.

    Run **outside** any sandbox (with network) so a later *sandboxed*,
    ``CARGO_NET_OFFLINE`` build finds every dep warm (``docs/command-sandbox.md`` §5).
    ``cargo fetch`` downloads but never runs build scripts, so no untrusted code
    executes here — the code-exec build happens confined + offline. Best-effort: a
    fetch failure is logged (the offline build will surface a hard error if a dep is
    genuinely missing), not raised.

    ``cargo_home`` fetches into a specific (per-run, private) ``CARGO_HOME`` — it must
    be the *same* home the sandboxed build will use, or the offline build won't find
    the deps. Defaults to the ambient ``CARGO_HOME`` when omitted.

    ``toolchain`` runs the fetch under a rustup toolchain (``cargo +<toolchain> fetch``).
    Pass :data:`SBF_TOOLCHAIN` so the warm uses the *same cargo the build will* — sharing a
    ``CARGO_HOME`` is not sufficient on its own, because two cargo versions disagree about
    the layout *within* it (see :data:`~composer.sandbox.recipes.CARGO_REGISTRY_PROTOCOL`).
    Falls back to the ambient cargo when the toolchain isn't installed, which is the normal
    state until the first ``cargo-build-sbf`` run creates the link.
    """
    overlay = {CARGO_REGISTRY_PROTOCOL_VAR: CARGO_REGISTRY_PROTOCOL}
    if cargo_home is not None:
        overlay["CARGO_HOME"] = str(cargo_home)
    args = ([f"+{toolchain}"] if toolchain else []) + ["fetch"]
    res = await run_local_command(
        cargo_binary, args, {}, workdir=Path(manifest_dir),
        timeout_s=timeout_s, env_overlay=overlay,
    )
    if res.exit_code != 0 and toolchain is not None:
        # No such toolchain yet (first build on this host links it) — the ambient cargo is
        # still worth warming with: it fills the registry, and only the *git* deps and the
        # index layout are version-sensitive.
        _log.info("cargo +%s fetch unavailable; warming with the ambient cargo", toolchain)
        return await warm_cargo_cache(
            manifest_dir, cargo_binary=cargo_binary, cargo_home=cargo_home, timeout_s=timeout_s
        )
    if res.exit_code != 0:
        _log.warning(
            "cargo fetch in %s failed (exit %s); a sandboxed offline build may fail. stderr:\n%s",
            manifest_dir,
            res.exit_code,
            res.stderr[-500:],
        )
    return res


async def build_program(
    project_root: str | Path,
    program: str,
    *,
    build_binary: str = "cargo-build-sbf",
    anchor_binary: str = "anchor",
    with_idl: bool = False,
    timeout_s: int = DEFAULT_BUILD_TIMEOUT_S,
    sandbox: SandboxConfig | None = None,
) -> BuiltProgram:
    """Compile ``program`` in the workspace at ``project_root`` to
    ``target/deploy/<program>.so``. If ``with_idl``, also try ``anchor idl build``
    (best-effort — not every project has an ``Anchor.toml``; the same-version
    harness path depends on the program crate directly and needs no IDL).

    ``cargo-build-sbf`` runs the user program's ``build.rs`` natively, so it is
    confined by ``sandbox`` when one is supplied (``docs/command-sandbox.md``);
    ``None`` runs it unsandboxed (trusted input only).

    Raises :class:`BuildError` if the ``.so`` is not produced.
    """
    root = Path(project_root)
    provider = policy = None
    if sandbox is not None and sandbox.enabled:
        provider = sandbox.resolve_provider()
        policy = sandbox.build_policy(root)
        assert policy is not None  # enabled config ⇒ build_policy returns a real policy
        # Warm the registry with network BEFORE the sandboxed, offline build (§5), into the
        # SAME private CARGO_HOME the sandboxed build will read (the policy's) — and under the
        # SAME cargo, via the `solana` toolchain `cargo-build-sbf` links to its platform-tools
        # Rust. Sharing the home is not enough by itself: the host cargo and the platform-tools
        # cargo can be many releases apart, and a pre-1.70 one defaults to the git index while
        # a modern host warm writes the sparse one, so the offline build misses a cache that is
        # sitting right there. `warm_cargo_cache` also pins the protocol on both sides.
        await warm_cargo_cache(
            root,
            cargo_home=policy.env_allowlist.get("CARGO_HOME"),
            toolchain=SBF_TOOLCHAIN,
            timeout_s=timeout_s,
        )

    res = await run_local_command(
        build_binary, [], {}, workdir=root, timeout_s=timeout_s, provider=provider, policy=policy
    )
    so = root / "target" / "deploy" / f"{program}.so"
    if res.exit_code != 0 or not so.is_file():
        raise BuildError("cargo-build-sbf", res)

    idl_path: Path | None = None
    if with_idl:
        out_rel = f"target/idl/{program}.json"
        idl_res = await run_local_command(
            anchor_binary, ["idl", "build", "-o", out_rel], {}, workdir=root,
            timeout_s=timeout_s, provider=provider, policy=policy,
        )
        candidate = root / out_rel
        if idl_res.exit_code == 0 and candidate.is_file():
            idl_path = candidate
        else:
            _log.warning(
                "IDL build did not produce %s (exit %s); continuing without an IDL "
                "(same-version harnesses depend on the program crate directly). stderr tail:\n%s",
                candidate,
                idl_res.exit_code,
                idl_res.stderr[-500:],
            )

    return BuiltProgram(program=program, so_path=so, idl_path=idl_path)


async def prepare_workspace(
    plan: WorkspacePrep,
    input: AuthorInput,
    *,
    source: SourceFields,
    sandbox: SandboxConfig | None,
    timeout_s: int,
) -> str | None:
    """Solana's :class:`~composer.rustapp.toolchain.WorkspaceToolchain`: execute the toolchain half
    of a wheel's ``workspace_prep`` plan (``docs/rust-pure-app.md`` §4) with this chain's tools.

    Only reached when the plan asks for more than files, and only after the host has written them
    (the manifest a warm or build reads is usually one). In order: ``cargo fetch`` each
    ``warm_dirs`` — but only when a sandbox is enabled, since the point of warming is that the later
    confined+offline build finds its deps already there — then build ``build_program``, then place
    the program's IDL if the wheel asked for one. Returns where the IDL was placed
    (workdir-relative), which the host reports back as the ``idl`` context key, else ``None``.

    Network stays Python-owned and the posture is the one §5 describes: fetches run *unconfined* (a
    fetch executes no untrusted code), the code-executing build runs *confined + offline*
    (:func:`build_program` handles both). The wheel supplies only which dirs/program — never a
    command line — and every path it names goes through
    :func:`~composer.rustapp.adapter.confined_target`, as the host's own writes do.

    The crate is resolved here rather than taken as an argument: ``input.program_crate`` is the lossy
    wire copy (unknown spelled ``""``), and filling in an IDL's program id needs to know whether
    anything was resolved at all. Same function, same inputs as the resolver that produced the wire
    copy — :func:`composer.rustapp.toolchain.source_crate`'s Solana entry — so the two agree.
    """
    workdir = Path(source.project_root)
    crate = resolve_program_crate(source.project_root, source.relative_path)
    idl_dest = plan.idl_dest

    if plan.warm_dirs and sandbox is not None and sandbox.enabled:
        # Warm into the SAME private CARGO_HOME the confined offline build will read.
        cargo_home = sandbox_cargo_home(str(workdir))
        for d in plan.warm_dirs:
            await warm_cargo_cache(
                confined_target(workdir, d), cargo_home=cargo_home, timeout_s=timeout_s
            )

    # An operator-supplied IDL wins over building one — for a program whose own toolchain isn't
    # installed (the usual reason the wheel wants an IDL at all), `anchor idl build` can't run.
    supplied = input.context.get("program_idl") or None
    idl_src = Path(supplied) if (idl_dest and supplied) else None
    if idl_src is not None and not idl_src.is_file():
        raise RuntimeError(f"--program-idl: no such file: {idl_src}")

    if plan.build_program:
        built = await build_program(
            str(workdir), plan.build_program, with_idl=bool(idl_dest) and idl_src is None,
            timeout_s=timeout_s, sandbox=sandbox,
        )
        if idl_dest and idl_src is None:
            idl_src = built.idl_path

    if not idl_dest:
        return None
    if idl_src is None:
        raise RuntimeError(
            "the harness must generate the program's types from its IDL (it cannot link the "
            "program's crate directly), but no IDL could be produced: `anchor idl build` did not "
            "emit one, which usually means the program's own anchor CLI version isn't installed. "
            "Supply one with --program-idl <file> — any Anchor IDL format, including the pre-0.30 "
            "layout."
        )
    dest = confined_target(workdir, idl_dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Normalized on the way in: an IDL must name the program's address, and the one `anchor idl
    # build` emits for a pre-0.30 program doesn't (see ``idl_with_program_id``).
    dest.write_text(idl_with_program_id(idl_src.read_text(), project_root=workdir, crate=crate))
    _log.info("harness IDL: %s -> %s", idl_src, idl_dest)
    return idl_dest
