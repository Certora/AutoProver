//! Catching `None` written for an optional account, which under the IDL path silently builds a
//! malformed instruction.
//!
//! Anchor transmits an *absent* optional account as the program's own id occupying a slot, and its
//! own `#[derive(Accounts)]` client codegen does exactly that (`anchor-syn`'s `to_account_metas`
//! pushes `AccountMeta::new_readonly(crate::ID, false)` for a `None`). `crucible-idl-gen` does not —
//! it wraps the push in `if let Some(..)`, so a `None` field is dropped from the account list
//! entirely. The instruction then arrives short: anchor either runs out of accounts
//! (`AccountNotEnoughKeys`) or, when the optional sits mid-struct, reads the *following* account in
//! its place (`AccountOwnedByWrongProgram`).
//!
//! Nothing about that is visible from a campaign. The action's transaction fails, the action returns
//! `false`, the fuzzer treats it as a dead end and moves on, and every property over the code behind
//! it holds vacuously. Measured on klend's 2026-08-10 run: `refreshReserve` (4 of its 6 accounts
//! optional) is queued as a prefix by ten actions, and the whole core lending flow — deposit,
//! borrow, repay, withdraw, liquidate — never executed once. Writing the program id for those three
//! `None`s alone took the run from 6.1% to 7.4% of edges and 10.7% to 13.0% of branches.
//!
//! So this is a gate rather than a note in the prompt. The klend fixture's author *knew* the
//! convention — one action hand-builds its metas and pushes the program id three times, and works —
//! and still wrote `None` in the shared helper next to it.

/// A `None` written for an optional account in an authored fixture.
pub(crate) struct AbsentOptional {
    accounts_struct: String,
    field: String,
}

/// Every `<field>: None` in an `accounts::<Name> { .. }` literal in `spec`.
///
/// Deliberately conservative: only a bare `None` counts, so a value this cannot read plainly (one
/// carrying a trailing comment, say) is passed over rather than guessed at. Missing one costs a
/// campaign; a false accusation costs an authoring round.
pub(crate) fn absent_optionals(spec: &str) -> Vec<AbsentOptional> {
    let mut found = Vec::new();
    for (at, _) in spec.match_indices("accounts::") {
        let rest = &spec[at + "accounts::".len()..];
        let name: String =
            rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
        if name.is_empty() {
            continue;
        }
        // Only a struct *literal*: the path in any other position (a type, an import) has no brace
        // after it, and anything but whitespace in between means this is not one either.
        let after = &rest[name.len()..];
        let Some(open) = after.find('{').filter(|i| after[..*i].trim().is_empty()) else {
            continue;
        };
        let Some(body) = braced(&after[open..]) else {
            continue;
        };
        found.extend(none_fields(body).into_iter().map(|field| AbsentOptional {
            accounts_struct: name.clone(),
            field,
        }));
    }
    found
}

/// What the author has to change, and why — the text a failed gate re-authors against.
pub(crate) fn explain(absent: &[AbsentOptional]) -> String {
    let mut out = String::from(
        "This fixture writes `None` for an optional account. Under the IDL path that DROPS the \
         account from the instruction instead of transmitting it, so the program receives a short \
         account list and rejects the instruction before any of its logic runs \
         (`AccountNotEnoughKeys` = 3005, or `AccountOwnedByWrongProgram` = 3007 when the optional \
         is not the last account). The action then fails on every draw, in silence.\n\n\
         Anchor represents an absent optional account as the PROGRAM'S OWN ID. Write that instead \
         of `None`:\n\n",
    );
    for a in absent {
        out.push_str(&format!(
            "    accounts::{} {{ .., {}: Some(self.program_id) }}   // was: {}: None\n",
            a.accounts_struct, a.field, a.field,
        ));
    }
    out.push_str(
        "\nEvery other field stays as it is. If you meant to send a deliberately malformed \
         instruction, build the `Vec<AccountMeta>` by hand rather than through the accounts struct, \
         so the omission is visible at the call site.",
    );
    out
}

/// The text inside the outermost `{..}` of `s`, which must start at its opening brace.
fn braced(s: &str) -> Option<&str> {
    let mut depth = 0usize;
    for (i, c) in s.char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&s[1..i]);
                }
            }
            _ => {}
        }
    }
    None
}

/// The names of the `field: None` entries at the top level of a struct-literal body.
fn none_fields(body: &str) -> Vec<String> {
    let mut fields = Vec::new();
    let mut depth = 0usize;
    let mut start = 0usize;
    let mut chunks: Vec<&str> = Vec::new();
    for (i, c) in body.char_indices() {
        match c {
            '{' | '(' | '[' => depth += 1,
            '}' | ')' | ']' => depth = depth.saturating_sub(1),
            ',' if depth == 0 => {
                chunks.push(&body[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    chunks.push(&body[start..]);
    for chunk in chunks {
        if let Some((name, value)) = chunk.split_once(':') {
            if value.trim() == "None" && !name.trim().is_empty() {
                fields.push(name.trim().to_string());
            }
        }
    }
    fields
}

#[cfg(test)]
mod tests {
    use super::*;

    fn caught(spec: &str) -> Vec<String> {
        absent_optionals(spec)
            .iter()
            .map(|a| format!("{}.{}", a.accounts_struct, a.field))
            .collect()
    }

    /// klend's own helper, trimmed — the one that cost that run its entire lending flow.
    const REFRESH_RESERVE: &str = r#"
        fn refresh_reserve_ix(&self, r: &ReserveInfo) -> Instruction {
            self.ix(
                instruction::RefreshReserve {}.data(),
                accounts::RefreshReserve {
                    reserve: r.key,
                    lending_market: r.market,
                    pyth_oracle: Some(r.oracle),
                    switchboard_price_oracle: None,
                    switchboard_twap_oracle: None,
                    scope_prices: None,
                }
                .to_account_metas(None),
            )
        }"#;

    #[test]
    fn the_helper_that_killed_the_klend_run_is_caught() {
        assert_eq!(
            caught(REFRESH_RESERVE),
            [
                "RefreshReserve.switchboard_price_oracle",
                "RefreshReserve.switchboard_twap_oracle",
                "RefreshReserve.scope_prices",
            ],
        );
    }

    #[test]
    fn the_none_that_belongs_there_is_not_touched() {
        // `to_account_metas(None)` sits immediately after the literal's closing brace and is the
        // argument every single call site passes. Reading it as a field would fail every fixture.
        assert!(caught("accounts::Foo { bar: baz }.to_account_metas(None)").is_empty());
    }

    #[test]
    fn a_path_that_is_not_a_struct_literal_is_left_alone() {
        for spec in [
            "use lending::accounts::RefreshReserve;",
            "fn f(a: accounts::RefreshReserve) -> Option<u8> { None }",
            "let x: Vec<accounts::Foo> = vec![]; let y = None;",
        ] {
            assert!(caught(spec).is_empty(), "{spec}");
        }
    }

    #[test]
    fn a_none_nested_inside_a_field_value_is_not_a_field() {
        // Only the account fields themselves are the hazard; a `None` inside a call or a nested
        // literal is ordinary Rust.
        let spec = "accounts::Foo { owner: self.pda(None), extra: Bar { inner: None } }";
        assert!(caught(spec).is_empty(), "{:?}", caught(spec));
    }

    #[test]
    fn every_literal_in_a_fixture_is_examined() {
        let spec = format!("{REFRESH_RESERVE}\naccounts::InitUserMetadata {{ referrer: None }}");
        assert_eq!(caught(&spec).len(), 4);
        assert!(caught(&spec).contains(&"InitUserMetadata.referrer".to_string()));
    }

    #[test]
    fn the_explanation_names_the_field_and_spells_the_replacement() {
        // The author re-authors against this text alone, so it has to carry the fix rather than
        // just the accusation.
        let said = explain(&absent_optionals(REFRESH_RESERVE));

        assert!(said.contains("scope_prices: Some(self.program_id)"), "{said}");
        assert!(said.contains("PROGRAM'S OWN ID"), "{said}");
    }
}
