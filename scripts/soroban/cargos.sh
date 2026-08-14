#!/bin/bash

DIR=$1

pushd $DIR > /dev/null 2>&1 
find . -name Cargo.toml | fgrep -v './Cargo.toml' | gsed 's#./\(.*\)/Cargo.toml#\1#'

