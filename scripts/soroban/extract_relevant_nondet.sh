#!/bin/bash

TESTS_JSON=$1
TYPES_JSON=$2

(for type in $(jsonpath_ng '$.contracts[*].functions[*].params[*]..type' $TESTS_JSON | fgrep -v \{ | sort | uniq); do
	 jq "[ .[] | select(.[\"name\"] == \"$type\") ]" $TYPES_JSON
    done) | jq -s '. | add'
