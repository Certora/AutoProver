//! The confinement wrapper (Python-authored) + the shared launcher helper.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// The confinement wrapper for a command, authored by Python
/// (`SandboxConfig.backend_spec`) and passed to `compile`/`validate`. The backend never
/// invents policy or names a sandbox mechanism: Python owns the confinement *intent* and
/// translates it into `argv_prefix`, an opaque argv the backend simply prepends to its
/// command — `[*argv_prefix, program, *args]` (see [`run_confined`]).
///
/// `argv_prefix` is **empty** for a passthrough (`provider="none"`) spec — the command runs
/// directly (the trusted path). Otherwise it is a full `run-confined <flags…> --` wrapper
/// (mirrors `composer/sandbox/launcher.py::LauncherProvider.argv_prefix`); its first element
/// is the launcher binary. Because the prefix is opaque, swapping the sandbox mechanism never
/// changes this shape.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Sandbox {
    #[serde(default)]
    pub argv_prefix: Vec<String>,
    #[serde(default = "default_timeout")]
    pub timeout_s: u64,
}

fn default_timeout() -> u64 {
    600
}

/// The captured result of a (confined) command.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandOutput {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Exit code synthesized when the program isn't found (mirrors shells' 127).
const NOT_FOUND_EXIT: i32 = 127;

/// Reject absolute paths / `..` traversal (mirrors `composer.sandbox.command._confined_target`).
fn confined_join(workdir: &std::path::Path, rel: &str) -> Result<std::path::PathBuf, String> {
    use std::path::{Component, Path};
    let p = Path::new(rel);
    if p.is_absolute() || p.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(format!("unsafe file path {rel:?}: absolute or traverses outside the workdir"));
    }
    Ok(workdir.join(p))
}

/// Materialize `files` into `workdir` (path-confined), then run `program args` there behind
/// `sandbox.argv_prefix` — i.e. `[*argv_prefix, program, *args]` (or `program args` directly,
/// when `argv_prefix` is empty). Blocks on the child; **call from within
/// `Python::allow_threads`**. Enforces `sandbox.timeout_s`.
///
/// The **command line (`program`/`args`) is authored by the trusted backend**; only file
/// *contents* may derive from the LLM (`docs/command-sandbox.md` §2, `docs/rust-applications.md`
/// §8). When present, the prefix's
/// `run-confined` confines *itself* (Landlock+seccomp+rlimits+env scrub) and `execve`s the tool.
pub fn run_confined(
    sandbox: &Sandbox,
    program: &str,
    args: &[String],
    files: &BTreeMap<String, String>,
    workdir: &std::path::Path,
) -> Result<CommandOutput, String> {
    use std::io::Read;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    // 1. Materialize untrusted files (contents may be LLM-derived; the command line is not).
    std::fs::create_dir_all(workdir).map_err(|e| e.to_string())?;
    for (rel, contents) in files {
        let target = confined_join(workdir, rel)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&target, contents).map_err(|e| e.to_string())?;
    }

    // 2. Build argv by prepending the (opaque, Python-authored) confinement prefix:
    // `[*argv_prefix, program, *args]`. When the prefix is empty this is `program args`
    // run directly; otherwise its first element is the launcher binary to spawn.
    let (launch, mut launch_args): (&str, Vec<String>) = match sandbox.argv_prefix.split_first() {
        Some((bin, rest)) => (bin, rest.to_vec()),
        None => (program, Vec::new()),
    };
    if !sandbox.argv_prefix.is_empty() {
        // The prefix ends at its `--`; the wrapped command follows it.
        launch_args.push(program.to_string());
    }
    launch_args.extend_from_slice(args);
    let mut cmd = Command::new(launch);
    cmd.args(&launch_args);
    cmd.current_dir(workdir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    // 3. Spawn + capture with a timeout. Reader threads avoid a pipe-buffer deadlock.
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(CommandOutput {
                exit_code: NOT_FOUND_EXIT,
                stdout: String::new(),
                stderr: format!("{launch}: not found"),
            })
        }
        Err(e) => return Err(e.to_string()),
    };
    let mut out = child.stdout.take().expect("piped stdout");
    let mut err = child.stderr.take().expect("piped stderr");
    let t_out = std::thread::spawn(move || {
        let mut s = Vec::new();
        let _ = out.read_to_end(&mut s);
        s
    });
    let t_err = std::thread::spawn(move || {
        let mut s = Vec::new();
        let _ = err.read_to_end(&mut s);
        s
    });

    let deadline = Instant::now() + Duration::from_secs(sandbox.timeout_s.max(1));
    let mut timed_out = false;
    let status = loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(st) => break Some(st),
            None => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    timed_out = true;
                    break None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    };
    let stdout = String::from_utf8_lossy(&t_out.join().unwrap_or_default()).into_owned();
    let stderr = String::from_utf8_lossy(&t_err.join().unwrap_or_default()).into_owned();
    if timed_out {
        return Ok(CommandOutput {
            exit_code: -1,
            stdout,
            stderr: format!("{stderr}\ncommand timed out after {}s", sandbox.timeout_s),
        });
    }
    Ok(CommandOutput {
        exit_code: status.and_then(|s| s.code()).unwrap_or(-1),
        stdout,
        stderr,
    })
}
