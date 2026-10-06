//! Pure chain-planning logic for the chain-mining plugin.
//!
//! No plugin API calls live in this module: the planner is handed a `Fn(Pos) ->
//! Option<BlockAt>` closure instead of a world, so it compiles and runs natively under
//! `cargo test` (the plugin API's host calls panic outside the server).

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashSet};

/// Blocks further than this from the origin on any axis are never chained.
pub const REACH: i32 = 16;
/// Maximum number of blocks in one chain, including the origin.
pub const MAX_CHAIN: usize = 64;

/// A block position, plain integers so this module does not depend on the plugin API.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct Pos {
    pub x: i32,
    pub y: i32,
    pub z: i32,
}

impl Pos {
    #[must_use]
    pub const fn new(x: i32, y: i32, z: i32) -> Self {
        Self { x, y, z }
    }

    /// Squared distance to `other`.
    #[must_use]
    pub fn distance2(&self, other: Pos) -> i64 {
        let dx = i64::from(self.x - other.x);
        let dy = i64::from(self.y - other.y);
        let dz = i64::from(self.z - other.z);
        dx * dx + dy * dy + dz * dz
    }

    /// Whether `self` is within `REACH` blocks of `origin` on every axis.
    #[must_use]
    pub fn within_reach(&self, origin: Pos) -> bool {
        (self.x - origin.x).abs() <= REACH
            && (self.y - origin.y).abs() <= REACH
            && (self.z - origin.z).abs() <= REACH
    }

    /// The 26 neighbours: faces, edges and corners.
    pub fn neighbours(&self) -> impl Iterator<Item = Pos> {
        let s = *self;
        (-1..=1).flat_map(move |dx| {
            (-1..=1).flat_map(move |dy| (-1..=1).map(move |dz| Pos::new(s.x + dx, s.y + dy, s.z + dz)))
        })
    }
}

/// What the planner is told about one position.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct BlockAt {
    /// Same kind means the same block id; block state properties are ignored.
    pub id: u16,
    /// The state id that is there now; only the state-ignoring test looks at it.
    pub state: u16,
    pub air: bool,
    /// Fluid, block entity or unbreakable: never chained, and no chain starts from it.
    pub excluded: bool,
}

/// Why a chain does not start at all.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Skip {
    Air,
    Excluded,
}

impl Skip {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Skip::Air => "air",
            Skip::Excluded => "excluded",
        }
    }
}

/// Heap key: nearest to the origin first, ties by lowest y, then x, then z. The key is unique
/// per position, so the order never depends on insertion order.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Candidate {
    distance2: i64,
    y: i32,
    x: i32,
    z: i32,
    pos: Pos,
}

impl Candidate {
    fn new(pos: Pos, origin: Pos) -> Self {
        Self {
            distance2: pos.distance2(origin),
            y: pos.y,
            x: pos.x,
            z: pos.z,
            pos,
        }
    }
}

/// Plans the extra blocks to break after `origin`, in the order they must be broken.
///
/// `lookup` answers what is at a position; `None` (an unloaded chunk, say) counts as air. The
/// set grows from the origin, always taking the frontier block nearest to it, which keeps the
/// set connected and deterministic. At most [`MAX_CHAIN`] blocks including the origin; the
/// returned vector holds only the extras, so at most 63 of them.
pub fn plan<F>(origin: Pos, lookup: F) -> Result<Vec<Pos>, Skip>
where
    F: Fn(Pos) -> Option<BlockAt>,
{
    let Some(origin_block) = lookup(origin) else {
        return Err(Skip::Air);
    };
    if origin_block.air {
        return Err(Skip::Air);
    }
    if origin_block.excluded {
        return Err(Skip::Excluded);
    }

    let mut chosen = vec![origin];
    let mut seen: HashSet<Pos> = HashSet::new();
    seen.insert(origin);
    // `Reverse` on `Candidate`'s derived order turns the heap into a nearest-first queue.
    let mut frontier: BinaryHeap<Reverse<Candidate>> = BinaryHeap::new();

    while chosen.len() < MAX_CHAIN {
        let last = chosen[chosen.len() - 1];
        for next in last.neighbours() {
            if !seen.insert(next) || !next.within_reach(origin) {
                continue;
            }
            if let Some(block) = lookup(next)
                && block.id == origin_block.id
                && !block.air
                && !block.excluded
            {
                frontier.push(Reverse(Candidate::new(next, origin)));
            }
        }
        let Some(Reverse(candidate)) = frontier.pop() else {
            break;
        };
        chosen.push(candidate.pos);
    }

    Ok(chosen.split_off(1))
}

/// Decodes a protocol `VarInt` (up to five bytes); anything malformed is `None`.
#[must_use]
pub fn read_varint(bytes: &[u8]) -> Option<u32> {
    let mut value: u32 = 0;
    let mut shift = 0;
    for (index, &byte) in bytes.iter().enumerate() {
        if index > 4 || (index == 4 && byte & 0xF0 != 0) {
            return None;
        }
        value |= u32::from(byte & 0x7F) << shift;
        if byte & 0x80 == 0 {
            return Some(value);
        }
        shift += 7;
    }
    None
}

/// Encodes a protocol `VarInt`.
#[must_use]
pub fn write_varint(mut value: u32) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(5);
    loop {
        let byte = (value & 0x7F) as u8;
        value >>= 7;
        if value == 0 {
            bytes.push(byte);
            return bytes;
        }
        bytes.push(byte | 0x80);
    }
}

/// Maximum damage (durability) of the items the plugin may wear down.
#[must_use]
pub fn max_damage(item_key: &str) -> Option<u32> {
    let name = item_key.strip_prefix("minecraft:").unwrap_or(item_key);
    if name == "shears" {
        return Some(238);
    }
    let (material, kind) = name.rsplit_once('_')?;
    match kind {
        "pickaxe" | "axe" | "shovel" | "hoe" | "sword" => Some(match material {
            "wooden" => 59,
            "stone" => 131,
            "copper" => 190,
            "iron" => 250,
            "golden" => 32,
            "diamond" => 1561,
            "netherite" => 2031,
            _ => return None,
        }),
        _ => None,
    }
}

/// The item the player held when the chain started, as far as the plugin cares about it.
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Tool {
    pub key: String,
    pub damage: u32,
    pub unbreakable: bool,
}

impl Tool {
    /// Builds a tool from its key, the bytes of its `damage` data component (a `VarInt`; missing
    /// means 0) and whether it carries the `unbreakable` component.
    #[must_use]
    pub fn new(key: String, damage: &[u8], unbreakable: bool) -> Self {
        Self {
            key,
            damage: read_varint(damage).unwrap_or(0),
            unbreakable,
        }
    }

    #[must_use]
    pub fn max_damage(&self) -> Option<u32> {
        max_damage(&self.key)
    }

    #[must_use]
    pub fn is_damageable(&self) -> bool {
        !self.unbreakable && self.max_damage().is_some()
    }

    /// The bytes to write back to the `damage` data component.
    #[must_use]
    pub fn damage_bytes(&self) -> Vec<u8> {
        write_varint(self.damage)
    }
}

/// What one extra block did to the tool.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Wear {
    /// Not a damageable item, or unbreakable: nothing to do.
    NotDamageable,
    /// Unbreaking skipped this block's damage.
    Skipped,
    /// Damage was applied; holds the new damage value.
    Applied(u32),
    /// The next damage would leave fewer than 1 point of durability: stop the chain.
    OutOfDurability,
}

/// Charges one block of wear to `tool`, honouring Unbreaking and never breaking the tool.
pub fn wear(tool: &mut Tool, unbreaking: u32, rng: &mut Rng) -> Wear {
    if !tool.is_damageable() {
        return Wear::NotDamageable;
    }
    if rng.skip_damage(unbreaking) {
        return Wear::Skipped;
    }
    let max = tool.max_damage().unwrap_or(0);
    // `max - damage - 1 < 1` would break the tool: stop before charging it.
    if max.saturating_sub(tool.damage).saturating_sub(1) < 1 {
        return Wear::OutOfDurability;
    }
    tool.damage += 1;
    Wear::Applied(tool.damage)
}

/// Small seeded PRNG (xorshift64*), so wear needs no dependency and is reproducible.
pub struct Rng {
    state: u64,
}

impl Rng {
    #[must_use]
    pub const fn new(seed: u64) -> Self {
        Self {
            state: if seed == 0 { 0x9E37_79B9_7F4A_7C15 } else { seed },
        }
    }

    #[must_use]
    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// A value in `[0, 1)`. The shift leaves 53 bits, so the cast is exact.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn next_unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Unbreaking level `n` skips the damage with probability `n / (n + 1)`.
    #[must_use]
    pub fn skip_damage(&mut self, unbreaking: u32) -> bool {
        if unbreaking == 0 {
            return false;
        }
        let level = f64::from(unbreaking);
        self.next_unit() < level / (level + 1.0)
    }
}

/// The held item as a `/loot` item argument: the item key plus the enchantments that change
/// drops. `enchantments` holds `(id, level)` pairs already filtered to those.
#[must_use]
pub fn tool_argument(key: &str, enchantments: &[(&str, u32)]) -> String {
    let picked: Vec<(&str, u32)> = enchantments
        .iter()
        .copied()
        .filter(|&(_, level)| level > 0)
        .collect();
    if picked.is_empty() {
        return key.to_string();
    }
    let inner = picked
        .iter()
        .map(|(id, level)| format!("\"{id}\":{level}"))
        .collect::<Vec<_>>()
        .join(",");
    format!("{key}[enchantments={{{inner}}}]")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(x: i32, y: i32, z: i32) -> Pos {
        Pos::new(x, y, z)
    }

    const AIR: BlockAt = BlockAt {
        id: 0,
        state: 0,
        air: true,
        excluded: false,
    };

    /// A world from a list of positions: everything listed is `id`, everything else is air.
    fn solid(positions: &[(i32, i32, i32)], id: u16) -> impl Fn(Pos) -> Option<BlockAt> {
        let set: HashSet<Pos> = positions.iter().map(|&(x, y, z)| p(x, y, z)).collect();
        move |pos| {
            if set.contains(&pos) {
                Some(BlockAt {
                    id,
                    state: id,
                    air: false,
                    excluded: false,
                })
            } else {
                Some(AIR)
            }
        }
    }

    #[test]
    fn same_block_with_different_state_ids_chains() {
        // An oak_log with axis=x next to one with axis=y: same block, different states.
        let lookup = |pos: Pos| match pos {
            q if q == p(0, 0, 0) => Some(BlockAt {
                id: 700,
                state: 701,
                air: false,
                excluded: false,
            }),
            q if q == p(1, 0, 0) => Some(BlockAt {
                id: 700,
                state: 704,
                air: false,
                excluded: false,
            }),
            q if q == p(1, 1, 0) => Some(BlockAt {
                id: 700,
                state: 900,
                air: false,
                excluded: false,
            }),
            _ => Some(AIR),
        };
        assert_eq!(plan(p(0, 0, 0), lookup).expect("chain"), vec![p(1, 0, 0), p(1, 1, 0)]);
    }

    #[test]
    fn edge_and_corner_neighbours_chain() {
        let vein = [(0, 0, 0), (1, 1, 1), (2, 2, 2), (-1, 1, 0)];
        let extra = plan(p(0, 0, 0), solid(&vein, 50)).expect("chain");
        assert_eq!(extra.len(), 3);
        assert!(extra.contains(&p(1, 1, 1)));
        assert!(extra.contains(&p(2, 2, 2)));
        assert!(extra.contains(&p(-1, 1, 0)));
    }

    #[test]
    fn isolated_same_block_and_other_kinds_stay() {
        let extra = plan(p(0, 0, 0), solid(&[(0, 0, 0), (1, 0, 0), (5, 0, 0)], 50)).expect("chain");
        assert_eq!(extra, vec![p(1, 0, 0)]);

        // A different block id touching the origin is not the same kind.
        let lookup = |pos: Pos| match pos {
            q if q == p(0, 0, 0) => Some(BlockAt {
                id: 50,
                state: 50,
                air: false,
                excluded: false,
            }),
            q if q == p(1, 0, 0) => Some(BlockAt {
                id: 51,
                state: 51,
                air: false,
                excluded: false,
            }),
            _ => Some(AIR),
        };
        assert_eq!(plan(p(0, 0, 0), lookup).expect("chain"), Vec::new());
    }

    #[test]
    fn air_and_excluded_origins_are_skipped() {
        assert_eq!(plan(p(0, 0, 0), |_| Some(AIR)), Err(Skip::Air));
        assert_eq!(Skip::Air.as_str(), "air");
        // Nothing to answer for an unloaded chunk: treated as air.
        assert_eq!(plan(p(0, 0, 0), |_| None), Err(Skip::Air));

        let barrel = |_| Some(BlockAt {
            id: 9,
            state: 9,
            air: false,
            excluded: true,
        });
        assert_eq!(plan(p(0, 0, 0), barrel), Err(Skip::Excluded));
        assert_eq!(Skip::Excluded.as_str(), "excluded");
    }

    #[test]
    fn excluded_neighbours_break_the_connection() {
        // A block entity in the middle of a vein: what lies beyond it is not reached.
        let lookup = |pos: Pos| match pos {
            q if q == p(0, 0, 0) || q == p(2, 0, 0) => Some(BlockAt {
                id: 5,
                state: 5,
                air: false,
                excluded: false,
            }),
            q if q == p(1, 0, 0) => Some(BlockAt {
                id: 5,
                state: 5,
                air: false,
                excluded: true,
            }),
            _ => Some(AIR),
        };
        assert_eq!(plan(p(0, 0, 0), lookup).expect("chain"), Vec::new());
    }

    #[test]
    fn cap_is_64_blocks_including_the_origin_nearest_first() {
        // A 5x5x5 cube around the origin: 57 of its 125 blocks sit at squared distance <= 5 (the
        // origin + 56), the next 24 sit at distance 6; the cap takes 63 extras = those 56 + 7.
        let cube: Vec<(i32, i32, i32)> = (-2..=2)
            .flat_map(|x| (-2..=2).flat_map(move |y| (-2..=2).map(move |z| (x, y, z))))
            .collect();
        let extra = plan(p(0, 0, 0), solid(&cube, 30)).expect("chain");
        assert_eq!(extra.len(), MAX_CHAIN - 1);
        assert_eq!(extra.iter().filter(|pos| pos.distance2(p(0, 0, 0)) <= 5).count(), 56);
        assert_eq!(extra.iter().filter(|pos| pos.distance2(p(0, 0, 0)) == 6).count(), 7);
        assert!(extra.iter().all(|pos| pos.distance2(p(0, 0, 0)) <= 6));

        let left: Vec<Pos> = cube
            .iter()
            .map(|&(x, y, z)| p(x, y, z))
            .filter(|pos| *pos != p(0, 0, 0) && !extra.contains(pos))
            .collect();
        // What is left is never nearer than what was taken.
        assert!(left.iter().all(|pos| pos.distance2(p(0, 0, 0)) >= 6));
    }

    #[test]
    fn order_is_nearest_first_then_lowest_y_then_x_then_z() {
        let vein = [
            (0, 0, 0),
            (0, 1, 0),
            (0, 0, 1),
            (1, 0, 0),
            (-1, 0, 0),
            (0, -1, 0),
            (0, 0, -1),
        ];
        let extra = plan(p(0, 0, 0), solid(&vein, 30)).expect("chain");
        assert_eq!(
            extra,
            vec![p(0, -1, 0), p(-1, 0, 0), p(0, 0, -1), p(0, 0, 1), p(1, 0, 0), p(0, 1, 0)]
        );
    }

    #[test]
    fn reach_is_16_blocks_on_every_axis() {
        let line: Vec<(i32, i32, i32)> = (0..20).map(|x| (x, 0, 0)).collect();
        let extra = plan(p(0, 0, 0), solid(&line, 40)).expect("chain");
        assert_eq!(extra.len(), REACH as usize);
        assert_eq!(extra.last(), Some(&p(16, 0, 0)));

        // A column: the limit is per axis, not a sphere.
        let column: Vec<(i32, i32, i32)> = (0..20).map(|y| (0, y, 0)).collect();
        let extra = plan(p(0, 0, 0), solid(&column, 40)).expect("chain");
        assert_eq!(extra.len(), REACH as usize);
        assert_eq!(extra.last(), Some(&p(0, 16, 0)));
    }

    #[test]
    fn varint_round_trips_protocol_values() {
        for value in [0u32, 1, 127, 128, 255, 300, 1560, 2030, 2_097_151, u32::MAX / 2] {
            let bytes = write_varint(value);
            assert_eq!(read_varint(&bytes), Some(value), "value {value}");
        }
        assert_eq!(write_varint(0), vec![0]);
        assert_eq!(write_varint(300), vec![0xAC, 0x02]);
        assert_eq!(read_varint(&[0x80]), None, "truncated varint");
        assert_eq!(read_varint(&[]), None);
    }

    #[test]
    fn tool_durability_table() {
        for (key, expected) in [
            ("minecraft:wooden_pickaxe", 59u32),
            ("minecraft:stone_axe", 131),
            ("minecraft:copper_shovel", 190),
            ("minecraft:iron_hoe", 250),
            ("minecraft:golden_sword", 32),
            ("minecraft:diamond_pickaxe", 1561),
            ("minecraft:netherite_pickaxe", 2031),
            ("minecraft:shears", 238),
        ] {
            assert_eq!(max_damage(key), Some(expected), "{key}");
        }
        for key in [
            "minecraft:stick",
            "minecraft:diamond",
            "minecraft:diamond_helmet",
            "stone_something",
        ] {
            assert_eq!(max_damage(key), None, "{key}");
        }
    }

    #[test]
    fn wear_charges_one_per_block_and_never_breaks_the_tool() {
        let mut rng = Rng::new(7);
        let mut tool = Tool::new("minecraft:diamond_pickaxe".into(), &[], false);
        for expected in 1..=10u32 {
            assert_eq!(wear(&mut tool, 0, &mut rng), Wear::Applied(expected));
        }
        assert_eq!(read_varint(&tool.damage_bytes()), Some(10));

        // One point of durability left (32 - 31 - 1 = 0 < 1): charging the next block is stopped.
        let mut nearly = Tool::new("minecraft:golden_shovel".into(), &write_varint(31), false);
        assert_eq!(wear(&mut nearly, 0, &mut rng), Wear::OutOfDurability);
        assert_eq!(wear(&mut nearly, 0, &mut rng), Wear::OutOfDurability);
        assert_eq!(nearly.damage, 31, "the tool is never broken");

        // One more point to spare (32 - 30 - 1 = 1): the last hit still lands, and the next stops.
        let mut last = Tool::new("minecraft:golden_shovel".into(), &write_varint(30), false);
        assert_eq!(wear(&mut last, 0, &mut rng), Wear::Applied(31));
        assert_eq!(wear(&mut last, 0, &mut rng), Wear::OutOfDurability);
        assert_eq!(last.damage, 31);
    }

    #[test]
    fn unbreakable_and_undamageable_items_take_no_wear() {
        let mut rng = Rng::new(3);
        let mut unbreakable = Tool::new("minecraft:diamond_pickaxe".into(), &[], true);
        for _ in 0..10 {
            assert_eq!(wear(&mut unbreakable, 0, &mut rng), Wear::NotDamageable);
        }
        assert_eq!(unbreakable.damage, 0);

        let mut stick = Tool::new("minecraft:stick".into(), &[], false);
        assert_eq!(wear(&mut stick, 0, &mut rng), Wear::NotDamageable);
    }

    #[test]
    fn unbreaking_skips_its_share_of_the_damage() {
        let mut rng = Rng::new(12345);
        let mut tool = Tool::new("minecraft:diamond_pickaxe".into(), &[], false);
        let mut applied = 0;
        for _ in 0..3000 {
            if matches!(wear(&mut tool, 3, &mut rng), Wear::Applied(_)) {
                applied += 1;
            }
        }
        // Unbreaking III skips 3 of 4; allow generous statistical slack.
        assert!((600..900).contains(&applied), "applied {applied} of 3000");

        let mut rng = Rng::new(99);
        for _ in 0..50 {
            assert!(!rng.skip_damage(0));
        }
    }

    #[test]
    fn seeded_prng_is_stable_and_never_zero() {
        let take = |seed| (0..8).map(|_| Rng::new(seed).next_u64()).collect::<Vec<u64>>();
        let mut a = Rng::new(42);
        let mut b = Rng::new(42);
        let mut c = Rng::new(43);
        let a: Vec<u64> = (0..8).map(|_| a.next_u64()).collect();
        let b: Vec<u64> = (0..8).map(|_| b.next_u64()).collect();
        let c: Vec<u64> = (0..8).map(|_| c.next_u64()).collect();
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert_ne!(take(0), take(1));
        assert!(a.iter().all(|&value| value != 0));
    }

    #[test]
    fn tool_argument_carries_only_enchantments_that_change_drops() {
        assert_eq!(
            tool_argument("minecraft:diamond_pickaxe", &[("minecraft:fortune", 0)]),
            "minecraft:diamond_pickaxe"
        );
        assert_eq!(
            tool_argument("minecraft:diamond_pickaxe", &[("minecraft:fortune", 3)]),
            "minecraft:diamond_pickaxe[enchantments={\"minecraft:fortune\":3}]"
        );
        assert_eq!(
            tool_argument(
                "minecraft:diamond_shovel",
                &[("minecraft:silk_touch", 1), ("minecraft:fortune", 2)]
            ),
            "minecraft:diamond_shovel[enchantments={\"minecraft:silk_touch\":1,\"minecraft:fortune\":2}]"
        );
    }
}