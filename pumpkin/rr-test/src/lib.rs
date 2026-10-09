//! Test plugin for Pumpkin PR #3887 (runtime block and item registries), bound straight to the
//! v0.2 WIT because pumpkin-plugin-api only targets v0.1. Every result goes to the log as a line
//! starting with `rrtlog`, so a console script can grep for it.

wit_bindgen::generate!({
    path: "wit",
    world: "plugin",
    generate_all,
});

use exports::pumpkin::plugin::metadata::{Guest as MetaGuest, PluginMetadata};
use pumpkin::plugin::block_registry as br;
use pumpkin::plugin::command::{Arg, ArgumentType, Command, CommandNode, StringType};
use pumpkin::plugin::common::BlockPos;
use pumpkin::plugin::item_registry as ir;
use pumpkin::plugin::logging::{log, Level};
use pumpkin::plugin::permission::{Permission, PermissionDefault};
use pumpkin::plugin::server::Server;

struct P;

fn say(msg: String) {
    log(Level::Info, &format!("rrtlog {msg}"));
}

fn prop(name: &str, value: &str) -> br::PropertyValue {
    br::PropertyValue { name: name.into(), value: value.into() }
}

fn enumeration(name: &str, values: &[&str]) -> br::BlockProperty {
    br::BlockProperty {
        name: name.into(),
        kind: br::PropertyKind::Enumeration(values.iter().map(|v| v.to_string()).collect()),
    }
}

fn boolean(name: &str) -> br::BlockProperty {
    br::BlockProperty { name: name.into(), kind: br::PropertyKind::Boolean }
}

fn definition(key: &str, properties: Vec<br::BlockProperty>, default: Vec<br::PropertyValue>,
              tags: &[&str]) -> br::BlockDefinition {
    br::BlockDefinition {
        key: key.into(),
        properties,
        default_state: default,
        hardness: 3.5,
        blast_resistance: 3.5,
        requires_correct_tool: true,
        sound_type: "stone".into(),
        luminance: 0,
        can_occlude: true,
        suffocating: true,
        replaceable: false,
        map_color: 11,
        collision_shape: br::BlockShape::FullCube,
        selection_shape: br::BlockShape::FullCube,
        connect_rules: vec![],
        drops: br::BlockDrops::SelfItem,
        tags: tags.iter().map(|t| t.to_string()).collect(),
    }
}

fn crusher() -> br::BlockDefinition {
    definition(
        "rrtest:crusher",
        vec![enumeration("facing", &["north", "south", "west", "east"]), boolean("lit")],
        vec![prop("facing", "north"), prop("lit", "false")],
        &["minecraft:mineable/pickaxe"],
    )
}

const FACING: [&str; 4] = ["north", "south", "west", "east"];
const HALF: [&str; 2] = ["top", "bottom"];
const SHAPE: [&str; 5] = ["straight", "inner_left", "inner_right", "outer_left", "outer_right"];

/// A block with oak_stairs' properties, declared in the order vanilla declares them.
fn stairs() -> br::BlockDefinition {
    definition(
        "rrtest:stairs",
        vec![
            enumeration("facing", &FACING),
            enumeration("half", &HALF),
            enumeration("shape", &SHAPE),
            boolean("waterlogged"),
        ],
        vec![prop("facing", "north"), prop("half", "bottom"), prop("shape", "straight"),
             prop("waterlogged", "false")],
        &[],
    )
}

fn compare_stairs() {
    let mut combos = vec![];
    for f in FACING {
        for h in HALF {
            for s in SHAPE {
                for w in ["true", "false"] {
                    combos.push(vec![prop("facing", f), prop("half", h), prop("shape", s),
                                     prop("waterlogged", w)]);
                }
            }
        }
    }
    let ours: Vec<Option<u32>> =
        combos.iter().map(|c| br::get_state_id("rrtest:stairs", c)).collect();
    let oak: Vec<Option<u32>> =
        combos.iter().map(|c| br::get_state_id("minecraft:oak_stairs", c)).collect();
    if ours.iter().chain(oak.iter()).any(Option::is_none) {
        say(format!("stairs FAIL missing ids ours_none={} oak_none={}",
                    ours.iter().filter(|x| x.is_none()).count(),
                    oak.iter().filter(|x| x.is_none()).count()));
        return;
    }
    let ours: Vec<u32> = ours.into_iter().flatten().collect();
    let oak: Vec<u32> = oak.into_iter().flatten().collect();
    let (ob, kb) = (*ours.iter().min().unwrap(), *oak.iter().min().unwrap());
    let mismatches: Vec<usize> =
        (0..combos.len()).filter(|&i| ours[i] - ob != oak[i] - kb).collect();
    let default_ours = br::get_state_id("rrtest:stairs", &[]).map(|x| x - ob);
    let default_oak = br::get_state_id("minecraft:oak_stairs", &[]).map(|x| x - kb);
    say(format!(
        "stairs combos={} mismatches={} first_mismatch={:?} default_offset ours={:?} oak={:?}",
        combos.len(), mismatches.len(), mismatches.first(), default_ours, default_oak));
}

fn overworld(server: &Server) -> Option<pumpkin::plugin::world::World> {
    server.get_world_by_name("minecraft:overworld")
}

fn describe(server: &Server, x: i32, y: i32, z: i32) -> String {
    match overworld(server) {
        Some(w) => {
            let s = w.get_block_state(BlockPos { x, y, z });
            format!("{x} {y} {z} id={} block={} name={}", s.id, s.block_id, s.block_name)
        }
        None => "no overworld".into(),
    }
}

impl Guest for P {
    fn init_plugin() {}

    fn on_load(context: Context) -> Result<(), String> {
        let server = context.get_server();
        // Optional: read some positions before anything is registered, to load their chunks
        // in the window before the registry freezes (Megalith's review point 4).
        let folder = context.get_data_folder();
        if let Ok(text) = std::fs::read_to_string(format!("{folder}/preread")) {
            for line in text.lines() {
                let v: Vec<i32> = line.split_whitespace().filter_map(|x| x.parse().ok()).collect();
                if v.len() == 3 {
                    say(format!("preread {}", describe(&server, v[0], v[1], v[2])));
                }
            }
        }

        say(format!("vanilla blocks={} states={} items={}", br::get_vanilla_block_count(),
                    br::get_vanilla_state_count(), ir::get_vanilla_items().len()));
        let id = br::register_block(&crusher());
        say(format!("register crusher -> {id:?}"));
        say(format!("register crusher again -> {:?}", br::register_block(&crusher())));
        let mut changed = crusher();
        changed.luminance = 7;
        say(format!("register crusher changed -> {:?}", br::register_block(&changed)));
        say(format!("register stairs -> {:?}", br::register_block(&stairs())));
        let item = ir::register_item(&ir::ItemDefinition {
            key: "rrtest:crusher".into(),
            components: vec![],
            max_stack_size: None,
        });
        say(format!("register item -> {item:?}"));
        say(format!("link -> {:?}", br::set_block_item("rrtest:crusher", "rrtest:crusher")));
        say(format!("block tag -> {:?}",
                    br::register_block_tag("minecraft:climbable", &["rrtest:stairs".into()])));
        for e in br::get_registered_blocks() {
            say(format!("block {} id={} base_state={} states={} item={:?}", e.key, e.id,
                        e.base_state_id, e.state_count, e.item_id));
        }
        compare_stairs();

        context
            .register_permission(&Permission {
                node: "rrtest:command".into(),
                description: "rrt".into(),
                default: PermissionDefault::Allow,
                children: vec![],
            })
            .map_err(|e| format!("permission: {e}"))?;
        let cmd = Command::new(&["rrt".into()], "registry test");
        let node = CommandNode::argument("args", &ArgumentType::String(StringType::Greedy));
        node.execute_with_handler_id(1);
        cmd.then(node);
        context.register_command(cmd, "rrtest:command");
        Ok(())
    }

    fn on_unload(_context: Context) -> Result<(), String> {
        Ok(())
    }

    fn handle_event(_event_id: u32, _server: Server, event: Event) -> Event {
        event
    }

    fn handle_command(_id: u32, _sender: CommandSender, server: Server, args: ConsumedArgs)
        -> Result<i32, CommandError> {
        let line = match args.get_value("args") {
            Arg::Simple(s) | Arg::Msg(s) => s,
            _ => String::new(),
        };
        let w: Vec<&str> = line.split_whitespace().collect();
        let num = |i: usize| w.get(i).and_then(|x| x.parse::<i32>().ok()).unwrap_or(0);
        match w.first().copied() {
            Some("get") => say(format!("get {}", describe(&server, num(1), num(2), num(3)))),
            Some("set") => {
                let key = w.get(4).copied().unwrap_or("rrtest:crusher");
                let props: Vec<br::PropertyValue> = w[5.min(w.len())..]
                    .iter()
                    .filter_map(|p| p.split_once('=').map(|(a, b)| prop(a, b)))
                    .collect();
                match (br::get_state_id(key, &props), overworld(&server)) {
                    (Some(id), Some(world)) => {
                        world.set_block_state(BlockPos { x: num(1), y: num(2), z: num(3) }, id as u16,
                                              pumpkin::plugin::world::BlockFlags::NOTIFY_LISTENERS);
                        say(format!("set {} {} {} {key} state={id}", num(1), num(2), num(3)));
                    }
                    other => say(format!("set failed {other:?}")),
                }
            }
            Some("late") => {
                let mut d = crusher();
                d.key = "rrtest:late".into();
                say(format!("late register -> {:?}", br::register_block(&d)));
                say(format!("late item -> {:?}", ir::register_item(&ir::ItemDefinition {
                    key: "rrtest:late".into(), components: vec![], max_stack_size: None })));
            }
            Some("ids") => {
                for e in br::get_registered_blocks() {
                    say(format!("block {} id={} base_state={} states={}", e.key, e.id,
                                e.base_state_id, e.state_count));
                }
                for e in ir::get_registered_items() {
                    say(format!("item {} id={}", e.key, e.id));
                }
            }
            _ => say(format!("unknown {line}")),
        }
        Ok(1)
    }

    fn handle_command_suggestion(_id: u32, _s: CommandSender, _server: Server,
                                 _r: SuggestionRequest) -> CommandSuggestions {
        CommandSuggestions { start: 0, length: 0, values: vec![] }
    }

    fn handle_task(_id: u32, _server: Server) {}

    fn handle_ipc_message(_s: pumpkin::plugin::ipc::PluginId, m: pumpkin::plugin::ipc::IpcMessage)
        -> Result<pumpkin::plugin::ipc::IpcMessage, String> {
        Ok(m)
    }

    fn handle_ai_goal_can_start(_g: u32, _s: Server, _e: pumpkin::plugin::world::Entity) -> bool {
        false
    }
    fn handle_ai_goal_should_continue(_g: u32, _s: Server, _e: pumpkin::plugin::world::Entity) -> bool {
        false
    }
    fn handle_ai_goal_start(_g: u32, _s: Server, _e: pumpkin::plugin::world::Entity) {}
    fn handle_ai_goal_tick(_g: u32, _s: Server, _e: pumpkin::plugin::world::Entity) {}
    fn handle_ai_goal_stop(_g: u32, _s: Server, _e: pumpkin::plugin::world::Entity) {}
    fn handle_generate_phase(_g: u32, _p: pumpkin::plugin::world::GenerationPhase,
                             _c: pumpkin::plugin::world::ChunkBuffer) {}
}

impl MetaGuest for P {
    fn get_metadata() -> PluginMetadata {
        PluginMetadata {
            name: "rrtest".into(),
            version: "0.1.0".into(),
            authors: vec!["pumpkin-modding-lab".into()],
            description: "Runtime registry test".into(),
            dependencies: vec![],
            permissions: vec!["registry.blocks".into(), "registry.items".into(),
                              "fs.read.data".into()],
        }
    }
}

use exports::pumpkin::plugin::gametest_callbacks as gtc;

impl gtc::Guest for P {
    fn invoke_void(_c: gtc::VoidCallbackId) {}
    fn invoke_test(_c: gtc::TestCallbackId, _t: gtc::Test) {}
    async fn invoke_async_test(_c: gtc::AsyncTestCallbackId, _t: gtc::Test) {}
    fn invoke_block_predicate(_c: gtc::BlockPredicateCallbackId, _p: gtc::BlockPermutation) -> bool {
        false
    }
    fn invoke_entity_predicate(_c: gtc::EntityPredicateCallbackId, _e: &gtc::Entity) -> bool {
        false
    }
}

export!(P);
