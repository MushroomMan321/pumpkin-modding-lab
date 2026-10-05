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

### Chain-mining plugin (ultimine-style)
- [ ] Feasibility check: can a plugin drop items, damage tools and detect sneaking today?
- [ ] Write the Qwen task + gate (tree/vein scan logic unit-tested; `/chainmine x y z` console
      command exercising the same path as a player break).
- [ ] Build it (clean-room, see CLAUDE.md), publish to the Pumpkin marketplace, post in #plugin-dev.
- [ ] Optional: mention it in #plugin-dev first; someone started a Rust vein miner there on 2026-09-29.

### Later
- [ ] `native` variant: the workload compiled into the server, to show the floor a bulk API can reach.
- [ ] Player-facing ModDecoded article ("Would a Rust server make my modpack faster?") once Phoenix
      has responded and the numbers are repeated.

## Done
- [x] 2026-10-05: benchmark scaffold, NeoForge and Pumpkin variants, harness, Qwen loop with
      escalation/handoff/ledger, dashboard.
- [x] 2026-10-05: 10k-machine comparison, write-cost ablation, bulk-write prototype
      ([docs/RESULTS.md](docs/RESULTS.md), commits `39ec4a6`, `1579a78`).
- [x] 2026-10-05: results posted in Pumpkin #plugin-dev; the plugin API is moving to v0.2, so the
      next round of measurements targets it.
