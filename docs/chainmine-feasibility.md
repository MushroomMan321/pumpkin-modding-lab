# Chain-mining plugin: feasibility on Pumpkin's plugin API

Checked 2026-10-05 against Pumpkin `2c7931a` (current `master`), plugin API v0.1, with v0.2 WIT
compared. Everything below comes from reading Pumpkin's own WIT (MIT/Apache) and host code
(GPL-3.0, read only, nothing copied); no chain-mining mod source was read.

## Summary

| Need | Today (v0.1) | How |
|---|---|---|
| Know a player broke a block | yes | `block-break-event` carries `player`, `block`, `block-pos`, `should-drop` |
| Same harvest rules as the first block | yes | `should-drop` is Pumpkin's `can_harvest` (false in creative or with the wrong tool) |
| Detect sneaking | yes | `player.as-entity().is-sneaking()`, or `player-toggle-sneak-event` |
| Read the held tool | yes, a copy | `player.get-item-in-hand(main)` returns a snapshot, not a live handle |
| Damage the tool | yes, roundabout | change the copy's `damage` component (`set-component`, raw bytes), then `set-item-in-hand` |
| Break extra blocks with proper drops | no direct call | see "Drops" below |
| Read block tags (`mineable/pickaxe`, ores, logs) | no | the plugin carries its own block lists |
| Talk to a client mod | yes | `player-custom-payload-event` (client to server), `player.send-custom-payload` (server to client) |

So it can be built on v0.1 now. The only clumsy part is drops.

## Drops

There is no `break-block` for plugins. Pumpkin's core `World::break_block(pos, cause, flags)`
already does the work (fires the break event, rolls the loot table with the cause's held tool,
sets air), but only the GameTest API (v0.2, test-only) exposes anything like it.

Workaround with what exists: per extra block, run `loot spawn <pos> mine <pos> mainhand` through
`server.execute-command` with the player as sender (Pumpkin's `/loot` rolls the block's loot table
with the tool, so Fortune and Silk Touch apply), then `set-block-state` to air. Setting a state
does not fire `block-break-event`, so the plugin does not re-trigger itself.

Confirmed at runtime (2026-10-05, scratch plugin calling `server.execute-command` as console,
explicit tool items):

| Block | Tool | Dropped |
|---|---|---|
| iron ore | diamond pickaxe | 1 raw iron |
| iron ore | none | 1 raw iron (loot tables ignore tool tier, so gate on `should-drop`) |
| stone | diamond pickaxe | 1 cobblestone |
| stone | Silk Touch pickaxe | 1 stone |
| diamond ore | diamond pickaxe | 1 diamond |
| diamond ore | Fortune III pickaxe, 5 rolls | 3, 1, 3, 2, 1 diamonds |
| oak log | diamond axe | 1 oak log |
| oak leaves | none / shears | nothing / 1 oak leaves |

Both enchantment syntaxes parse (`{"minecraft:fortune":3}` and `{fortune:3}`), and the items
spawn as item entities. Not yet tested: a player as sender with `mainhand` (needs a connected
client).

Testing needed a patched server: Pumpkin's `/forceload` marks chunks but never loads them, so on
a server with no players nothing is loaded and writes go nowhere (Pumpkin-MC/Pumpkin#2592; fix
attempts #3326 and #3610 were closed, #3845 is open). The scratch build `pumpkin-2c7931a-fl` adds
a force ticket for forced chunks. With a player online this does not matter.

Costs: two host round trips per extra block (about 0.33 ms each, see RESULTS.md), so a 64-block
vein is about 40 ms if done in one tick. Spread it over ticks with the scheduler (for example 16
blocks per tick).

Small upstream ask that would remove the workaround: expose
`world.break-block(pos, cause: option<player>, drop: bool)` in v0.2, a thin wrapper over the
existing `World::break_block`. A plugin calling it must ignore the break events it causes.

## Tool damage details

- Pumpkin applies the vanilla 1 damage for the first block *after* the break event returns. The
  plugin only adds damage for the extra blocks.
- `get-components` lists only the stack's patch, so an untouched tool shows no `max_damage`. The
  plugin needs a small table of vanilla tool durabilities.
- Unbreaking: read with `get-enchantments`; apply the vanilla 1/(level+1) chance per block.
- If the tool would break, stop the chain there and clear the hand.

## Client side

The vanilla client cannot send a keybind or draw the outline of the blocks that will go, so the
default is "sneak while mining". An optional client mod (Fabric or NeoForge, Java, for the
Minecraft version Pumpkin speaks, currently 26.3) can do both over a custom payload channel:

- client to server: "chain key held" on/off;
- server to client: the list of block positions the chain would take, for the outline.

Players without the client mod still get the sneak behaviour. The server side stays a Pumpkin
plugin; the client mod is an ordinary Java mod and does not depend on Pumpkin.

Decision for v1: server plugin only. The outline can be drawn for vanilla clients with glowing
block display entities (`block-display-entity` plus a glow colour), so the client mod would only
add a real keybind. Singleplayer never runs Pumpkin, so the existing Java chain-mining mods
already cover it.
