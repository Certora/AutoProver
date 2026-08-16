# summarization-target detector

A standalone AutoProver tool that, from **one prover run**, ranks the functions worth summarizing and says
**how** — per-function over-approximation (via `smtool`'s overapprox generator) or a whole-contract
symbolic model (via `smtool`'s driver). It decides *what* to summarize; `smtool` generates the summaries.
Autosetup runs it after a slow/timeout run to summarize the expensive functions before paying for them.

It is a separate tool from `smtool` and only **reuses** AutoProver code: `smtool.difficulty` (the prover's
nonlinearity report) and `certora_autosetup` (the solc AST reader).

## Signals

1. **Nonlinear (SMT phase)** — `smtool.difficulty`: functions whose inlined body contributes nonlinear ops.
2. **Hashing / encoding (build phase)** — a solc-AST walk (`scan_ast`): `keccak256`/`sha256`/`ecrecover`
   with `abi.encode*` context; assembly is excluded (Yul isn't a Solidity `FunctionCall`), and the input
   length class (dynamic vs fixed-size) is used to rank. Invisible to the difficulty report.
3. **Resolved-expensive external** — a nonlinear hotspot whose owning contract isn't the CUT.

A **reachability-from-the-main-contract gate** prunes signal-2 candidates the CUT can't reach, using the
solc AST's internal call edges plus the prover's `externalCallGraph.json` (resolved links + dispatch).

## Usage

```
# AST-only hashing signal (no run needed):
python -m summarization_detector --cut Router --conf run.conf

# with a prover run (adds nonlinear + external signals) and the reachability gate:
python -m summarization_detector --cut Router --ast path/to/.asts.json \
    --job-url https://.../output/<id>/<hash>/ \
    --external-call-graph path/to/Reports/externalCallGraph.json
```

`externalCallGraph.json` is emitted by the prover (EVMVerifier `ExternalCallSiteCollector`) at scene
setup — after call resolution, before the per-rule optimize pass.
