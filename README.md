# pumpkin-modding-lab

Experiments toward modded play on [Pumpkin](https://github.com/Pumpkin-MC/Pumpkin), the Rust
Minecraft server with WASM plugins: what plugin work costs, what the plugin API can already do,
and plugins built on it. Formerly `pumpkin-server-bench`; old links redirect here.

| Part | What | Where |
|---|---|---|
| Benchmark | The same mod workload on Pumpkin and NeoForge, and a batched-write prototype for the plugin API | below, [docs/RESULTS.md](docs/RESULTS.md) |
| Chain-mining plugin | Sneak-to-chain mining for vanilla clients, built by a local Qwen loop and play-tested | [pumpkin/plugin-chainmine](pumpkin/plugin-chainmine), [feasibility](docs/chainmine-feasibility.md) |
| Machine layer | Block entities, menus and ticking machines on vanilla blocks from a v0.1 plugin, with the API gaps found | [docs/machine-feasibility.md](docs/machine-feasibility.md) |

Task list: [TASKS.md](TASKS.md).

## Benchmark

Does a Minecraft mod run faster as a Pumpkin (Rust) plugin than as a NeoForge (Java) mod,
once it is doing real server work at scale?

Every variant runs the same workload: a grid of machines that tick every server tick,
pass energy to their neighbours and write blocks into the world (see [SPEC.md](SPEC.md)).
Every run is checked against a reference checksum before its timings are kept, so a
variant can't look fast by doing less work.

### Results

10,000 machines, 100 block writes per tick, 2,400 measured ticks after 1,200 warm-up ticks. Every run
reproduced the reference checksum. Full write-up: [docs/RESULTS.md](docs/RESULTS.md).

| Variant | Server | Mean tick | p99 tick | CPU / tick | Memory |
|---|---|---:|---:|---:|---:|
| Block entity per machine | NeoForge 26.3 | 1.48 ms | 2.84 ms | 2.46 ms | 3,700 MB |
| One loop over all machines | NeoForge 26.3 | **0.63 ms** | 1.02 ms | 1.51 ms | 3,302 MB |
| WASM plugin, one host call per write | Pumpkin `2c7931a` | 36.03 ms | 55.82 ms ⚠️ | 12.34 ms | **183 MB** |
| WASM plugin, one host call per tick | Pumpkin + [bulk-write patch](pumpkin/patches/bulk-write.py) | 2.21 ms | 3.66 ms | 1.17 ms | 184 MB |

⚠️ over the 50 ms tick budget (20 TPS).

- Plugin compute is close to free: 1 machine and 10,000 machines give the same tick time on Pumpkin.
- Each world write from a WASM plugin costs about 0.33 ms, almost all of it the host-call round trip
  (`pump_blocking`), so tick time grows in a straight line with the number of writes.
- Sending a tick's writes in one `set-block-states` call (a 31-line prototype) cuts that to about
  6 µs per write. What remains is about 1.2 ms per tick for calling the plugin at all.
- Single runs on a DGX Spark (GB10), server pinned to its ten Cortex-X925 cores.

### Variants

| Server | Variant | What it measures | Status |
|---|---|---|---|
| NeoForge 26.3 | `block_entity` | One ticking block entity per machine, the way real mods are written | Measured |
| NeoForge 26.3 | `batched` | The same logic in one loop per tick, separating design from language | Measured |
| Pumpkin | `wasm-batched` | WASM plugin, state inside the plugin, one host call per world write | Measured |
| Pumpkin | `bulk` | `wasm-batched` with each tick's writes in one `set-block-states` call (needs the patch) | Measured |
| Pumpkin | `native` | The same logic compiled into the server | Planned |
| Pumpkin | worker threads | Copy-then-apply on other cores, as in pumpkin-patch's client host | Planned |

## Layout

| Path | What |
|---|---|
| `SPEC.md` | The workload contract every variant follows |
| `workload/` | Rust reference implementation, golden checksums, `checksum` tool |
| `neoforge/` | NeoForge mod: both Java variants plus the tick probe |
| `pumpkin/` | Pumpkin WASM plugins: `probe/` (tick timing) and the variants; `patches/bulk-write.py` adds `set-block-states`; `plugin-chainmine/`; `feas-machine/` (machine-layer scratch plugin) |
| `harness/psb.py` | Runs one configuration through the server console and writes JSON |
| `scripts/` | Feasibility checks run through the server console (`feas_*.py`) |
| `qwen/` | The Qwen build loop: task specs, `gate.sh`, `loop.py` |
| `results/raw/` | One JSON per run |

## Method

- Host: DGX Spark (GB10), server pinned to the ten Cortex-X925 cores (`5-9,15-19`).
  The model-serving process is paused (SIGSTOP) during timed runs.
- Tick time: NeoForge from the first `ServerTickEvent.Pre` listener to the last
  `ServerTickEvent.Post` listener. Pumpkin from its own `ServerTickEndEvent.duration_nanos`,
  which starts before `ServerTickStartEvent` handlers run. Both cover the full world tick.
- `/probe arm <ticks>` records exactly the measured ticks; idle ticks are never counted.
- CPU time for the whole process (all threads) is read from `/proc`, so work moved off
  the main thread still counts.
- Fresh world per run, chunks force-loaded, warm-up before measuring (JIT and caches).

## Running

On the benchmark host, with Pumpkin cloned and built at `~/psb/Pumpkin`, JDK 25 at
`~/psb/opt/jdk-25` and the NeoForge server installed at `~/psb/servers/neoforge`:

```bash
scripts/sync.sh                       # from your PC: mirror this repo to ~/psb/bench
python3 harness/psb.py --server neoforge --variant block_entity --n 10000 --rate 10 --repeats 3
python3 harness/psb.py --server pumpkin --variant wasm-batched --n 10000 --rate 10 --repeats 3
```

Qwen loop, for a Pumpkin variant:

```bash
python3 qwen/loop.py --task qwen/tasks/wasm-batched.md --variant wasm-batched
```

The NeoForge server needs `pause-when-empty-seconds=0` in `server.properties`: with no players
connected it otherwise stops ticking after 60 seconds. The harness fails a run whose tick count stops
advancing. Host-specific loop settings (model endpoint, usage log) go in `~/psb/qwen-loop.json`.

## How this was built

The harness, workload, NeoForge mod, probe, `bulk` variant and patch were written with Claude.
`pumpkin/wasm-batched` was written by a local model (Qwen3.8-Flash-Next) through `qwen/loop.py`,
gated on a build, clippy with `-D warnings` and a matching checksum on a live server, then reviewed.

## License

MIT, see [LICENSE](LICENSE). `pumpkin/patches/bulk-write.py` patches Pumpkin's GPL-3.0 server code
and is offered under GPL-3.0 to match.
