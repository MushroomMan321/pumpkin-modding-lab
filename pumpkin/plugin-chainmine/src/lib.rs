//! Chain-mining plugin.
//!
//! When a player breaks a block while sneaking, the blocks of the same kind connected to it
//! (an ore vein, a tree trunk) are broken too, with normal drops and tool wear.
//!
//! The pure parts — which blocks belong to the chain, the item string for `/loot`, the
//! durability table, the `VarInt` codec, the wear roll — live in [`plan`] and are unit-tested
//! there. Everything in this file talks to the server.

#![warn(clippy::pedantic)]
#![allow(
    clippy::missing_errors_doc,
    clippy::missing_panics_doc,
    clippy::module_name_repetitions,
    clippy::ref_option,
    clippy::trivially_copy_pass_by_ref,
    clippy::type_complexity
)]

mod plan;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard};

use pumpkin_plugin_api::{
    Context, Plugin, PluginMetadata, Server,
    command::{
        Arg, ArgumentType, Command, CommandError, CommandNode, CommandSender, ConsumedArgs,
        StringType,
    },
    command_wit::Number,
    commands::CommandHandler,
    common::Hand,
    data_components::DataComponent,
    events::{BlockBreakEvent, EventData, EventHandler, EventPriority},
    permission::{Permission, PermissionDefault, PermissionLevel},
    player::Player,
    scheduler::schedule_repeating_task,
    server::CommandSender as RunAs,
    text::TextComponent,
    world::{BlockFlags, BlockPos, World},
};

// `register_plugin!` emits wit glue whose exported names (e.g. `pumpkin:plugin/api@0.1.0#...`)
// are only meaningful for a wasm component: a native `cdylib` build feeds them to a link-time
// version script that `ld` cannot parse. Native builds only need the rlib (unit tests), so the
// registration is compiled for wasm targets only.
#[cfg(target_family = "wasm")]
use pumpkin_plugin_api::register_plugin;

use crate::plan::{BlockAt, Pos, Rng, Tool, Wear};

const PLUGIN: &str = "chainmine";
/// Blocks written per server tick: every write is a round trip, so chains are drained slowly.
const PER_TICK: usize = 8;
/// The item `/chainmine test` assumes when the sender names none.
const DEFAULT_TOOL: &str = "minecraft:diamond_pickaxe";
/// The enchantments that change what `/loot mine` yields, so the ones worth repeating.
const DROP_ENCHANTMENTS: [&str; 2] = ["minecraft:fortune", "minecraft:silk_touch"];
/// The enchantment that spares the tool.
const UNBREAKING: &str = "minecraft:unbreaking";
/// Writing a block: neighbours and listeners must hear about it. A function, because the flag
/// type's `|` is not a `const fn`.
fn write_flags() -> BlockFlags {
    BlockFlags::NOTIFY_NEIGHBORS | BlockFlags::NOTIFY_LISTENERS
}

/// The `damage` component of a stack (one protocol `VarInt`; missing means 0) and whether the
/// item is unbreakable. The stack type is named by the caller, so this stays a macro.
macro_rules! damage_of {
    ($stack:expr) => {{
        let components = $stack.get_components();
        let damage = components
            .iter()
            .find(|entry| matches!(entry.component, DataComponent::Damage))
            .map(|entry| entry.value.clone())
            .unwrap_or_default();
        let unbreakable = components
            .iter()
            .any(|entry| matches!(entry.component, DataComponent::Unbreakable));
        (damage, unbreakable)
    }};
}

fn fail(message: &str) -> CommandError {
    CommandError::CommandFailed(TextComponent::text(message))
}

fn block_pos(pos: Pos) -> BlockPos {
    BlockPos {
        x: pos.x,
        y: pos.y,
        z: pos.z,
    }
}

fn to_pos(pos: BlockPos) -> Pos {
    Pos::new(pos.x, pos.y, pos.z)
}

fn int_arg(args: &ConsumedArgs, name: &str) -> Result<i32, CommandError> {
    match args.get_value(name) {
        Arg::Num(Ok(Number::Int32(value))) => Ok(value),
        _ => Err(CommandError::InvalidConsumption(Some(name.into()))),
    }
}

fn string_arg(args: &ConsumedArgs, name: &str) -> Result<String, CommandError> {
    match args.get_value(name) {
        Arg::Simple(value) => Ok(value),
        _ => Err(CommandError::InvalidConsumption(Some(name.into()))),
    }
}

fn overworld(server: &Server) -> Option<World> {
    server.get_world_by_name("minecraft:overworld")
}

const OVERWORLD: &str = "minecraft:overworld";

/// True for the blocks that are liquid: they flow, so they never form a chain.
fn is_fluid(name: &str) -> bool {
    matches!(name, "water" | "lava" | "bubble_column") || name.ends_with("_fluid")
}

fn is_air(name: &str) -> bool {
    matches!(name, "air" | "cave_air" | "void_air")
}

/// Read one block the way [`plan`] wants it: ids, air, and whether it may be chained at all.
/// Reads are direct host calls, so planning a whole chain costs no more than a scan.
fn block_at(world: &World) -> impl Fn(Pos) -> Option<BlockAt> {
    move |pos: Pos| {
        let at = block_pos(pos);
        let block = world.get_block(at);
        let air = is_air(&block.name);
        // Fluids flow, block entities hold inventories, and a hardness below zero means the
        // block cannot be broken at all: none of them may join a chain, nor start one.
        let excluded = air
            || is_fluid(&block.name)
            || block.hardness < 0.0
            || world.get_block_entity(at).is_some();
        Some(BlockAt {
            id: block.id,
            state: world.get_block_state_id(at),
            air,
            excluded,
        })
    }
}

fn lock<T>(slot: &Mutex<T>) -> MutexGuard<'_, T> {
    slot.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// One chain in progress: the blocks left to break, in the order they must be broken.
struct Chain {
    /// The dimension the chain is in (the world the player mined in).
    dimension: String,
    /// Where the player mined: every drop lands there.
    origin: Pos,
    /// The block the chain is made of.
    block_id: u16,
    /// The held item as a `/loot` item argument; empty for a bare hand.
    tool: String,
    /// The player whose held item wears, and that item's key. `None` for the test command and
    /// for a bare hand: nothing wears.
    wear: Option<(String, String)>,
    queue: VecDeque<Pos>,
    broken: usize,
    done: bool,
}

impl Chain {
    /// Close the chain and report it. The test command has long returned by the time a chain
    /// ends, so the line goes to the server log.
    fn finish(&mut self) {
        if self.done {
            return;
        }
        self.done = true;
        tracing::info!("{PLUGIN} done extra={}", self.broken);
    }

    fn stop(&mut self, reason: &str) {
        if self.done {
            return;
        }
        tracing::info!(
            "{PLUGIN} stop reason={reason} broken={} origin={} {} {}",
            self.broken,
            self.origin.x,
            self.origin.y,
            self.origin.z
        );
        self.queue.clear();
        self.finish();
    }
}

/// The chains being drained, in the order they started.
#[derive(Default)]
struct Queues(Mutex<Vec<Chain>>);

impl Queues {
    fn push(&self, chain: Chain) {
        lock(&self.0).push(chain);
    }

    fn take(&self) -> Vec<Chain> {
        std::mem::take(&mut *lock(&self.0))
    }

    fn restore(&self, chains: Vec<Chain>) {
        *lock(&self.0) = chains;
    }
}

struct State {
    queues: Queues,
    /// The state id of air, read once. Writing is a round trip, so a chain must not pay for it
    /// per block and a task must not pay for it per tick.
    air: Mutex<Option<u16>>,
    tick: Mutex<u64>,
}

impl State {
    fn new() -> Self {
        Self {
            queues: Queues::default(),
            air: Mutex::new(None),
            tick: Mutex::new(0),
        }
    }

    /// A fresh, seeded generator for one tick's wear rolls.
    fn rng(&self) -> Rng {
        let mut tick = lock(&self.tick);
        *tick = tick.wrapping_add(1);
        Rng::new(0x5DEE_CE66_D000_0001 ^ *tick)
    }

    /// The air state id, read once and kept: writing is a round trip, so a chain must not pay
    /// for it per block and a task must not pay for it per tick.
    fn air(&self, world: &World) -> Option<u16> {
        let mut cached = self.air.lock().ok()?;
        if let Some(state) = *cached {
            return Some(state);
        }
        // Any empty place high in the overworld works; the name says whether it really is air.
        let mut state = None;
        for y in [200, 260, 300, 150] {
            let at = block_pos(Pos::new(0, y, 0));
            if is_air(&world.get_block(at).name) {
                state = Some(world.get_block_state_id(at));
                break;
            }
        }
        let state = state?;
        *cached = Some(state);
        Some(state)
    }
}

/// The `/loot` item argument for the held item, and the item's key. `None` for a bare hand.
fn held(player: &Player) -> Option<(String, String)> {
    let stack = player.get_item_in_hand(Hand::Right)?;
    let key = stack.get_registry_key();
    let mut enchantments: Vec<(&str, u32)> = Vec::new();
    for id in DROP_ENCHANTMENTS {
        if let Some(level) = stack.get_custom_enchantment_level(id)
            && level > 0
        {
            enchantments.push((id, level));
        }
    }
    Some((plan::tool_argument(&key, &enchantments), key))
}

/// Charge the player for one block. The stack is a copy, so the new damage is written back.
/// Wear starts from the item's damage as it is now: Pumpkin wears the tool for the block the
/// player broke after this plugin planned the chain, and other things may wear it too.
/// Returns false when the chain must stop: the hand is empty, the item was swapped for another,
/// or the next point of damage would be the last point of durability the tool has.
fn wear_tool(player: &Player, key: &str, rng: &mut Rng) -> bool {
    let Some(stack) = player.get_item_in_hand(Hand::Right) else {
        return false;
    };
    if stack.get_registry_key() != key {
        return false;
    }
    let (damage, unbreakable) = damage_of!(stack);
    let mut tool = Tool::new(key.to_string(), &damage, unbreakable);
    let unbreaking = stack
        .get_custom_enchantment_level(UNBREAKING)
        .unwrap_or(0);
    match plan::wear(&mut tool, unbreaking, rng) {
        Wear::Applied(_) => {
            stack.set_component(DataComponent::Damage, &tool.damage_bytes());
            player.set_item_in_hand(Hand::Right, Some(stack));
            true
        }
        Wear::Skipped | Wear::NotDamageable => true,
        Wear::OutOfDurability => false,
    }
}

/// `/loot mine` in the chain's dimension (the console's own world is the overworld), with the
/// item and its enchantments as `<tool>`; no tool argument for a bare hand.
fn loot_command(dimension: &str, origin: Pos, target: Pos, tool: &str) -> String {
    let tool = if tool.is_empty() { String::new() } else { format!(" {tool}") };
    format!(
        "execute in {dimension} run loot spawn {}.5 {}.5 {}.5 mine {} {} {}{tool}",
        origin.x, origin.y, origin.z, target.x, target.y, target.z
    )
}

/// Break one block the way the player would: loot where they mined, then air. The console runs
/// `/loot` because a player sender would need operator rights for it.
fn break_block(server: &Server, world: &World, chain: &Chain, air: u16, target: Pos) -> bool {
    let at = block_pos(target);
    // Someone else got to it, or it was never what the plan thought it was.
    if world.get_block_id(at) != chain.block_id {
        return false;
    }
    let command = loot_command(&chain.dimension, chain.origin, target, &chain.tool);
    server.execute_command(&command, RunAs::Console);
    world.set_block_state(at, air, write_flags());
    true
}

/// Break up to [`PER_TICK`] blocks of every chain in progress.
fn drain(server: &Server, state: &State) {
    let mut chains = state.queues.take();
    if chains.is_empty() {
        return;
    }
    let mut rng = state.rng();
    let mut budget = PER_TICK;
    for chain in &mut chains {
        if chain.done {
            continue;
        }
        let Some(world) = server.get_world_by_name(&chain.dimension) else {
            chain.stop("world-gone");
            continue;
        };
        let air = state.air(&world);
        let mut stopped = None;
        while budget > 0 {
            let Some(target) = chain.queue.front().copied() else {
                break;
            };
            if world.get_block_id(block_pos(target)) != chain.block_id {
                chain.queue.pop_front();
                continue;
            }
            if let Some((name, key)) = &chain.wear {
                let Some(player) = server.get_player_by_name(name) else {
                    stopped = Some("player-gone");
                    break;
                };
                if !wear_tool(&player, key, &mut rng) {
                    stopped = Some("tool-spent-or-swapped");
                    break;
                }
            }
            let Some(air) = air else {
                // Nothing to write with; try again on the next tick.
                break;
            };
            if break_block(server, &world, chain, air, target) {
                chain.broken += 1;
            }
            chain.queue.pop_front();
            budget -= 1;
        }
        if let Some(reason) = stopped {
            chain.stop(reason);
        }
        if chain.queue.is_empty() {
            chain.finish();
        }
    }
    chains.retain(|chain| !chain.done);
    state.queues.restore(chains);
}

struct BreakHandler {
    state: Arc<State>,
}

impl EventHandler<BlockBreakEvent> for BreakHandler {
    fn handle(
        &self,
        _server: Server,
        event: EventData<BlockBreakEvent>,
    ) -> EventData<BlockBreakEvent> {
        // `should_drop` is Pumpkin's harvest check: false in creative and with the wrong tool,
        // so a chain started on such a break would hand out drops the player cannot get.
        if event.cancelled || !event.should_drop {
            return event;
        }
        let Some(player) = event.player.as_ref() else {
            return event;
        };
        if !player.as_entity().is_sneaking() {
            return event;
        }
        // A bare hand chains too (logs, dirt): no tool argument and nothing to wear.
        let (tool, wear) = match held(player) {
            Some((tool, key)) => (tool, Some((player.get_name(), key))),
            None => (String::new(), None),
        };
        // The world the player is mining in, not always the overworld.
        let world = player.as_entity().get_world();
        let dimension = world.get_dimension();
        let origin = to_pos(event.block_pos);
        let block_id = world.get_block_id(block_pos(origin));
        let air = self.state.air(&world);
        let queue = match plan::plan(origin, block_at(&world)) {
            Ok(queue) => queue,
            Err(skip) => {
                tracing::debug!(
                    "{PLUGIN} skip reason={} origin={} {} {}",
                    skip.as_str(),
                    origin.x,
                    origin.y,
                    origin.z
                );
                return event;
            }
        };
        if queue.is_empty() || air.is_none() {
            return event;
        }
        tracing::debug!(
            "{PLUGIN} start origin={} {} {} extra={}",
            origin.x,
            origin.y,
            origin.z,
            queue.len()
        );
        self.state.queues.push(Chain {
            dimension,
            origin,
            // Pumpkin has not removed the origin yet while this handler runs.
            block_id,
            tool,
            wear,
            queue: VecDeque::from(queue),
            broken: 0,
            done: false,
        });
        event
    }
}

/// `/chainmine test <x> <y> <z> <tool>`: the same run as a sneaking survival break, with no
/// player — no sneaking check, no harvest check, no tool wear — and it breaks the origin
/// itself first, because no real break happened.
struct TestCommand {
    state: Arc<State>,
}

impl CommandHandler for TestCommand {
    fn handle(
        &self,
        sender: CommandSender,
        server: Server,
        args: ConsumedArgs,
    ) -> Result<i32, CommandError> {
        let origin = Pos::new(
            int_arg(&args, "x")?,
            int_arg(&args, "y")?,
            int_arg(&args, "z")?,
        );
        let tool = {
            let given = string_arg(&args, "tool")?;
            let given = given.trim();
            if given.is_empty() {
                DEFAULT_TOOL.to_string()
            } else {
                given.to_string()
            }
        };
        let world = overworld(&server).ok_or_else(|| fail("chainmine: no overworld"))?;
        let air = self
            .state
            .air(&world)
            .ok_or_else(|| fail("chainmine: state is busy"))?;
        let at = block_pos(origin);
        let block_id = world.get_block_id(at);
        let name = world.get_block(at).name;
        let queue = match plan::plan(origin, block_at(&world)) {
            Ok(queue) => queue,
            Err(skip) => {
                let reason = skip.as_str();
                sender
                    .send_message(TextComponent::text(&format!("{PLUGIN} skip reason={reason}")));
                tracing::info!(
                    "{PLUGIN} skip reason={reason} origin={} {} {}",
                    origin.x,
                    origin.y,
                    origin.z
                );
                return Ok(0);
            }
        };
        sender.send_message(TextComponent::text(&format!(
            "{PLUGIN} test origin={name} extra={}",
            queue.len()
        )));
        tracing::info!(
            "{PLUGIN} test origin={name} extra={} at {} {} {}",
            queue.len(),
            origin.x,
            origin.y,
            origin.z
        );
        let chain = Chain {
            dimension: OVERWORLD.to_string(),
            origin,
            block_id,
            tool,
            wear: None,
            queue: VecDeque::from(queue),
            broken: 0,
            done: false,
        };
        // The break of the origin that a real player break would have been.
        break_block(&server, &world, &chain, air, origin);
        self.state.queues.push(chain);
        Ok(1)
    }
}

pub struct ChainMinePlugin {
    state: Arc<State>,
}

impl Plugin for ChainMinePlugin {
    fn new() -> Self {
        Self {
            state: Arc::new(State::new()),
        }
    }

    fn metadata(&self) -> PluginMetadata {
        PluginMetadata {
            name: PLUGIN.into(),
            version: env!("CARGO_PKG_VERSION").into(),
            authors: vec!["psb".into()],
            description: "Break a whole vein or trunk with a sneaking break.".into(),
            dependencies: vec![],
            permissions: vec![],
        }
    }

    fn on_load(&self, context: Context) -> pumpkin_plugin_api::Result<()> {
        context.register_event_handler::<BlockBreakEvent, _>(
            BreakHandler {
                state: Arc::clone(&self.state),
            },
            EventPriority::Normal,
            true,
        )?;
        context.register_permission(&Permission {
            node: format!("{PLUGIN}:test"),
            description: "Run the chain-mining test command".into(),
            default: PermissionDefault::Op(PermissionLevel::Two),
            children: Vec::new(),
        })?;
        let names = [PLUGIN.to_string()];
        let int_arg = |name: &str| CommandNode::argument(name, &ArgumentType::Integer((None, None)));
        let command = Command::new(&names, "Chain mining").then(CommandNode::literal("test").then(
            int_arg("x").then(int_arg("y").then(int_arg("z").then(
                CommandNode::argument("tool", &ArgumentType::String(StringType::Greedy))
                    .execute(TestCommand {
                        state: Arc::clone(&self.state),
                    }),
            ))),
        ));
        context.register_command(command, &format!("{PLUGIN}:test"));
        // Drain the chains a little every tick: each write is a round trip.
        let state = Arc::clone(&self.state);
        schedule_repeating_task(1, 1, move |server: Server| {
            drain(&server, &state);
        });
        Ok(())
    }
}

// The wit glue exports names such as `pumpkin:plugin/api@0.1.0#...`; those are only valid for
// the wasm component, and the native (test) build of the `cdylib` crate type would feed them to
// a link-time version script that cannot parse them. Native builds only need the library itself.
#[cfg(target_family = "wasm")]
register_plugin!(ChainMinePlugin);