//! The **Crucible** application — AutoProver's Solana verification backend, which
//! authors [Crucible](https://github.com/asymmetric-research/crucible) fuzz harnesses
//! and gates them with the local `crucible` CLI. Pairs with the shared `solana`
//! ecosystem front half (see `docs/crucible-application.md`).
//!
//! A passive [`Backend`](autoprover_sdk::Backend) (`docs/rust-applications.md`): it supplies the
//! descriptor, toolchain precondition checks, the per-invariant `units`, the authoring prompts
//! (fixture + tests), and the two gating callouts — `compile` (a `crucible … --dry-run`
//! build) and `validate` (one `crucible … --mode explore` fuzz run per unit) — which run
//! the toolchain through the shared `run_confined` launcher. Python owns the loop.
//!
//! ## Where things live
//!
//! `app` holds the seam — the [`Backend`](autoprover_sdk::Backend) impl — and nothing else; each
//! callout is a few lines over the module that owns the material it needs:
//!
//!  * `declaration` — what this application declares about itself at load time.
//!  * `toolchain` — what must already be installed for a run to be possible.
//!  * `layout` — the harness crate's paths, and the names one unit produces.
//!  * `harness` — the harness crate itself: which program, where its types come from, every file.
//!  * `section` — one component's authored tests: its declaration, and its module file.
//!  * `prompts` — the authoring and review turns' prompts.
//!  * `facts` — the Anchor API facts mined from the analyzed model for the fixture author.
//!  * `templates` — the `.j2` bindings every renderer above fills.
//!  * `build_log` — did the harness build, and what does the author need to see.
//!  * `triage` — what a fuzz finding means, and which of a shared target's rows it refutes.
//!
//! Nothing is re-exported: the crate's only public surface is the PyO3 module below, so every module
//! is private and the names above are files, not paths a Rust consumer can reach.

mod app;
mod build_log;
mod campaign;
mod coverage;
mod declaration;
mod facts;
mod harness;
mod layout;
mod prompts;
mod section;
mod templates;
mod toolchain;
mod triage;

#[cfg(test)]
mod testkit;

use app::CrucibleApp;

// Emits one crate-root `#[pyfunction]` per callout, so no module above may be named after one of
// them — which is why the descriptor's is `declaration`.
autoprover_sdk::export_app!(crucible_app, CrucibleApp);
