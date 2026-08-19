//! The Rust half of the wire round-trip fuzzer (`tests/test_wire_roundtrip.py`).
//!
//! Reads and writes one JSON object per line on stdin/stdout. Python keeps this process alive
//! for a whole test instead of starting a new one per example.
//!
//! Two operations, one for each direction:
//!
//!  * `echo` — parse a host-built payload as the matching Rust type and write it back. A field
//!    this side does not know is dropped, so when the host parses the reply it no longer matches
//!    what it sent. That is the outbound check.
//!  * `gen` — build a value from bytes the host sends (Hypothesis entropy, not a local RNG) and
//!    serialize it. One generator then drives both directions and shrinking still works. The host
//!    parses the result, dumps it, and sends it back through `echo` to compare. That is the
//!    inbound check.
//!
//! `gen` builds from the Rust type so a field only this side declares still appears. A generator
//! built from the host's schema would never produce that field, and would miss that kind of drift.

use std::io::{self, BufRead, Write};

use arbitrary::{Arbitrary, Unstructured};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use autoprover_sdk::args::AppArgs;
use autoprover_sdk::authoring::{AuthorInput, Judge, Prompt, Property};
use autoprover_sdk::descriptor::AppDescriptor;
use autoprover_sdk::ffi::CalloutError;
use autoprover_sdk::finalize::{ComponentOutcome, FinalizeComponent, FinalizeInput};
use autoprover_sdk::outcome::{Check, CompileResult, SkippedProperty, Target, ValidateOutcome};
use autoprover_sdk::prep::{SandboxGrants, WorkspacePrep};
use autoprover_sdk::sandbox::Sandbox;

/// Every payload root that crosses the FFI as a whole message, named the way the host names it.
///
/// An enum rather than a bare string match: adding a root without teaching [`dispatch`] about it
/// is then a compile error, and a name the host misspells fails as a `Request` that won't
/// deserialize — reported against the field instead of silently falling through.
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum WireType {
    // Outbound — the host sends these, so this side only ever deserializes them.
    AppArgs,
    AuthorInput,
    Target,
    FinalizeInput,
    /// The confinement wrapper, mirrored by a `TypedDict` rather than a pydantic model
    /// (`composer.sandbox.config.BackendSpec`) — a mirror all the same.
    Sandbox,
    // Inbound — a wheel returns these, so this side is what produces them.
    AppDescriptor,
    CompileResult,
    ValidateOutcome,
    Prompt,
    Judge,
    Checks,
    WorkspacePrep,
    SandboxGrants,
    // The error envelope any inbound callout may return instead of its payload.
    CalloutError,
    // Nested — never a whole message. Addressable anyway so the field-set check can generate one on
    // its own and compare its keys against the host's mirror, rather than having to find them at
    // some depth inside a parent, below opaque payloads whose keys are not field names at all.
    Property,
    SkippedProperty,
    FinalizeComponent,
    ComponentOutcome,
}

/// What both operations need of a payload type: the host's two directions, plus generation.
trait Wire: serde::de::DeserializeOwned + Serialize + for<'a> Arbitrary<'a> {}
impl<T> Wire for T where T: serde::de::DeserializeOwned + Serialize + for<'a> Arbitrary<'a> {}

/// One operation, applied to whichever type [`dispatch`] resolved. A trait rather than a closure
/// because the callee is generic over the payload type and closures cannot be.
trait Op {
    fn run<T: Wire>(self) -> Result<Value, Fault>;
}

/// Resolve `ty` to its Rust type and hand it to `op` — the single place the wire names and the
/// types meet, so neither operation can cover a different set than the other.
fn dispatch<O: Op>(ty: WireType, op: O) -> Result<Value, Fault> {
    match ty {
        WireType::AppArgs => op.run::<AppArgs>(),
        WireType::AuthorInput => op.run::<AuthorInput>(),
        WireType::Target => op.run::<Target>(),
        WireType::FinalizeInput => op.run::<FinalizeInput>(),
        WireType::Sandbox => op.run::<Sandbox>(),
        WireType::AppDescriptor => op.run::<AppDescriptor>(),
        WireType::CompileResult => op.run::<CompileResult>(),
        WireType::ValidateOutcome => op.run::<ValidateOutcome>(),
        WireType::Prompt => op.run::<Prompt>(),
        WireType::Judge => op.run::<Judge>(),
        WireType::Checks => op.run::<Vec<Check>>(),
        WireType::WorkspacePrep => op.run::<WorkspacePrep>(),
        WireType::SandboxGrants => op.run::<SandboxGrants>(),
        WireType::CalloutError => op.run::<CalloutError>(),
        WireType::Property => op.run::<Property>(),
        WireType::SkippedProperty => op.run::<SkippedProperty>(),
        WireType::FinalizeComponent => op.run::<FinalizeComponent>(),
        WireType::ComponentOutcome => op.run::<ComponentOutcome>(),
    }
}

struct Echo(Value);

impl Op for Echo {
    fn run<T: Wire>(self) -> Result<Value, Fault> {
        Ok(serde_json::to_value(serde_json::from_value::<T>(self.0)?)?)
    }
}

struct Gen<'a>(Unstructured<'a>);

impl<'a> Op for Gen<'a> {
    fn run<T: Wire>(mut self) -> Result<Value, Fault> {
        Ok(serde_json::to_value(T::arbitrary(&mut self.0)?)?)
    }
}

/// Why an operation produced no payload — kept apart from a successful answer because the two mean
/// different things to the caller: running out of entropy is the harness's own limit and the
/// example is skipped, while a serde failure is the divergence the fuzzer is looking for.
enum Fault {
    Exhausted,
    Failed(String),
}

impl From<serde_json::Error> for Fault {
    fn from(e: serde_json::Error) -> Self {
        Fault::Failed(e.to_string())
    }
}

impl From<arbitrary::Error> for Fault {
    fn from(e: arbitrary::Error) -> Self {
        match e {
            arbitrary::Error::NotEnoughData => Fault::Exhausted,
            other => Fault::Failed(other.to_string()),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    Echo { ty: WireType, payload: Value },
    Gen { ty: WireType, entropy: Vec<u8> },
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum Response {
    Ok { payload: Value },
    Exhausted,
    Error { message: String },
}

impl From<Result<Value, Fault>> for Response {
    fn from(result: Result<Value, Fault>) -> Self {
        match result {
            Ok(payload) => Response::Ok { payload },
            Err(Fault::Exhausted) => Response::Exhausted,
            Err(Fault::Failed(message)) => Response::Error { message },
        }
    }
}

fn handle(line: &str) -> Response {
    match serde_json::from_str::<Request>(line) {
        Ok(Request::Echo { ty, payload }) => dispatch(ty, Echo(payload)).into(),
        Ok(Request::Gen { ty, entropy }) => dispatch(ty, Gen(Unstructured::new(&entropy))).into(),
        Err(e) => Response::Error {
            message: format!("malformed request: {e}"),
        },
    }
}

fn main() -> io::Result<()> {
    let stdin = io::stdin().lock();
    let mut stdout = io::stdout().lock();
    for line in stdin.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        // A response that won't serialize would desynchronize the pipe, so it can't be a `?`:
        // every request gets exactly one line back.
        let response = handle(&line);
        let encoded = serde_json::to_string(&response)
            .unwrap_or_else(|e| format!(r#"{{"status":"error","message":"unencodable: {e}"}}"#));
        writeln!(stdout, "{encoded}")?;
        stdout.flush()?;
    }
    Ok(())
}
