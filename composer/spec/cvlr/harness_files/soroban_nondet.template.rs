//! Arbitrary values of the Soroban SDK's types.
//!
//! The scaffold's `Nondet` implementations for the contract's own `#[contracttype]`s build their
//! fields from these, and a rule can call them directly. They are functions rather than `Nondet`
//! implementations because neither the trait nor the SDK types are local to this crate.
//!
//! A collection is at most five elements long: the prover unrolls the loop that fills it.
//!
//! Every scalar comes from `nondet::<T>()`. Not `cvlr_nondet_small_i128` / `_u128`: the prover does
//! not model those, so what they return is neither arbitrary nor fresh, and a Soroban amount is an
//! `i128`.

#![allow(dead_code)]

use cvlr::nondet::nondet;
use soroban_sdk::{
    auth::Context, crypto::Hash, Address, Bytes, BytesN, Duration, Env, IntoVal, Map, String,
    Symbol, Timepoint, TryFromVal, Val, Vec, I256, U256,
};

pub fn nondet_val() -> Val {
    let v: u64 = nondet();
    Val::from_payload(v)
}

pub fn nondet_hash() -> Hash<32> {
    let env: Env = Env::default();
    let bytes: Bytes = nondet_val().into_val(&env);
    env.crypto().sha256(&bytes)
}

pub fn nondet_context() -> Context {
    let env: Env = Env::default();
    let val = nondet_val();
    Context::try_from_val(&env, &val).expect("nondet context")
}

pub fn nondet_address() -> Address {
    let v: u64 = nondet();
    // 77 is the address object's tag.
    let val = Val::from_payload((v << 8) | 77);
    Address::try_from_val(&Env::default(), &val).unwrap()
}

pub fn nondet_map_internal<K, V>(newk: fn() -> K, newv: fn() -> V) -> Map<K, V>
where
    K: IntoVal<Env, Val> + TryFromVal<Env, Val>,
    V: IntoVal<Env, Val> + TryFromVal<Env, Val>,
{
    let env = Env::default();
    let mut out: Map<K, V> = Map::new(&env);
    let mut i: u32 = 0;
    let l: u32 = nondet();
    if l <= 5 {
        while i < l {
            out.set(newk(), newv());
            i += 1;
        }
    }
    out
}

pub fn nondet_vec_internal<V>(newv: fn() -> V) -> Vec<V>
where
    V: IntoVal<Env, Val> + TryFromVal<Env, Val>,
{
    let env = Env::default();
    let mut out: Vec<V> = Vec::new(&env);
    let mut i: u32 = 0;
    let l: u32 = nondet();
    if l <= 5 {
        while i < l {
            out.push_back(newv());
            i += 1;
        }
    }
    out
}

pub fn nondet_symbol() -> Symbol {
    let v: u64 = nondet();
    // 74 is the small-symbol tag.
    let val = Val::from_payload((v << 8) | 74);
    Symbol::try_from_val(&Env::default(), &val).unwrap()
}

pub fn nondet_string() -> String {
    let nd: u8 = nondet();
    String::from_bytes(&Env::default(), &[nd])
}

pub fn nondet_bytes1() -> Bytes {
    let v: u8 = nondet();
    Bytes::from_slice(&Env::default(), &[v])
}

pub fn nondet_bytes() -> Bytes {
    nondet_bytes1()
}

#[link(wasm_import_module = "env")]
#[allow(improper_ctypes)]
extern "C" {
    fn CVT_nondet_bytes_n_20() -> BytesN<20>;
    fn CVT_nondet_bytes_n_32() -> BytesN<32>;
    fn CVT_nondet_bytes_n_64() -> BytesN<64>;
    fn CVT_nondet_bytes_n_65() -> BytesN<65>;
}

pub fn nondet_bytesn() -> BytesN<32> {
    nondet_bytesn_32()
}

pub fn nondet_bytesn_20() -> BytesN<20> {
    unsafe { CVT_nondet_bytes_n_20() }
}

pub fn nondet_bytesn_32() -> BytesN<32> {
    unsafe { CVT_nondet_bytes_n_32() }
}

pub fn nondet_bytesn_64() -> BytesN<64> {
    unsafe { CVT_nondet_bytes_n_64() }
}

pub fn nondet_bytesn_65() -> BytesN<65> {
    unsafe { CVT_nondet_bytes_n_65() }
}

pub fn nondet_duration() -> Duration {
    Duration::from_seconds(&Env::default(), nondet())
}

pub fn nondet_timepoint() -> Timepoint {
    Timepoint::from_unix(&Env::default(), nondet())
}

pub fn nondet_u256() -> U256 {
    U256::from_parts(&Env::default(), nondet(), nondet(), nondet(), nondet())
}

pub fn nondet_i256() -> I256 {
    I256::from_parts(&Env::default(), nondet(), nondet(), nondet(), nondet())
}

pub fn nondet_i32() -> i32 {
    nondet()
}

pub fn nondet_u32() -> u32 {
    nondet()
}

pub fn nondet_u64() -> u64 {
    nondet()
}

pub fn nondet_i128() -> i128 {
    nondet()
}

pub fn nondet_u128() -> u128 {
    nondet()
}

pub fn nondet_bool() -> bool {
    nondet()
}
