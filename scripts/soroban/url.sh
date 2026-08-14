#!/bin/bash

MY_DIR=`dirname $0`

DIR=$1
cat $DIR/.git/config | awk -f $MY_DIR/url.awk
