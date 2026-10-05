#!/usr/bin/env bash
# Pass/fail gate for one Pumpkin variant. The Qwen loop may not edit this file.
#   qwen/gate.sh <variant>        e.g. qwen/gate.sh wasm-batched
# Stages, in order; the first failure stops the gate:
#   1. build      cargo build --release for the variant's crate (wasm32-wasip2)
#   2. lint       cargo clippy with warnings as errors
#   3. smoke      a short real run on Pumpkin through the harness; the run's
#                 /psb status checksum must equal the reference implementation's
set -uo pipefail
variant="${1:?usage: gate.sh <variant>}"
crate="psb-${variant}"
bench="$HOME/psb/bench"
. "$HOME/.cargo/env"

stage() { echo; echo "=== gate stage: $1"; }
fail() { echo; echo "GATE FAILED at stage: $1"; exit 1; }

cd "$bench/pumpkin" || fail setup
stage build
taskset -c 10-14 cargo build --release -p "$crate" 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail build

stage lint
taskset -c 10-14 cargo clippy --release -p "$crate" -- -D warnings 2>&1 | tail -60
[ "${PIPESTATUS[0]}" -eq 0 ] || fail lint

stage smoke
cd "$bench" || fail setup
taskset -c 10-14 cargo build --release -q --manifest-path workload/Cargo.toml --bin checksum || fail setup
timeout 900 python3 harness/psb.py --server pumpkin --variant "$variant" \
    --n 1000 --rate 10 --warmup 100 --measure 200 --settle 3 --no-pause 2>&1 | tail -40
[ "${PIPESTATUS[0]}" -eq 0 ] || fail smoke

echo
echo "GATE PASSED"
