#!/usr/bin/env bash
# Space start: fetch the brain code + knowledge from the private model repo, restore accounts/chats, serve.
set -euo pipefail
export SITE_DATA=/home/user/app/data
mkdir -p "$SITE_DATA"
python fetch.py
export PYTHONPATH=/home/user/rcb
export ARETY_KNOWLEDGE_DIR=/home/user/knowledge
export ARETY_DICTIONARY=/home/user/db/dictionary.sqlite
export ARETY_KNOWLEDGE_DB=/home/user/db/wikidata.sqlite
export ARETY_ENCYCLOPEDIA=/home/user/db/encyclopedia.sqlite
export ARETY_CHAT_DATA=$SITE_DATA HOST=0.0.0.0 PORT=7860
python server.py & SERVER=$!
python persist.py & PERSIST=$!
trap 'kill -TERM $PERSIST $SERVER; wait' TERM INT
wait $SERVER
