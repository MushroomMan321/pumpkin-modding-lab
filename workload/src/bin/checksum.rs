//! `checksum <n> <rate_permille> <ticks>` prints the expected `/psb status` values.
//! The harness uses it to verify every run.

use psb_workload::Grid;

fn main() {
    let args: Vec<u64> = std::env::args()
        .skip(1)
        .map(|a| a.parse().expect("arguments must be integers"))
        .collect();
    let [n, rate, ticks] = args[..] else {
        eprintln!("usage: checksum <n> <rate_permille> <ticks>");
        std::process::exit(2);
    };
    let mut g = Grid::new(n as u32, rate as u32);
    for _ in 0..ticks {
        g.step(|_| {});
    }
    println!("energy_sum={:016x} writes={}", g.energy_sum(), g.writes());
}
