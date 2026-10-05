//! Pumpkin variant `wasm-batched` of the `energy-grid` benchmark (see `SPEC.md`).
//!
//! All machine state lives inside this plugin; the workload is advanced once per
//! server tick from a blocking `ServerTickStartEvent` handler, which does one batch
//! of work: a single `Grid::step` followed by every world write the step produced.
//!
//! The `/psb` command drives a run:
//!   /psb setup <n> <rate_permille>  place the grid and reset to tick 0
//!   /psb start                      one workload tick per server tick, forever
//!   /psb stop                       stop running
//!   /psb run <ticks>                exactly <ticks> workload ticks, then stop
//!   /psb status                     report tick, energy checksum and write count

use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use psb_workload::{Display, Grid, X0, Y_DISPLAY, Y_MACHINE, Z0};
use pumpkin_plugin_api::{
    Block, Context, Plugin, PluginMetadata, Server,
    command::{Arg, ArgumentType, Command, CommandError, CommandNode, CommandSender, ConsumedArgs},
    command_wit::Number,
    commands::CommandHandler,
    events::{EventData, EventHandler, EventPriority, ServerTickStartEvent},
    register_plugin,
    text::TextComponent,
    world::{BlockFlags, BlockPos, World},
};

/// Highest accepted `n`.
const MAX_N: i32 = 1_000_000;
/// Highest accepted `rate_permille`.
const MAX_RATE: i32 = 1_000;

const STONE: &str = "minecraft:stone";
const LIME_WOOL: &str = "minecraft:lime_wool";
const WHITE_WOOL: &str = "minecraft:white_wool";

/// Everything the workload needs. Block state ids are resolved once per setup so a
/// tick never touches the block registry.
struct Bench {
    grid: Grid,
    rate_permille: i32,
    /// Ticks still to run after `/psb run <ticks>`; negative means unlimited.
    remaining: i64,
    lime: u16,
    white: u16,
}

/// `Some` after `/psb setup`. Event handlers run on the server thread, and so do
/// command handlers, but the lock keeps that assumption from mattering.
static BENCH: Mutex<Option<Bench>> = Mutex::new(None);
/// Mirrors `Bench.remaining > 0` so idle ticks never take the lock.
static RUNNING: AtomicBool = AtomicBool::new(false);

/// The default state id of a registered block, or a command error naming it.
fn state_id(name: &str) -> Result<u16, CommandError> {
    Block::from_name(name)
        .map(|block| block.default_state_id)
        .ok_or_else(|| CommandError::CommandFailed(TextComponent::text(&format!("unknown block {name}"))))
}

/// The overworld, by dimension name or by falling back to the first loaded world.
fn overworld(server: &Server) -> Option<World> {
    for name in ["minecraft:overworld", "overworld", "world"] {
        if let Some(world) = server.get_world_by_name(name) {
            return Some(world);
        }
    }
    let mut worlds = server.get_all_worlds();
    let index = worlds
        .iter()
        .position(|world| world.get_dimension() == "minecraft:overworld")
        .unwrap_or(0);
    if index < worlds.len() {
        Some(worlds.swap_remove(index))
    } else {
        None
    }
}

/// The block position of a display block, from the grid side length.
fn display_pos(side: u32, machine: u32) -> BlockPos {
    BlockPos {
        x: X0 + (machine % side) as i32,
        y: Y_DISPLAY,
        z: Z0 + (machine / side) as i32,
    }
}

/// Sets a display block, matching NeoForge's `Block.UPDATE_ALL`.
fn set_display(world: &World, side: u32, machine: u32, state: u16) {
    world.set_block_state(
        display_pos(side, machine),
        state,
        BlockFlags::NOTIFY_NEIGHBORS | BlockFlags::NOTIFY_LISTENERS,
    );
}

fn status_line(bench: &Bench) -> String {
    format!(
        "psb status tick={} n={} rate={} energy_sum={:016x} writes={} variant=wasm-batched running={}",
        bench.grid.tick(),
        bench.grid.n(),
        bench.rate_permille,
        bench.grid.energy_sum(),
        bench.grid.writes(),
        bench.remaining != 0,
    )
}

fn int_arg(args: &ConsumedArgs, name: &str) -> Result<i32, CommandError> {
    match args.get_value(name) {
        Arg::Num(Ok(Number::Int32(value))) => Ok(value),
        _ => Err(CommandError::InvalidConsumption(Some(name.into()))),
    }
}

struct Setup;

impl CommandHandler for Setup {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let n = int_arg(&args, "n")?;
        let rate = int_arg(&args, "rate_permille")?;
        if !(1..=MAX_N).contains(&n) {
            return Err(CommandError::CommandFailed(TextComponent::text(&format!(
                "n must be in 1..={MAX_N}"
            ))));
        }
        if !(0..=MAX_RATE).contains(&rate) {
            return Err(CommandError::CommandFailed(TextComponent::text(&format!(
                "rate_permille must be in 0..={MAX_RATE}"
            ))));
        }

        let stone = state_id(STONE)?;
        let lime = state_id(LIME_WOOL)?;
        let white = state_id(WHITE_WOOL)?;
        let world = overworld(&server).ok_or_else(|| {
            CommandError::CommandFailed(TextComponent::text("psb: no overworld (force-load the grid's chunks?)"))
        })?;

        let grid = Grid::new(n as u32, rate as u32);
        // Setup places without neighbour updates, so it never cascades.
        let flags = BlockFlags::NOTIFY_LISTENERS;
        for machine in 0..grid.n() {
            let (x, z) = grid.position(machine);
            for (y, state) in [(Y_MACHINE, stone), (Y_DISPLAY, white)] {
                world.set_block_state(
                    BlockPos {
                        x,
                        y,
                        z,
                    },
                    state,
                    flags,
                );
            }
        }

        let side = grid.side();
        let line = format!("psb setup n={n} rate={rate} side={side} variant=wasm-batched");
        let mut slot = BENCH
            .lock()
            .map_err(|error| CommandError::CommandFailed(TextComponent::text(&error.to_string())))?;
        *slot = Some(Bench {
            grid,
            rate_permille: rate,
            remaining: 0,
            lime,
            white,
        });
        RUNNING.store(false, Ordering::Relaxed);

        sender.send_message(TextComponent::text(&line));
        Ok(1)
    }
}

/// `/psb start` and `/psb stop` share everything but the flag they set.
struct Toggle {
    start: bool,
}

impl CommandHandler for Toggle {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        let mut slot = BENCH
            .lock()
            .map_err(|error| CommandError::CommandFailed(TextComponent::text(&error.to_string())))?;
        let bench = slot
            .as_mut()
            .ok_or_else(|| CommandError::CommandFailed(TextComponent::text("psb not set up")))?;
        bench.remaining = if self.start { -1 } else { 0 };
        RUNNING.store(self.start, Ordering::Relaxed);
        sender.send_message(TextComponent::text(if self.start {
            "psb started"
        } else {
            "psb stopped"
        }));
        Ok(1)
    }
}

struct Run;

impl CommandHandler for Run {
    fn handle(&self, sender: CommandSender, _: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let ticks = int_arg(&args, "ticks")?;
        if ticks < 1 {
            return Err(CommandError::CommandFailed(TextComponent::text(
                "ticks must be >= 1",
            )));
        }
        let mut slot = BENCH
            .lock()
            .map_err(|error| CommandError::CommandFailed(TextComponent::text(&error.to_string())))?;
        let bench = slot
            .as_mut()
            .ok_or_else(|| CommandError::CommandFailed(TextComponent::text("psb not set up")))?;
        bench.remaining = i64::from(ticks);
        RUNNING.store(true, Ordering::Relaxed);
        sender.send_message(TextComponent::text("psb running"));
        Ok(1)
    }
}

struct Status;

impl CommandHandler for Status {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        let line = match BENCH.lock() {
            Ok(slot) => match slot.as_ref() {
                Some(bench) => status_line(bench),
                None => "psb status tick=0 n=0 rate=0 energy_sum=0000000000000000 writes=0 variant=wasm-batched running=false".into(),
            },
            Err(error) => return Err(CommandError::CommandFailed(TextComponent::text(&error.to_string()))),
        };
        sender.send_message(TextComponent::text(&line));
        Ok(1)
    }
}

struct TickStart;

impl EventHandler<ServerTickStartEvent> for TickStart {
    fn handle(&self, server: Server, event: EventData<ServerTickStartEvent>) -> EventData<ServerTickStartEvent> {
        if RUNNING.load(Ordering::Relaxed)
            && let Err(error) = self.tick(&server)
        {
            RUNNING.store(false, Ordering::Relaxed);
            server.broadcast(&error);
        }
        event
    }
}

impl TickStart {
    fn tick(&self, server: &Server) -> Result<(), String> {
        let mut slot = BENCH.lock().map_err(|error| error.to_string())?;
        let Some(bench) = slot.as_mut() else {
            return Ok(());
        };
        if bench.remaining == 0 {
            RUNNING.store(false, Ordering::Relaxed);
            return Ok(());
        }

        let world = overworld(server).ok_or_else(|| "psb: overworld is gone".to_string())?;
        let (lime, white, side) = (bench.lime, bench.white, bench.grid.side());
        let grid = &mut bench.grid;
        // One batch: the energy step plus every write it asks for, in order.
        grid.step(|write| {
            let state = match write.block {
                Display::Lime => lime,
                Display::White => white,
            };
            set_display(&world, side, write.machine, state);
        });

        if bench.remaining > 0 {
            bench.remaining -= 1;
            if bench.remaining == 0 {
                RUNNING.store(false, Ordering::Relaxed);
            }
        }
        Ok(())
    }
}

pub struct BatchedPlugin;

impl Plugin for BatchedPlugin {
    fn new() -> Self {
        BatchedPlugin
    }

    fn metadata(&self) -> PluginMetadata {
        PluginMetadata {
            name: "psb-wasm-batched".into(),
            version: env!("CARGO_PKG_VERSION").into(),
            authors: vec!["pumpkin-server-bench".into()],
            description: "energy-grid workload, one batch of work per server tick".into(),
            dependencies: vec![],
            permissions: vec![],
        }
    }

    fn on_load(&self, context: Context) -> pumpkin_plugin_api::Result<()> {
        context.register_event_handler::<ServerTickStartEvent, _>(TickStart, EventPriority::Normal, true)?;

        let command = Command::new(&["psb".to_string()], "energy-grid benchmark workload")
            .then(CommandNode::literal("setup").then(
                CommandNode::argument("n", &ArgumentType::Integer((Some(1), Some(MAX_N)))).then(
                    CommandNode::argument("rate_permille", &ArgumentType::Integer((Some(0), Some(MAX_RATE))))
                        .execute(Setup),
                ),
            ))
            .then(CommandNode::literal("start").execute(Toggle { start: true }))
            .then(CommandNode::literal("stop").execute(Toggle { start: false }))
            .then(CommandNode::literal("run").then(
                CommandNode::argument("ticks", &ArgumentType::Integer((Some(1), None))).execute(Run),
            ))
            .then(CommandNode::literal("status").execute(Status));
        context.register_command(command, "psb-wasm-batched:psb");
        Ok(())
    }
}

register_plugin!(BatchedPlugin);