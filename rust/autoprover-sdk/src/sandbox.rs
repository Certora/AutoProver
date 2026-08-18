//! Where a blocking callout runs its toolchain: the workdir, the Python-authored confinement
//! wrapper, and the launcher helper that spawns behind it.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// The confinement wrapper for a command, authored by Python
/// (`SandboxConfig.backend_spec`) and passed to `compile`/`validate`. The backend never
/// invents policy or names a sandbox mechanism: Python owns the confinement *intent* and
/// translates it into `argv_prefix`, an opaque argv the backend simply prepends to its
/// command — `[*argv_prefix, program, *args]` (see [`Workspace::run`]).
///
/// `argv_prefix` is **empty** for a passthrough (`provider="none"`) spec — the command runs
/// directly (the trusted path). Otherwise it is a full `run-confined <flags…> --` wrapper
/// (mirrors `composer/sandbox/launcher.py::LauncherProvider.argv_prefix`); its first element
/// is the launcher binary. Because the prefix is opaque, swapping the sandbox mechanism never
/// changes this shape.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub struct Sandbox {
    pub argv_prefix: Vec<String>,
    pub timeout_s: u64,
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
fn confined_join(workdir: &Path, rel: &str) -> Result<PathBuf, String> {
    use std::path::Component;
    let p = Path::new(rel);
    if p.is_absolute() || p.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(format!("unsafe file path {rel:?}: absolute or traverses outside the workdir"));
    }
    Ok(workdir.join(p))
}

/// Where one blocking callout runs its toolchain: the directory, and the confinement to run
/// behind. The two always travel together — every command a backend runs needs both — so
/// [`Backend::compile`](crate::Backend::compile) and [`Backend::validate`](crate::Backend::validate)
/// are handed this rather than a loose pair.
#[derive(Debug, Clone)]
pub struct Workspace {
    /// The workdir: where files are materialized and the command runs. Also the root every path a
    /// backend reads back (a checker's output file, say) is resolved against.
    pub dir: PathBuf,
    /// The confinement wrapper to run behind.
    pub sandbox: Sandbox,
}

impl Workspace {
    /// Materialize `files` into the workdir (path-confined), then run `program args` there behind
    /// the sandbox's `argv_prefix` — i.e. `[*argv_prefix, program, *args]` (or `program args`
    /// directly, when the prefix is empty). Blocks on the child; the host already calls the
    /// blocking callouts with the GIL released. Enforces `sandbox.timeout_s` by SIGKILL of the
    /// child's process group, so descendants die with the leader.
    ///
    /// The **command line (`program`/`args`) is authored by the trusted backend**; only file
    /// *contents* may derive from the LLM (`docs/command-sandbox.md` §2,
    /// `docs/rust-applications.md` §8). When present, the prefix's `run-confined` confines *itself*
    /// (Landlock+seccomp+rlimits+env scrub) and `execve`s the tool.
    pub fn run<I, S>(
        &self,
        program: &str,
        args: I,
        files: &BTreeMap<String, String>,
    ) -> Result<CommandOutput, String>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        run_confined(&self.sandbox, program, args, files, &self.dir)
    }
}

/// How a spawned command finished: it exited, or the wall-clock timeout fired.
enum ChildStatus {
    Exited(ExitStatus),
    TimedOut,
}

/// The body of [`Workspace::run`], over the parts rather than the bundle.
fn run_confined<I, S>(
    sandbox: &Sandbox,
    program: &str,
    args: I,
    files: &BTreeMap<String, String>,
    workdir: &Path,
) -> Result<CommandOutput, String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    materialize_files(workdir, files)?;
    let mut cmd = confined_command(sandbox, program, args, workdir);
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(not_found(cmd.get_program()));
        }
        Err(e) => return Err(e.to_string()),
    };
    let (t_out, t_err) = pipe_readers(&mut child);
    let timeout_s = sandbox.timeout_s.max(1);
    let status = wait_with_timeout(&mut child, timeout_s)?;
    let stdout = lossy_utf8(t_out);
    let stderr = lossy_utf8(t_err);
    Ok(match status {
        ChildStatus::Exited(st) => CommandOutput {
            exit_code: st.code().unwrap_or(-1),
            stdout,
            stderr,
        },
        ChildStatus::TimedOut => CommandOutput {
            exit_code: -1,
            stdout,
            stderr: format!("{stderr}\ncommand timed out after {timeout_s}s"),
        },
    })
}

/// Write `files` into `workdir`, rejecting absolute / `..` paths. Contents may be
/// LLM-derived; the command line is not.
fn materialize_files(workdir: &Path, files: &BTreeMap<String, String>) -> Result<(), String> {
    std::fs::create_dir_all(workdir).map_err(|e| e.to_string())?;
    for (rel, contents) in files {
        let target = confined_join(workdir, rel)?;
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::write(&target, contents).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// `[*argv_prefix, program, *args]`, or `program args` when the prefix is empty.
/// Owns a process group so the timeout SIGKILL takes descendants with the leader.
/// `run-confined` execve's in place, so the group leader *is* the command; cargo/rustc
/// children inherit the group unless they leave it.
fn confined_command<I, S>(sandbox: &Sandbox, program: &str, args: I, workdir: &Path) -> Command
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let mut cmd = match sandbox.argv_prefix.split_first() {
        Some((bin, rest)) => {
            // The prefix ends at its `--`; the wrapped command follows it.
            let mut cmd = Command::new(bin);
            cmd.args(rest).arg(program);
            cmd
        }
        None => Command::new(program),
    };
    cmd.args(args)
        .current_dir(workdir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd
}

fn not_found(program: &OsStr) -> CommandOutput {
    CommandOutput {
        exit_code: NOT_FOUND_EXIT,
        stdout: String::new(),
        stderr: format!("{}: not found", program.to_string_lossy()),
    }
}

/// Drain stdout/stderr on side threads so a full pipe cannot deadlock the waiter.
fn pipe_readers(child: &mut Child) -> (JoinHandle<Vec<u8>>, JoinHandle<Vec<u8>>) {
    let out = child.stdout.take().expect("piped stdout");
    let err = child.stderr.take().expect("piped stderr");
    (read_pipe(out), read_pipe(err))
}

fn read_pipe(mut pipe: impl Read + Send + 'static) -> JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut s = Vec::new();
        let _ = pipe.read_to_end(&mut s);
        s
    })
}

fn lossy_utf8(buf: JoinHandle<Vec<u8>>) -> String {
    String::from_utf8_lossy(&buf.join().unwrap_or_default()).into_owned()
}

fn wait_with_timeout(child: &mut Child, timeout_s: u64) -> Result<ChildStatus, String> {
    let deadline = Instant::now() + Duration::from_secs(timeout_s);
    loop {
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(st) => return Ok(ChildStatus::Exited(st)),
            None if Instant::now() >= deadline => {
                kill_spawned(child);
                let _ = child.wait();
                return Ok(ChildStatus::TimedOut);
            }
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    }
}

/// Kill the spawned command. On Unix this is the process group we created at spawn
/// (`pgid == pid`); `Child::kill` would leave descendants running.
fn kill_spawned(child: &mut Child) {
    #[cfg(unix)]
    {
        let pid = child.id() as i32;
        // Negative pid = process group. Kill the group *before* wait so we cannot
        // reap the leader and then killpg a reused pid.
        let _ = unsafe { libc::kill(-pid, libc::SIGKILL) };
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn unique_workdir() -> PathBuf {
        std::env::temp_dir().join(format!(
            "ap-sandbox-pg-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ))
    }

    fn pid_gone(pid: i32, within: Duration) -> bool {
        let deadline = Instant::now() + within;
        loop {
            if unsafe { libc::kill(pid, 0) } != 0 {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn timeout_kills_the_process_group() {
        let dir = unique_workdir();
        std::fs::create_dir_all(&dir).unwrap();
        let sandbox = Sandbox {
            argv_prefix: Vec::new(),
            timeout_s: 1,
        };
        let out = run_confined(
            &sandbox,
            "sh",
            ["-c", "sleep 100 & echo $! > child.pid; exec sleep 100"],
            &BTreeMap::new(),
            &dir,
        )
        .unwrap();
        assert_eq!(out.exit_code, -1, "{}", out.stderr);
        assert!(out.stderr.contains("timed out"), "{}", out.stderr);
        let child_pid: i32 = std::fs::read_to_string(dir.join("child.pid"))
            .unwrap()
            .trim()
            .parse()
            .expect("grandchild pid");
        let gone = pid_gone(child_pid, Duration::from_secs(2));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(gone, "grandchild {child_pid} still alive after timeout");
    }
}
