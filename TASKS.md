# Tasks

Open work first, newest decisions at the top of each section. Move a task to Done (with the date
and a link to the commit or result) when it is finished.

## Open

### Next up
- [ ] **Cost per extra plugin.** Load 1, 10, 50 and 100 copies of a minimal tick-listening plugin
      and measure tick time. Every run so far used a single workload plugin, so this is unknown and
      decides whether a many-mod setup is viable.
- [ ] **Cost of reads.** Same ablation as the writes, with `get-block-state` per call.
- [ ] **Fixed floor without the per-tick world lookup.** Cache the overworld in the plugin to split
      "calling a plugin" from "one host call".
- [ ] **Repeats.** Three runs of each main configuration before anything is published beyond Discord.

### v0.2 (Pumpkin plugin ABI, PR #3713)
- [ ] Draft a short comment for #3713 (the user posts it): should v0.2 include bulk calls
      (`set-block-states`, bulk reads), or is async meant to make them unnecessary? Link docs/RESULTS.md.
- [ ] When the v0.2 runtime lands: port `wasm-batched` to v0.2 through the Qwen loop and rerun:
      one write at a time, overlapping async writes, bulk (if in the design).
- [ ] **Fix the unloaded-chunk problem in that rerun** (found 2026-10-05; deferred to v0.2 on the
      Pumpkin team's "wait for v0.2"). The v0.1 runs wrote into unloaded chunks: Pumpkin's
      `/forceload` never loads chunks (Pumpkin-MC/Pumpkin#2592) and runs have no player, so the
      chunk write was skipped while the follow-up work (`on_block_state_set`) still ran; the
      in-plugin checksum cannot catch it. For the rerun: use a server whose forceload loads
      chunks (patch on branch `fix/forceload-tickets`, worktree `~/psb/Pumpkin-fl`, binary
      `~/psb/bin/pumpkin-2c7931a-fl`, or upstream if fixed by then), add a read-back check to
      the harness, and say in the v0.2 results that the v0.1 numbers had this flaw.

### Chain-mining plugin, v1 = server plugin only (decided 2026-10-05)
- v1: sneak to chain, drops via `/loot` run as console, tool wear, no client mod. Outline
  (glowing block displays) is the next task after v1 passes. A keybind client mod only if
  players ask for it.
- [x] 2026-10-05: Qwen built v1; gate passed at turn 209 (run `20261005-185239`, 4 nudges,
      3 hints, one of them for a gate bug). Archived on the benchmark host as
      `~/psb/archive/plugin-chainmine-v1-20261005`, copy in `pumpkin/plugin-chainmine`.
- [x] 2026-10-05: review of the player path found three bugs the gate cannot see, fixed by hand
      (scratch build `~/psb/scratch/cm-v1`, unit tests 15/15, clippy clean, smoke 23/23 incl. a
      new drops check): wear was compared against the damage remembered at the start, so
      Pumpkin's own wear for the origin stopped every real chain after the first block; chains
      always ran in the overworld; a bare hand never chained. Task text updated for future runs.
- [x] 2026-10-05: ledger-mode run from scratch (`20261005-193834`, Swift1.5): gate passed at
      turn 153 in 29 min, 162 calls, 3 manager steps, 2 milestone restarts, 1 hint (the test
      command broke the origin before planning). Earlier runs on the old loop: 135 + 209 turns,
      5 hints. Not a clean A/B (those started without the cheat sheet); the `--ledger off`
      baseline was skipped by decision. Its code got tool wear right but has the overworld-only
      and bare-hand gaps; archived as `~/psb/archive/plugin-chainmine-ledger-20261005`. The
      fixed v1 in `pumpkin/plugin-chainmine` stays the play-test build.
- [ ] Play-test with a real client: sneak-break a vein, Fortune/Silk Touch, tool wear and the
      stop-before-breaking rule, switching items mid-chain.
- [ ] Known v1 gaps: no XP from extra blocks (`/loot` gives none); ops see a "Dropped ..." line
      per block (console feedback is broadcast to ops).
- [ ] Optional upstream ask (the user posts it): `world.break-block(pos, cause, drop)` in v0.2.
- [ ] Publish to the Pumpkin marketplace, post in #plugin-dev.
- [ ] Optional: mention it in #plugin-dev first; someone started a Rust vein miner there on 2026-09-29.

### Machine layer (block entities and menus on vanilla blocks, started 2026-10-06)
- Chosen instead of helping on runtime registries (PR #3887, reviewers want it after 1.0).
  Feasibility: buildable on v0.1 today, see [docs/machine-feasibility.md](docs/machine-feasibility.md).
  Scratch plugin `pumpkin/feas-machine`, checks `scripts/feas_machine_headless.py` (21/21) and
  `scripts/feas_chunk_unload.py` (5/5).
- [ ] Upstream asks (the user posts them; check for duplicates first): `ChunkLoadEvent` is never
      fired; `open-gui` returns no window ID and there is no `close-screen`; plugin menus reject
      clicks on the player's half ("invalid slot index 27"); `plugin unload` leaves permissions
      registered so a reload fails; forced chunks aren't reloaded after a restart.
- [ ] Decide the first real machine (crusher on a barrel is the scratch one), then a Qwen task and
      gate for it, following the chain-mining loop setup.
- [ ] Not checked: the hand stack after placing a plugin-given machine item in survival (client
      still showed 4 after placing 1).

### Later
- [ ] `native` variant: the workload compiled into the server, to show the floor a bulk API can reach.
- [ ] Player-facing ModDecoded article ("Would a Rust server make my modpack faster?") once Phoenix
      has responded and the numbers are repeated.

## Done
- [x] 2026-10-06: machine-layer feasibility, headless and with a real client driven in the
      background (menus, progress arrow, right-click to open, placing a machine item, restart
      survival) ([docs/machine-feasibility.md](docs/machine-feasibility.md)).
- [x] 2026-10-05: Qwen task and gate for the chain-mining plugin; the loop accepts `--gate` and
      `--first-read`; gate checked end to end against a stub plugin (scene building and probes
      pass, plugin checks fail as expected).
- [x] 2026-10-05: `/loot spawn <pos> mine <pos> <tool>` from a plugin gives tool-aware drops
      (Silk Touch, Fortune, shears confirmed; results in
      [docs/chainmine-feasibility.md](docs/chainmine-feasibility.md)).
- [x] 2026-10-05: chain-mining feasibility: buildable on v0.1 today; drops need a `/loot`
      workaround ([docs/chainmine-feasibility.md](docs/chainmine-feasibility.md)).
- [x] 2026-10-05: benchmark scaffold, NeoForge and Pumpkin variants, harness, Qwen loop with
      escalation/handoff/ledger, dashboard.
- [x] 2026-10-05: 10k-machine comparison, write-cost ablation, bulk-write prototype
      ([docs/RESULTS.md](docs/RESULTS.md), commits `39ec4a6`, `1579a78`).
- [x] 2026-10-05: results posted in Pumpkin #plugin-dev; the plugin API is moving to v0.2, so the
      next round of measurements targets it.
