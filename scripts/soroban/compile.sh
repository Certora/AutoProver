#!/bin/bash

export SOROBAN_SDK_BUILD_SYSTEM_SUPPORTS_SPEC_SHAKING_V2=1

TOP=$1
shift

for d in $(find $TOP -name Cargo.toml); do
  (pushd $(dirname $d); cargo build --target=wasm32v1-none "$@")
done
