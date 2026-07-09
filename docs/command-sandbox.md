# Design — Sandboxing the `RunCommand` effect (Phase 6)

**Status:** design. Detailed design for [crucible-application.md §7.4](./crucible-application.md#L436)
and [§9 Phase 6](./crucible-application.md#L634) — the *required*, definition-of-done phase.

**One-line summary.** Every command run through the `RunCommand` effect compiles and/or runs
LLM-authored *native* code (§7.2). Today that runs with the full ambient environment of the
AutoProver process. Phase 6 confines each such command — with no network, no inherited secrets, and
only its own inputs on the filesystem — using **unprivileged, in-process kernel sandboxing
(Landlock + seccomp)** that needs no container changes, no namespaces, no capabilities, and no
custom runtime. It is a single wrapper around [`run_local_command`](../composer/sandbox/command.py).
Done is proven by an escape test.

---

## 1. Why this is required, not optional

The outer AutoProver container protects the *host* from AutoProver. It does **not** protect
AutoProver's own secrets, network access, and filesystem from code running *inside* it. And the
`RunCommand` effect deliberately runs untrusted native code:

- `cargo build-sbf` on the **user-supplied program** compiles it natively — running its
  `build.rs`, its proc-macros, and (for a future Prover/CVLR backend) LLM-munged source.
- `crucible run` compiles the **LLM-authored harness** (its `setup()`, `action_*`, `build.rs`)
  and then runs it as a native LiteSVM-in-process binary (§7.2 — verified native, no SVM sandbox).

So arbitrary code of the LLM's (and the analyzed program's) choosing executes with whatever
ambient authority the AutoProver process has: `ANTHROPIC_API_KEY`, `CERTORA*` cloud tokens,
`AWS_*`, `PG*`, the network route to `169.254.169.254`, and the entire bind-mounted host project.
The trust boundary from §7.2 ("the LLM authors only file *contents*, never argv") stops the LLM
from choosing *what command runs* — it does nothing about what that command, once running, can
*reach*. That is this phase's job.

Until this phase is green the backend may run only in a trusted, offline environment on trusted
input (the gate scenario). This is the definition of done.

---

## 2. Threat model

| | |
|---|---|
| **Asset** | AutoProver's ambient secrets (LLM/cloud API keys, DB creds), its network egress (incl. `169.254.169.254` metadata → IAM role creds on EC2), and host files outside the command's declared inputs. |
| **Adversary** | Native code the LLM authored (harness `setup`/`action`/`build.rs`) **and** native code in the analyzed program (its `build.rs`, proc-macros) that `cargo build-sbf` runs. Assume it is actively hostile and knows it is being fuzzed. |
| **Trust boundary** | The process boundary of each `RunCommand` invocation. Inside: untrusted. Outside: the trusted AutoProver process. `program`+`args` are trusted (Rust decider / Python build step author them, §7.2); only the *files* are untrusted. |
| **Assumptions** | (1) The outer container/host is the infrastructure's boundary against the host machine and other tenants (on EC2, the Nitro hypervisor) — this phase is the boundary *within* the container, between AutoProver and its own untrusted child. (2) The kernel is patched and Landlock-capable (§8). (3) The host toolchains we grant read access are trusted. |
| **Non-goals** | Protecting the host machine *from the container* (the infrastructure does that). A full VM boundary between AutoProver and the child (that is what gVisor/Kata/VM-per-run would add at the infra layer, orthogonal to this phase — §6). Defending against a malicious *`program`/`args`* — those are trusted by construction (§7.2). |

**Explicit guarantees the sandbox must provide:**

1. **No network** — no egress at all, including DNS and `169.254.169.254`.
2. **No secrets** — the child's environment is a scrubbed allowlist, and it cannot recover
   AutoProver's secrets out-of-band (via `/proc/<parent>/environ` or `ptrace` — see §6, the
   same-uid caveats).
3. **Minimal filesystem** — only the command's own inputs are writable; toolchains are read-only;
   nothing else of the host is readable.
4. **Resource caps + wall-clock kill** — memory / CPU-time / pids / file-size bounded; a hung or
   runaway command is killed.
5. **Offline, code-exec-free dependency resolution** — all network dep-fetching happens *outside*
   the sandbox and *before* any untrusted code runs (§5); the sandboxed build is `--offline`.

---

## 3. What runs inside, and what it legitimately needs

The hard part of sandboxing a compiler+fuzzer is that it needs a *lot* of real toolchain — the
sandbox is only useful if it grants exactly that and nothing more. The three command shapes and
their real needs:

| Command | Reads (grant **ro+x**) | Writes (grant **rw**) | Network |
|---|---|---|---|
| `cargo build-sbf <program>` | rust toolchain (`RUSTUP_HOME`), solana platform-tools (the sBPF toolchain), warm cargo registry (`CARGO_HOME/registry`), program crate source | program crate `target/` | none (offline) |
| `crucible run <prog> <test> …` | the `crucible` binary + its libs, rust toolchain, cargo registry, the **crucible checkout crates** (path deps from `CrucibleDep`, §6.1), the built `.so` + IDL | the harness crate `target/`, corpus/output dirs | none (offline) |
| `cargo build` (harness, if run directly) | as above | harness `target/` | none (offline) |

Common surface, resolved once at sandbox-config time and expressed as Landlock rules (§6):

- **Rust toolchain** — `RUSTUP_HOME` (default `~/.rustup`), `cargo`/`rustc` shims — read+exec.
- **Cargo home** — `CARGO_HOME` (default `~/.cargo`): the `cargo` binary and the **registry cache**,
  read-only inside; warmed *outside* (§5).
- **Solana platform-tools** — cargo-build-sbf's sBPF rust toolchain — read+exec.
- **The `crucible` binary** and libs it dlopens — read+exec.
- **The crucible checkout** (`$CRUCIBLE_REPO/crates/…`) — the path deps — read-only.
- **System runtime** — `/usr`, `/bin`, `/lib`, `/lib64` — read+exec (needed for the toolchain's own
  dynamic linking and subprocesses).
- **Device nodes** — `/dev/null`, `/dev/urandom`, `/dev/zero`, `/dev/tty` — read+write. The toolchain
  opens these constantly (a build *fails* without `/dev/null` — validated during step 2). Landlock
  rules can target individual files, so we grant these specific nodes rather than the whole `/dev`
  tree; either way `mknod` stays blocked (no capability), so no new devices can be created.
- **Workdir** — the crate tree + `target/` + corpus/output — the primary read-write grant.

Everything else — the rest of the bind-mounted project, `/etc`, `/proc/<other-pids>`, `$HOME`, the
process environment — is **not granted**, therefore inaccessible. Confinement is default-deny.

> The exact host paths (`RUSTUP_HOME`, platform-tools dir, crucible binary) are **resolved by the
> host at config time**, not hardcoded — see the `SandboxPolicy` in §7. They are discovered from the
> environment the same way `resolve_crucible_repo` already discovers the checkout.

---

## 4. The seam — one function, unchanged signature

All command execution already funnels through
[`run_local_command`](../composer/sandbox/command.py) (both the IoC `RunCommand` effect via
[`RealEffects.run_command`](../composer/rustapp/adapter.py#L120) and the Solana build step
[`build_program`](../composer/spec/solana/build.py)). It lives in the backend-agnostic
[`composer/sandbox`](../composer/sandbox/) package — outside `rustapp` — so Python-based backends can
run confined commands too, not just the Rust-IoC ones. The sandbox wraps exactly this one function.

**The mechanism sits behind a `SandboxProvider` seam, so it is swappable.** `run_local_command`
never names a concrete tool. It holds a **tool-agnostic `SandboxPolicy`** (the *intent*: rw paths,
ro paths, env allowlist, rlimits, network-off — §7) and a `SandboxProvider` that translates that
intent into a concrete launch:

```python
class SandboxProvider(Protocol):
    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec: ...
    def available(self) -> Availability: ...   # drives fail-closed (§7)

# run_local_command, unchanged shape:
spec = provider.wrap(policy, program, args)
create_subprocess_exec(*spec.argv, cwd=workdir, env=spec.env, …)
```

The first provider is our **custom launcher shim** (§6): `LaunchSpec.argv == ["run-confined",
*policy_argv, "--", program, *args]`, all authored by trusted Python (never the LLM). Swapping to an
off-the-shelf tool later — `landrun`, `sandlock` — is a *new `SandboxProvider` implementation that
maps the same `SandboxPolicy` to that tool's flags*; the policy, this seam, `run_local_command`,
`RealEffects`, and the escape-test gate (§10) are all untouched. The provider is chosen by config
(`CommandConfig` / an env var), defaulting to the custom launcher. The `none` provider is a
passthrough (`argv == [program, *args]`) — byte-for-byte today's behavior for the EVM/Foundry paths
and explicit trusted-input dev runs.

Nothing in the Rust decider, the ABI, the driver, or the artifact store changes — this is why §7.4
could defer it to last.

Two properties `run_local_command` *already* enforces stay in force and are the first line of
defense (the sandbox is the second): the command runs via **exec, not a shell**, and every written
file path is **confined to the workdir** (`_confined_target`). The sandbox does not replace these;
it assumes them.

---

## 5. Offline dependency resolution — split fetch (network, no exec) from build (exec, no network)

The tension: `cargo build` needs its dependency crates, but the sandbox has no network. Resolution
splits cleanly along the code-execution line:

- **`cargo fetch` / `cargo vendor` download but never run build scripts** — no untrusted code
  executes during fetch. So the *fetch* happens **outside** the sandbox, with network, as a trusted
  prep step, warming `CARGO_HOME/registry` (or producing a vendored dir + source-replacement
  config).
- **`cargo build` runs build scripts and proc-macros** — this is where untrusted code executes, so
  it happens **inside** the sandbox, `--offline`, against the already-warm cache.

The harness `Cargo.toml` is **host-owned** (`CrucibleDep.render_deps`, pinned versions, §6.1), so
its dep graph is fixed and vendorable deterministically. The program-under-test's `Cargo.toml` is
user-supplied, but `cargo fetch` on it is still exec-free, so the same split holds for the build-sbf
step. This also closes the build-time supply-chain vector: with `--offline` and a pre-warmed cache,
a malicious `build.rs` cannot pull a payload at build time.

---

## 6. Mechanism: unprivileged Landlock + seccomp self-sandboxing

### Why not a namespace sandbox (bwrap/nsjail) or gVisor

The obvious tools (bwrap, nsjail) build the sandbox out of **namespaces** (user + mount + net +
pid), then `pivot_root` into a minimal filesystem. That model **fights the container**: creating an
unprivileged user namespace and mounting inside it is exactly what Docker's default seccomp +
AppArmor block. Validated empirically (python:3.12-slim, host kernel 7.0.11, `bwrap 0.11.0`, uid
1000):

| Approach under Docker defaults | Outcome |
|---|---|
| unprivileged `bwrap` | ✗ userns creation blocked by default **seccomp** |
| `bwrap`, `seccomp=unconfined` | ✗ `mount --make-rslave` blocked by **AppArmor** `docker-default` |
| `bwrap`, `seccomp=unconfined`+`apparmor=unconfined` | ✓ works — but requires **weakening the whole container's LSMs** (rejected) |
| setuid `bwrap` | ✗ `capset` blocked (Docker capability bounding set drops `CAP_SETPCAP`) |

Making bwrap work would mean either **stripping the container's own seccomp/AppArmor** (widening the
host-kernel attack surface across *all* of AutoProver — the opposite of what a sandboxing phase
should do) or running AutoProver under a **gVisor/Kata** runtime. gVisor works, but (a) it imposes
its *heaviest* overhead precisely on our syscall/I/O-bound compile+fuzz workload, and (b) its benefit
— protecting the host kernel — is an *infrastructure* boundary that on EC2 is already provided by the
Nitro hypervisor. Neither is worth coupling this phase to a deployment decision.

### The chosen model: the process sandboxes itself

Instead of building a new namespace *around* the command, the command **restricts itself** using two
unprivileged kernel facilities — the model Chrome, OpenSSH, and systemd use. Both need **no
namespaces, no capabilities, no root, and no `--security-opt`**, and both work in a **stock**
container. Validated (stock python:3.12-slim, uid 1000, Docker default profile):

| Guarantee | Probe result | Mechanism |
|---|---|---|
| filesystem — write outside workdir | ✗ `EACCES` | **Landlock** (full ABI FS bit set, grant only workdir rw) |
| filesystem — read host file (`/etc/passwd`) | ✗ `EACCES` | Landlock (no grant) |
| **secret** — read `/proc/<parent>/environ` | ✗ `EACCES` | Landlock (no `/proc` grant) |
| **secret** — `ptrace(ATTACH, parent)` | ✗ `EPERM` | **seccomp** (deny `ptrace`, `process_vm_readv`) |
| network — `socket(AF_INET)` | ✗ `EPERM` | seccomp (deny inet-domain sockets → blocks TCP, UDP/DNS, IMDS) |
| legitimate — write workdir, `exec` toolchain | ✓ works | Landlock rw grant + r+x on toolchain paths |

- **[Landlock](https://docs.kernel.org/userspace-api/landlock.html)** (LSM; Linux ≥5.13, we observed
  ABI **8**) — an unprivileged process installs a filesystem ruleset on itself: default-deny, then
  grant rw to the workdir and read+exec to the toolchain paths of §3, handling the *full* set of FS
  access rights the running ABI supports (else unhandled operations stay unrestricted). This is what
  confines reads *and* writes and — crucially — closes the `/proc/<parent>/environ` leak that a user
  namespace would otherwise have closed for free.
- **seccomp-BPF self-filter** (`PR_SET_NO_NEW_PRIVS` + `SECCOMP_SET_MODE_FILTER`) — installing a
  *stricter* filter on yourself is unprivileged and permitted by Docker's default profile. It denies
  the network (`socket` with `AF_INET`/`AF_INET6` — covering TCP, UDP/DNS, and the IMDS endpoint,
  while leaving `AF_UNIX` for benign local IPC) and the remaining same-uid secret vectors
  (`ptrace`, `process_vm_readv`/`writev`).
- **env allowlist** — the launcher `execve`s with a scrubbed environment (PATH, HOME, CARGO_HOME,
  RUSTUP_HOME, TERM, and benign build vars only). The `--clearenv` equivalent, done in-process.
- **rlimits** — `setrlimit` for `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_NPROC` / `RLIMIT_FSIZE` (§7).

Landlock and seccomp are **preserved across `execve`** (with `NO_NEW_PRIVS`) and **inherited across
`fork`**, so the launcher applies them once and every descendant — `cargo`, `rustc`, each `build.rs`,
the linker, the fuzz binary — runs confined.

### The same-uid caveat, and why it is closed

A user namespace (bwrap) would have run the child under a *remapped* uid, so cross-process access to
AutoProver was denied by credential mismatch. Self-sandboxing keeps the child at AutoProver's **own
uid**, so the two out-of-band secret vectors must be closed *explicitly* — and are: `/proc/<parent>/
environ` by **not granting `/proc`** in the Landlock ruleset (proven `EACCES`), and `ptrace`/
`process_vm_readv` by the **seccomp deny-list** (proven `EPERM`). These are the only same-uid vectors
to AutoProver's memory/env; both verified closed in the stock container.

### The launcher: a custom shim over audited crates (not hand-rolled primitives)

The first `SandboxProvider` (§4) is a small **trusted Rust launcher** (`run-confined`) that applies the
four confinements to itself, then `execve`s the command. It does **not** hand-write raw seccomp BPF
or raw Landlock syscalls — it composes two mature, permissively-licensed crates:

- **[`landlock`](https://crates.io/crates/landlock)** — the reference Rust binding; does ABI
  negotiation and the full FS access-right set (the fiddly part §11 Q1 warns about).
- **[`seccompiler`](https://crates.io/crates/seccompiler)** — the seccomp-BPF compiler from **AWS
  Firecracker**; we hand it a small allow/deny policy, not raw bytecode.

plus `setrlimit` and an env allowlist. So the security-sensitive primitives are audited upstream;
our code is the glue + the policy. We build Rust already, so this adds no new toolchain.

### Alternatives considered — and why the seam stays swappable (§4)

Two off-the-shelf tools do essentially this model. Neither is adopted *now*, but the `SandboxProvider`
seam means either can be dropped in later as a new provider mapping the same `SandboxPolicy`:

- **[`landrun`](https://github.com/zouuup/landrun)** (Go CLI, **MIT**, mature ~2.2k★, FS floor 5.13):
  excellent for Landlock FS + env, and the reference for our CLI shape. But it blocks network via
  **Landlock network rules (TCP-only, kernel ≥6.7)** — it does **not** block UDP/DNS, and degrades
  fail-open on older kernels — and has no rlimits. It would need a seccomp companion anyway, so it
  doesn't save the hard part.
- **[`sandlock`](https://github.com/multikernel/sandlock)** (Python+Rust, Landlock+seccomp): the
  closest match to our full model, but requires **kernel ≥6.12 (Landlock ABI v6)** — above Amazon
  Linux 2023's 6.1 — and ships an **unstated license** plus more surface than we need (MITM proxy,
  COW, notification supervisor). A strong candidate to revisit *if* the kernel-floor and license
  questions are resolved and reviewers prefer an off-the-shelf boundary.

The custom launcher wins for now on **kernel floor** (5.13, because we block network with seccomp not
Landlock), **license clarity**, and **minimal surface** — while the provider seam keeps the door open
to swap in `sandlock`/`landrun` with no change to the policy or the gate.

### The chief advantage: deployment-independence

Because it needs nothing from the container, the same code path runs identically on a dev laptop,
self-managed EC2, ECS, EKS, and even Fargate, and under `runc` or gVisor alike. **It decouples Phase
6 from the open deployment/tenancy questions** — those can be settled later as an *infrastructure*
hardening decision (VM-per-run / gVisor / IMDSv2 hop-limit / least-privilege IAM), layered *on top*
of this in-process boundary, not blocking it.

**Residual risk:** a Landlock/seccomp bypass or a kernel LPE would let the child reach the container
(and then only as far as the infrastructure boundary allows — the container, or on EC2 the Nitro
VM). Named; mitigated by keeping the kernel patched, by the env/network already being denied, and by
the orthogonal infra hardening above for higher-trust-risk deployments.

---

## 7. Resource limits, and the config surface

**Resource caps** are `setrlimit` calls the launcher makes on itself before `execve` (lowering your
own limits is unprivileged; inherited by all descendants): `RLIMIT_AS` (address space / memory-ish),
`RLIMIT_CPU` (CPU-seconds — a wall-clock-independent bound), `RLIMIT_NPROC` (fork-bomb guard),
`RLIMIT_FSIZE` (disk-fill guard). `RLIMIT_AS` is crude (address space, not RSS) but dependency-free;
a **cgroup v2** scope (`memory.max`, `pids.max`, `cpu.max`) is the robust upgrade if the container
grants writable cgroup delegation — note it, defer it. The existing asyncio `wait_for(...,
timeout_s)` in `run_local_command` stays the primary wall-clock kill.

The confinement *intent* is a **tool-agnostic** policy object (the same one every `SandboxProvider`
consumes, §4) — deliberately naming no mechanism, so a future provider swap needs no policy change:

```python
@dataclass(frozen=True)
class SandboxPolicy:
    rw_paths: tuple[Path, ...]                # the workdir (+ any writable scratch)
    ro_paths: tuple[Path, ...]                # toolchains, crucible checkout, platform-tools, /usr…
    env_allowlist: Mapping[str, str]          # PATH, HOME, CARGO_HOME, RUSTUP_HOME, TERM, …
    network: bool = False                     # egress allowed? default off
    mem_bytes: int = ...
    cpu_seconds: int = ...
    nproc: int = ...
    fsize_bytes: int = ...
    # program + args come per-call from run_local_command
```

**Provider selection is separate config, not part of the policy** — a `CommandConfig.sandbox_provider`
knob (`"launcher"` = the custom Rust shim, default; `"none"` = passthrough; later `"landrun"` /
`"sandlock"`), overridable by env var. `run_local_command` gains `policy: SandboxPolicy | None` +
the resolved provider (default provider `"none"` when no policy, so existing callers and the EVM path
are unchanged). `RealEffects` builds the policy from a host-resolved config (toolchain paths
discovered like `resolve_crucible_repo` already does), and `build_program` uses the same.

**Fail-closed.** Before running under a real sandbox provider, `provider.available()` is checked
(for the launcher: kernel Landlock ABI present). If it isn't — or the provider cannot apply its
confinement — the command **refuses to run** rather than silently executing unconfined. The failure
is a **prominent, actionable message** naming the reason ("the command sandbox requires a
Landlock-capable kernel (Linux ≥5.13); this backend cannot run without it — see
docs/command-sandbox.md §8"). The `none` provider is a *separate*, explicit, logged choice for the
trusted EVM/Foundry callers and trusted-input dev runs — never reached as a fallback from a failed
sandbox setup.

---

## 8. Platform requirements — Linux with Landlock; nothing else supported

Landlock and seccomp are **Linux** facilities. This backend is supported only on a Linux host with a
**Landlock-capable kernel (≥5.13; ≥6.7 adds Landlock network rules as defense-in-depth)** — which
AutoProver's own container already provides (Amazon Linux 2023 = 6.1, recent Ubuntu, and the dev
container all qualify). **macOS is not a supported configuration** (team decision): there is no
Landlock, and no macOS-native equivalent is planned. A Mac developer runs this backend the way
AutoProver already runs — inside the Linux container.

If the sandbox cannot be established (non-Linux host, or a kernel without Landlock), the run
**fails immediately** with the §7 fail-closed message. This is the one uniform response everywhere
the sandbox is unavailable: refuse to run, loudly, rather than run untrusted native code unconfined.

---

## 9. Implementation plan

1. **The `SandboxProvider` seam + `SandboxPolicy`** — *done* ([composer/sandbox/policy.py](../composer/sandbox/policy.py)):
   the tool-agnostic policy (§7), the `SandboxProvider` protocol (`wrap` → `LaunchSpec`, `available`),
   the `none` passthrough provider, the name registry, and `ensure_available` / `SandboxUnavailable`.
   Pure, unit-tested. **This is the isolation layer that makes the mechanism swappable** — everything
   else depends only on this interface, never on a concrete tool. Lives in the backend-agnostic
   [`composer/sandbox`](../composer/sandbox/) package (with `run_local_command`), not under `rustapp`.
2. **The custom launcher provider** — *done*: the `run-confined` **trusted Rust binary**
   ([rust/run-confined](../rust/run-confined)) + the `LauncherProvider`
   ([composer/sandbox/launcher.py](../composer/sandbox/launcher.py)) that maps a
   `SandboxPolicy` to its argv. `run-confined --ro <path>… --rw <path>… --allow-env NAME[=VAL]…
   --rlimit-* … [--allow-network] -- <program> <args…>` sets rlimits + `NO_NEW_PRIVS`, builds the
   Landlock ruleset (best-effort ABI negotiation, full FS bit set, deny-by-default + §3 grants) via
   the [`landlock`](https://crates.io/crates/landlock) crate, builds the seccomp filter (deny inet
   sockets + ptrace/process_vm_*) via [`seccompiler`](https://crates.io/crates/seccompiler), applies
   both, then `execve`s the command with an env scrubbed to the allowlist. `--probe` reports the
   kernel Landlock ABI and drives `available()` → fail-closed (§7). Enforcement smoke-tested on the
   host (write-outside / `/etc/passwd` / `/proc/<parent>/environ` / inet-socket all denied; workdir
   write, AF_UNIX, and toolchain `exec` allowed); argv mapping golden-tested. Full escape gate is
   step 5.
3. **Thread `policy` + provider through `run_local_command`** (default provider `none` = unchanged)
   and build the policy in `RealEffects` / `build_program` from host-resolved toolchain paths;
   provider chosen by `CommandConfig.sandbox_provider`.
4. **Offline prep (§5)** — a `cargo fetch` warm step outside the sandbox; sandboxed builds add
   `--offline`.
5. **The escape-test gate (§10).**

Each step is behind the seam, so the existing Phase 1–5 gates keep passing throughout (they run the
`none` provider until step 3 flips Crucible's default to the launcher provider). A later off-the-shelf
swap (`landrun`/`sandlock`) is *only* a new step-2-style provider — steps 1, 3, 4, 5 and the gate are
untouched.

---

## 10. The gate — an escape test

A new expensive gate (`tests/test_crucible_sandbox_gate.py`) with two halves:

**A. Escape attempts are denied.** Author a *malicious* harness whose `setup()` (and a companion
`build.rs`) attempts, and record the outcome of each:

- **Read a planted secret** — set a fake `ANTHROPIC_API_KEY=canary-<nonce>` in AutoProver's env
  before the run; the harness (a) reads `std::env::var("ANTHROPIC_API_KEY")` and (b) tries to read
  it out-of-band from `/proc/<ppid>/environ`, writing whatever it finds to a workdir file. Assert
  the value is **absent** by both routes.
- **Read a host file outside the workdir** — the harness tries to open `/etc/passwd` and a file in
  the real project root *outside* the granted workdir. Assert **permission denied**.
- **Reach the network** — the harness tries to connect to an external host **and** to
  `http://169.254.169.254/latest/meta-data/` (the EC2 metadata endpoint → IAM creds). Assert **both
  fail** (socket creation denied).

The harness must not be able to fail the assertions silently — it writes each probe's result into
the workdir (allowed) and the test reads them back, asserting every probe reports *denied*.

**B. The legitimate path still works.** The existing `solana_vault` gate ([§8](./crucible-application.md#L545))
passes **unchanged** under the launcher provider — the shared fixture is authored, the `.so` builds,
tests compile and fuzz, verdicts are produced. This proves the sandbox grants exactly the toolchain
the real work needs and nothing more.

Because the gate is written against the `SandboxProvider` seam (§4), not a specific tool, it doubles
as the **conformance test any future provider must pass** — swapping in `landrun`/`sandlock` means
re-running this same gate green, nothing more.

Only when both halves are green may the backend run on untrusted input (the §9 definition of done).

---

## 11. Open questions

1. **Landlock ABI coverage / negotiation.** The launcher must handle the full FS access-right set of
   the *running* kernel's ABI (unhandled rights stay unrestricted) with best-effort fallback on older
   kernels. The `landlock` crate does this; confirm the minimum supported ABI on our target AMIs and
   what "best-effort" degrades to (e.g. pre-ABI-3 has no `TRUNCATE` handling).
2. **AF_UNIX / netlink allowance.** The seccomp filter denies `AF_INET`/`AF_INET6` but allows
   `AF_UNIX`. Confirm the toolchain (cargo jobserver, rustc, linker) needs nothing more; if a
   benign `AF_NETLINK` use surfaces, decide whether to allow it (it can read but not egress).
3. **rlimits vs cgroup v2 (§7).** Is `RLIMIT_AS` enough to contain a memory-hungry fuzzer, or do we
   need cgroup `memory.max` (and thus writable cgroup delegation in the container) sooner?
4. **Cache warming cost (§5).** Per-run `cargo fetch` adds latency; is a shared, pre-warmed
   read-only registry volume worth it for CI throughput?
5. **Off-the-shelf provider swap (deferred, seam is ready — §4/§6).** `sandlock` (needs kernel
   ≥6.12; unstated license) or `landrun` (+ a seccomp companion for UDP/DNS + rlimits) could replace
   the custom launcher as a new `SandboxProvider` if reviewers prefer an off-the-shelf boundary. Blocked
   today on the kernel-floor (target AMI ≥6.12?) and license questions; revisit once those resolve.
   The provider seam + the gate-as-conformance-test (§10) make the swap mechanical.
6. **Infra-layer hardening (orthogonal, non-blocking).** Independent of this in-process boundary,
   deployments running genuinely untrusted programs should also apply the standard EC2 hardening —
   least-privilege instance IAM role, IMDSv2 with hop limit 1, egress-restricted security group, and
   (if desired) VM-per-run or a gVisor runtime. Decide per deployment when the tenancy model is
   settled; none of it blocks Phase 6.
