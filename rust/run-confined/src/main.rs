//! `run-confined` — the trusted launcher for the `RunCommand` sandbox.
//!
//! It applies four unprivileged, in-kernel confinements to *itself*, then `execve`s
//! the requested command (which inherits all of them across the exec):
//!
//!   1. **Landlock** — a filesystem ruleset: default-deny, then grant `--rw` paths
//!      full access and `--ro` paths read+execute. Confines reads *and* writes and,
//!      by not granting `/proc`, closes the same-uid `/proc/<parent>/environ` leak.
//!   2. **seccomp** — deny inet-domain `socket()` (blocks TCP, UDP/DNS, and the EC2
//!      metadata endpoint) and `ptrace`/`process_vm_readv`/`process_vm_writev`.
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

use landlock::{
    Access, AccessFs, CompatLevel, Compatible, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};
use seccompiler::{
    apply_filter, BpfProgram, SeccompAction, SeccompCmpArgLen, SeccompCmpOp, SeccompCondition,
    SeccompFilter, SeccompRule, TargetArch,
};

/// Bad command line (a programming error on the trusted caller's side).
const EXIT_USAGE: i32 = 2;
/// The sandbox could not be established — fail-closed, command NOT run.
const EXIT_SANDBOX_UNAVAILABLE: i32 = 3;
/// The confined `execve` itself failed (e.g. program not found on PATH).
const EXIT_EXEC_FAILED: i32 = 127;

#[derive(Default)]
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
    let argv: Vec<String> = std::env::args().skip(1).collect();

    if argv.first().map(String::as_str) == Some("--probe") {
        probe();
    }

    let cfg = parse(&argv).unwrap_or_else(|e| die(EXIT_USAGE, &e));

    // Order matters: rlimits + env are harmless early; apply Landlock, then seccomp
    // LAST so our own setup syscalls aren't caught by the filter; then exec.
    set_rlimits(&cfg);
    set_no_new_privs();
    if let Err(e) = apply_landlock(&cfg) {
        die(EXIT_SANDBOX_UNAVAILABLE, &format!("Landlock setup failed: {e}"));
    }
    if let Err(e) = apply_seccomp(&cfg) {
        die(EXIT_SANDBOX_UNAVAILABLE, &format!("seccomp setup failed: {e}"));
    }

    let mut cmd = Command::new(&cfg.program);
    cmd.args(&cfg.args).env_clear().envs(cfg.env.iter().cloned());
    // `exec` replaces this process image; it only returns on failure.
    let err = cmd.exec();
    die(EXIT_EXEC_FAILED, &format!("exec {:?} failed: {err}", cfg.program));
}

/// `--probe`: report whether the kernel supports Landlock. Exit 0 + print the
/// enforcement status if so; exit `EXIT_SANDBOX_UNAVAILABLE` otherwise. Drives
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
        .and_then(|r| r.create())
        .and_then(|r| r.restrict_self());
    match status {
        Ok(s) if !matches!(s.ruleset, RulesetStatus::NotEnforced) => {
            println!("landlock {:?}", s.ruleset);
            std::process::exit(0);
        }
        _ => die(
            EXIT_SANDBOX_UNAVAILABLE,
            "kernel does not support Landlock (need Linux >= 5.13); refusing to run unconfined",
        ),
    }
}

fn parse(argv: &[String]) -> Result<Config, String> {
    let mut cfg = Config::default();
    let mut i = 0;

    let take = |i: &mut usize, flag: &str| -> Result<String, String> {
        *i += 1;
        argv.get(*i)
            .cloned()
            .ok_or_else(|| format!("{flag} requires a value"))
    };
    let parse_u64 = |s: &str, flag: &str| -> Result<u64, String> {
        s.parse::<u64>().map_err(|_| format!("{flag} expects an integer, got {s:?}"))
    };

    while i < argv.len() {
        match argv[i].as_str() {
            "--rw" => cfg.rw_paths.push(PathBuf::from(take(&mut i, "--rw")?)),
            "--ro" => cfg.ro_paths.push(PathBuf::from(take(&mut i, "--ro")?)),
            "--allow-network" => cfg.allow_network = true,
            "--rlimit-as" => cfg.rlimit_as = Some(parse_u64(&take(&mut i, "--rlimit-as")?, "--rlimit-as")?),
            "--rlimit-cpu" => cfg.rlimit_cpu = Some(parse_u64(&take(&mut i, "--rlimit-cpu")?, "--rlimit-cpu")?),
            "--rlimit-nproc" => cfg.rlimit_nproc = Some(parse_u64(&take(&mut i, "--rlimit-nproc")?, "--rlimit-nproc")?),
            "--rlimit-fsize" => cfg.rlimit_fsize = Some(parse_u64(&take(&mut i, "--rlimit-fsize")?, "--rlimit-fsize")?),
            "--allow-env" => {
                let spec = take(&mut i, "--allow-env")?;
                if let Some((name, value)) = spec.split_once('=') {
                    cfg.env.push((name.to_string(), value.to_string()));
                } else if let Ok(value) = std::env::var(&spec) {
                    // NAME with no '=': pass through from the current environment if set.
                    cfg.env.push((spec, value));
                }
                // NAME not present in the environment: silently skip (nothing to pass).
            }
            "--" => {
                i += 1;
                if i >= argv.len() {
                    return Err("no program given after `--`".to_string());
                }
                cfg.program = argv[i].clone();
                cfg.args = argv[i + 1..].to_vec();
                return Ok(cfg);
            }
            other => return Err(format!("unknown flag {other:?} (did you forget `--` before the command?)")),
        }
        i += 1;
    }
    Err("missing `--` and command to run".to_string())
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
    // process; ensures no exec can regain privileges.
    unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
}

fn apply_landlock(cfg: &Config) -> Result<(), String> {
    // Handle the full access-right set the crate knows; BestEffort tolerates a kernel
    // that lacks the newest rights, but we still require Landlock to be *enforcing*
    // at all (checked below) — otherwise we would silently run unconfined.
    let abi = ABI::V5;

    let mut created = Ruleset::default()
        .set_compatibility(CompatLevel::BestEffort)
        .handle_access(AccessFs::from_all(abi))
        .map_err(|e| e.to_string())?
        .create()
        .map_err(|e| e.to_string())?;

    for p in &cfg.ro_paths {
        match PathFd::new(p) {
            Ok(fd) => {
                created = created
                    .add_rule(PathBeneath::new(fd, AccessFs::from_read(abi)))
                    .map_err(|e| e.to_string())?;
            }
            Err(e) => eprintln!("run-confined: skipping missing --ro path {p:?}: {e}"),
        }
    }
    for p in &cfg.rw_paths {
        match PathFd::new(p) {
            Ok(fd) => {
                created = created
                    .add_rule(PathBeneath::new(fd, AccessFs::from_all(abi)))
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
        // Deny socket() for AF_INET / AF_INET6 (arg 0) — blocks TCP, UDP (incl. DNS),
        // and the IMDS endpoint. AF_UNIX and other local families still work.
        let cond = |domain: i32| -> Result<SeccompRule, String> {
            SeccompRule::new(vec![SeccompCondition::new(
                0,
                SeccompCmpArgLen::Dword,
                SeccompCmpOp::Eq,
                domain as u64,
            )
            .map_err(|e| e.to_string())?])
            .map_err(|e| e.to_string())
        };
        rules.insert(
            libc::SYS_socket as i64,
            vec![cond(libc::AF_INET)?, cond(libc::AF_INET6)?],
        );
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
