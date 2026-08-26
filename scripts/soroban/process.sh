#!/bin/bash

MY_DIR=$(dirname $0)

DIR=$1
CONTRACT_JSON=/tmp/contracts.json
TYPES_JSON=/tmp/types.json
USES_TXT=/tmp/uses.rs

URL=$(bash $MY_DIR/url.sh $DIR)

CS="$(bash $MY_DIR/cargos.sh $DIR)"
if [ -n "$CS" ]; then
  python $MY_DIR/soroban_repo_scraper_7.py $URL --cargo_dirs $CS > $CONTRACT_JSON
else
  python $MY_DIR/soroban_repo_scraper_7.py $URL > $CONTRACT_JSON
fi       

python $MY_DIR/parse_contracttypes.py $DIR | jq '[ .[] | select(.["use"] | contains("test") | not) ]' > $TYPES_JSON

(echo "#![allow(unused)]"; jq -r '"use " +.[]["use"] + ";"' < $TYPES_JSON)  > $USES_TXT

(cat $USES_TXT; (python $MY_DIR/extract_enums.py --json | jinja2  $MY_DIR/enum_nondet.j2)) > $DIR/src/nondet_enum.rs

cat $USES_TXT > $DIR/src/nondet_struct.rs
(jq -f $MY_DIR/nonrec.jq < $TYPES_JSON | jq '{ "types": . }' | jinja2 $MY_DIR/struct_nondet.j2) >> $DIR/src/nondet_struct.rs

(cat $USES_TXT; (jinja2 $MY_DIR/sanity_rules.j2 $CONTRACT_JSON)) > $DIR/src/sanity.rs

mkdir -p $DIR/conf
jq '.["packages"][] | { "name": .["name"], "functions": [ .["contracts"][]["impl_blocks"][] | select(.source_file | contains("test") | not).functions[].name ]}' $CONTRACT_JSON | jinja2 $MY_DIR/sanity_conf.j2 > $DIR/conf/sanity.conf
