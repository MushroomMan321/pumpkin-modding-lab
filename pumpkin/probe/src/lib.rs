//! Tick-time probe for Pumpkin. Records `duration-nanos` from Pumpkin's own
//! `ServerTickEndEvent`, which covers the tick from its start (including
//! `ServerTickStartEvent` handlers, where workload plugins do their work) to the
//! end of the world tick. Output matches the NeoForge probe so the harness
//! parses both the same way:
//! `probe ticks=<n> mean_us=.. p50_us=.. p95_us=.. p99_us=.. max_us=..`
//!
//! `/probe arm <ticks>` clears the samples and records exactly the next `<ticks>`
//! ticks, so idle ticks after the workload stops are never counted.

use std::sync::Mutex;
use std::sync::atomic::{AtomicI64, Ordering};

use pumpkin_plugin_api::{
    Context, Plugin, PluginMetadata, Server,
    command::{Arg, ArgumentType, Command, CommandError, CommandNode, CommandSender, ConsumedArgs},
    command_wit::Number,
    commands::CommandHandler,
    events::{EventData, EventHandler, EventPriority, ServerTickEndEvent},
    permission::{Permission, PermissionDefault},
    register_plugin,
    text::TextComponent,
};

static SAMPLES: Mutex<Vec<i64>> = Mutex::new(Vec::new());
/// Ticks still to record after `/probe arm`; negative means unlimited.
static REMAINING: AtomicI64 = AtomicI64::new(-1);

struct TickEnd;

impl EventHandler<ServerTickEndEvent> for TickEnd {
    fn handle(&self, _server: Server, event: EventData<ServerTickEndEvent>) -> EventData<ServerTickEndEvent> {
        let remaining = REMAINING.load(Ordering::Relaxed);
        if remaining == 0 {
            return event;
        }
        if remaining > 0 {
            REMAINING.store(remaining - 1, Ordering::Relaxed);
        }
        if let Ok(mut s) = SAMPLES.lock() {
            s.push(event.duration_nanos);
        }
        event
    }
}

struct Reset;

impl CommandHandler for Reset {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        SAMPLES.lock().map(|mut s| s.clear()).ok();
        REMAINING.store(-1, Ordering::Relaxed);
        sender.send_message(TextComponent::text("probe reset"));
        Ok(1)
    }
}

struct Arm;

impl CommandHandler for Arm {
    fn handle(&self, sender: CommandSender, _: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let Arg::Num(Ok(Number::Int32(ticks))) = args.get_value("ticks") else {
            return Err(CommandError::InvalidConsumption(Some("ticks".into())));
        };
        SAMPLES.lock().map(|mut s| s.clear()).ok();
        REMAINING.store(i64::from(ticks), Ordering::Relaxed);
        sender.send_message(TextComponent::text(&format!("probe armed ticks={ticks}")));
        Ok(1)
    }
}

struct Dump;

impl CommandHandler for Dump {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        let mut sorted = SAMPLES.lock().map(|s| s.clone()).unwrap_or_default();
        sorted.sort_unstable();
        sender.send_message(TextComponent::text(&summary(&sorted)));
        Ok(1)
    }
}

fn summary(sorted: &[i64]) -> String {
    let n = sorted.len();
    if n == 0 {
        return "probe ticks=0".into();
    }
    let pct = |p: f64| {
        let idx = ((p / 100.0 * n as f64).ceil() as usize).clamp(1, n) - 1;
        sorted[idx] as f64 / 1000.0
    };
    let mean = sorted.iter().map(|&v| v as f64).sum::<f64>() / n as f64 / 1000.0;
    format!(
        "probe ticks={n} mean_us={mean:.1} p50_us={:.1} p95_us={:.1} p99_us={:.1} max_us={:.1}",
        pct(50.0),
        pct(95.0),
        pct(99.0),
        sorted[n - 1] as f64 / 1000.0
    )
}

struct ProbePlugin;

impl Plugin for ProbePlugin {
    fn new() -> Self {
        ProbePlugin
    }

    fn metadata(&self) -> PluginMetadata {
        PluginMetadata {
            name: "psb-probe".into(),
            version: env!("CARGO_PKG_VERSION").into(),
            authors: vec!["pumpkin-server-bench".into()],
            description: "Records tick durations for the benchmark harness".into(),
            dependencies: vec![],
            permissions: vec![],
        }
    }

    fn on_load(&self, context: Context) -> pumpkin_plugin_api::Result<()> {
        context.register_event_handler::<ServerTickEndEvent, _>(TickEnd, EventPriority::Lowest, true)?;
        context.register_permission(&Permission {
            node: "psb-probe:probe".into(),
            description: "Use /probe".into(),
            default: PermissionDefault::Op(pumpkin_plugin_api::permission::PermissionLevel::Two),
            children: Vec::new(),
        })?;
        let names = ["probe".to_string()];
        let command = Command::new(&names, "Benchmark tick probe")
            .then(CommandNode::literal("reset").execute(Reset))
            .then(CommandNode::literal("arm").then(
                CommandNode::argument("ticks", &ArgumentType::Integer((Some(1), None))).execute(Arm),
            ))
            .then(CommandNode::literal("dump").execute(Dump));
        context.register_command(command, "psb-probe:probe");
        Ok(())
    }
}

register_plugin!(ProbePlugin);
