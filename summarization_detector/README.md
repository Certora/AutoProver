# summarization-target detector

A standalone AutoProver tool that, from **one prover run**, ranks the functions worth summarizing and says
**how** — per-function over-approximation or a whole-contract symbolic model. It decides *what* to
summarize; a downstream generator (curated summaries, a symbolic-model tool, or the CVL_GEN agent)
produces the actual summaries. A caller runs it after a slow/timeout (or sanity) run to summarize the
expensive functions before paying for them.

It is self-contained: it reads the prover's difficulty report via its own `difficulty` module and uses
`certora_autosetup` (the solc AST reader).

## Signals

1. **Nonlinear (SMT phase)** — the `difficulty` module: functions whose inlined body contributes nonlinear ops.
2. **Hashing / encoding (build phase)** — a solc-AST walk (`scan_ast`): `keccak256`/`sha256`/`ecrecover`
   with `abi.encode*` context; assembly is excluded (Yul isn't a Solidity `FunctionCall`), and the input
   length class (dynamic vs fixed-size) is used to rank. Invisible to the difficulty report.
3. **Resolved-expensive external** — a nonlinear hotspot whose owning contract isn't the CUT.

A **reachability-from-the-main-contract gate** prunes signal-2 candidates the CUT can't reach, using the
solc AST's internal call edges plus the prover's `externalCallGraph.json` (resolved links + dispatch).

## Usage

The one input is a prover-run **URL** — it fetches the sources + conf, derives the main contract (the
conf's `verify` field), generates the AST, and pulls the difficulty report:

```
python -m summarization_detector --url https://.../output/<id>/<hash>/
```

Optional: `--cut` (override the derived contract), `--solc-dir` (so the conf's `solcN.NN` resolves),
`--work-dir`, `--external-call-graph` (else auto-found in the fetched tree), `--include-dependencies`.

Offline path (when you already have the artifacts instead of a URL):

```
python -m summarization_detector --cut Router --conf run.conf          # AST-only hashing signal
python -m summarization_detector --cut Router --ast .asts.json \
    --job-url https://.../ --external-call-graph Reports/externalCallGraph.json
```

`externalCallGraph.json` is emitted by the prover (EVMVerifier `ExternalCallSiteCollector`) at scene
setup — after call resolution, before the per-rule optimize pass — and enables the reachability gate.

## difficulty_profile — post-hoc: where did the prover time actually go?

`detect.py` predicts what to summarize *before* the rules exist. `difficulty_profile.py` is its
post-hoc counterpart: from a completed (esp. timed-out) run it reads the prover's own difficulty tree
per slow rule and attributes the nonlinearity / path-count hotspots to source functions, classifying
each as **cut** (a function of the contract under test → a generator's over-approx/precise model), **library**
(inlined lib → library model), **external** (linked dependency → dependency model), or **cvl-model**
(already summarized). The CUT and the scene's linked contracts are read from the run's treeViewStatus,
so nothing is protocol-specific.

    python -m summarization_detector.difficulty_profile <job-url-or-hash> [more jobs...] --min-minutes 20
    python -m summarization_detector.difficulty_profile <hashes> --json      # for pipeline consumption

Use it to decide, with evidence, whether a scene's timeouts are in dependency/library code (a dependency/library model
dependency/library model helps) or in the contract under test itself (a per-function model / harness).
