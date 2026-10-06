#!/usr/bin/env bash
# Pass/fail gate for the chain-mining plugin. The Qwen loop may not edit this file.
#   qwen/gate-chainmine.sh
# Stages, in order; the first failure stops the gate:
#   1. build      cargo build --release (wasm32-wasip2)
#   2. lint       cargo clippy -D warnings, for wasm and for the native test build
#   3. test       native unit tests; at least 8 must pass
#   4. smoke      qwen/chainmine_smoke.py on a real Pumpkin server
set -uo pipefail
crate="psb-plugin-chainmine"
bench="$HOME/psb/bench"
. "$HOME/.cargo/env"
host="$(rustc -vV | sed -n 's/^host: //p')"

stage() { echo; echo "=== gate stage: $1"; }
fail() { echo; echo "GATE FAILED at stage: $1"; exit 1; }

cd "$bench/pumpkin" || fail setup
stage build
taskset -c 10-14 cargo build --release -p "$crate" 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail build

stage lint
taskset -c 10-14 cargo clippy --release -p "$crate" -- -D warnings 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail lint
taskset -c 10-14 cargo clippy --release -p "$crate" --target "$host" --tests -- -D warnings 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail lint

stage test
out="$(taskset -c 10-14 cargo test --release -p "$crate" --target "$host" --lib 2>&1)"
code=$?
echo "$out" | tail -60
[ "$code" -eq 0 ] || fail test
passed="$(echo "$out" | sed -n 's/^test result: ok\. \([0-9]*\) passed.*/\1/p' | awk '{s+=$1} END {print s+0}')"
echo "unit tests passed: $passed (need at least 8)"
[ "$passed" -ge 8 ] || fail test

stage smoke
timeout 900 python3 "$bench/qwen/chainmine_smoke.py" 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail smoke

echo
echo "GATE PASSED"
