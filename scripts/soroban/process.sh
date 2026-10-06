#!/bin/bash

UPDATE=""
while getopts ":c:ea" opt; do
  case "${opt}" in
    e)
      UPDATE="ethnum"
      ;;
    a)
      UPDATE="all"
      ;;
    c)
      CVLR_DIR="${OPTARG}"
      echo Using $CVLR_DIR	
      ;;
    \?)
      echo "Error: Invalid option ${opt}" >&2
      exit 1
      ;;
    :)
      echo "Error: Option ${opt} requires an argument." >&2
      exit 1
      ;;
  esac
done

shift $((OPTIND -1))

MY_DIR=$(realpath $(dirname $0))

SDK_USAGE_JSON=/tmp/sdk_versions.json
BYTESN_HEADER=/tmp/bytesn_header.rs

if [[ -e $1/Cargo.lock ]]; then
    DIR=$(realpath $1)
else
    DIR=$(realpath $1/$(dirname $(python $MY_DIR/classify_cargo.py $1 | jq -r 'to_entries[] | select(.["value"]["category"] == "WORKSPACE_ROOT").key')))
fi

echo Using $DIR
cd $DIR

if [[ "$UPDATE" == "all" ]]; then
    cargo update
fi

python $MY_DIR/soroban_sdk_versions.py $DIR --json > $SDK_USAGE_JSON

export SDK_VERSION=$(jq -r '.["latest_version"]' $SDK_USAGE_JSON)
echo using SDK $SDK_VERSION

if [[ "$UPDATE" == "ethnum" ]]; then
    cargo update -p ethnum
fi

SDKS=$(jq '.["lock_versions"] | length' $SDK_USAGE_JSON)
if [[ "$SDKS" -gt 1 ]]; then
    echo "warning: overriding older specified SDKs"
fi

python $MY_DIR/generate_sanity.py $DIR

python $MY_DIR/use-cvlr-soroban.py $DIR $CVLR_DIR $SDK_VERSION

jq -f $MY_DIR/bytesn_functions.jq ./sanity_summary.json | jinja2 $MY_DIR/bytesn_functions.j2 > $BYTESN_HEADER

bash -v $MY_DIR/generate_nondet_2.sh $DIR

bash $MY_DIR/sanity_rules.sh $DIR $BYTESN_HEADER

mkdir conf
python $MY_DIR/sanity_conf.py ./sanity_summary.json $MY_DIR/sanity_conf.j2
