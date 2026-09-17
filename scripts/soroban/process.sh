#!/bin/bash

DIR=$1

CVLR_DIR=$2

MY_DIR=$(realpath $(dirname $0))

SDK_USAGE_JSON=/tmp/sdk_versions.json

python $MY_DIR/soroban_sdk_versions.py $1 --json > $SDK_USAGE_JSON
export SDK_VERSION=$(jq -r '.["latest_version"]' $SDK_USAGE_JSON)
echo using SDK $SDK_VERSION

SDKS=$(jq '.["lock_versions"] | length' $SDK_USAGE_JSON)
if [[ "$SDKS" -gt 1 ]]; then
    echo "warning: overriding older specified SDKs"
fi

python $MY_DIR/generate_sanity.py $DIR

python $MY_DIR/use-cvlr-soroban.py $DIR $CVLR_DIR $SDK_VERSION

bash $MY_DIR/generate_nondet_2.sh $DIR

bash $MY_DIR/sanity_rules.sh $DIR

