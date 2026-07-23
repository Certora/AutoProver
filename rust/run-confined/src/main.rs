//! `run-confined` — the trusted launcher for the `RunCommand` sandbox.
//!
//! It applies four unprivileged, in-kernel confinements to *itself*, then `execve`s
//! the requested command (which inherits all of them across the exec):
//!
//!   1. **Landlock** — a filesystem ruleset: default-deny, then grant `--rw` paths
//!      full access and `--ro` paths read+execute. Confines reads *and* writes and,
//!      by not granting `/proc`, closes the same-uid `/proc/<parent>/environ` leak.
//!      On kernels with ABI ≥6, also scopes signals and abstract Unix sockets so the
//!      child cannot SIGKILL the parent or talk to abstract UDS outside the sandbox.
//!      On kernels with ABI ≥4 and `--allow-network` off, also default-denies Landlock
//!      TCP bind/connect (defense-in-depth; UDP still blocked by seccomp).
//!   2. **seccomp** — deny non-`AF_UNIX` `socket()` (blocks TCP, UDP/DNS, IMDS, netlink,
//!      vsock, …), deny `io_uring_*` (blocks the classic seccomp network bypass), and
//!      deny `ptrace`/`process_vm_readv`/`process_vm_writev`. On x86_64 each deny is
//!      mirrored onto its x32-ABI syscall number (`nr | 0x4000_0000`) so the x32
//!      calling convention cannot slip a denied syscall past the exact-number rules.
//!   3. **env allowlist** — `execve` with only `--allow-env` variables (a scrubbed
//!      environment).
//!   4. **rlimits** — `--rlimit-*` caps on address space / CPU-seconds / pids / file size.
//!
//! This is trusted code: its argv is authored by the Python side (never the LLM,
//! which controls only file *contents*). It is **fail-closed** — any setup failure,
//! or a kernel without Landlock, exits nonzero *without* execing the command, so
//! untrusted input never runs unconfined.
//!
//! See `docs/command-sandbox.md` (§6) for the design and the validation matrix.

use std::collections::BTreeMap;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;

use clap::Parser;
use landlock::{
    Access, AccessFs, AccessNet, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset,
    RulesetAttr, RulesetCreatedAttr, RulesetStatus, Scope, ABI,
};
use seccompiler::{
    apply_filter, BpfProgram, SeccompAction, SeccompCmpArgLen, SeccompCmpOp, SeccompCondition,
    SeccompFilter, SeccompRule, TargetArch,
};

// Exit codes follow the coreutils exec-wrapper convention (`env`, `timeout`,
// `nice`): the launcher reserves the high 125–127 band for *its own* failures so
// they can't be confused with the wrapped command's status, which otherwise passes
// through untouched across the `execve`.

/// `run-confined` itself failed before handing off — a bad argv *or* the sandbox
/// could not be established. Fail-closed: the command was NOT run. (Both cases mean
/// "we never reached your command"; the specific reason is on stderr / `--probe`.)
const EXIT_LAUNCHER_FAILED: i32 = 125;
/// The command was found but could not be executed (e.g. not executable, bad format)
/// — `exec` failed with something other than `ENOENT`.
const EXIT_NOT_EXECUTABLE: i32 = 126;
/// The command was not found — `exec` failed with `ENOENT`. Matches shells' 127.
const EXIT_NOT_FOUND: i32 = 127;

/// Command-line surface. The argv is authored by the trusted Python caller, so the
/// contract here mirrors that caller's expectations; `--probe` is handled separately
/// (before clap) because it short-circuits into a self-restricting kernel check.
#[derive(Parser)]
#[command(
    name = "run-confined",
    about = "Confine this process (Landlock + seccomp + rlimits + scrubbed env), then exec the command after `--`.",
    disable_help_flag = true
)]
struct Cli {
    /// Grant full read+write access beneath PATH (repeatable).
    #[arg(long = "rw", value_name = "PATH")]
    rw: Vec<PathBuf>,

    /// Grant read+execute access beneath PATH (repeatable).
    #[arg(long = "ro", value_name = "PATH")]
    ro: Vec<PathBuf>,

    /// Allow network syscalls (skip the seccomp + Landlock net deny).
    #[arg(long = "allow-network")]
    allow_network: bool,

    /// Pass NAME=VALUE, or bare NAME to forward it from the current environment if set
    /// (repeatable). NAME not in the environment is silently skipped.
    #[arg(long = "allow-env", value_name = "SPEC")]
    allow_env: Vec<String>,

    /// RLIMIT_AS cap (bytes of address space).
    #[arg(long = "rlimit-as", value_name = "BYTES")]
    rlimit_as: Option<u64>,
    /// RLIMIT_CPU cap (CPU-seconds).
    #[arg(long = "rlimit-cpu", value_name = "SECONDS")]
    rlimit_cpu: Option<u64>,
    /// RLIMIT_NPROC cap (max processes).
    #[arg(long = "rlimit-nproc", value_name = "COUNT")]
    rlimit_nproc: Option<u64>,
    /// RLIMIT_FSIZE cap (max file size, bytes).
    #[arg(long = "rlimit-fsize", value_name = "BYTES")]
    rlimit_fsize: Option<u64>,

    /// The command to run, given after `--`: PROGRAM followed by its ARGS.
    #[arg(last = true, required = true, value_name = "PROGRAM [ARGS...]")]
    command: Vec<String>,
}

impl Cli {
    /// Lower the parsed surface into the `Config` the confinement steps consume,
    /// resolving `--allow-env` specs against the current environment.
    fn into_config(self) -> Config {
        let mut env = Vec::new();
        for spec in self.allow_env {
            if let Some((name, value)) = spec.split_once('=') {
                env.push((name.to_string(), value.to_string()));
            } else if let Ok(value) = std::env::var(&spec) {
                // NAME with no '=': pass through from the current environment if set.
                env.push((spec, value));
            }
            // NAME not present in the environment: silently skip (nothing to pass).
        }

        // `required = true` on `command` guarantees at least the program is present.
        let mut command = self.command;
        let program = command.remove(0);

        Config {
            rw_paths: self.rw,
            ro_paths: self.ro,
            env,
            allow_network: self.allow_network,
            rlimit_as: self.rlimit_as,
            rlimit_cpu: self.rlimit_cpu,
            rlimit_nproc: self.rlimit_nproc,
            rlimit_fsize: self.rlimit_fsize,
            program,
            args: command,
        }
    }
}

struct Config {
    rw_paths: Vec<PathBuf>,
    ro_paths: Vec<PathBuf>,
    env: Vec<(String, String)>,
    allow_network: bool,
    rlimit_as: Option<u64>,
    rlimit_cpu: Option<u64>,
    rlimit_nproc: Option<u64>,
    rlimit_fsize: Option<u64>,
    program: String,
    args: Vec<String>,
}

fn die(code: i32, msg: &str) -> ! {
    eprintln!("run-confined: {msg}");
    std::process::exit(code);
}

fn main() {
    // `--probe` short-circuits before clap: it is a standalone kernel-capability check
    // (it restricts *this* throwaway process), takes no other arguments, and must not
    // trip clap's required-`command` rule.
    if std::env::args().nth(1).as_deref() == Some("--probe") {
        probe();
    }

    // clap prints its own usage/error text; a malformed argv is a launcher failure
    // (EXIT_LAUNCHER_FAILED). Help/version requests are not errors and still exit 0.
    let cfg = Cli::try_parse()
        .unwrap_or_else(|e| {
            let _ = e.print();
            std::process::exit(if e.use_stderr() { EXIT_LAUNCHER_FAILED } else { 0 });
        })
        .into_config();

    // Order matters: rlimits + env are harmless early; apply Landlock, then seccomp
    // LAST so our own setup syscalls aren't caught by the filter; then exec.
    set_rlimits(&cfg);
    set_no_new_privs();
    if let Err(e) = apply_landlock(&cfg) {
        die(EXIT_LAUNCHER_FAILED, &format!("Landlock setup failed: {e}"));
    }
    if let Err(e) = apply_seccomp(&cfg) {
        die(EXIT_LAUNCHER_FAILED, &format!("seccomp setup failed: {e}"));
    }

    let mut cmd = Command::new(&cfg.program);
    cmd.args(&cfg.args).env_clear().envs(cfg.env.iter().cloned());
    // `exec` replaces this process image; it only returns on failure. Split ENOENT
    // (not found → 127) from every other exec error (found but unrunnable → 126).
    let err = cmd.exec();
    let code = if err.raw_os_error() == Some(libc::ENOENT) {
        EXIT_NOT_FOUND
    } else {
        EXIT_NOT_EXECUTABLE
    };
    die(code, &format!("exec {:?} failed: {err}", cfg.program));
}

/// `--probe`: report whether the kernel supports Landlock. Exit 0 + print the
/// enforcement status if so; exit `EXIT_LAUNCHER_FAILED` otherwise. Drives
/// Python's fail-closed `available()` check.
///
/// We probe through the crate's public API rather than the raw
/// `landlock_create_ruleset` syscall — the crate deliberately hides the numeric
/// ABI, and this reuses the exact BestEffort negotiation `apply_landlock` does.
/// It restricts *this* process as a side effect, which is harmless: `--probe` is
/// a throwaway process that exits immediately after reporting.
fn probe() -> ! {
    let status = Ruleset::default()
        .set_compatibility(CompatLevel::BestEffort)
        .handle_access(AccessFs::from_all(ABI::V5))
        .and_then(|r| r.scope(Scope::from_all(ABI::V6)))
        .and_then(|r| r.create())
        .and_then(|r| r.restrict_self());
    match status {
        Ok(s) if !matches!(s.ruleset, RulesetStatus::NotEnforced) => {
            println!("landlock {:?}", s.ruleset);
            std::process::exit(0);
        }
        _ => die(
            EXIT_LAUNCHER_FAILED,
            "kernel does not support Landlock (need Linux >= 5.13); refusing to run unconfined",
        ),
    }
}

fn set_rlimits(cfg: &Config) {
    let set = |resource: libc::__rlimit_resource_t, value: u64| {
        let lim = libc::rlimit { rlim_cur: value, rlim_max: value };
        // Best-effort: a failure to *lower* a limit is not worth aborting the run over.
        unsafe { libc::setrlimit(resource, &lim) };
    };
    if let Some(v) = cfg.rlimit_as {
        set(libc::RLIMIT_AS, v);
    }
    if let Some(v) = cfg.rlimit_cpu {
        set(libc::RLIMIT_CPU, v);
    }
    if let Some(v) = cfg.rlimit_nproc {
        set(libc::RLIMIT_NPROC, v);
    }
    if let Some(v) = cfg.rlimit_fsize {
        set(libc::RLIMIT_FSIZE, v);
    }
}

fn set_no_new_privs() {
    // Required before loading a seccomp filter (and by Landlock) for an unprivileged
    // process; ensures no exec can regain privileges. Fail-closed if it cannot be set.
    let rc = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
    if rc != 0 {
        die(
            EXIT_LAUNCHER_FAILED,
            &format!(
                "PR_SET_NO_NEW_PRIVS failed: {}",
                std::io::Error::last_os_error()
            ),
        );
    }
}

fn apply_landlock(cfg: &Config) -> Result<(), String> {
    // Handle the full access-right set the crate knows; BestEffort tolerates a kernel
    // that lacks the newest rights, but we still require Landlock to be *enforcing*
    // at all (checked below) — otherwise we would silently run unconfined.
    //
    // FS rights: ABI V5 (covers up through IoctlDev; V6/V7 add no new FS bits).
    // Scopes (Signal + AbstractUnixSocket): ABI V6 — BestEffort drops them on older
    // kernels (residual same-uid risk documented in command-sandbox.md §6).
    // Net TCP deny: ABI V4 — BestEffort; defense-in-depth next to seccomp.
    let abi_fs = ABI::V5;

    let mut ruleset = Ruleset::default()
        .set_compatibility(CompatLevel::BestEffort)
        .handle_access(AccessFs::from_all(abi_fs))
        .map_err(|e| e.to_string())?
        .scope(Scope::from_all(ABI::V6))
        .map_err(|e| e.to_string())?;

    if !cfg.allow_network {
        // No TCP bind/connect rules → default-deny for Landlock net (when supported).
        ruleset = ruleset
            .handle_access(AccessNet::from_all(ABI::V4))
            .map_err(|e| e.to_string())?;
    }

    let mut created = ruleset.create().map_err(|e| e.to_string())?;

    for p in &cfg.ro_paths {
        match PathFd::new(p) {
            Ok(fd) => {
                created = created
                    .add_rule(PathBeneath::new(fd, AccessFs::from_read(abi_fs)))
                    .map_err(|e| e.to_string())?;
            }
            Err(e) => eprintln!("run-confined: skipping missing --ro path {p:?}: {e}"),
        }
    }
    for p in &cfg.rw_paths {
        match PathFd::new(p) {
            Ok(fd) => {
                created = created
                    .add_rule(PathBeneath::new(fd, AccessFs::from_all(abi_fs)))
                    .map_err(|e| e.to_string())?;
            }
            Err(e) => return Err(format!("required --rw path {p:?} is unopenable: {e}")),
        }
    }

    let status = created.restrict_self().map_err(|e| e.to_string())?;
    if matches!(status.ruleset, RulesetStatus::NotEnforced) {
        return Err("kernel did not enforce Landlock (need Linux >= 5.13)".to_string());
    }
    Ok(())
}

fn apply_seccomp(cfg: &Config) -> Result<(), String> {
    let mut rules: BTreeMap<i64, Vec<SeccompRule>> = BTreeMap::new();

    if !cfg.allow_network {
        // Deny socket() for every domain *except* AF_UNIX (cargo jobserver, etc.).
        // Matching arg0 != AF_UNIX covers AF_INET/INET6 (TCP+UDP/DNS+IMDS), AF_NETLINK,
        // AF_PACKET, AF_VSOCK, and any future family — not just the two inet domains.
        let non_unix = SeccompRule::new(vec![SeccompCondition::new(
            0,
            SeccompCmpArgLen::Dword,
            SeccompCmpOp::Ne,
            libc::AF_UNIX as u64,
        )
        .map_err(|e| e.to_string())?])
        .map_err(|e| e.to_string())?;
        rules.insert(libc::SYS_socket as i64, vec![non_unix]);
    }

    // io_uring can create sockets and connect without calling socket(2), which is a
    // well-known seccomp bypass. Offline builds do not need it — deny unconditionally.
    for nr in [
        libc::SYS_io_uring_setup,
        libc::SYS_io_uring_enter,
        libc::SYS_io_uring_register,
    ] {
        rules.insert(nr as i64, Vec::new());
    }

    // Deny cross-process memory/ptrace (belt-and-suspenders to Landlock's own
    // out-of-domain ptrace restriction). An empty rule vec = match unconditionally.
    for nr in [
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
    ] {
        rules.insert(nr as i64, Vec::new());
    }

    // Close the x32-ABI bypass. On x86_64, a task can invoke any syscall under the
    // *same* AUDIT_ARCH_X86_64 identity but with the number OR'd with
    // `__X32_SYSCALL_BIT` (0x4000_0000) — the x32 calling convention. seccompiler's
    // architecture guard only checks AUDIT_ARCH (which x32 shares with x86_64), so an
    // x32 call sails past it, then misses our exact-number JEQ rules below and lands on
    // the default `Allow` — a total bypass of every deny above (x32 `socket`, `ptrace`,
    // `io_uring_*`, `process_vm_*`). libseccomp guards against this automatically;
    // seccompiler does not. We mirror each deny onto its x32-tagged number so both the
    // native and x32 forms are caught (and any deny added above is mirrored for free).
    // aarch64 has no such per-syscall compat bit — its AArch32 compat uses a distinct
    // AUDIT_ARCH that the arch guard already kills — so this is x86_64-only.
    #[cfg(target_arch = "x86_64")]
    {
        const X32_SYSCALL_BIT: i64 = 0x4000_0000;
        let mirrored: Vec<(i64, Vec<SeccompRule>)> = rules
            .iter()
            .map(|(nr, chain)| (nr | X32_SYSCALL_BIT, chain.clone()))
            .collect();
        rules.extend(mirrored);
    }

    let filter = SeccompFilter::new(
        rules,
        SeccompAction::Allow,                     // default: allow syscalls we didn't name
        SeccompAction::Errno(libc::EPERM as u32), // named + matched: deny with EPERM
        target_arch(),
    )
    .map_err(|e| e.to_string())?;

    let program: BpfProgram = filter.try_into().map_err(|e: seccompiler::BackendError| e.to_string())?;
    apply_filter(&program).map_err(|e| e.to_string())
}

fn target_arch() -> TargetArch {
    #[cfg(target_arch = "x86_64")]
    {
        TargetArch::x86_64
    }
    #[cfg(target_arch = "aarch64")]
    {
        TargetArch::aarch64
    }
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    {
        compile_error!("run-confined supports only x86_64 and aarch64")
    }
}
