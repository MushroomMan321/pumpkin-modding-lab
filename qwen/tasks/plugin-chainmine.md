# Task: chain-mining plugin `plugin-chainmine` (v1, server side only)

Write a Pumpkin WebAssembly plugin: when a player breaks a block while sneaking, the plugin
also breaks the connected blocks of the same kind (a whole ore vein, a whole tree trunk),
with normal drops and tool wear.

## Rules you must follow

- This is an original design. Do not copy, imitate the code of, or name any existing
  chain-mining or vein-mining mod. Only the behaviour written below counts.
- Only use plugin API functions that exist on stock Pumpkin. `world.set-block-states` (plural)
  is a local benchmark patch: never call it.
- You may only create or change files under `pumpkin/plugin-chainmine/`.

## Where

- Crate `pumpkin/plugin-chainmine/` (package `psb-plugin-chainmine`,
  `crate-type = ["cdylib"]`; unit tests run natively with `cargo test --lib`). Depend on
  `pumpkin-plugin-api = { workspace = true }` and `tracing = "0.1"`, nothing else. Plugin
  metadata name: `chainmine`.
- Keep the pure logic (no plugin API calls) in its own module, for example `src/plan.rs`,
  and unit-test it there. The plugin API compiles natively but its host calls panic outside
  the server, so tests must never call it.

## Behaviour

### Trigger
Register a blocking `BlockBreakEvent` handler. Start a chain only when all hold:
- the event has a player, is not cancelled, and `should_drop` is true (Pumpkin sets it only
  when the player may harvest the block with the held item, and never in creative);
- the player is sneaking (`player.as_entity().is_sneaking()`);
- the origin block may be chained (see Exclusions).

### Which blocks
- Same kind means the same block (compare block ids, ignore block state properties, so an
  `oak_log` with `axis=x` matches an `oak_log` with `axis=y`).
- Connected means any of the 26 neighbours (faces, edges and corners).
- Grow the set from the origin, always taking next the frontier block nearest to the origin
  by squared distance (ties: lowest y, then x, then z). This keeps the set connected and
  makes it deterministic.
- At most 64 blocks per chain including the origin (so at most 63 extra), and only blocks
  within 16 blocks of the origin on every axis (`|dx|, |dy|, |dz| <= 16`).
- Exclusions (never chained, and no chain starts from them): air, fluids, blocks with a block
  entity (chests, barrels, furnaces, ...), and unbreakable blocks (hardness below 0).
- Reading blocks is cheap (direct host calls): plan the whole chain inside the event handler.
- Work in the world the player is in (`player.as_entity().get_world()`), not always the
  overworld: remember its dimension (`world.get_dimension()`) with the chain and look the world
  up again by that name when breaking (`server.get_world_by_name`).

### Breaking
- Writing is expensive (each call is a round trip), so break at most 8 blocks per server
  tick, using the scheduler, starting on the tick after the event.
- Before breaking a block, check it is still the same block; skip it if not.
- To break a block: run, with `server.execute_command(..., CommandSender::Console)`:
  `execute in <dimension> run loot spawn <ox>.5 <oy>.5 <oz>.5 mine <x> <y> <z> <tool>`
  (the console's own world is the overworld; `execute in` runs `/loot` in the chain's world)
  where `<ox> <oy> <oz>` is the origin (all drops land where the player mined) and `<tool>`
  is the player's held item written as an item argument: the item key plus its
  enchantments, e.g. `minecraft:diamond_pickaxe[enchantments={"minecraft:fortune":3}]`, or
  just `minecraft:diamond_pickaxe` when it has none. A bare hand chains too: leave `<tool>`
  out and apply no wear. Then set the block to air with
  notify-neighbors and notify-listeners. (A player sender would need operator rights for
  `/loot`; the console does not.)
- Pumpkin itself breaks the origin and wears the tool for it; the plugin handles only the
  extra blocks.

### Tool wear
- For each extra block, before breaking it, read the player's main-hand item again (it is a
  copy). Stop the chain if the player is gone or holds a different item (compare item keys).
- Charge wear from the item's damage as it is at that moment, never from a value remembered
  when the chain started: Pumpkin wears the tool for the origin after your handler returns,
  so a remembered value is always out of date.
- Damageable items: wooden 59, stone 131, copper 190, iron 250, golden 32, diamond 1561,
  netherite 2031 (pickaxe, axe, shovel, hoe, sword), shears 238. Anything else, or an item
  with the `unbreakable` component, takes no wear.
- Current damage is the `damage` data component (missing means 0). Its bytes are a single
  VarInt (protocol encoding). Write it back with `set_component` and `set_item_in_hand`.
- Each extra block costs 1 damage, except that Unbreaking level `n` skips the damage with
  probability `n / (n + 1)` (use a small seeded PRNG of your own; no new dependencies).
- Never break the tool: stop the chain when the next damage would leave fewer than 1 point
  of durability (`max - damage - 1 < 1`).

### Test command (console)
Register `/chainmine test <x> <y> <z> <tool>` (permission default: operator level 2; `<tool>`
is the rest of the line, a greedy string). It runs the same code as a player break, as if a
sneaking survival player holding `<tool>` broke the block at `x y z`, with these differences:
there is no player, so no sneaking check, no `should_drop` check and no tool wear; and the
command breaks the origin itself first (same `loot` + air steps), because no real break
happened. Print with `sender.send_message`:
- `chainmine test origin=<block key> extra=<k>` when a chain starts, `k` = planned extra blocks;
- `chainmine skip reason=<excluded|air>` when the origin may not be chained (nothing broken);
- `chainmine done extra=<k>` when the scheduled breaking has finished, `k` = extra blocks
  actually broken. The command has returned by then, so write this line to the server log
  with `tracing::info!` (add `tracing = "0.1"` as a dependency; the plugin API forwards
  `tracing` to the server log). Log it for player chains too. The gate waits for this line.

## Finding the API

Everything this task needs from the API is in `qwen/notes/pumpkin-plugin-api.md`, checked
against the compiler and a real server. `qwen/templates/plugin/src/lib.rs` is a skeleton that
compiles and uses every one of those calls (command with integer and greedy arguments,
permission, `BlockBreakEvent` handler, scheduler, world reads, `execute_command` as console,
held item and its components, logging). Read those two files first, then write code. Read the
API source (`../Pumpkin/crates/pumpkin-plugin-api`, generated from
`../Pumpkin/crates/pumpkin-plugin-wit/v0.1/*.wit`) only to resolve a compiler error.

## Done means

`qwen/gate-chainmine.sh` prints `GATE PASSED`: build, clippy with `-D warnings` (wasm and
native), at least 8 passing unit tests, and a smoke run on a real Pumpkin server in which
`/chainmine test` breaks exactly the expected blocks in these cases: a small vein with
diagonal links next to a different ore and an isolated ore of the same kind; a solid stone
cube larger than the 64-block cap; a straight line longer than the 16-block reach; an oak log
column touching planks, a stripped log and a birch log; a barrel and bedrock (must be
skipped). Pumpkin's `/setblock` cannot set block states, so "ignore block state properties"
is checked only by your unit tests: include one.
