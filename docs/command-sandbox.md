# Design — Sandboxing the `RunCommand` effect (Phase 6)

**Status:** implemented. Design + record for [crucible-application.md §7.4](./crucible-application.md#L436)
and [§9 Phase 6](./crucible-application.md#L634) — the *required*, definition-of-done phase. The
sandbox mechanism is built and validated (§9 steps 1–5 done, gate §10 green — incl. the full LLM
e2e passing under the launcher); Crucible runs confined by default. Open items are orthogonal to the
sandbox (§11): a shared-`Cargo.toml` feature race that lost one of three instructions, and per-run
`CARGO_HOME`/tightening follow-ups.

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
ambient authority the AutoProver process has.
The trust boundary from §7.2 ("the LLM authors only file *contents*, never argv") stops the LLM
from choosing *what command runs* — it does nothing about what that command, once running, can
*reach*. That is this phase's job.

Until this phase is green the backend may run only in a trusted, offline environment on trusted
input (the gate scenario). This is the definition of done.

---

## 2. Threat model

| | |
|---|---|
| **Asset** | AutoProver's ambient secrets, and host files outside the command's declared inputs. |
| **Adversary** | Native code the LLM authored (harness `setup`/`action`/`build.rs`) **and** native code in the analyzed program (its `build.rs`, proc-macros) that `cargo build-sbf` runs. Assume it is actively hostile and knows it is being fuzzed. |
| **Trust boundary** | The process boundary of each `RunCommand` invocation. Inside: untrusted. Outside: the trusted AutoProver process. `program`+`args` are trusted (Rust decider / Python build step author them, §7.2); only the *files* are untrusted. |
| **Assumptions** | (1) The outer container/host is the infrastructure's boundary against the host machine and other tenants (on EC2, the Nitro hypervisor) — this phase is the boundary *within* the container, between AutoProver and its own untrusted child. (2) The kernel is patched and Landlock-capable (§8). (3) The host toolchains we grant read access are trusted. |
| **Non-goals** | Protecting the host machine *from the container* (the infrastructure does that). A full VM boundary between AutoProver and the child (that is what gVisor/Kata/VM-per-run would add at the infra layer, orthogonal to this phase — §6). Defending against a malicious *`program`/`args`* — those are trusted by construction (§7.2). |

**Explicit guarantees the sandbox must provide:**

1. **No network** — no egress at all, including DNS.
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
- **Cargo home** — shared `CARGO_HOME` (default `~/.cargo`): only **`bin/`** is granted read+exec
  (the `cargo` / `cargo-*` shims on `PATH`). The home **root is not granted**, so
  `credentials.toml` / private-registry tokens stay unreadable. Offline registry contents live in
  the private per-run `CARGO_HOME` under the workdir (§11 item 5), warmed *outside* (§5).
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
- **A private temp dir** — `<workdir>/.sandbox_tmp`, with `TMPDIR`/`TMP`/`TEMP` pointed at it. The
  **linker** writes scratch files to `$TMPDIR` (default `/tmp`) during `cargo build`; we do *not*
  grant the shared `/tmp` (it may hold host/other-run secrets and would defeat the escape test), so a
  per-run temp under the already-writable workdir is redirected in. (A fresh harness build fails at
  the link step — "Cannot create temporary file in /tmp/" — without this; found via the e2e gate.)
- **A private cargo home** — `<workdir>/.sandbox_cargo`, with `CARGO_HOME` pointed at it (§11 item 5).
  The offline `cargo build` writes there (source extraction, locks); keeping it per-run means
  untrusted build code can't poison a *shared* `~/.cargo` (which stays read-only, for the `cargo`
  binary). The warm step (§5) fetches into this same home.

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
step. This also closes the build-time supply-chain vector: with offline + a pre-warmed cache, a
malicious `build.rs` cannot pull a payload at build time.

**Implementation (step 4).** "Offline inside" is one env var, not per-tool flags: the policy sets
**`CARGO_NET_OFFLINE=1`** in the child env, which forces *every* cargo invocation offline — including
the nested `cargo` that `crucible run` spawns to build the harness — so we never thread `--offline`
through each tool ([recipes.py](../composer/sandbox/recipes.py), `offline=True` default). "Fetch
outside" is [`warm_cargo_cache`](../composer/spec/solana/build.py) — a `cargo fetch` run *unsandboxed*
(no provider → network on) before the confined build; `build_program` calls it before the sandboxed
`cargo build-sbf`. The harness crate has its own deps (libafl, litesvm, …), so it needs its own warm
at manifest-assembly time; wiring that exact call site (and confirming whether `CARGO_HOME` must be
granted rw for cargo's build-time source extraction, or pre-extracted during the warm) lands with the
gate in step 5, where a real offline build proves it. All of this is inert until a sandbox is enabled.

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
| filesystem — read host file outside grants | ✗ `EACCES` | Landlock (no grant); note `/etc` *is* granted for NSS — escape gate uses a planted host file, not `/etc/passwd` |
| filesystem — cargo `credentials.toml` | ✗ `EACCES` | policy grants shared cargo **`bin/` only**, never the home root |
| **secret** — read `/proc/<parent>/environ` | ✗ `EACCES` | Landlock (no `/proc` grant) |
| **secret** — `ptrace(ATTACH, parent)` | ✗ `EPERM` | **seccomp** (deny `ptrace`, `process_vm_readv`) |
| network — `socket(AF_INET)` / netlink / vsock | ✗ `EPERM` | seccomp: deny `socket` when domain **≠ `AF_UNIX`** |
| network — `io_uring_setup` (seccomp bypass) | ✗ `EPERM` | seccomp: deny `io_uring_{setup,enter,register}` |
| network — x32-ABI `socket` (seccomp bypass) | ✗ `EPERM` | seccomp: each deny mirrored onto its x32 syscall number (`nr \| 0x4000_0000`) |
| network — TCP via Landlock (defense-in-depth) | ✗ deny | Landlock net rules (ABI ≥4), no bind/connect grants |
| same-uid — `kill(parent)` / abstract UDS | ✗ `EPERM` | Landlock **scopes** `Signal` + `AbstractUnixSocket` (ABI ≥6 / Linux ≥6.12) |
| legitimate — write workdir, `exec` toolchain, `AF_UNIX` | ✓ works | Landlock rw grant + r+x on toolchain paths; `AF_UNIX` still allowed |

- **[Landlock](https://docs.kernel.org/userspace-api/landlock.html)** (LSM; Linux ≥5.13, we observed
  ABI **8**) — an unprivileged process installs a filesystem ruleset on itself: default-deny, then
  grant rw to the workdir and read+exec to the toolchain paths of §3, handling the *full* set of FS
  access rights the running ABI supports (else unhandled operations stay unrestricted). This is what
  confines reads *and* writes and — crucially — closes the `/proc/<parent>/environ` leak that a user
  namespace would otherwise have closed for free. On ABI ≥6 it also installs **scopes** (signals +
  abstract Unix sockets). On ABI ≥4 with network off it default-denies Landlock TCP bind/connect
  (defense-in-depth next to seccomp; UDP is still seccomp-only).
- **seccomp-BPF self-filter** (`PR_SET_NO_NEW_PRIVS` + `SECCOMP_SET_MODE_FILTER`) — installing a
  *stricter* filter on yourself is unprivileged and permitted by Docker's default profile. It denies
  `socket` for every domain **except `AF_UNIX`** (so TCP, UDP/DNS, IMDS, netlink, vsock, … are
  blocked while cargo's jobserver still works), denies **`io_uring_*`** (the classic way to create
  sockets without calling `socket(2)`), and denies the remaining same-uid secret vectors
  (`ptrace`, `process_vm_readv`/`writev`). On **x86_64** each deny is **mirrored onto its x32-ABI
  syscall number** (`nr | 0x4000_0000`): the x32 calling convention runs under the same
  `AUDIT_ARCH_X86_64` identity, so seccompiler's arch guard passes it through, and without the mirror
  an x32-tagged `socket`/`io_uring`/`ptrace` would miss the exact-number rules and reach
  default-allow — a full bypass (which libseccomp guards against automatically; seccompiler does not).
  Still a **deny-list** on top of default-allow — not a full syscall allowlist; residual risk is
  tracked in §11.
- **env allowlist** — the launcher `execve`s with a scrubbed environment (PATH, HOME, CARGO_HOME,
  RUSTUP_HOME, TERM, and benign build vars only). The `--clearenv` equivalent, done in-process.
- **rlimits** — `setrlimit` for `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_NPROC` / `RLIMIT_FSIZE` (§7).

Landlock and seccomp are **preserved across `execve`** (with `NO_NEW_PRIVS`) and **inherited across
`fork`**, so the launcher applies them once and every descendant — `cargo`, `rustc`, each `build.rs`,
the linker, the fuzz binary — runs confined.

### The same-uid caveat, and why it is closed

A user namespace (bwrap) would have run the child under a *remapped* uid, so cross-process access to
AutoProver was denied by credential mismatch. Self-sandboxing keeps the child at AutoProver's **own
uid**, so out-of-band vectors must be closed *explicitly*:

| Vector | Close | Floor |
|---|---|---|
| `/proc/<parent>/environ` | Landlock: no `/proc` grant | 5.13 |
| `ptrace` / `process_vm_*` | seccomp deny | any seccomp |
| `kill` / signals to parent | Landlock scope `Signal` | **6.12** (ABI 6) |
| abstract Unix sockets to outside | Landlock scope `AbstractUnixSocket` | **6.12** (ABI 6) |
| path-based Unix sockets | Landlock FS (socket inode must be under a grant) | 5.13 |
| readable secrets under toolchain paths | policy: grant shared cargo **`bin/` only**, not `~/.cargo` root (`credentials.toml`) | policy |

On kernels **below 6.12** the two scopes are BestEffort-dropped: signal and abstract-UDS remain a
**residual same-uid risk** (the child can still be killed by the wall-clock timeout; abstract
listeners are uncommon in the AutoProver container). Target AMI upgrades past 6.12 close them
fully; the escape suite asserts scopes only when the running kernel is ≥6.12.

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
(for the launcher: Landlock is present *and* actually enforcing). If it isn't — or the provider cannot apply its
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
   Landlock ruleset (best-effort ABI negotiation, full FS bit set, deny-by-default + §3 grants,
   scopes for signals/abstract UDS on ABI ≥6, TCP default-deny on ABI ≥4 when network is off) via
   the [`landlock`](https://crates.io/crates/landlock) crate, builds the seccomp filter (deny
   non-`AF_UNIX` sockets, `io_uring_*`, and ptrace/process_vm_*) via
   [`seccompiler`](https://crates.io/crates/seccompiler), applies both, then `execve`s the command
   with an env scrubbed to the allowlist. `--probe` builds a best-effort ruleset and reports whether
   Landlock actually *enforces* (not the numeric ABI, which the crate hides), driving `available()`
   → fail-closed (§7). Enforcement smoke-tested on the host (write-outside / planted host file /
   `/proc/<parent>/environ` / inet+io_uring+netlink sockets all denied; workdir write, AF_UNIX, and
   toolchain `exec` allowed); argv mapping golden-tested. Full escape gate is step 5.
3. **Thread `policy` + provider through `run_local_command`** — *done*: the runner accepts
   `provider`/`policy` (default `None` → the `none` passthrough, byte-for-byte today's behavior) and
   is fail-closed via `ensure_available`. A `SandboxConfig` ([composer/sandbox/config.py](../composer/sandbox/config.py))
   selects the provider (`$COMPOSER_SANDBOX_PROVIDER`, default `none`) and builds the policy via the
   `rust_build_policy` recipe ([composer/sandbox/recipes.py](../composer/sandbox/recipes.py) — the
   workdir and `/dev` nodes rw; discovered rust/cargo/platform-tool and system dirs ro, incl. `/etc`
   for NSS; env allowlist; network off). Threaded through `RealEffects` and `RustBackend`/`RustFormalizer`
   ([composer/rustapp/adapter.py](../composer/rustapp/adapter.py)), `build_program`
   ([composer/spec/solana/build.py](../composer/spec/solana/build.py)), and the Crucible pipeline
   (which adds the crucible checkout + binary to `extra_ro`). Integration-tested: `run_local_command`
   under the launcher denies out-of-workdir reads and network while allowing the workdir + toolchain.
4. **Offline prep (§5)** — *done*: `warm_cargo_cache` (a `cargo fetch` run outside the sandbox,
   network on) warms the registry, and the policy sets `CARGO_NET_OFFLINE=1` so the confined build —
   and the nested cargo `crucible run` spawns — run offline. Wired into `build_program`; the
   harness-dir warm is `CrucibleArtifactStore.warm_dependencies`, called from `prepare_formalization`
   after the manifest is placed when a sandbox is on. `CARGO_HOME` is granted rw (the crucible policy)
   so cargo can extract crate sources offline.
5. **The escape-test gate (§10)** — *done*, and **Crucible's default provider is now `launcher`**
   (`_crucible_sandbox`; override with `COMPOSER_SANDBOX_PROVIDER=none`). Validated:
   - **Part A (escape suite) — green** ([tests/test_sandbox_escape.py](../tests/test_sandbox_escape.py)):
     a `rustc`-compiled malicious program run through the real launcher has every vector *denied*
     (secret env, `/proc/<ppid>/environ`, host file outside the workdir, external TCP, and
     `169.254.169.254`), with an unconfined control confirming the leaks would otherwise happen.
   - **Part B — green**: a real `cargo-build-sbf` of `solana_vault` under the launcher (offline,
     confined) produces the `.so` ([tests/test_crucible_sandbox_gate.py](../tests/test_crucible_sandbox_gate.py)
     — this caught the relative-policy-path bug; grants must be absolute), and a real
     `crucible run --dry-run` under the launcher builds the harness *offline* and runs LiteSVM
     (`Harness validation passed!`).
   - **Full LLM vertical — green**: the e2e gate (`tests/test_crucible_e2e_gate.py`) passes under the
     launcher (`COMPOSER_SANDBOX_PROVIDER=launcher`): analysis → 23 properties → shared fixture
     authored → per-instruction harness build + fuzz, all confined + offline, with **all three
     instructions (initialize / deposit / withdraw) delivered with fuzz verdicts** (`BAD` —
     counterexamples found). Getting here required the `/tmp` fix below and the shared-crate
     concurrency fix (§11 item 8); before the latter, `initialize` was dropped to a `Cargo.toml`
     feature race.

   **Root cause found via the gate:** every fresh harness build initially failed at the *link* step —
   `Cannot create temporary file in /tmp/: Permission denied` (the linker's `$TMPDIR` scratch, which
   the policy didn't grant). A link failure reads as "could not compile", so the LLM kept rewriting a
   fine fixture. Fixed by redirecting `TMPDIR` to a private `<workdir>/.sandbox_tmp` (§3) rather than
   granting the shared `/tmp`. The `RunCommand` failure logging added alongside the authoring
   improvements is what surfaced it.

Each step is behind the seam, so the earlier Phase 1–5 gates keep passing. **Prerequisite of the
flip:** `run-confined` must be resolvable — `$RUN_CONFINED_BIN`, then PATH, then the dev build
(`cargo build -p run-confined --release`). Containers opt in via the `scripts/docker-compose.sandbox.yml`
overlay, which builds the launcher (`scripts/Dockerfile.sandbox`) and mounts it read-only at
`$RUN_CONFINED_BIN`. Otherwise Crucible fail-closes (§7/§8). A later off-the-shelf swap
(`landrun`/`sandlock`) is *only* a new step-2-style provider — the seam, policy, and gate are
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
  fail** (socket creation denied). Also: `io_uring_setup` (seccomp bypass), `socket(AF_NETLINK)`,
  `socket(AF_VSOCK)` — all denied; `socket(AF_UNIX)` still allowed.
- **Same-uid control plane** (when kernel ≥6.12) — `kill(parent, 0)` and connect to an abstract
  Unix socket owned outside the sandbox are denied (Landlock scopes).
- **Cargo credentials** — a planted `credentials.toml` under the shared cargo home is **not**
  readable (policy grants `bin/` only).

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
2. **AF_UNIX-only socket allow (done for hostile domains).** seccomp now denies `socket` when
   domain **≠ `AF_UNIX`** (so netlink/vsock/packet are closed too) and denies `io_uring_*`. Confirm
   the toolchain (cargo jobserver, rustc, linker) never needs another domain; if a benign
   `AF_NETLINK` use surfaces, decide whether to allow it narrowly. Full syscall **allowlist**
   (default-deny) remains a possible hardening step if the deny-list residual risk is unacceptable.
   **x32-ABI bypass — closed.** A deny-list keyed on exact x86_64 syscall numbers was bypassable via
   the x32 ABI (same `AUDIT_ARCH_X86_64`, number OR'd with `0x4000_0000`): the arch guard passes and
   the exact-number rules miss, hitting default-allow. Critical because on kernels < 6.7 (e.g. the
   AL2023 6.1 target) Landlock provides *no* network filtering, so seccomp is the sole network
   control. Fixed by mirroring every deny onto its x32 number (`apply_seccomp`), regression-tested in
   `tests/test_sandbox_escape.py` (asserts the x32 `socket` is denied with `EPERM` from seccomp, not
   `ENOSYS` from the kernel). This is the deny-list's one arch-level hole; a full allowlist would also
   close it structurally.
3. **rlimits vs cgroup v2 (§7).** Is `RLIMIT_AS` enough to contain a memory-hungry fuzzer, or do we
   need cgroup `memory.max` (and thus writable cgroup delegation in the container) sooner?
4. **Cache warming cost (§5).** Per-run `cargo fetch` adds latency; is a shared, pre-warmed
   read-only registry volume worth it for CI throughput?
5. **Per-run `CARGO_HOME` — done.** An offline `cargo build` *writes* to `CARGO_HOME` (extracts crate
   sources, takes locks), and that build runs untrusted `build.rs`/proc-macro code — so a writable
   *shared* `~/.cargo` was a cross-run poisoning surface (overwrite an extracted `registry/src` to
   hit a later run). Fixed: `rust_build_policy` points `CARGO_HOME` at a **private per-run dir under
   the workdir** (`sandbox_cargo_home` → `<workdir>/.sandbox_cargo`), the warm step (`warm_cargo_cache`,
   unsandboxed) fetches *into that same home*, and the shared cargo home is granted **read-only on
   `bin/` only** (`shared_cargo_ro_paths`) — never the home root, so `credentials.toml` cannot be
   read by untrusted code. Untrusted writes touch only the run's throwaway cache. Validated: a fresh
   fetch into an empty private home + a confined offline build succeed. **Remaining cost:** deps are
   re-fetched per run (no shared writable cache); a shared *read-only* index/cache to avoid the
   re-download is the deferred optimization (add specific cache subtrees to `shared_cargo_ro_paths`,
   still not the home root).
6. **Off-the-shelf provider swap (deferred, seam is ready — §4/§6).** `sandlock` (needs kernel
   ≥6.12; unstated license) or `landrun` (+ a seccomp companion for UDP/DNS + rlimits) could replace
   the custom launcher as a new `SandboxProvider` if reviewers prefer an off-the-shelf boundary. Blocked
   today on the kernel-floor (target AMI ≥6.12?) and license questions; revisit once those resolve.
   The provider seam + the gate-as-conformance-test (§10) make the swap mechanical.
7. **Infra-layer hardening (orthogonal, non-blocking).** Independent of this in-process boundary,
   deployments running genuinely untrusted programs should also apply the standard EC2 hardening —
   least-privilege instance IAM role, IMDSv2 with hop limit 1, egress-restricted security group, and
   (if desired) VM-per-run or a gVisor runtime. Decide per deployment when the tenancy model is
   settled; none of it blocks Phase 6.
8. **Shared-`Cargo.toml`/`main.rs` race (crucible backend) — fixed.** The per-component sessions
   share one `fuzz/<prog>/` crate; concurrent runs raced on both files (the observed
   "package does not contain this feature: `c_<slug>`" that dropped `initialize`, and a latent
   `main.rs` clobber). Fixed two ways: `prepare_component` now reserves Cargo features
   **cumulatively** (the manifest only grows, so no feature is lost), and per-component command runs
   are **serialized + atomic** (`run_local_command` materializes files and runs as one unit under a
   `Semaphore(1)` shared by `RustFormalizer`), while the LLM authoring turns still run concurrently.
   The remaining parallelism win — concurrent *builds/fuzzing* — needs a crate-per-component (§10 Q1);
   deferred.
