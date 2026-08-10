//! The harness crate a run writes: which program it targets, where its types come from, and every
//! file that renders from those two facts (manifest, toolchain pin, crate root, section paths).
//!
//! One renderer, so the crate a gated build compiles and the crate the user receives are the same
//! bytes (docs/crucible-component-units.md §17).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use askama::Template;
use autoprover_sdk::authoring::AuthorInput;
use autoprover_solana::{anchor_compat_key, SolanaPrepFacts, SolanaSourceUnit};

use crate::layout::{
    harness_dir, to_project_root, CRATE_ROOT, PREFLIGHT_FEATURE, PREFLIGHT_ROOT,
};
use crate::section::Section;
use crate::templates::{
    CargoDeps, CargoToml, HarnessGitignore, IdlPrelude, PreflightEntry, RootLayout, RustToolchain,
};

// The crucible/solana/anchor stack a harness pins (docs/crucible-application.md §6.1). Hardcoded
// for now to the combination the installed toolchain matches (was Python's `CrucibleHarness`).
pub(crate) const ANCHOR_VERSION: &str = "1.0.1";
const SOLANA_VERSION: &str = "3.0";
const LIBAFL_VERSION: &str = "0.15.1";

/// The toolchain the harness crate is built with — the `crucible` CLI forces this channel for the
/// harness build (`try_cargo_build`), so the crate pins it too (see `rust_toolchain.j2`).
const HARNESS_TOOLCHAIN: &str = "stable";

/// The crucible checkout that resolves the harness crate's path deps (`$CRUCIBLE_REPO`). Read
/// here so crate rendering is fully wheel-owned; `validate_preconditions` guarantees it is set.
pub(crate) fn crucible_repo() -> Option<PathBuf> {
    std::env::var("CRUCIBLE_REPO").ok().map(PathBuf::from)
}

/// Can the harness depend on the program's crate directly?
///
/// Only when the program's Anchor major matches the one this wheel links. Anchor's generated
/// `InstructionData` / `ToAccountMetas` impls belong to the exact `anchor-lang` crate that produced
/// them, so a program on another major can never satisfy `crucible-test-context`'s trait bounds —
/// and its transitive Solana stack usually cannot even co-resolve with ours (Solana 1.17's
/// `solana-frozen-abi` pins `ahash =0.8.5`, while `libafl 0.15` needs `^0.8.11`). No version
/// pinning fixes either; the IDL path exists for exactly this case.
///
/// An unknown requirement (no `anchor-lang`, or a git/path dep) keeps the crate path: that is the
/// historical behaviour, and the compiler reports it precisely if it turns out to be wrong.
pub(crate) fn crate_dep_usable(cr: &SolanaSourceUnit) -> bool {
    match (cr.anchor_compat(), anchor_compat_key(ANCHOR_VERSION)) {
        (Some(theirs), Some(ours)) => theirs == ours,
        _ => true,
    }
}

/// Where the harness gets the program's Anchor types from — the one axis the crate rendering turns
/// on (`docs/crucible-application.md` §6.1).
pub(crate) enum ProgramTypes {
    /// A path dependency on the program's crate: the real types, requiring a matching Anchor major.
    Crate,
    /// Generated from the program's IDL by `crucible-idl-gen`, at this harness-crate-relative path.
    /// The harness does not depend on the program at all, so the program's own toolchain never
    /// enters the harness build — the only way to fuzz a program built against another stack.
    Idl(String),
}

/// Everything the harness crate's rendering needs, derived once per callout from an [`AuthorInput`]
/// (or, in `finalize`, from the outcome set): which program it targets, and where its types come
/// from.
pub(crate) struct HarnessSpec {
    /// The analysis identifier. Names the harness crate itself — its directory under
    /// [`HARNESS_ROOT`](crate::layout::HARNESS_ROOT), package `<program>_fuzz`, and the selector
    /// `crucible run <program>` resolves — nothing else.
    program: String,
    /// The program under test's crate — every part populated, `SolanaSourceUnit::of` having applied
    /// the layout convention to whatever the host resolved. Used for the path dep under
    /// [`ProgramTypes::Crate`]; its `lib` is the module holding the program's types under *either*
    /// mode, so the authored fixture's `use <id>::*` is identical.
    cr: SolanaSourceUnit,
    types: ProgramTypes,
}

impl HarnessSpec {
    /// The spec for `input`. The mode follows what the host reports the prep established — an `idl`
    /// fact means it placed one at that (workdir-relative) path because `workspace_prep` asked for
    /// one, so the types are generated. See [`CrucibleApp::workspace_prep`](crate::app::CrucibleApp),
    /// which makes the decision.
    pub(crate) fn of(input: &AuthorInput) -> Self {
        Self::new(
            &input.program,
            SolanaSourceUnit::from_input(input),
            SolanaPrepFacts::from_input(input).idl.unwrap_or_default(),
        )
    }

    /// `idl_at` is the IDL's workdir-relative path, or empty for the crate path.
    pub(crate) fn new(program: &str, cr: SolanaSourceUnit, idl_at: String) -> Self {
        let types = if idl_at.is_empty() {
            ProgramTypes::Crate
        } else {
            // `declare_fuzz_program!` resolves its path against the harness crate's manifest dir,
            // so make the host's workdir-relative report crate-relative.
            let prefix = format!("{}/", harness_dir(program));
            ProgramTypes::Idl(idl_at.strip_prefix(&prefix).unwrap_or(&idl_at).to_string())
        };
        Self { program: program.to_string(), cr, types }
    }

    /// The harness crate's directory, relative to the project root — the prefix of every file this
    /// spec renders. Pass [`HarnessSpec::dir_arg`] to `crucible run -C`, not this.
    pub(crate) fn dir(&self) -> String {
        harness_dir(&self.program)
    }

    /// The harness crate's directory as `crucible run -C` must receive it: **absolute**, resolved
    /// against the workdir the host materialized the crate in.
    ///
    /// The CLI passes `-C` through unresolved (every *other* path it takes goes through its
    /// `resolve_path`), then spawns the built binary with the harness dir as the child's cwd. On
    /// Unix the chdir precedes the exec, so a relative `-C` makes the equally-relative binary path
    /// resolve a second time against the crate — `<dir>/<dir>/target/release/invariant_test` — and
    /// the spawn fails with a bare `No such file or directory`, *after* a successful build. An
    /// absolute `-C` cannot be re-resolved, so it doesn't depend on that.
    pub(crate) fn dir_arg(&self, workdir: &Path) -> String {
        workdir.join(self.dir()).to_string_lossy().into_owned()
    }

    /// A crate-relative path spelled from the project root, the frame the host writes files in.
    fn path(&self, rel: &str) -> String {
        format!("{}/{rel}", self.dir())
    }

    /// The module the program's `instruction`/`accounts`/state types live under — the crate's lib
    /// name in both modes (the generated module is named for it), so prompts don't branch.
    pub(crate) fn crate_id(&self) -> &str {
        &self.cr.lib
    }

    /// Are the program's types generated from its IDL (rather than its crate)?
    pub(crate) fn is_idl(&self) -> bool {
        matches!(self.types, ProgramTypes::Idl(_))
    }

    /// Where the host should place the IDL when this spec's mode needs one: inside the harness
    /// crate, so the delivered crate carries the IDL it was built against.
    pub(crate) fn idl_dest(&self) -> String {
        self.path(&format!("idls/{}.json", self.cr.lib))
    }

    /// The `[dependencies]` block — the pinned crucible/solana/anchor stack plus *either* the
    /// program crate as a path dep *or* `crucible-idl-gen` and the three crates its generated code
    /// references: `bytemuck` (zero-copy state casts), `ctor` (schema registration) and `fixed`
    /// (`I80F48` conversions, emitted for any `Wrapped*I80F48`-shaped IDL type — which every
    /// fixed-point lending program has).
    fn deps(&self, repo: &Path) -> String {
        let crates = repo.join("crates");
        let cf = crates.join("crucible-fuzzer").display().to_string();
        let ctc = crates.join("crucible-test-context").display().to_string();
        let idl_gen = crates.join("crucible-idl-gen").display().to_string();
        CargoDeps {
            cf: &cf,
            ctc: &ctc,
            idl: self.is_idl(),
            idl_gen: &idl_gen,
            package: &self.cr.package,
            crate_dir: &self.cr.dir,
            to_root: &to_project_root(),
            anchor_version: ANCHOR_VERSION,
            libafl_version: LIBAFL_VERSION,
            solana_version: SOLANA_VERSION,
        }
        .render()
        .expect("render cargo_deps")
    }

    /// The harness `Cargo.toml`: one `[[bin]]` (always named `invariant_test`, at `bin_path`)
    /// selected by a per-component Cargo feature. `features` are inert (`f = []`) — Crucible's macro
    /// self-gates `main()` by fn name == feature — so a build only needs the feature it selects
    /// declared.
    fn cargo_toml(&self, repo: &Path, bin_path: &str, features: &[String]) -> String {
        let feats = if features.is_empty() {
            "# (no components yet)".to_string()
        } else {
            features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
        };
        let deps = self.deps(repo);
        CargoToml { program: &self.program, deps: &deps, feats: &feats, bin_path }
            .render()
            .expect("render cargo_toml")
    }

    /// Everything in the crate that isn't source: a `Cargo.toml` pointing `[[bin]]` at `bin_path`
    /// and declaring exactly `features`, the `rust-toolchain.toml` that pins which cargo resolves
    /// this directory, and the `.gitignore` keeping the run's build output out of the deliverable.
    /// The first two are needed before dependency warming, which is why these are separable from
    /// the crate's sources.
    pub(crate) fn manifest_files(
        &self, bin_path: &str, features: &[String],
    ) -> BTreeMap<String, String> {
        let mut files = BTreeMap::new();
        files.insert(
            self.path("rust-toolchain.toml"),
            RustToolchain { channel: HARNESS_TOOLCHAIN }.render().expect("render rust_toolchain"),
        );
        files.insert(
            self.path(".gitignore"),
            HarnessGitignore.render().expect("render harness_gitignore"),
        );
        if let Some(repo) = crucible_repo() {
            files.insert(self.path("Cargo.toml"), self.cargo_toml(&repo, bin_path, features));
        }
        files
    }

    /// `main.rs` for `spec`: under the IDL path the generated module is declared here rather than
    /// by the model — the host owns the crate's scaffolding, and the authored fixture only ever
    /// writes `use <crate_id>::*`.
    fn main_rs(&self, root: &str) -> String {
        match &self.types {
            ProgramTypes::Crate => root.to_string(),
            ProgramTypes::Idl(path) => {
                let decl = IdlPrelude { module: self.crate_id(), path }
                    .render()
                    .expect("render idl_prelude");
                format!("{decl}\n{root}")
            }
        }
    }

    /// Every Cargo feature a crate covering `units` declares: the wheel's own preflight first — the
    /// one target that exists before any unit does — then one per component.
    pub(crate) fn features(units: &[String]) -> Vec<String> {
        std::iter::once(PREFLIGHT_FEATURE.to_string()).chain(units.iter().cloned()).collect()
    }

    /// The **deliverable** crate for `units`: the manifest, the toolchain pin, and a [`CRATE_ROOT`]
    /// holding the fixture, the preflight entry, and one gated `mod` + `#[invariant_test]` entry per
    /// component.
    ///
    /// Rendered by the **setup gate** — the first callout holding both halves, the authored fixture
    /// and the unit set — and re-rendered identically by `crate_root`, whose job is to land these
    /// files for the run whose setup spec came from cache and so never reached that gate. One
    /// renderer and one set of inputs, so `main.rs` never exists in a provisional shape and the gated
    /// builds compile the crate the user receives (docs/crucible-component-units.md §17).
    ///
    /// A `#[cfg]`-disabled `mod` is stripped before rustc resolves its file, so declaring every
    /// component here costs a build nothing — including the setup gate, which runs before a single
    /// section file exists: it compiles the one feature it selects and never looks for the others.
    pub(crate) fn scaffold(&self, fixture: &str, units: &[String]) -> BTreeMap<String, String> {
        let mut files = self.manifest_files(CRATE_ROOT, &Self::features(units));
        files.insert(self.path(CRATE_ROOT), self.main_rs(&self.root_text(fixture, units)));
        files
    }

    /// A crate root: the fixture, the layout header, the preflight entry, then one gated `mod` +
    /// `#[invariant_test]` entry per component feature.
    fn root_text(&self, fixture: &str, units: &[String]) -> String {
        let program_so = format!("{}target/deploy/{}.so", to_project_root(), self.crate_id());
        let layout = RootLayout { program_so: &program_so }.render().expect("render root_layout");
        let preflight = PreflightEntry { program: &self.program, feature: PREFLIGHT_FEATURE }
            .render()
            .expect("render preflight_entry");
        let decls = std::iter::once(preflight)
            .chain(units.iter().map(|f| Section::entry(f)))
            .collect::<Vec<_>>()
            .join("\n\n");
        format!("{}\n\n{layout}\n\n{decls}\n", fixture.trim_end())
    }

    /// The one-file crate the **preflight** gate builds, at [`PREFLIGHT_ROOT`]: the wheel's skeleton
    /// fixture and the preflight entry, and nothing else — it runs before analysis, so no unit exists
    /// to declare and nothing has been authored to put behind one.
    ///
    /// It is not [`CRATE_ROOT`] because nothing that belongs there exists yet, and writing that path
    /// from a gate this early would leave a half-crate at the deliverable's own root.
    pub(crate) fn preflight_files(&self, fixture: &str) -> BTreeMap<String, String> {
        let mut files = self.manifest_files(PREFLIGHT_ROOT, &Self::features(&[]));
        files.insert(self.path(PREFLIGHT_ROOT), self.main_rs(&self.root_text(fixture, &[])));
        files
    }

    /// The one file a component callout writes: its own section. The crate root already declares it
    /// (`scaffold`), so a gated build materializes nothing else — this is what makes the section the
    /// only thing that varies between a build and the deliverable.
    pub(crate) fn section_files(
        &self, feature: &str, authored: &str,
    ) -> BTreeMap<String, String> {
        BTreeMap::from([(self.section_path(feature), Section::file(feature, authored))])
    }

    /// Where one component's authored tests live — `src/<feature>.rs`, the file the crate root's
    /// `mod <feature>;` resolves to.
    pub(crate) fn section_path(&self, feature: &str) -> String {
        self.path(&format!("src/{feature}.rs"))
    }
}

#[cfg(test)]
mod tests {
    //! The build-critical crate files must render byte-identically to the former `format!` output
    //! (else the harness crate won't compile), and every gate must write the crate the run
    //! delivers — no provisional shapes, no half-crates.
    use super::*;
    use crate::app::CrucibleApp;
    use crate::layout::feature_of;
    use crate::testkit::{
        at, code_only, distinct_crate, gated, lending_crate, prep_input, scaffold, skewed_crate,
        spec_of,
    };
    use autoprover_sdk::Backend;

    /// The expected `[dependencies]` block, spelled out independently of the template (originally
    /// the `crucible_deps` `format!` body, kept as the oracle across the askama migration). The
    /// program's types come either from its crate or from `crucible-idl-gen` — never both.
    fn expected_deps(spec: &HarnessSpec, repo: &Path) -> String {
        let crates = repo.join("crates");
        let (idl_deps, program_dep) = if spec.is_idl() {
            (
                format!(
                    "bytemuck = \"1.14\"\n\
                     crucible-idl-gen = {{ path = \"{}\" }}\n\
                     ctor = \"0.6\"\n\
                     ctrlc = \"3.4\"\n\
                     fixed = \"1\"\n",
                    crates.join("crucible-idl-gen").display()
                ),
                String::new(),
            )
        } else {
            (
                "ctrlc = \"3.4\"\n".to_string(),
                format!(
                    "{} = {{ path = \"{}{}\", features = [\"no-entrypoint\"] }}\n",
                    spec.cr.package,
                    to_project_root(),
                    spec.cr.dir
                ),
            )
        };
        format!(
            "crucible-fuzzer = {{ path = \"{cf}\" }}\n\
             crucible-test-context = {{ path = \"{ctc}\" }}\n\
             anchor-lang = \"{ANCHOR_VERSION}\"\n\
             arbitrary = {{ version = \"1\", features = [\"derive\"] }}\n\
             {idl_deps}\
             libafl = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\", \"cli\", \"prelude\"] }}\n\
             libafl_bolts = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\"] }}\n\
             {program_dep}solana-keypair = \"{SOLANA_VERSION}\"\n\
             solana-pubkey = \"{SOLANA_VERSION}\"\n\
             solana-signer = \"{SOLANA_VERSION}\"",
            cf = crates.join("crucible-fuzzer").display(),
            ctc = crates.join("crucible-test-context").display(),
        )
    }

    /// The expected harness manifest, spelled out independently of the template.
    fn expected_cargo_toml(
        spec: &HarnessSpec, repo: &Path, bin_path: &str, features: &[String],
    ) -> String {
        let feats = if features.is_empty() {
            "# (no components yet)".to_string()
        } else {
            features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
        };
        format!(
            "[package]\n\
             name = \"{program}_fuzz\"\n\
             version = \"0.1.0\"\n\
             edition = \"2021\"\n\
             \n\
             [workspace]\n\
             \n\
             [dependencies]\n\
             {deps}\n\
             \n\
             [[bin]]\n\
             name = \"invariant_test\"\n\
             path = \"{bin_path}\"\n\
             \n\
             [features]\n\
             {feats}\n",
            program = spec.program,
            deps = expected_deps(spec, repo),
        )
    }

    /// A path inside the `vault` harness crate, as the host receives it (workdir-relative). Derived
    /// rather than spelled so a move touches one place — `the_crate_lands_under_the_deliverable_dir`
    /// is what pins the layout itself.
    fn at_vault(rel: &str) -> String {
        at("vault", rel)
    }

    /// [`at_vault`] for the `lending` program the crate-root fixtures below analyze.
    fn at_lending(rel: &str) -> String {
        at("lending", rel)
    }

    #[test]
    fn crate_files_render_the_expected_manifest() {
        let repo = Path::new("/home/user/crucible");
        let specs = [
            spec_of(SolanaSourceUnit::default().resolved("vault"), ""),
            spec_of(distinct_crate(), ""),
            spec_of(skewed_crate(), &at_vault("idls/example_lending.json")),
        ];
        for spec in &specs {
            assert_eq!(spec.deps(repo), expected_deps(spec, repo));
            // empty features
            assert_eq!(
                spec.cargo_toml(repo, CRATE_ROOT, &[]),
                expected_cargo_toml(spec, repo, CRATE_ROOT, &[]),
            );
            // one and several features
            for feats in
                [vec!["c_invariants".to_string()], vec!["c_preflight".into(), "c_invariants".into()]]
            {
                for bin_path in [CRATE_ROOT, PREFLIGHT_ROOT] {
                    assert_eq!(
                        spec.cargo_toml(repo, bin_path, &feats),
                        expected_cargo_toml(spec, repo, bin_path, &feats),
                        "cargo_toml mismatch for {bin_path} + features {feats:?}",
                    );
                }
            }
        }
    }

    #[test]
    fn the_crate_lands_under_the_deliverable_dir_and_declares_its_program_so() {
        // Spelled literally exactly here — every other path in these tests derives from
        // `harness_dir`, so this is the one assertion a move has to be argued past. The crate is
        // part of the deliverable rather than a stray `fuzz/` at the project root; `crucible run`
        // is pointed at it with `-C`.
        assert_eq!(harness_dir("vault"), "certora/crucible/fuzz/vault");
        assert_eq!(to_project_root(), "../../../../");
        // `crucible run` chdirs into the crate, so the crate's own paths are anchored at that
        // depth. The crate root spells it once; the fixture (authored, and unable to check it)
        // only ever names the constant.
        let files = spec_of(distinct_crate(), "").scaffold("struct Fixture {}", &[]);
        let main_rs = &files[&at_vault(CRATE_ROOT)];
        assert!(
            main_rs.contains(
                "const PROGRAM_SO: &str = \"../../../../target/deploy/example_lending.so\";"
            ),
            "unexpected .so constant in:\n{main_rs}"
        );
        // Living under the deliverable dir means a user commits this crate — and a run fills it
        // with ~900 MB of build output that must not go with it (`crucible run` chdirs here, and
        // `find_fuzz_binary` pins `target/`, so it cannot be redirected).
        let ignore = &files[&at_vault(".gitignore")];
        for entry in ["target/", "crashes/"] {
            assert!(ignore.contains(entry), "{entry} not ignored in:\n{ignore}");
        }
    }

    #[test]
    fn the_cli_is_pointed_at_the_crate_absolutely() {
        // `-C` is the one path the CLI takes without resolving it, and it makes the harness dir the
        // spawned binary's cwd — so a *relative* `-C` leaves the binary path (derived from it, and
        // equally relative) to resolve a second time against the crate, and the spawn dies with a
        // bare `No such file or directory` after a clean build. Absolute cannot be re-resolved.
        let arg = spec_of(distinct_crate(), "").dir_arg(Path::new("/w"));
        assert_eq!(arg, format!("/w/{}", harness_dir("vault")));
        assert!(Path::new(&arg).is_absolute(), "a relative -C would be re-resolved: {arg}");
    }

    #[test]
    fn the_program_dep_points_at_the_resolved_crate_not_the_analysis_id() {
        let repo = Path::new("/home/user/crucible");
        let deps = spec_of(distinct_crate(), "").deps(repo);
        let up = to_project_root();
        // Keyed by the Cargo package name, pointing at the real directory — both independent of
        // the `vault` identifier the harness crate itself is named after.
        assert!(
            deps.contains(&format!(
                "example-lending = {{ path = \"{up}programs/lend\", features = [\"no-entrypoint\"] }}"
            )),
            "unexpected program dep in:\n{deps}"
        );
        // The layout convention still holds when the host resolved nothing.
        let legacy = spec_of(SolanaSourceUnit::default().resolved("vault"), "").deps(repo);
        assert!(
            legacy.contains(&format!(
                "vault = {{ path = \"{up}programs/vault\", features = [\"no-entrypoint\"] }}"
            )),
            "unexpected fallback dep in:\n{legacy}"
        );
    }

    #[test]
    fn the_idl_path_drops_the_program_dep_and_declares_the_generated_module() {
        let repo = Path::new("/home/user/crucible");
        let spec = spec_of(skewed_crate(), &at_vault("idls/example_lending.json"));
        let deps = spec.deps(repo);
        // Nothing about the program under test is in the graph — that is the whole point: its
        // Anchor/Solana stack can neither co-resolve with ours nor satisfy our trait bounds.
        assert!(!deps.contains("programs/lend"), "program still a dependency:\n{deps}");
        assert!(!deps.contains("no-entrypoint"), "program still a dependency:\n{deps}");
        for needle in ["crucible-idl-gen = { path =", "bytemuck = \"1.14\"", "ctor = \"0.6\""] {
            assert!(deps.contains(needle), "missing {needle:?} in:\n{deps}");
        }
        // The generated module is declared for the model, keyed to the crate's lib name so the
        // authored fixture's `use <id>::*` reads the same as on the crate path. The macro resolves
        // its path against the harness crate, so the host's workdir-relative report is stripped.
        let main_rs = spec.main_rs("use example_lending::*;\n");
        assert!(
            main_rs.contains(
                "crucible_idl_gen::declare_fuzz_program!(example_lending = \"idls/example_lending.json\");"
            ),
            "unexpected prelude in:\n{main_rs}"
        );
        assert!(main_rs.ends_with("use example_lending::*;\n"), "authored source not kept verbatim");
        // The crate path adds nothing to what the model wrote.
        assert_eq!(spec_of(distinct_crate(), "").main_rs("fn x() {}"), "fn x() {}");
    }

    #[test]
    fn the_harness_crate_pins_its_own_toolchain() {
        // rustup picks a toolchain by directory and the harness crate lives *inside* the target
        // project, so without this file the project's `rust-toolchain.toml` would decide which cargo
        // warms the deps — a different one from the `crucible` build's, and often too old to parse a
        // dependency's manifest at all.
        let spec = spec_of(distinct_crate(), "");
        let pin = &spec.manifest_files(CRATE_ROOT, &[])[&at_vault("rust-toolchain.toml")];
        assert!(pin.contains(&format!("channel = \"{HARNESS_TOOLCHAIN}\"")), "unexpected pin:\n{pin}");
        // Emitted with the crate under every path: warming (manifest only) and the deliverable.
        assert!(spec
            .preflight_files("struct Fixture {}")
            .contains_key(&at_vault("rust-toolchain.toml")));
        let plan = CrucibleApp.workspace_prep(&prep_input(distinct_crate(), serde_json::json!({})));
        assert!(plan.files.contains_key(&at_vault("rust-toolchain.toml")));
    }

    #[test]
    fn crate_dep_usability_tracks_the_anchor_compatibility_unit() {
        let with = |anchor: &str| SolanaSourceUnit { anchor: anchor.into(), ..distinct_crate() };
        // Same major as ours (1.0.1) — patch/minor differences are compatible.
        for ok in [ANCHOR_VERSION, "1.0.0", "^1.2", "=1.9.9"] {
            assert!(crate_dep_usable(&with(ok)), "{ok} should be linkable");
        }
        // A 0.x release is its own compatibility unit, so every one of them is out.
        for bad in ["0.29.0", "0.31", "^0.30.1", "2.0.0"] {
            assert!(!crate_dep_usable(&with(bad)), "{bad} should NOT be linkable");
        }
        // Unknown (not an Anchor program, or a git dep) keeps the historical crate path.
        for unknown in ["", "*", "workspace"] {
            assert!(crate_dep_usable(&with(unknown)), "{unknown:?} should fall back to the crate");
        }
    }

    #[test]
    fn the_crate_root_is_written_before_anything_is_authored() {
        // It depends only on the fixture and the unit NAMES, which is what lets it be written once
        // between the setup step and fan-out — and therefore never rewritten by a gated build.
        let files = scaffold(&["a", "b"]);
        let code = code_only(&files[&at_lending("src/main.rs")]);
        assert!(code.contains("struct Fixture {}"), "{code}");
        assert!(code.contains("mod c_a;") && code.contains("mod c_b;"), "{code}");
        // Every declared module has a feature, so `crucible run lending c_b` resolves from the start.
        if let Some(cargo) = files.get(&at_lending("Cargo.toml")) {
            assert!(cargo.contains("c_a = []") && cargo.contains("c_b = []"), "{cargo}");
        }
        // No section bodies: none have been authored yet.
        assert!(!code.contains("read_token_balance"), "{code}");
    }

    #[test]
    fn the_preflight_is_a_crate_of_one_file_that_is_not_the_deliverables_root() {
        // It runs before analysis, so nothing that belongs in `src/main.rs` exists yet — and writing
        // that path this early is what used to leave a half-crate at the deliverable's own root. Same
        // `[[bin]]` NAME (all `crucible run` will ever execute) at a path of its own.
        let spec = HarnessSpec::new("lending", SolanaSourceUnit::default(), String::new());
        let gate = spec.preflight_files("struct Fixture {}");
        assert_eq!(
            gate.keys().filter(|k| k.ends_with(".rs")).collect::<Vec<_>>(),
            vec![&at_lending("src/preflight.rs")],
            "the preflight must be one source file",
        );
        assert!(!gate.contains_key(&at_lending("src/main.rs")), "{:?}", gate.keys());
        if let Some(cargo) = gate.get(&at_lending("Cargo.toml")) {
            assert!(cargo.contains(r#"name = "invariant_test""#), "{cargo}");
            assert!(cargo.contains(r#"path = "src/preflight.rs""#), "{cargo}");
        }
        // That one file is a whole crate: the fixture and the gated entry the dry-run selects.
        let root = &gate[&at_lending("src/preflight.rs")];
        assert!(root.contains("struct Fixture {}"), "{root}");
        assert!(root.contains("#[cfg(feature = \"preflight\")]"), "{root}");
        assert!(root.contains("fn preflight(fixture: &mut Fixture)"), "{root}");
        // And from the setup gate on, the same bin points at the real root.
        if let Some(cargo) = scaffold(&["a"]).get(&at_lending("Cargo.toml")) {
            assert!(cargo.contains(r#"path = "src/main.rs""#), "{cargo}");
        }
    }

    #[test]
    fn the_preflight_is_an_entry_the_delivered_crate_can_still_run() {
        // The gates' sanity check survives into the deliverable as a target like any component's, so
        // a user can re-run it (`crucible run <program> preflight --dry-run`) through the same
        // mechanism rather than it being a build-time artifact nobody can reach.
        let shipped = scaffold(&["a"]);
        let main_rs = &shipped[&at_lending("src/main.rs")];
        assert!(main_rs.contains("#[cfg(feature = \"preflight\")]"), "{main_rs}");
        assert!(main_rs.contains("fn preflight(fixture: &mut Fixture)"), "{main_rs}");
        // Its body is inline — there is no authored section to delegate to, and so no file to drift.
        assert!(!main_rs.contains("mod preflight;"), "{main_rs}");
        assert!(!shipped.contains_key(&at_lending("src/preflight.rs")), "{:?}", shipped.keys());
        // And it asserts nothing about the program: it proves the fixture compiles and loads.
        assert!(!main_rs.contains("fuzz_assert"), "{main_rs}");
    }

    #[test]
    fn the_setup_gate_builds_the_crate_the_run_delivers() {
        // The setup gate is the first callout holding both the fixture and the unit set, so it builds
        // the real `src/main.rs` rather than a provisional root something later has to complete.
        // `crate_root` re-emits the same files for the run whose setup spec came from cache and so
        // never reached that gate — byte-identical, or `main.rs` would have two assembled shapes and
        // the §17 drift would be back.
        let units = ["a", "b"].map(feature_of).to_vec();
        let gate = HarnessSpec::new("lending", lending_crate(), String::new())
            .scaffold("struct Fixture {}", &units);
        assert_eq!(gate, scaffold(&["a", "b"]), "the setup gate and `crate_root` disagree");
        // The gate's build materializes no section file: every component's is `#[cfg]`-disabled and
        // stripped before rustc resolves it, which is what lets the real root be built this early.
        assert_eq!(
            gate.keys().filter(|k| k.ends_with(".rs")).collect::<Vec<_>>(),
            vec![&at_lending("src/main.rs")],
        );
    }

    #[test]
    fn a_gated_build_writes_only_its_own_section() {
        // The crate root is already on disk, written once for the whole unit set, so a gate adds one
        // file and rewrites nothing. A `#[cfg]`-disabled `mod` is stripped before rustc resolves its
        // file, which is why declaring every component costs this build nothing.
        let gate = gated("c_a", "pub fn invariants(fixture: &mut Fixture) {}");
        assert_eq!(
            gate.keys().collect::<Vec<_>>(),
            vec![&at_lending("src/c_a.rs")],
            "a gated build must not rewrite the crate root or the manifest",
        );
    }
}
