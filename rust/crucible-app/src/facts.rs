//! The Anchor API facts mined out of the analyzed model, so the fixture author need not dig through
//! the full JSON (or rediscover Anchor's naming by exploring).

use askama::Template;

use crate::templates::{ApiFacts, IxFact};

/// snake_case → PascalCase — Anchor's `instruction`/`accounts` struct naming.
fn to_pascal(snake: &str) -> String {
    snake
        .split('_')
        .filter(|s| !s.is_empty())
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        })
        .collect()
}

/// A concise, high-signal "API facts" block mined from the analyzed model so the author
/// need not dig through the full JSON (or rediscover Anchor names by exploring): the crate
/// id, declare_id, state types, and each instruction's snake→Pascal name + args + accounts.
/// Returns "" if the model shape isn't recognized.
pub(crate) fn api_facts(
    analyzed: &serde_json::Value, program: &str, crate_id: &str,
) -> String {
    let components = match analyzed.get("components").and_then(|c| c.as_array()) {
        Some(c) => c,
        None => return String::new(),
    };
    let is_prog = |c: &&serde_json::Value| c.get("instructions").is_some_and(|i| i.is_array());
    let prog = components
        .iter()
        .find(|c| {
            is_prog(c)
                && (c.get("program_identifier").and_then(|v| v.as_str()) == Some(program)
                    || c.get("name").and_then(|v| v.as_str()) == Some(program))
        })
        .or_else(|| components.iter().find(is_prog));
    let prog = match prog {
        Some(p) => p,
        None => return String::new(),
    };

    let str_of = |v: Option<&serde_json::Value>| v.and_then(|x| x.as_str()).unwrap_or("?").to_string();
    // The crate id is the dependency's actual *lib* name (`SolanaSourceUnit::lib`), NOT the analysis's
    // `program_identifier` — which may be the `#[program] pub mod` name, or just the label the user
    // passed — either of which would mis-resolve `use <id>::*`. Surface the module name as a note
    // only when it differs (the template renders it iff `analysis_id` is `Some`).
    let analysis_raw = str_of(prog.get("program_identifier"));
    let analysis_id: Option<String> =
        (analysis_raw != crate_id && analysis_raw != "?").then_some(analysis_raw);
    let program_id = prog
        .get("program_id")
        .and_then(|v| v.as_str())
        .unwrap_or("(not declared)")
        .to_string();
    let account_types: Vec<String> = prog
        .get("account_types")
        .and_then(|v| v.as_array())
        .map(|types| types.iter().filter_map(|t| t.as_str().map(String::from)).collect())
        .unwrap_or_default();
    let instructions: Vec<IxFact> = prog
        .get("instructions")
        .and_then(|v| v.as_array())
        .map(|ixs| {
            ixs.iter()
                .map(|ix| {
                    let name = str_of(ix.get("name"));
                    let pascal = to_pascal(&name);
                    let args = ix
                        .get("args")
                        .and_then(|v| v.as_array())
                        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                        .unwrap_or_default();
                    let accounts = ix
                        .get("accounts")
                        .and_then(|v| v.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|x| x.get("name").and_then(|n| n.as_str()).map(String::from))
                                .collect()
                        })
                        .unwrap_or_default();
                    IxFact { name, pascal, args, accounts }
                })
                .collect()
        })
        .unwrap_or_default();
    ApiFacts {
        crate_id,
        analysis_id: analysis_id.as_deref(),
        program_id,
        account_types,
        instructions,
    }
    .render()
    .expect("render api_facts")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn api_facts_renders_from_the_analyzed_model() {
        let model = serde_json::json!({
            "components": [{
                "name": "vault",
                "program_identifier": "vault_program",
                "program_id": "Vau1t111",
                "account_types": ["VaultState"],
                "instructions": [
                    {"name": "deposit", "args": ["amount"],
                     "accounts": [{"name": "vault"}, {"name": "depositor"}]},
                ],
            }],
        });
        let out = api_facts(&model, "vault", "vault");
        for needle in [
            "crate id (for `use <id>::*`): vault",
            "pub mod vault_program",                  // module-name note (differs from crate id)
            "declare_id / program id: Vau1t111",
            "state/account types: VaultState",
            "- deposit → instruction::Deposit, accounts::Deposit; args: [amount]; accounts: [vault, depositor]",
        ] {
            assert!(out.contains(needle), "api_facts missing {needle:?} in:\n{out}");
        }
        assert!(!out.contains("{{") && !out.contains("{%"), "template residue in api_facts");
        // The component is still found by the analysis identifier, but the crate id rendered for
        // `use <id>::*` is the dependency's lib name (here they differ, as in lend).
        let out = api_facts(&model, "vault", "example_lending");
        assert!(out.contains("crate id (for `use <id>::*`): example_lending"), "in:\n{out}");
        // Unrecognized model shape → empty (unchanged contract).
        assert_eq!(api_facts(&serde_json::json!({}), "vault", "vault"), "");
    }
}
