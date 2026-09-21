#!/bin/bash

CVLR_DIR=$2

MY_DIR=$(realpath $(dirname $0))

MY_TMP_DIR=$(mktemp -d)

cp -r $1 $MY_TMP_DIR/`basename $1`

bash $MY_DIR/process.sh $MY_TMP_DIR/`basename $1` $2

cd $MY_TMP_DIR/`basename $1`

cargo update

stellar contract build

cd conf

for f in *.conf; do
    certoraSorobanProver $f
done

