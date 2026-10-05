# Task: Pumpkin variant `wasm-batched`

Write a Pumpkin WebAssembly plugin that runs the `energy-grid` workload, keeping all
machine state inside the plugin and doing one batch of work per server tick.

## Where

- Create the crate `pumpkin/wasm-batched/` (package name `psb-wasm-batched`,
  `crate-type = ["cdylib"]`). The workspace already includes `pumpkin/wasm-*`.
  The build must produce `pumpkin/target/wasm32-wasip2/release/psb_wasm_batched.wasm`.
- Depend on `pumpkin-plugin-api` and `psb-workload` with `{ workspace = true }`.
- You may only create or change files under `pumpkin/wasm-batched/`.

## What it must do

Read `SPEC.md` first. It is the contract; follow it exactly.

1. Use `psb_workload::Grid` for the energy step and the list of writes. Do not
   reimplement the arithmetic.
2. Register the `/psb` command with these subcommands, printing exactly these lines with
   `sender.send_message(TextComponent::text(...))`:
   - `psb setup <n> <rate_permille>` places the grid and prints
     `psb setup n=<n> rate=<rate> side=<s> variant=wasm-batched`
   - `psb start` prints `psb started`; `psb stop` prints `psb stopped`
   - `psb run <ticks>` prints `psb running` and runs exactly `<ticks>` workload ticks
   - `psb status` prints
     `psb status tick=<t> n=<n> rate=<rate> energy_sum=<16 lowercase hex digits> writes=<w> variant=wasm-batched running=<true|false>`
   `n` is 1..=1000000, `rate_permille` 0..=1000, `ticks` >= 1 (integer arguments).
3. Setup places, for every machine, `minecraft:stone` at `y = Y_MACHINE` and
   `minecraft:white_wool` at `y = Y_DISPLAY`, using only the notify-listeners update flag
   (no neighbour updates), in the overworld.
4. Register a blocking `ServerTickStartEvent` handler (normal priority). While running, each
   server tick it calls `Grid::step` once and applies every write with the world's
   `set_block_state`, using notify-neighbors and notify-listeners (this matches NeoForge's
   `Block.UPDATE_ALL`). Lime is `minecraft:lime_wool`, white is `minecraft:white_wool`.
   Look up block state ids once at setup, not every tick.
5. Plugin metadata name: `psb-wasm-batched`. Request no permissions.

## Finding the API

The plugin API is the crate at `../Pumpkin/crates/pumpkin-plugin-api` (relative to the
repo). It is generated from the WIT files in `../Pumpkin/crates/pumpkin-plugin-wit/v0.1/`.
Read the source rather than guessing. A working plugin that registers commands, a
command argument and an event handler is `pumpkin/probe/src/lib.rs`; copy its patterns.

## Done means

`qwen/gate.sh wasm-batched` prints `GATE PASSED`: it builds, passes clippy with
`-D warnings`, and a real run on Pumpkin reproduces the reference checksum.
