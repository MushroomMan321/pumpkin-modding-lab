# Workload spec: `energy-grid` v1

Every variant (NeoForge or Pumpkin, Java, WASM or native) runs exactly this
workload. A variant is correct only if it reproduces the reference checksums in
`workload/` for the same `(n, rate_permille, ticks)`.

## Layout

- `n` machines, `s = ceil(sqrt(n))`.
- Machine `i` (for `0 <= i < n`) sits at `x = X0 + (i % s)`, `z = Z0 + (i / s)`.
- Machine block at `y = Y_MACHINE`, display block at `y = Y_DISPLAY`.
- `X0 = 0`, `Z0 = 0`, `Y_MACHINE = 200`, `Y_DISPLAY = 201`.
- Before measurement, every display block is `minecraft:white_wool`.
  Machine blocks are `minecraft:stone`, except in variants that use a real
  block entity, which use their own machine block there.
- All chunks covering the grid are force-loaded with `/forceload`.

## Neighbours

The neighbours of `i` are the machines directly north, south, east and west of it
in the grid (`i - s`, `i + s`, `i - 1` if `i % s != 0`, `i + 1` if `(i + 1) % s != 0`),
counted only when `< n` and `>= 0`. `deg(i)` is how many there are (2 to 4).

## State

Each machine holds one unsigned 64-bit energy value `e[i]`.
Initial value: `e[i] = i % 97`.

## One tick (tick number `t`, starting at 0)

All arithmetic is unsigned 64-bit and wraps on overflow (Java: `long` with
two's-complement wrap, same bits).

1. Energy step, double buffered (results must not depend on machine order):
   ```
   next[*] = 0
   for each i:
       share = e[i] / 8                      (integer division)
       next[i] += e[i] - share * deg(i) + 1  (the +1 is generation)
       for each neighbour j of i:
           next[j] += share
   e = next
   ```
2. World writes:
   ```
   w = n * rate_permille / 1000              (integer division)
   for k in 0 .. w:
       g = t * w + k                         (global write counter)
       j = g % n
       pass = g / n
       block = if pass % 2 == 0 { lime_wool } else { white_wool }
       set display block of machine j to `block`
   ```
   Every write changes the block (white to lime on even passes, lime to white
   on odd ones), so every write is a real world change.

## Checksum

After `ticks` ticks:

```
energy_sum = sum over i of e[i] * (i + 1)      (wrapping u64)
writes     = ticks * w
```

Report `energy_sum` as 16 lowercase hex digits.

## Commands

Every variant registers the same commands, so the harness can drive any of them
over RCON:

| Command | Effect |
|---|---|
| `/psb setup <n> <rate_permille>` | Place the grid (machine + white display blocks), reset state to tick 0, do not run |
| `/psb start` | Start running one workload tick per server tick |
| `/psb stop` | Stop running |
| `/psb run <ticks>` | Run exactly `<ticks>` workload ticks, one per server tick, then stop |
| `/psb status` | Print `psb status tick=<t> n=<n> rate=<r> energy_sum=<hex> writes=<w>` |

The harness, not the variant, measures tick time (see `harness/`).
