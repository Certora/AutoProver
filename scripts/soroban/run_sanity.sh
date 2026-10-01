#!/bin/bash

CVLR_DIR=$2

MY_DIR=$(realpath $(dirname $0))

MY_TMP_DIR=$(mktemp -d)

PROJECT_TMP=$MY_TMP_DIR/`basename $1`

cp -r $1 $PROJECT_TMP

shift; shift

. $MY_DIR/process.sh -c $CVLR_DIR "$@" $PROJECT_TMP 

cargo update

stellar contract build --profile release-with-logs

cd conf

for f in *.conf; do
    certoraSorobanProver $f
done

