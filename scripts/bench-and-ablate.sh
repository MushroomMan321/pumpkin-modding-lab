#!/usr/bin/env bash
# First comparison (three variants at 10k machines) followed by the host-call ablation on
# Pumpkin. Runs on the benchmark host, inside tmux. Always resumes the model process at the
# end, whatever happened, as a second line of defence behind the harness's own resume.
cd "$HOME/psb/bench" || exit 1
trap 'pkill -CONT -x gb10_inference' EXIT
mkdir -p results

{
  for sv in neoforge:block_entity neoforge:batched pumpkin:wasm-batched; do
    python3 -u harness/psb.py --server "${sv%%:*}" --variant "${sv#*:}" --n 10000 --rate 10
  done
  echo BENCH-DONE
} 2>&1 | tee results/bench-n10000-r10.log

{
  for nr in 1:0 1000:0 10000:0 1000:10 1000:50 1000:100; do
    python3 -u harness/psb.py --server pumpkin --variant wasm-batched \
      --n "${nr%%:*}" --rate "${nr#*:}" --warmup 200 --measure 600 --settle 3
  done
  echo ABLATE-DONE
} 2>&1 | tee results/ablate-wasm-batched.log
