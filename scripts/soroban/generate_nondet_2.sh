#!/bin/bash

MY_DIR=$(dirname $(realpath $0))

TYPES_JSON=/tmp/types.json
TYPES_FOR_FILE=/tmp/types_for_file.json

cd $1

python $MY_DIR/parse_contracttypes.py . > $TYPES_JSON

for f in $(jq -r '.[]["file"]' $TYPES_JSON | sort | uniq); do
    echo processing $f
    
    jq "[ .[] | select(.[\"file\"] == \"$f\") ]" $TYPES_JSON > $TYPES_FOR_FILE

    cat >> $f <<EOF
use cvlr::nondet::Nondet;
use cvlr_soroban_derive::rule;
use cvlr_soroban::*;
EOF

    (jq -f $MY_DIR/nonrec.jq < $TYPES_FOR_FILE |  jq '{ "types": [ .[] | select(has("fields") and (.["fields"][0] | has("name")))]}' | jinja2 $MY_DIR/struct_nondet.j2) >> $f
	
    (jq -f $MY_DIR/nonrec.jq < $TYPES_FOR_FILE |  jq '{ "types": [ .[] | select(has("fields") and (.["fields"][0] | has("name") | not))]}' | jinja2 $MY_DIR/struct_unnamed_nondet.j2) >> $f
	
    (jq -f $MY_DIR/nonrec.jq < $TYPES_FOR_FILE | jq  '{ "types": [ .[] | select(has("variants")) ]}' | jinja2 $MY_DIR/enum_nondet.j2) >> $f

done
