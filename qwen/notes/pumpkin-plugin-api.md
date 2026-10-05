# Pumpkin plugin API (v0.1) cheat sheet

Checked 2026-10-05 against Pumpkin `2c7931a`. Every call below compiles in
`qwen/templates/plugin/src/lib.rs`, and that skeleton ran on a real server. Start from the
skeleton; use this sheet instead of reading the API source. Read the source only when the
compiler reports an error you cannot place.

## Imports that work

```rust
use pumpkin_plugin_api::{
    Context, Plugin, PluginMetadata, Server,
    command::{Arg, ArgumentType, Command, CommandError, CommandNode, CommandSender, ConsumedArgs, StringType},
    command_wit::Number,
    commands::CommandHandler,
    common::Hand,
    data_components::DataComponent,
    events::{BlockBreakEvent, EventData, EventHandler, EventPriority},
    permission::{Permission, PermissionDefault, PermissionLevel},
    register_plugin,
    scheduler::{schedule_delayed_task, schedule_repeating_task, cancel_task},
    server::CommandSender as RunAs,
    text::TextComponent,
    world::{BlockFlags, BlockPos},
};
```

Logging: add `tracing = "0.1"` and use `tracing::info!(...)`; it lands in the server log.

## Commands

- Build: `Command::new(&["name".to_string()], "description")`, then `.then(CommandNode::literal("sub")
  .then(CommandNode::argument("x", &ArgumentType::Integer((None, None))) ... .execute(Handler)))`.
- Greedy rest-of-line string: `ArgumentType::String(StringType::Greedy)` (variants: `SingleWord`,
  `Quotable`, `Greedy`).
- Register: `context.register_permission(&Permission { node, description, default:
  PermissionDefault::Op(PermissionLevel::Two), children: Vec::new() })?` then
  `context.register_command(command, "plugin:node")`.
- Handler: `impl CommandHandler for H { fn handle(&self, sender: CommandSender, server: Server, args:
  ConsumedArgs) -> Result<i32, CommandError> }`.
- Arguments: `args.get_value("x")` (not `get`). Integers arrive as `Arg::Num(Ok(Number::Int32(v)))`
  (there is no `Arg::Integer`); strings as `Arg::Simple(s)`.
- Reply: `sender.send_message(TextComponent::text(&format!(...)))`.
- Errors: `CommandError::InvalidConsumption(Some(name.into()))`, `CommandError::CommandFailed(TextComponent)`.

## Running a server command

`server.execute_command(&cmd, RunAs::Console)` where `RunAs` is `pumpkin_plugin_api::server::CommandSender`.
This is a different type from `command::CommandSender` (the one a command handler receives), which has
no `Console` variant. Console feedback is printed to the server log (and broadcast to ops).

## Events

- `impl EventHandler<BlockBreakEvent> for H { fn handle(&self, server: Server, event:
  EventData<BlockBreakEvent>) -> EventData<BlockBreakEvent> }`; return the event.
- Fields: `event.player: Option<Player>`, `event.block: String` (name without `minecraft:`),
  `event.block_pos: BlockPos`, `event.exp`, `event.should_drop: bool`, `event.cancelled: bool`.
- Register blocking: `context.register_event_handler::<BlockBreakEvent, _>(H, EventPriority::Normal, true)?`.
- `should_drop` is Pumpkin's harvest check: false in creative or with the wrong tool.

## World

- `server.get_world_by_name("minecraft:overworld") -> Option<World>`.
- Reads (direct host calls, cheap): `world.get_block(pos)` returns a record with `name` (no
  `minecraft:` prefix), `id: u16` (block id, same for every state of the block), `hardness: f32`
  (negative = unbreakable), and more; `world.get_block_state_id(pos) -> u16` (differs per state);
  `world.get_block_id(pos)`; `world.get_block_entity(pos).is_some()` for chests, barrels, ...
- Writes (each about 0.33 ms, a round trip): `world.set_block_state(pos, state_id, BlockFlags::NOTIFY_NEIGHBORS
  | BlockFlags::NOTIFY_LISTENERS)`. Never `set_block_states` (plural): it is a local benchmark patch.
- `BlockPos { x, y, z }` (i32 fields).
- Combine flags with `|` (there is no `union` method).
- Setting a block does not fire `BlockBreakEvent`.

## Players and items

- `player.as_entity().is_sneaking()`.
- `player.get_item_in_hand(Hand::Right) -> Option<ItemStack>`: `Hand::Right` is the main hand,
  `Hand::Left` the off hand. The stack is a copy: change it, then
  `player.set_item_in_hand(Hand::Right, Some(stack))`.
- Item id: `stack.get_registry_key() -> String`, e.g. `"minecraft:diamond_pickaxe"` (there is no
  `item_id`). Typed: `ItemStackExt::get_item() -> Option<Item>`.
- Enchantment level: `stack.get_custom_enchantment_level("minecraft:unbreaking") -> Option<u32>`. Avoid
  `get_enchantments` (reported to panic on some keys).
- Components: `stack.get_components()` lists only components changed from the item's defaults, each with
  `.component: DataComponent` and `.value: Vec<u8>`. `stack.set_component(DataComponent::Damage, &bytes)`
  takes a slice. `damage` is one protocol VarInt; an untouched tool has no `damage` entry (= 0) and its
  `max_damage` is not listed either.

## Scheduler

`schedule_delayed_task(ticks, move |server: Server| { ... })` and
`schedule_repeating_task(delay, period, move |server: Server| { ... }) -> u32` (cancel with
`cancel_task(id)`). Closures must be `Fn + Send + Sync + 'static`: keep shared state in
`Arc<Mutex<...>>` or a `static Mutex`.

## Testing

- `crate-type = ["cdylib", "rlib"]` lets `cargo test --target <host triple>` run natively. The API
  compiles natively but every host call panics there: keep tested logic in plain functions.
- The benchmark host has no player online: chunks load only on the patched server
  (`~/psb/bin/pumpkin-2c7931a-fl`), whose `/forceload` works.
