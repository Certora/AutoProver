#!/bin/bash

MY_DIR=$(dirname $(realpath $0))

TESTS_JSON=/tmp/tests.json
TESTS_RS=/tmp/tests.rs
SANITY_TMP=/tmp/sanity.rs
TYPES_JSON=/tmp/types.json
NONDET_JSON=/tmp/nondet_types.json
USES_TXT=/tmp/uses.rs

cd $1

for sf in $(jq -r '(.["contracts"] + .["traits"])[]["sanity_file"]' ./sanity_summary.json | sort | uniq); do

    if [[ "$sf" == *"fuzz"* ]]; then
	echo skipping $sf
	continue
    fi
    
    jq "{ \"contracts\": [(.[\"contracts\"] + .[\"traits\"])[] | select(.[\"sanity_file\"] == \""$sf"\")]} " ./sanity_summary.json > $TESTS_JSON 

    jinja2 $MY_DIR/sanity_rules.j2 $TESTS_JSON > $TESTS_RS

    cat $sf $TESTS_RS > $SANITY_TMP    
    mv $SANITY_TMP $sf
    
done
