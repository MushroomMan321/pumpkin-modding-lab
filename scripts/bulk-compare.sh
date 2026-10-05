#!/usr/bin/env bash
# One host call per write (wasm-batched) against one host call per tick (bulk), both on the
# SAME Pumpkin binary: the build with pumpkin/patches/bulk-write.py applied. Runs on the
# benchmark host inside tmux; always resumes the model process at the end.
#   scripts/bulk-compare.sh <pumpkin-binary>
bin="${1:?usage: bulk-compare.sh <pumpkin-binary>}"
cd "$HOME/psb/bench" || exit 1
trap 'pkill -CONT -x gb10_inference' EXIT
mkdir -p results

{
  # Short runs at 1,000 machines: 10 and 100 writes per tick.
  for rate in 10 100; do
    for variant in wasm-batched bulk; do
      python3 -u harness/psb.py --server pumpkin --variant "$variant" --pumpkin-bin "$bin" \
        --n 1000 --rate "$rate" --warmup 200 --measure 600 --settle 3
    done
  done
  # The main benchmark's configuration: 10,000 machines, 100 writes per tick, full length.
  for variant in wasm-batched bulk; do
    python3 -u harness/psb.py --server pumpkin --variant "$variant" --pumpkin-bin "$bin" \
      --n 10000 --rate 10
  done
  echo BULK-DONE
} 2>&1 | tee results/bulk-compare.log
