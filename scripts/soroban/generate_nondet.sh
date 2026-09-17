#!/bin/bash

MY_DIR=$(dirname $(realpath $0))

TESTS_JSON=/tmp/tests.json
TESTS_RS=/tmp/tests.rs
SANITY_TMP=/tmp/sanity.rs
TYPES_JSON=/tmp/types.json
NONDET_JSON=/tmp/nondet_types.json
USES_TXT=/tmp/uses.rs

cd $1

for cf in $(find . -name Cargo.toml); do
    if [[ "$cf" == *"fuzz"* ]]; then
	echo skipping $cf
	continue
    fi

    if [[ -d $(dirname $cf)/src ]]; then
	CRATE=$(awk -F= '/^name/ { print $2; }' $cf)
	
	echo $CRATE
	
	pushd $(dirname $cf)
    python $MY_DIR/parse_contracttypes.py `pwd` | jq "[.[] | select(.[\"file\"] | contains(\"fuzz\") or contains(\"test\") | not) | select (.[\"public\"] or .[\"owning_create\"] ==$CRATE)]" > $TYPES_JSON
    popd

    if [[ "[]" != "$(cat $TYPES_JSON)" ]]; then
	NONDET_ENUM_RS=$(dirname $cf)/src/nondet_enum.rs
	NONDET_STRUCT_RS=$(dirname $cf)/src/nondet_struct.rs
	
	(echo "#![allow(unused)]";
	 (jq -r '"use " +.[]["use"] + ";"' < $TYPES_JSON | fgrep -v "use ;" | fgrep -v fuzz | egrep -v ':tests?:' | sort | uniq;
	  jq -r '.[]["variants"] | select(. != null)[]["fields"] | select(. != null)[] | select(has("uses")) | select(.["type"]["public"])["uses"][] ' $TYPES_JSON  | sort | uniq | awk '{ print "use " $1 ";" }') | sort | uniq) > $USES_TXT

	sort $USES_TXT | uniq > $NONDET_STRUCT_RS
	(jq -f $MY_DIR/nonrec.jq < $TYPES_JSON | jq '{ "types": . }' | jinja2 $MY_DIR/struct_nondet.j2) >> $NONDET_STRUCT_RS
	
	sort $USES_TXT | uniq > $NONDET_ENUM_RS
	(jq -f $MY_DIR/nonrec.jq < $TYPES_JSON | jq '{ "types": . }' | jinja2 $MY_DIR/enum_nondet.j2) >> $NONDET_ENUM_RS

	if [[ -e $(dirname $cf)/src/mod.rs ]]; then
	    echo "pub mod nondet_enum;" >> $(dirname $cf)/src/mod.rs
	    echo "pub mod nondet_struct;" >> $(dirname $cf)/src/mod.rs
	else
	    echo "pub mod nondet_enum;" >> $(dirname $cf)/src/lib.rs
	    echo "pub mod nondet_struct;" >> $(dirname $cf)/src/lib.rs
	fi
    fi
    fi
done
