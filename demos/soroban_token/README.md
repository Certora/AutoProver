# Soroban demo: AutoProver on the sunbeam token

A complete, unedited AutoProver run against a Soroban contract — the first one that went end to
end, on 2026-09-30. Everything here is output: nothing was written by hand, and nothing has been
tidied up after the fact.

## The target

`project/` is a copy of the `token` contract from the sunbeam tutorials
(`projects/token`), with its own pre-existing Certora setup removed so the run started cold, and
its lockfile moved to `soroban-sdk 22.0.11` (the `cvlr-soroban` branch for the SDK-22 generation
requires `^22.0.8`).

`project/src/lib.rs` is the contract. Its `initialize` is the interesting part:

```rust
pub fn initialize(e: Env, admin: Address) {
    e.storage().persistent().set(&"ADMIN", &admin);
}
```

No authorization, and no re-initialization guard. The run found that, and said what it costs.

## What the run produced

Everything under `project/src/certora/` was written by the pipeline: the scaffold wrote the
module tree and `project/Cargo.toml`'s CVLR pins and `certora` feature, and the authoring agent
wrote `specs/admin_setup.rs` — five rules over four extracted properties.

- `outputs/reports/report.html` — the rendered report. Start here.
- `outputs/reports/report.json` — the same content as data: three critical findings, each with an
  impact, an attack path, stated assumptions, and a prover counterexample as proof.
- `outputs/properties/` — the extracted properties, the property-to-rule mapping, the author's
  commentary, and the assumptions the rules carry.
- `submission/` — what the prover was actually given: the generated conf, the generated build
  script (`confined_build.py`) and the command file it reads (`confined_build.json`), and the `.wasm` that was verified.
- `logs/` — the console summary (`console.log`), the full run log, and the phase/task event
  stream the `inspect-run` tooling reads.

## Verdicts

| Rule | Result |
|---|---|
| `initialize_persists_admin` | VERIFIED |
| `initialize_then_mint_uses_stored_admin` | VERIFIED |
| `unauthenticated_admin_overwrite_enables_unlimited_mint` | VIOLATED — declared expected |
| `initialize_is_one_shot` | VIOLATED — declared expected |
| `admin_rotation_requires_current_admin_auth` | VIOLATED — declared expected |

The three violations are the deliverable, not a failure: the author read each counterexample,
judged the defect real, and marked the rule expected-to-fail, so the run completed *with* the bugs
in it rather than weakening the rules until they passed.

[Prover job](https://prover.certora.com/jobStatus/33158/f6379ea60d3d408ea6f64d4ad332e156?anonymousKey=2eb78cace9487137f6d67b5d2c0a63d25b216d13)
(an anonymous link — the output is readable without a Certora login).

## Reading the numbers honestly

- 59 minutes wall clock, of which 49 were analysis and property extraction; 3m32s was prover time
  across 3 submissions, and the prover's own reported runtime was 36 seconds.
- One component of four was authored (`--max-properties 4`), under a $25 budget cap.
- The three findings are the *same* defect reported three times. The report's grouping step should
  have merged them; that is a known defect in the report layer, not in the Soroban work.
- The run was unconfined (`COMPOSER_SANDBOX_PROVIDER=none`, since the command sandbox is Linux
  only), so by this repo's own terms these are development results, not production ones.
- `submission/cvlr_admin_setup.conf` names its build script by absolute path, under the temporary
  directory the run owned. It is kept as a record of what was submitted, not as something to rerun.

## Reproducing it

```bash
console-soroban <project> src/lib.rs:Token --budget budget.json --max-properties 4
```

Needs `ANTHROPIC_API_KEY`, `CERTORAKEY`, a local postgres (`docker compose create && docker
compose start`), and a Certora login for reading results back
(`python -c "from certora_login.cli import main; main()" --force-file`).
