//! The Rust half of the wire round-trip fuzzer (`tests/test_wire_roundtrip.py`).
//!
//! Speaks newline-delimited JSON on stdin/stdout so the Python side drives one long-lived process
//! for a whole test rather than spawning one per example. Two operations, one per direction of the
//! seam:
//!
//!  * `echo` — deserialize a host-built payload into the mirrored Rust type and re-serialize it.
//!    A field this side doesn't know is dropped on the way through, so the host's re-parse of the
//!    answer differs from what it sent. That is the outbound check.
//!  * `gen` — build a value from entropy the host supplies and serialize it. The entropy comes
//!    from Hypothesis (as bytes) rather than a seeded RNG here, so one generator drives both
//!    directions and shrinking still works. That is the inbound check: the host parses the answer,
//!    dumps it, and sends it back through `echo` to compare.
//!
//! Being generated from the *Rust* type is the point of `gen`: a field only this side declares is
//! populated, which is the drift a generator derived from the host's schema cannot see.

use std::io::{self, BufRead, Write};

use arbitrary::{Arbitrary, Unstructured};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use autoprover_sdk::args::AppArgs;
use autoprover_sdk::authoring::{AuthorInput, Failure, Prompt, Property};
use autoprover_sdk::descriptor::AppDescriptor;
use autoprover_sdk::finalize::{ComponentOutcome, FinalizeComponent, FinalizeInput};
use autoprover_sdk::outcome::{CompileResult, Target, Unit, ValidateOutcome};
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
    Failure,
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
    Units,
    WorkspacePrep,
    SandboxGrants,
    // Nested — never a whole message. Addressable anyway so the field-set check can generate one on
    // its own and compare its keys against the host's mirror, rather than having to find them at
    // some depth inside a parent, below opaque payloads whose keys are not field names at all.
    Property,
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
        WireType::Failure => op.run::<Failure>(),
        WireType::Target => op.run::<Target>(),
        WireType::FinalizeInput => op.run::<FinalizeInput>(),
        WireType::Sandbox => op.run::<Sandbox>(),
        WireType::AppDescriptor => op.run::<AppDescriptor>(),
        WireType::CompileResult => op.run::<CompileResult>(),
        WireType::ValidateOutcome => op.run::<ValidateOutcome>(),
        WireType::Prompt => op.run::<Prompt>(),
        WireType::Units => op.run::<Vec<Unit>>(),
        WireType::WorkspacePrep => op.run::<WorkspacePrep>(),
        WireType::SandboxGrants => op.run::<SandboxGrants>(),
        WireType::Property => op.run::<Property>(),
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
        Err(e) => Response::Error { message: format!("malformed request: {e}") },
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
