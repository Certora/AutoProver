# summarization-target detector

A standalone AutoProver tool that, from **one prover run**, ranks the functions worth summarizing — a
candidate list with *why* each is prover-hostile, *where* it can be summarized, and (for a curated
public-library match) a suggested summary. It decides *what* to summarize, **not** *how*: the summarization
strategy (per-function over-approximation, a whole-contract symbolic model, …) and the actual summary text
are the consumer's job. A caller runs it after a slow/timeout (or sanity) run to summarize the expensive
functions before paying for them.

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
