//! A hand-written probe for the loop bound — the one prover setting an author may raise.
//!
//! `docs/cvlr-backend-plan.md` §7.6.2 measured that a loop the bound cannot cover comes back as a
//! violated *"Unwinding condition in a loop"*. This fixture turns that observation into an
//! instrument: rules over loops of different shapes, run under two confs that differ in one key.
//!
//! **The loop has to survive the compiler, and two drafts of this file did not.** Measured, by
//! disassembling the artifact each one produced:
//!
//! * A counter compared against its own bound is close-formed and the property folded to a
//!   constant. Ten instructions reached the prover, with no loop among them.
//! * Iterating a fixed-size array is worse, and quietly so: the length is a compile-time ceiling on
//!   the trip count, so LLVM fully unrolls no matter how symbolic the bound *inside* the loop is.
//!   Assuming `limit <= 4` over an eight-element slice buys nothing.
//!
//! **And surviving the compiler is not enough.** A third draft did produce a loop — confirmed in
//! the
//! disassembly — whose trip count was `cvlr_assume!`d to be at most four, and the prover verified
//! it
//! under a bound of two. The CLI's own help for the option says why: it sets "a single iteration
//! for
//! variable iterations loops, *all iterations for fixed iterations loops*". A trip count the prover
//! can determine is unrolled completely and never reaches the bound at all, so assuming one is a
//! way
//! of opting out of the setting this fixture exists to measure.
//!
//! The measured loop below is therefore bounded in fact but not by anything a value analysis reads
//! off an assumption. That difference is the fixture, and it is also the reason the author prompt
//! puts constraining the trip count above raising the bound: the first can remove the need for the
//! second outright.
//!
//! `tests/test_cvlr_loop_bound.py` checks the built artifact for a backward branch before it
//! submits anything, because every one of these failures looks exactly like success.

use cvlr::prelude::*;

/// A running maximum over a nondeterministic stream, returning the first sample beside it.
fn max_of(limit: u64) -> (u64, u64) {
    let first: u64 = nondet();
    let mut max = first;
    let mut n: u64 = 1;
    while n < limit {
        let v: u64 = nondet();
        if v > max {
            max = v;
        }
        n += 1;
    }
    (first, max)
}

/// How many times `x` can be divided by three before it reaches zero.
///
/// Division rather than a shift: LLVM's loop-idiom recognition rewrites a shift-until-zero loop
/// into
/// a count-leading-zeros intrinsic, and the loop disappears with it.
fn divide_steps(mut x: u64) -> u64 {
    let mut n: u64 = 0;
    while x > 0 {
        x /= 3;
        n += 1;
    }
    n
}

/// The control: a loop that exits within the recommended starting point's bound.
///
/// If this fails, the problem is not the bound — the harness, the build or the platform is at
/// fault, and the rules below tell you nothing.
#[rule]
pub fn rule_trip_count_within_the_default_bound() {
    let limit: u64 = nondet();
    cvlr_assume!(limit == 1);
    let (first, max) = max_of(limit);
    cvlr_assert!(max >= first);
}

/// The measurement: a loop that always exits, in more iterations than the default bound allows.
///
/// Five divisions take any `x` below a hundred to zero, so the property is true and a large enough
/// bound proves it. Seeing that requires reasoning about repeated division, which is why the bound
/// is what decides the verdict here and did not in the draft that assumed its trip count outright.
#[rule]
pub fn rule_trip_count_beyond_the_default_bound() {
    let x: u64 = nondet();
    cvlr_assume!(x < 100);
    cvlr_assert!(divide_steps(x) <= 5);
}

/// The tripwire: a loop with no bound at all, which no setting can discharge.
///
/// Not gated on a verdict — it is how the run says whether unwinding conditions are being generated
/// at all. If this one verifies, the prover is not modelling the loop and nothing else in this file
/// means what it says.
#[rule]
pub fn rule_trip_count_the_prover_cannot_bound() {
    let mut n: u64 = 0;
    while !nondet::<bool>() {
        n += 1;
    }
    cvlr_assert!(n >= 0);
}
