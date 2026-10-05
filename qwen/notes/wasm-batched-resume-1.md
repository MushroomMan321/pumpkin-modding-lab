Handoff from the supervisor (the previous run hit its turn limit).

1. Done: pumpkin/wasm-batched/src/lib.rs is written and compiles (cargo_check passes).
2. The gate's lint stage failed three times only because clippy was not installed on this
   machine. That was an environment problem, not your code, and it is fixed now.
3. Clippy now reports one real issue in your code: "this `if` statement can be collapsed"
   (clippy::collapsible_if). Run the gate to see the exact location.
4. Next step: run_gate, fix the collapsible `if`, run_gate again. After lint, the smoke stage
   starts a real Pumpkin server with your plugin and checks the /psb status checksum against
   the reference; if it fails, read the harness output and the server log lines it prints.
   Do not read or search the loop's own files (qwen/loop.py, qwen/runs); they cannot help.
