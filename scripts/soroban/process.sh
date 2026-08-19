#!/bin/bash

MY_DIR=$(dirname $0)

DIR=$1

URL=$(bash $MY_DIR/url.sh $DIR)

CS="$(bash $MY_DIR/cargos.sh $DIR)"
if [ -n "$CS" ]; then
  python $MY_DIR/soroban_repo_scraper_7.py $URL --cargo_dirs $CS
else
  python $MY_DIR/soroban_repo_scraper_7.py $URL 
fi       
