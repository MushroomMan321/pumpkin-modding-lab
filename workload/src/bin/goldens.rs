//! Prints the `GOLDENS` table for `src/lib.rs`.

use psb_workload::Grid;

const CASES: &[(u32, u32, u64)] = &[
    (1, 0, 10),
    (37, 300, 200),
    (1_000, 10, 1_000),
    (10_000, 10, 1_000),
    (10_000, 100, 1_000),
    (50_000, 10, 200),
];

fn main() {
    println!("pub const GOLDENS: &[(u32, u32, u64, u64, u64)] = &[");
    for &(n, rate, ticks) in CASES {
        let mut g = Grid::new(n, rate);
        for _ in 0..ticks {
            g.step(|_| {});
        }
        println!(
            "    ({n}, {rate}, {ticks}, 0x{:016x}, {}),",
            g.energy_sum(),
            g.writes()
        );
    }
    println!("];");
}
