//! Plugin skeleton for loop tasks. It compiles against the Pumpkin plugin API (v0.1) and shows,
//! in working form, the calls most plugins need: a command with integer and greedy-string
//! arguments, a permission, a blocking event handler, the scheduler, world reads and writes,
//! running a server command, the player's held item and its data components, and logging.
//! Replace the bodies; keep what you use. Notes on each call: qwen/notes/pumpkin-plugin-api.md.

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
    scheduler::schedule_delayed_task,
    server::CommandSender as RunAs,
    text::TextComponent,
    world::{BlockFlags, BlockPos},
};

const PLUGIN: &str = "skeleton";

fn int(args: &ConsumedArgs, name: &str) -> Result<i32, CommandError> {
    match args.get_value(name) {
        Arg::Num(Ok(Number::Int32(v))) => Ok(v),
        _ => Err(CommandError::InvalidConsumption(Some(name.into()))),
    }
}

fn text(args: &ConsumedArgs, name: &str) -> Result<String, CommandError> {
    match args.get_value(name) {
        Arg::Simple(s) => Ok(s),
        _ => Err(CommandError::InvalidConsumption(Some(name.into()))),
    }
}

/// `/skeleton probe <x> <y> <z> <rest of line>`: reads a block, runs a server command, and logs
/// from a task scheduled one tick later.
struct Probe;

impl CommandHandler for Probe {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let pos = BlockPos { x: int(&args, "x")?, y: int(&args, "y")?, z: int(&args, "z")? };
        let rest = text(&args, "rest")?;
        let world = server
            .get_world_by_name("minecraft:overworld")
            .ok_or_else(|| CommandError::CommandFailed(TextComponent::text("no overworld")))?;

        // Reads are direct host calls and cheap.
        let block = world.get_block(pos);
        let state = world.get_block_state_id(pos);
        let has_block_entity = world.get_block_entity(pos).is_some();
        sender.send_message(TextComponent::text(&format!(
            "{PLUGIN} block={} id={} state={state} hardness={} block_entity={has_block_entity} rest={rest}",
            block.name, block.id, block.hardness
        )));

        // Writes and commands are round trips (about 0.33 ms each): spread many over ticks.
        server.execute_command(&format!("say {PLUGIN} probed {} {} {}", pos.x, pos.y, pos.z), RunAs::Console);
        schedule_delayed_task(1, move |server: Server| {
            if let Some(world) = server.get_world_by_name("minecraft:overworld") {
                let _ = world.get_block_id(pos);
            }
            tracing::info!("{PLUGIN} task ran one tick later");
        });
        Ok(1)
    }
}

/// Blocking handler: runs before Pumpkin finishes the break, can read everything about it.
struct OnBreak;

impl EventHandler<BlockBreakEvent> for OnBreak {
    fn handle(&self, _server: Server, event: EventData<BlockBreakEvent>) -> EventData<BlockBreakEvent> {
        let Some(player) = event.player.as_ref() else {
            return event;
        };
        if event.cancelled || !event.should_drop || !player.as_entity().is_sneaking() {
            return event;
        }
        // The held item is a copy: change it, then put it back with set_item_in_hand.
        // Hand::Right is the main hand, Hand::Left the off hand.
        if let Some(stack) = player.get_item_in_hand(Hand::Right) {
            let unbreaking = stack.get_custom_enchantment_level("minecraft:unbreaking").unwrap_or(0);
            let damage = stack
                .get_components()
                .into_iter()
                .find(|c| c.component == DataComponent::Damage)
                .map(|c| c.value);
            tracing::info!("{PLUGIN} break {} at {:?} unbreaking={unbreaking} damage_bytes={damage:?}",
                event.block, event.block_pos);
            stack.set_component(DataComponent::Damage, &[0]); // a VarInt: 0
            player.set_item_in_hand(Hand::Right, Some(stack));
        }
        event
    }
}

struct Skeleton;

impl Plugin for Skeleton {
    fn new() -> Self {
        Skeleton
    }

    fn metadata(&self) -> PluginMetadata {
        PluginMetadata {
            name: PLUGIN.into(),
            version: env!("CARGO_PKG_VERSION").into(),
            authors: vec!["pumpkin-server-bench".into()],
            description: "Plugin skeleton".into(),
            dependencies: vec![],
            permissions: vec![],
        }
    }

    fn on_load(&self, context: Context) -> pumpkin_plugin_api::Result<()> {
        context.register_event_handler::<BlockBreakEvent, _>(OnBreak, EventPriority::Normal, true)?;
        context.register_permission(&Permission {
            node: format!("{PLUGIN}:command"),
            description: "Use the plugin's command".into(),
            default: PermissionDefault::Op(PermissionLevel::Two),
            children: Vec::new(),
        })?;
        let names = [PLUGIN.to_string()];
        let int_arg = |name: &str| CommandNode::argument(name, &ArgumentType::Integer((None, None)));
        let command = Command::new(&names, "Plugin skeleton").then(CommandNode::literal("probe").then(
            int_arg("x").then(int_arg("y").then(int_arg("z").then(
                CommandNode::argument("rest", &ArgumentType::String(StringType::Greedy)).execute(Probe),
            ))),
        ));
        context.register_command(command, &format!("{PLUGIN}:command"));
        let _ = BlockFlags::NOTIFY_NEIGHBORS | BlockFlags::NOTIFY_LISTENERS; // flags for set_block_state
        Ok(())
    }
}

register_plugin!(Skeleton);
