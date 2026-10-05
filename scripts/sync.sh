#!/usr/bin/env bash
# Mirrors the working tree to the benchmark host (spark2 by default), deleting files
# that no longer exist locally. Build outputs, caches, results, loop logs and the
# Qwen-written crates (pumpkin/wasm-*, qwen/runs) on the host are kept; pull those
# with scripts/pull.sh.
# Usage: scripts/sync.sh [host]
set -euo pipefail
host="${1:-spark2}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
tar --exclude=./.git \
    --exclude=./neoforge/build --exclude=./neoforge/.gradle --exclude=./neoforge/run \
    --exclude='*/target' --exclude=./results \
    --exclude=./qwen/runs --exclude='./qwen/*.log' --exclude='./pumpkin/wasm-*' \
    -czf - . | ssh -o BatchMode=yes "$host" '
      set -e
      rm -rf ~/psb/.incoming && mkdir -p ~/psb/.incoming ~/psb/bench
      tar xzf - -C ~/psb/.incoming
      rsync -a --delete \
        --exclude=/neoforge/build --exclude=/neoforge/.gradle --exclude=/neoforge/run \
        --exclude=target --exclude=/results --exclude=/run \
        --exclude=/qwen/runs --exclude="/qwen/*.log" --exclude="/pumpkin/wasm-*" \
        ~/psb/.incoming/ ~/psb/bench/'
