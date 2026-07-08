"""Assemble a Crucible fuzz-harness crate (``docs/crucible-application.md`` §7.1).

Crucible's CLI runs a single ``invariant_test`` binary selected by a Cargo feature
(``find_fuzz_binary`` hardcodes the bin name), so per-component fan-out maps to
**one crate, one ``[[bin]]``, one feature-gated test section per component**, all
sharing the fixture/actions. This renders that crate:

* ``Cargo.toml`` — the feature list (one per component) + the pinned crucible /
  solana / anchor dependency stack (§6.1);
* ``src/main.rs`` — the shared fixture/actions, then each component's test fn
  verbatim (not user-``#[cfg]``-gated).

Selection is handled *by Crucible's own macros*: ``#[invariant_test] fn <name>``
(and ``#[crucible_fuzz]``) generate a ``main()`` behind ``#[cfg(feature = "<name>")]``
(crucible-invariant-macro), so building ``--features <name>`` compiles exactly one
``main()``. The load-bearing convention is therefore **the test fn's name equals
its Cargo feature** — here both are ``c_<slug>``. The store declares each feature
in ``Cargo.toml`` (the macro also reads the feature list to emit a "no test
selected" fallback ``main``); it must *not* wrap sections in ``#[cfg]`` itself, or
the macro's own cfg gate won't line up.

The dependency stack is pinned to the combination the installed toolchain matches;
a version table (§6.1) replaces the hardcoding later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# The crucible/solana/anchor stack a harness pins (docs §6.1). Hardcoded for now to
# the combination the installed toolchain matches (anchor-cli 1.1.x / solana 3.x).
_ANCHOR_VERSION = "1.0.1"
_SOLANA_VERSION = "3.0"
_LIBAFL_VERSION = "0.15.1"


@dataclass(frozen=True)
class CrucibleDep:
    """Resolves the fuzz crate's dependencies: the Crucible crates + the program.

    ``crucible_repo`` is a local checkout — the §6.1 version-resolved source; a git
    ref or a vendored image is a later refinement. ``program_crate`` /
    ``program_rel`` point the harness at the program under test as a path dep
    (``features = ["no-entrypoint"]``), so it links the same-version bindings.
    """

    crucible_repo: Path
    program_crate: str
    program_rel: str  # path from fuzz/<program>/ to the program crate

    def render_deps(self) -> str:
        crates = self.crucible_repo / "crates"
        return "\n".join(
            [
                f'crucible-fuzzer = {{ path = "{crates / "crucible-fuzzer"}" }}',
                f'crucible-test-context = {{ path = "{crates / "crucible-test-context"}" }}',
                f'anchor-lang = "{_ANCHOR_VERSION}"',
                'arbitrary = { version = "1", features = ["derive"] }',
                'ctrlc = "3.4"',
                f'libafl = {{ version = "{_LIBAFL_VERSION}", features = ["std", "cli", "prelude"] }}',
                f'libafl_bolts = {{ version = "{_LIBAFL_VERSION}", features = ["std"] }}',
                f'{self.program_crate} = {{ path = "{self.program_rel}", features = ["no-entrypoint"] }}',
                f'solana-keypair = "{_SOLANA_VERSION}"',
                f'solana-pubkey = "{_SOLANA_VERSION}"',
                f'solana-signer = "{_SOLANA_VERSION}"',
            ]
        )


@dataclass
class CrucibleHarness:
    """A buildable Crucible harness crate under construction: a shared fixture plus
    a set of per-component, feature-gated test sections."""

    program: str
    dep: CrucibleDep
    # Shared prelude + #[fuzz_fixture] + action_* — everything but the test fns.
    # Mutable: the authoring loop fills it in prepare_formalization (later phase).
    fixture_source: str = ""
    _components: dict[str, str] = field(default_factory=dict)  # feature -> test section

    @staticmethod
    def feature_for(slug: str) -> str:
        """The Cargo feature for a component slug — which, by Crucible's macro
        convention, must also be the name of the component's test fn."""
        return f"c_{slug}"

    def add_component(self, feature: str, test_source: str) -> None:
        """Register (or replace) a component's test section, keyed by its feature.

        ``test_source`` is the component's ``#[invariant_test]`` / ``#[crucible_fuzz]``
        fn *verbatim* — its fn name must equal ``feature`` (see module docstring).
        It is NOT ``#[cfg]``-wrapped here; the macro self-gates by fn name."""
        self._components[feature] = test_source.strip()

    @property
    def features(self) -> list[str]:
        return sorted(self._components)

    def render_cargo_toml(self, extra_features: tuple[str, ...] = ()) -> str:
        feats = sorted(set(self.features) | set(extra_features))
        features = "\n".join(f"{f} = []" for f in feats) or "# (no components yet)"
        return f"""\
[package]
name = "{self.program}_fuzz"
version = "0.1.0"
edition = "2021"

[workspace]

[dependencies]
{self.dep.render_deps()}

[[bin]]
name = "invariant_test"
path = "src/main.rs"

[features]
{features}
"""

    def render_main_rs(self) -> str:
        # Sections are emitted verbatim — Crucible's macros self-gate main() by
        # fn name == feature, so a --features <c_slug> build keeps exactly one main.
        body = "\n\n".join(self._components[f] for f in self.features)
        return self.fixture_source.rstrip() + "\n\n" + body + ("\n" if body else "")

    def write(self, fuzz_dir: Path) -> Path:
        """Write ``Cargo.toml`` + ``src/main.rs`` under ``fuzz_dir``; return the
        ``main.rs`` path."""
        (fuzz_dir / "src").mkdir(parents=True, exist_ok=True)
        (fuzz_dir / "Cargo.toml").write_text(self.render_cargo_toml())
        main = fuzz_dir / "src" / "main.rs"
        main.write_text(self.render_main_rs())
        return main

    def write_manifest(self, fuzz_dir: Path, extra_features: tuple[str, ...] = ()) -> Path:
        """Write only ``Cargo.toml`` (with ``extra_features`` unioned in). Used to
        pre-place the manifest before the setup session writes ``src/main.rs`` —
        the fixture decider can't render the deps (they're host-resolved, §6.1)."""
        fuzz_dir.mkdir(parents=True, exist_ok=True)
        cargo = fuzz_dir / "Cargo.toml"
        cargo.write_text(self.render_cargo_toml(extra_features))
        return cargo
