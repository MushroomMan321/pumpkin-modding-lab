//! Feasibility probe for a plugin-side machine layer (block entity data, container inventories,
//! ticking, menus) on Pumpkin's v0.1 plugin API. Scratch code: every subcommand answers one
//! question and prints a single `mfeas ...` line the test script matches.
//!
//! Console:
//!   mfeas be <x> <y> <z>                       block entity kind and container slots
//!   mfeas put <x> <y> <z> <slot> <count> <item> write a stack into a container slot
//!   mfeas mark <x> <y> <z> <value> / read ...   plugin data on a block entity
//!   mfeas chunkmark <cx> <cz> <value> / chunkread <cx> <cz>   plugin data on a chunk
//!   mfeas filewrite <text> / fileread           the plugin's data folder (WASI)
//!   mfeas crusher <x> <y> <z>                   register a machine on a barrel: 1 cobblestone in
//!                                               slot 0 becomes 1 gravel in slot 26 every 20 ticks
//!   mfeas machines                              list registered machines and their counters
//! Player:
//!   mfeas gui furnace|chest                     open a plugin menu; furnace animates its arrow
//!   mfeas syncid                                the window id the plugin believes is open
//! Events: chunk loads restore machines from chunk data; right-clicking a machine opens its menu
//! instead of the barrel; placing a barrel named "Crusher" registers it; menu clicks and container
//! click/close packets are logged with their window ids.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use pumpkin_plugin_api::{
    Context, Plugin, PluginMetadata, Server,
    block_entity::BlockEntityType,
    command::{Arg, ArgumentType, Command, CommandError, CommandNode, CommandSender, ConsumedArgs, StringType},
    command_wit::Number,
    commands::CommandHandler,
    common::{Hand, NbtTag, NbtTree},
    events::{
        BlockPlaceEvent, ChunkLoadEvent, EventData, EventHandler, EventPriority, InteractAction,
        InventoryClickEvent, InventoryOpenEvent, PacketReceivedEvent, PlayerInteractEvent,
    },
    gui::Gui,
    item_stack::ItemStack,
    java_packets::{CCloseContainer, ClientboundPacket, CSetContainerProperty},
    permission::{Permission, PermissionDefault, PermissionLevel},
    permissions::{FS_READ_DATA, FS_WRITE_DATA},
    player::Player,
    register_plugin,
    scheduler::schedule_repeating_task,
    text::TextComponent,
    world::{BlockPos, World},
    Screen,
};

const NS: &str = "mfeas";
const CLICK_PACKET: i32 = 18; // serverbound play CONTAINER_CLICK in 26.3
const CLOSE_PACKET: i32 = 19; // serverbound play CONTAINER_CLOSE

#[derive(Clone, Copy, Default, Debug)]
struct Crusher {
    progress: u32,
    crushed: u32,
}

static MACHINES: Mutex<BTreeMap<(i32, i32, i32), Crusher>> = Mutex::new(BTreeMap::new());
static TICKER: AtomicBool = AtomicBool::new(false);
/// Per player: the plugin's mirror of Pumpkin's screen sync id (0..=100, +1 per opened screen).
static SYNC: Mutex<BTreeMap<String, u8>> = Mutex::new(BTreeMap::new());
static DATA_DIR: Mutex<String> = Mutex::new(String::new());

fn say(sender: &CommandSender, line: String) {
    sender.send_message(TextComponent::text(&line));
}

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

fn pos(args: &ConsumedArgs) -> Result<BlockPos, CommandError> {
    Ok(BlockPos { x: int(args, "x")?, y: int(args, "y")?, z: int(args, "z")? })
}

fn fail(msg: &str) -> CommandError {
    CommandError::CommandFailed(TextComponent::text(msg))
}

fn overworld(server: &Server) -> Result<World, CommandError> {
    server.get_world_by_name("minecraft:overworld").ok_or_else(|| fail("no overworld"))
}

fn string_tree(value: &str) -> NbtTree {
    NbtTree { root: 0, tags: vec![NbtTag::StringTag(value.to_string())] }
}

fn tree_string(tree: &NbtTree) -> String {
    match tree.tags.get(tree.root as usize) {
        Some(NbtTag::StringTag(s)) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn kind(be: &BlockEntityType) -> &'static str {
    match be {
        BlockEntityType::BarrelBlockEntity(_) => "barrel",
        BlockEntityType::ChestBlockEntity(_) => "chest",
        BlockEntityType::FurnaceBlockEntity(_) => "furnace",
        BlockEntityType::HopperBlockEntity(_) => "hopper",
        BlockEntityType::DropperBlockEntity(_) => "dropper",
        _ => "other",
    }
}

fn container_of(be: &BlockEntityType) -> Option<pumpkin_plugin_api::block_entity::ContainerBlockEntity> {
    match be {
        BlockEntityType::BarrelBlockEntity(b) => Some(b.get_container()),
        BlockEntityType::ChestBlockEntity(b) => Some(b.get_container()),
        BlockEntityType::FurnaceBlockEntity(b) => Some(b.get_container()),
        BlockEntityType::HopperBlockEntity(b) => Some(b.get_container()),
        BlockEntityType::DropperBlockEntity(b) => Some(b.get_container()),
        _ => None,
    }
}

fn base_of(be: &BlockEntityType) -> Option<pumpkin_plugin_api::block_entity::BlockEntity> {
    container_of(be).map(|c| c.get_block_entity())
}

fn slots(container: &pumpkin_plugin_api::block_entity::ContainerBlockEntity) -> String {
    let mut out = Vec::new();
    for i in 0..container.get_size() {
        if let Some(stack) = container.get_stack(i) {
            out.push(format!("{i}:{}x{}", stack.get_registry_key().trim_start_matches("minecraft:"), stack.get_count()));
        }
    }
    if out.is_empty() { "empty".into() } else { out.join(",") }
}

// ---------------------------------------------------------------- console checks

struct Be;
impl CommandHandler for Be {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let p = pos(&args)?;
        let line = match overworld(&server)?.get_block_entity(p) {
            None => format!("mfeas be none at {} {} {}", p.x, p.y, p.z),
            Some(be) => match container_of(&be) {
                Some(c) => format!("mfeas be kind={} size={} slots={}", kind(&be), c.get_size(), slots(&c)),
                None => format!("mfeas be kind={} container=no", kind(&be)),
            },
        };
        say(&sender, line);
        Ok(1)
    }
}

struct Put;
impl CommandHandler for Put {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let p = pos(&args)?;
        let slot = int(&args, "slot")? as u32;
        let count = int(&args, "count")?;
        let item = text(&args, "item")?;
        let be = overworld(&server)?.get_block_entity(p).ok_or_else(|| fail("no block entity"))?;
        let c = container_of(&be).ok_or_else(|| fail("not a container"))?;
        let stack = if count > 0 { Some(ItemStack::new(&item, count as u8)) } else { None };
        c.set_stack(slot, stack);
        say(&sender, format!("mfeas put slot={slot} slots={}", slots(&c)));
        Ok(1)
    }
}

struct Mark;
impl CommandHandler for Mark {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let p = pos(&args)?;
        let value = text(&args, "value")?;
        let be = overworld(&server)?.get_block_entity(p).ok_or_else(|| fail("no block entity"))?;
        let base = base_of(&be).ok_or_else(|| fail("no base block entity"))?;
        base.set_custom_data(NS, "mark", &string_tree(&value));
        say(&sender, format!("mfeas mark set value={value} dirty={}", base.is_dirty()));
        Ok(1)
    }
}

struct ReadMark;
impl CommandHandler for ReadMark {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let p = pos(&args)?;
        let line = match overworld(&server)?.get_block_entity(p).as_ref().and_then(base_of) {
            None => "mfeas read none".to_string(),
            Some(base) => match base.get_custom_data(NS, "mark") {
                Some(t) => format!("mfeas read value={}", tree_string(&t)),
                None => "mfeas read value=<missing>".to_string(),
            },
        };
        say(&sender, line);
        Ok(1)
    }
}

struct ChunkMark;
impl CommandHandler for ChunkMark {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let (cx, cz) = (int(&args, "cx")?, int(&args, "cz")?);
        let value = text(&args, "value")?;
        let chunk = overworld(&server)?.get_chunk(cx, cz).ok_or_else(|| fail("chunk not loaded"))?;
        chunk.set_custom_data(NS, "mark", &string_tree(&value));
        say(&sender, format!("mfeas chunkmark set {cx} {cz} value={value}"));
        Ok(1)
    }
}

struct ChunkRead;
impl CommandHandler for ChunkRead {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let (cx, cz) = (int(&args, "cx")?, int(&args, "cz")?);
        let line = match overworld(&server)?.get_chunk(cx, cz) {
            None => "mfeas chunkread not-loaded".to_string(),
            Some(chunk) => match chunk.get_custom_data(NS, "mark") {
                Some(t) => format!("mfeas chunkread value={}", tree_string(&t)),
                None => "mfeas chunkread value=<missing>".to_string(),
            },
        };
        say(&sender, line);
        Ok(1)
    }
}

struct FileWrite;
impl CommandHandler for FileWrite {
    fn handle(&self, sender: CommandSender, _: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let body = text(&args, "text")?;
        let path = format!("{}/feas.txt", DATA_DIR.lock().map(|d| d.clone()).unwrap_or_default());
        let line = match std::fs::write(&path, &body) {
            Ok(()) => format!("mfeas filewrite ok path={path}"),
            Err(e) => format!("mfeas filewrite error={e} path={path}"),
        };
        say(&sender, line);
        Ok(1)
    }
}

struct FileRead;
impl CommandHandler for FileRead {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        let path = format!("{}/feas.txt", DATA_DIR.lock().map(|d| d.clone()).unwrap_or_default());
        let line = match std::fs::read_to_string(&path) {
            Ok(s) => format!("mfeas fileread value={s}"),
            Err(e) => format!("mfeas fileread error={e}"),
        };
        say(&sender, line);
        Ok(1)
    }
}

// ---------------------------------------------------------------- machines

fn chunk_of(p: (i32, i32, i32)) -> (i32, i32) {
    (p.0.div_euclid(16), p.2.div_euclid(16))
}

/// Machine positions in a chunk, stored on the chunk as "x,y,z;x,y,z".
fn save_chunk_list(world: &World, chunk: (i32, i32)) {
    let list: Vec<String> = MACHINES
        .lock()
        .map(|m| m.keys().filter(|k| chunk_of(**k) == chunk).map(|k| format!("{},{},{}", k.0, k.1, k.2)).collect())
        .unwrap_or_default();
    if let Some(c) = world.get_chunk(chunk.0, chunk.1) {
        c.set_custom_data(NS, "machines", &string_tree(&list.join(";")));
    }
}

/// Registers a machine. Its counter lives on the block entity and is read when the machine
/// first runs, so registering works even while its chunk is not loaded (startup).
fn register(p: (i32, i32, i32), source: &str) {
    if let Ok(mut m) = MACHINES.lock() {
        m.entry(p).or_default();
    }
    tracing::info!("mfeaslog machine registered at {} {} {} source={source}", p.0, p.1, p.2);
    start_ticker();
}

fn index_path() -> String {
    format!("{}/machines.txt", DATA_DIR.lock().map(|d| d.clone()).unwrap_or_default())
}

/// Every machine position, one "x,y,z" per line, in the plugin's data folder. Pumpkin never
/// fires ChunkLoadEvent, so this index is what brings machines back after a restart.
fn save_index() {
    let lines: Vec<String> = MACHINES
        .lock()
        .map(|m| m.keys().map(|k| format!("{},{},{}", k.0, k.1, k.2)).collect())
        .unwrap_or_default();
    if let Err(e) = std::fs::write(index_path(), lines.join("\n")) {
        tracing::warn!("mfeaslog index write failed: {e}");
    }
}

fn load_index() {
    let Ok(text) = std::fs::read_to_string(index_path()) else { return };
    for line in text.lines() {
        let v: Vec<i32> = line.split(',').filter_map(|n| n.trim().parse().ok()).collect();
        if let [x, y, z] = v[..] {
            register((x, y, z), "index");
        }
    }
}

fn start_ticker() {
    if TICKER.swap(true, Ordering::SeqCst) {
        return;
    }
    schedule_repeating_task(1, 1, |server: Server| {
        let Some(world) = server.get_world_by_name("minecraft:overworld") else { return };
        let keys: Vec<(i32, i32, i32)> = MACHINES.lock().map(|m| m.keys().copied().collect()).unwrap_or_default();
        for k in keys {
            let due = {
                let Ok(mut m) = MACHINES.lock() else { return };
                let Some(state) = m.get_mut(&k) else { continue };
                state.progress += 1;
                if state.progress < 20 {
                    continue;
                }
                state.progress = 0;
                true
            };
            if due {
                crush_once(&world, k);
            }
        }
    });
}

/// One crushing step: only touches the world when a step completes (every 20 ticks).
fn crush_once(world: &World, k: (i32, i32, i32)) {
    let Some(be) = world.get_block_entity(BlockPos { x: k.0, y: k.1, z: k.2 }) else { return };
    let Some(c) = container_of(&be) else { return };
    let Some(input) = c.get_stack(0) else { return };
    if !input.get_registry_key().ends_with("cobblestone") {
        return;
    }
    let out_slot = c.get_size() - 1;
    let out_count = match c.get_stack(out_slot) {
        None => 0,
        Some(s) if s.get_registry_key().ends_with("gravel") && s.get_count() < 64 => s.get_count(),
        Some(_) => return,
    };
    let left = input.get_count() - 1;
    c.set_stack(0, if left > 0 { Some(ItemStack::new("minecraft:cobblestone", left)) } else { None });
    c.set_stack(out_slot, Some(ItemStack::new("minecraft:gravel", out_count + 1)));
    let base = base_of(&be);
    let before: u32 = base
        .as_ref()
        .and_then(|b| b.get_custom_data(NS, "crushed"))
        .and_then(|t| tree_string(&t).parse().ok())
        .unwrap_or(0);
    let crushed = before + 1;
    if let Ok(mut m) = MACHINES.lock()
        && let Some(s) = m.get_mut(&k)
    {
        s.crushed = crushed;
    }
    if let Some(base) = base {
        base.set_custom_data(NS, "crushed", &string_tree(&crushed.to_string()));
    }
    tracing::info!("mfeaslog crushed at {} {} {} total={crushed}", k.0, k.1, k.2);
}

struct CrusherCmd;
impl CommandHandler for CrusherCmd {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let p = pos(&args)?;
        let world = overworld(&server)?;
        let be = world.get_block_entity(p).ok_or_else(|| fail("no block entity"))?;
        if !matches!(be, BlockEntityType::BarrelBlockEntity(_)) {
            return Err(fail("not a barrel"));
        }
        let k = (p.x, p.y, p.z);
        register(k, "command");
        save_index();
        save_chunk_list(&world, chunk_of(k));
        say(&sender, format!("mfeas crusher registered {} {} {}", p.x, p.y, p.z));
        Ok(1)
    }
}

struct Machines;
impl CommandHandler for Machines {
    fn handle(&self, sender: CommandSender, _: Server, _: ConsumedArgs) -> Result<i32, CommandError> {
        let list: Vec<String> = MACHINES
            .lock()
            .map(|m| m.iter().map(|(k, s)| format!("{},{},{}:crushed={}", k.0, k.1, k.2, s.crushed)).collect())
            .unwrap_or_default();
        say(&sender, format!("mfeas machines count={} {}", list.len(), list.join(" ")));
        Ok(1)
    }
}

struct OnChunkLoad;
impl EventHandler<ChunkLoadEvent> for OnChunkLoad {
    fn handle(&self, server: Server, event: EventData<ChunkLoadEvent>) -> EventData<ChunkLoadEvent> {
        let Some(chunk) = event.target_world.get_chunk(event.chunk_x, event.chunk_z) else {
            return event;
        };
        if let Some(list) = chunk.get_custom_data(NS, "machines") {
            let list = tree_string(&list);
            tracing::info!("mfeaslog chunk {} {} loaded with machines={list}", event.chunk_x, event.chunk_z);
            for entry in list.split(';').filter(|s| !s.is_empty()) {
                let v: Vec<i32> = entry.split(',').filter_map(|n| n.parse().ok()).collect();
                if let [x, y, z] = v[..] {
                    register((x, y, z), "chunk-load");
                }
            }
        }
        event
    }
}

// ---------------------------------------------------------------- menus

fn player_key(player: &Player) -> String {
    format!("{:?}", player.as_entity().get_uuid())
}

fn bump_sync(player: &Player) -> u8 {
    let key = player_key(player);
    let mut m = SYNC.lock().unwrap_or_else(|e| e.into_inner());
    let id = m.entry(key).or_insert(0);
    *id = *id % 100 + 1;
    *id
}

fn current_sync(player: &Player) -> u8 {
    SYNC.lock().map(|m| m.get(&player_key(player)).copied().unwrap_or(0)).unwrap_or(0)
}

fn open_menu(player: &Player, screen: Screen, title: &str, items: &[(u32, &str, u8)]) -> u8 {
    let gui = Gui::new(screen, TextComponent::text(title));
    for &(slot, item, count) in items {
        gui.set_item(slot, ItemStack::new(item, count));
    }
    gui.set_allow_grab_items(false);
    gui.set_allow_put_items(false);
    player.open_gui(gui);
    bump_sync(player)
}

/// Drives a furnace menu's flame and arrow with container-property packets for ~10 seconds.
/// Player handles can't move into a task, so each step looks the player up by name.
fn animate_furnace(name: String, window: u8) {
    fn send(server: &Server, name: &str, window: u8, property: i32, value: i32) {
        if let Some(java) = server.get_player_by_name(name).and_then(|p| p.as_java()) {
            java.send_packet(&ClientboundPacket::CSetContainerProperty(CSetContainerProperty {
                window_id: i32::from(window),
                property,
                value,
            }));
        }
    }
    let step = std::sync::Arc::new(Mutex::new(0i32));
    let id = std::sync::Arc::new(Mutex::new(0u32));
    let id_in = id.clone();
    let task = schedule_repeating_task(1, 2, move |server: Server| {
        let mut s = step.lock().unwrap_or_else(|e| e.into_inner());
        // 0 fuel left, 1 fuel total, 2 cook progress, 3 cook total (vanilla furnace properties).
        if *s == 0 {
            send(&server, &name, window, 1, 200);
            send(&server, &name, window, 3, 200);
        }
        *s += 10;
        let progress = *s % 200;
        send(&server, &name, window, 0, 200 - progress);
        send(&server, &name, window, 2, progress);
        if *s >= 1000 {
            let t = *id_in.lock().unwrap_or_else(|e| e.into_inner());
            pumpkin_plugin_api::scheduler::cancel_task(t);
        }
    });
    *id.lock().unwrap_or_else(|e| e.into_inner()) = task;
}

fn open_kind(player: &Player, which: &str) -> u8 {
    match which {
        "furnace" => {
            let w = open_menu(player, Screen::Furnace, "Crusher (furnace screen)",
                &[(0, "minecraft:cobblestone", 12), (1, "minecraft:coal", 3), (2, "minecraft:gravel", 5)]);
            animate_furnace(player.get_name(), w);
            w
        }
        _ => open_menu(player, Screen::Generic9x3, "Crusher (chest screen)",
            &[(10, "minecraft:cobblestone", 12), (13, "minecraft:iron_pickaxe", 1), (16, "minecraft:gravel", 5)]),
    }
}

/// The target player: the sender, or the `player` argument when run from the console.
fn target(sender: &CommandSender, server: &Server, args: &ConsumedArgs) -> Result<Player, CommandError> {
    match text(args, "player") {
        Ok(name) => server.get_player_by_name(&name).ok_or_else(|| fail("no such player")),
        Err(_) => sender.as_player().ok_or_else(|| fail("players only")),
    }
}

struct GuiCmd;
impl CommandHandler for GuiCmd {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let which = text(&args, "kind")?;
        let player = target(&sender, &server, &args)?;
        let window = open_kind(&player, &which);
        say(&sender, format!("mfeas gui opened kind={which} expected_window={window}"));
        tracing::info!("mfeaslog gui opened kind={which} expected_window={window}");
        Ok(1)
    }
}

/// Hands the player a machine item: a barrel with a custom name and plugin custom data.
struct GiveCmd;
impl CommandHandler for GiveCmd {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let player = target(&sender, &server, &args)?;
        let item = ItemStack::new("minecraft:barrel", 4);
        item.set_custom_name(Some(TextComponent::text("Crusher")));
        item.set_custom_data(NS, "machine", &string_tree("crusher"));
        player.set_item_in_hand(Hand::Right, Some(item));
        say(&sender, format!("mfeas give ok slot={}", player.get_selected_slot()));
        Ok(1)
    }
}

/// No close-screen call in the v0.1 API: send the raw close packet for the mirrored window.
/// The server's own screen state isn't told, so it still thinks the menu is open.
struct CloseCmd;
impl CommandHandler for CloseCmd {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let player = target(&sender, &server, &args)?;
        let window = current_sync(&player);
        let java = player.as_java().ok_or_else(|| fail("java players only"))?;
        java.send_packet(&ClientboundPacket::CCloseContainer(CCloseContainer { sync_id: i32::from(window) }));
        say(&sender, format!("mfeas close sent window={window}"));
        Ok(1)
    }
}

struct SyncCmd;
impl CommandHandler for SyncCmd {
    fn handle(&self, sender: CommandSender, server: Server, args: ConsumedArgs) -> Result<i32, CommandError> {
        let player = target(&sender, &server, &args)?;
        say(&sender, format!("mfeas syncid mirror={}", current_sync(&player)));
        Ok(1)
    }
}

struct OnOpen;
impl EventHandler<InventoryOpenEvent> for OnOpen {
    fn handle(&self, _: Server, event: EventData<InventoryOpenEvent>) -> EventData<InventoryOpenEvent> {
        if !event.cancelled {
            let id = bump_sync(&event.player);
            tracing::info!("mfeaslog vanilla screen opening expected_window={id}");
        }
        event
    }
}

fn read_varint(bytes: &[u8]) -> Option<i32> {
    let mut value = 0i32;
    for (i, b) in bytes.iter().take(5).enumerate() {
        value |= i32::from(b & 0x7f) << (7 * i);
        if b & 0x80 == 0 {
            return Some(value);
        }
    }
    None
}

struct OnPacket;
impl EventHandler<PacketReceivedEvent> for OnPacket {
    fn handle(&self, _: Server, event: EventData<PacketReceivedEvent>) -> EventData<PacketReceivedEvent> {
        if event.packet_id == CLICK_PACKET || event.packet_id == CLOSE_PACKET {
            let window = read_varint(&event.raw_payload);
            tracing::info!(
                "mfeaslog packet {} window={window:?} mirror={}",
                if event.packet_id == CLICK_PACKET { "click" } else { "close" },
                current_sync(&event.player)
            );
        }
        event
    }
}

struct OnClick;
impl EventHandler<InventoryClickEvent> for OnClick {
    fn handle(&self, _: Server, event: EventData<InventoryClickEvent>) -> EventData<InventoryClickEvent> {
        tracing::info!(
            "mfeaslog click window_type={:?} slot={} raw_slot={} type={:?} item={:?}",
            event.window_type,
            event.slot,
            event.raw_slot,
            event.click_type,
            event.clicked_item.as_ref().map(|s| s.get_registry_key())
        );
        event
    }
}

struct OnInteract;
impl EventHandler<PlayerInteractEvent> for OnInteract {
    fn handle(&self, _: Server, mut event: EventData<PlayerInteractEvent>) -> EventData<PlayerInteractEvent> {
        if !matches!(event.action, InteractAction::RightClickBlock) {
            return event;
        }
        let Some(p) = event.clicked_pos else { return event };
        let is_machine = MACHINES.lock().map(|m| m.contains_key(&(p.x, p.y, p.z))).unwrap_or(false);
        tracing::info!("mfeaslog interact block={} at {} {} {} machine={is_machine}", event.block, p.x, p.y, p.z);
        if is_machine {
            event.cancelled = true;
            let w = open_menu(&event.player, Screen::Furnace, "Crusher",
                &[(0, "minecraft:cobblestone", 1), (2, "minecraft:gravel", 1)]);
            animate_furnace(event.player.get_name(), w);
            tracing::info!("mfeaslog interact opened machine menu expected_window={w}");
        }
        event
    }
}

struct OnPlace;
impl EventHandler<BlockPlaceEvent> for OnPlace {
    fn handle(&self, _: Server, event: EventData<BlockPlaceEvent>) -> EventData<BlockPlaceEvent> {
        let held = event.player.get_item_in_hand(Hand::Right);
        let name = held.as_ref().and_then(|s| s.get_custom_name()).map(|t| t.get_text());
        let tagged = held.as_ref().and_then(|s| s.get_custom_data(NS, "machine")).map(|t| tree_string(&t));
        let components: Vec<String> = held
            .as_ref()
            .map(|s| s.get_components().iter().map(|c| format!("{:?}", c.component)).collect())
            .unwrap_or_default();
        tracing::info!(
            "mfeaslog place block={} at {} {} {} held={:?} custom_name={name:?} machine_tag={tagged:?} components={components:?}",
            event.block_placed,
            event.block_pos.x,
            event.block_pos.y,
            event.block_pos.z,
            held.as_ref().map(|s| s.get_registry_key())
        );
        let named = name.as_deref().is_some_and(|n| n.contains("Crusher"));
        if event.block_placed.ends_with("barrel") && (tagged.is_some() || named) {
            let k = (event.block_pos.x, event.block_pos.y, event.block_pos.z);
            // The block entity exists only after the place completes: register next tick.
            pumpkin_plugin_api::scheduler::schedule_delayed_task(1, move |server: Server| {
                register(k, "placed");
                save_index();
                if let Some(w) = server.get_world_by_name("minecraft:overworld") {
                    save_chunk_list(&w, chunk_of(k));
                }
            });
        }
        event
    }
}

// ---------------------------------------------------------------- plugin

struct Feas;

impl Plugin for Feas {
    fn new() -> Self {
        Feas
    }

    fn metadata(&self) -> PluginMetadata {
        PluginMetadata {
            name: "mfeas".into(),
            version: env!("CARGO_PKG_VERSION").into(),
            authors: vec!["pumpkin-server-bench".into()],
            description: "Machine-layer feasibility probe".into(),
            dependencies: vec![],
            permissions: vec![FS_READ_DATA.into(), FS_WRITE_DATA.into()],
        }
    }

    fn on_load(&self, context: Context) -> pumpkin_plugin_api::Result<()> {
        if let Ok(mut d) = DATA_DIR.lock() {
            *d = context.get_data_folder();
        }
        load_index();
        context.register_event_handler::<ChunkLoadEvent, _>(OnChunkLoad, EventPriority::Normal, true)?;
        context.register_event_handler::<InventoryOpenEvent, _>(OnOpen, EventPriority::Lowest, true)?;
        context.register_event_handler::<PacketReceivedEvent, _>(OnPacket, EventPriority::Lowest, true)?;
        context.register_event_handler::<InventoryClickEvent, _>(OnClick, EventPriority::Lowest, true)?;
        context.register_event_handler::<PlayerInteractEvent, _>(OnInteract, EventPriority::Normal, true)?;
        context.register_event_handler::<BlockPlaceEvent, _>(OnPlace, EventPriority::Lowest, true)?;
        context.register_permission(&Permission {
            node: "mfeas:command".into(),
            description: "Use /mfeas".into(),
            default: PermissionDefault::Op(PermissionLevel::Two),
            children: Vec::new(),
        })?;

        let i = |name: &str| CommandNode::argument(name, &ArgumentType::Integer((None, None)));
        let word = |name: &str| CommandNode::argument(name, &ArgumentType::String(StringType::SingleWord));
        let rest = |name: &str| CommandNode::argument(name, &ArgumentType::String(StringType::Greedy));
        let xyz = |leaf: CommandNode| i("x").then(i("y").then(i("z").then(leaf)));
        let command = Command::new(&["mfeas".to_string()], "Machine-layer feasibility probe")
            .then(CommandNode::literal("be").then(i("x").then(i("y").then(i("z").execute(Be)))))
            .then(CommandNode::literal("put").then(xyz(i("slot").then(i("count").then(rest("item").execute(Put))))))
            .then(CommandNode::literal("mark").then(xyz(word("value").execute(Mark))))
            .then(CommandNode::literal("read").then(i("x").then(i("y").then(i("z").execute(ReadMark)))))
            .then(CommandNode::literal("chunkmark").then(i("cx").then(i("cz").then(word("value").execute(ChunkMark)))))
            .then(CommandNode::literal("chunkread").then(i("cx").then(i("cz").execute(ChunkRead))))
            .then(CommandNode::literal("filewrite").then(rest("text").execute(FileWrite)))
            .then(CommandNode::literal("fileread").execute(FileRead))
            .then(CommandNode::literal("crusher").then(i("x").then(i("y").then(i("z").execute(CrusherCmd)))))
            .then(CommandNode::literal("machines").execute(Machines))
            .then(CommandNode::literal("gui").then(word("kind").execute(GuiCmd)))
            .then(CommandNode::literal("syncid").execute(SyncCmd))
            .then(CommandNode::literal("give").execute(GiveCmd))
            .then(CommandNode::literal("guifor").then(word("player").then(word("kind").execute(GuiCmd))))
            .then(CommandNode::literal("givefor").then(word("player").execute(GiveCmd)))
            .then(CommandNode::literal("closefor").then(word("player").execute(CloseCmd)))
            .then(CommandNode::literal("syncfor").then(word("player").execute(SyncCmd)));
        context.register_command(command, "mfeas:command");
        Ok(())
    }
}

register_plugin!(Feas);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varint_reads_window_ids() {
        assert_eq!(read_varint(&[0x05, 0x00]), Some(5));
        assert_eq!(read_varint(&[0x64]), Some(100));
        assert_eq!(read_varint(&[0x80, 0x01]), Some(128));
    }

    #[test]
    fn chunk_of_handles_negatives() {
        assert_eq!(chunk_of((0, 64, 0)), (0, 0));
        assert_eq!(chunk_of((-1, 64, -17)), (-1, -2));
    }
}
