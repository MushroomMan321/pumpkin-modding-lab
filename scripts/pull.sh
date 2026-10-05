#!/usr/bin/env bash
# Copies results and Qwen-written code back from the benchmark host into this repo.
# Usage: scripts/pull.sh [host]
set -euo pipefail
host="${1:-spark2}"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
ssh -o BatchMode=yes "$host" 'cd ~/psb/bench && tar czf - --exclude=target results/raw $(ls -d pumpkin/wasm-* qwen/runs 2>/dev/null)' | tar xzf -
