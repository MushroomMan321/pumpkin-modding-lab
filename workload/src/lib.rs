//! Reference implementation of the `energy-grid` workload described in `SPEC.md`.
//!
//! Pumpkin variants can depend on this crate directly. Other variants must
//! reproduce its checksums (see `GOLDENS`).

#![no_std]
extern crate alloc;

use alloc::vec;
use alloc::vec::Vec;

/// World position of the grid's first machine.
pub const X0: i32 = 0;
pub const Z0: i32 = 0;
pub const Y_MACHINE: i32 = 200;
pub const Y_DISPLAY: i32 = 201;

/// Block a display write sets.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Display {
    White,
    Lime,
}

/// One world write produced by a tick.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Write {
    pub machine: u32,
    pub block: Display,
}

pub struct Grid {
    n: u32,
    s: u32,
    rate_permille: u32,
    tick: u64,
    energy: Vec<u64>,
    next: Vec<u64>,
}

impl Grid {
    pub fn new(n: u32, rate_permille: u32) -> Self {
        assert!(n > 0, "n must be positive");
        assert!(rate_permille <= 1000, "rate_permille must be <= 1000");
        let s = side(n);
        let energy = (0..n).map(|i| u64::from(i % 97)).collect();
        Self {
            n,
            s,
            rate_permille,
            tick: 0,
            energy,
            next: vec![0; n as usize],
        }
    }

    pub fn n(&self) -> u32 {
        self.n
    }

    pub fn side(&self) -> u32 {
        self.s
    }

    pub fn tick(&self) -> u64 {
        self.tick
    }

    /// Writes per tick: `n * rate_permille / 1000`.
    pub fn writes_per_tick(&self) -> u32 {
        (u64::from(self.n) * u64::from(self.rate_permille) / 1000) as u32
    }

    /// Block position `(x, z)` of machine `i`.
    pub fn position(&self, i: u32) -> (i32, i32) {
        (X0 + (i % self.s) as i32, Z0 + (i / self.s) as i32)
    }

    /// Advances one tick and calls `write` for every world write, in order.
    pub fn step(&mut self, mut write: impl FnMut(Write)) {
        let n = self.n as usize;
        let s = self.s as usize;
        self.next.fill(0);
        for i in 0..n {
            let e = self.energy[i];
            let share = e / 8;
            let mut deg = 0u64;
            let mut give = |j: usize, next: &mut [u64]| {
                next[j] = next[j].wrapping_add(share);
                deg += 1;
            };
            if i >= s {
                give(i - s, &mut self.next);
            }
            if i + s < n {
                give(i + s, &mut self.next);
            }
            if i % s != 0 {
                give(i - 1, &mut self.next);
            }
            if (i + 1) % s != 0 && i + 1 < n {
                give(i + 1, &mut self.next);
            }
            let keep = e.wrapping_sub(share.wrapping_mul(deg)).wrapping_add(1);
            self.next[i] = self.next[i].wrapping_add(keep);
        }
        core::mem::swap(&mut self.energy, &mut self.next);

        let w = u64::from(self.writes_per_tick());
        let n64 = u64::from(self.n);
        for k in 0..w {
            let g = self.tick.wrapping_mul(w).wrapping_add(k);
            let block = if (g / n64) % 2 == 0 {
                Display::Lime
            } else {
                Display::White
            };
            write(Write {
                machine: (g % n64) as u32,
                block,
            });
        }
        self.tick += 1;
    }

    /// `sum(e[i] * (i + 1))`, wrapping.
    pub fn energy_sum(&self) -> u64 {
        self.energy
            .iter()
            .enumerate()
            .fold(0u64, |acc, (i, &e)| acc.wrapping_add(e.wrapping_mul(i as u64 + 1)))
    }

    pub fn writes(&self) -> u64 {
        self.tick * u64::from(self.writes_per_tick())
    }
}

/// `ceil(sqrt(n))` without floating point.
pub fn side(n: u32) -> u32 {
    let mut s = 1u32;
    while u64::from(s) * u64::from(s) < u64::from(n) {
        s += 1;
    }
    s
}

/// `(n, rate_permille, ticks, energy_sum, writes)` that every variant must reproduce.
/// Regenerate with `cargo run --bin goldens` only when SPEC.md changes.
pub const GOLDENS: &[(u32, u32, u64, u64, u64)] = &[
    (1, 0, 10, 0x000000000000000a, 0),
    (37, 300, 200, 0x00000000000256be, 2200),
    (1000, 10, 1000, 0x000000001f3d063e, 10000),
    (10000, 10, 1000, 0x0000000c33c1d925, 100000),
    (10000, 100, 1000, 0x0000000c33c1d925, 1000000),
    (50000, 10, 200, 0x000000482ca137d8, 100000),
];

#[cfg(test)]
mod tests {
    use super::*;

    fn run(n: u32, rate: u32, ticks: u64) -> (u64, u64) {
        let mut g = Grid::new(n, rate);
        for _ in 0..ticks {
            g.step(|_| {});
        }
        (g.energy_sum(), g.writes())
    }

    #[test]
    fn goldens_match() {
        for &(n, rate, ticks, sum, writes) in GOLDENS {
            assert_eq!(run(n, rate, ticks), (sum, writes), "n={n} rate={rate} ticks={ticks}");
        }
    }

    #[test]
    fn energy_is_conserved_plus_generation() {
        let mut g = Grid::new(1000, 0);
        let start: u64 = g.energy.iter().sum();
        for _ in 0..50 {
            g.step(|_| {});
        }
        let end: u64 = g.energy.iter().sum();
        assert_eq!(end, start + 50 * 1000);
    }

    #[test]
    fn every_write_changes_the_block() {
        let n = 37;
        let mut g = Grid::new(n, 300);
        let mut shown = vec![Display::White; n as usize];
        for _ in 0..200 {
            g.step(|w| {
                assert_ne!(shown[w.machine as usize], w.block);
                shown[w.machine as usize] = w.block;
            });
        }
    }

    #[test]
    fn side_is_ceil_sqrt() {
        assert_eq!(side(1), 1);
        assert_eq!(side(4), 2);
        assert_eq!(side(5), 3);
        assert_eq!(side(10_000), 100);
        assert_eq!(side(50_000), 224);
    }
}
