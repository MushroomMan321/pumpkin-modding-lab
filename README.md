# pumpkin-server-bench

Does a Minecraft mod run faster as a Pumpkin (Rust) plugin than as a NeoForge (Java) mod,
once it is doing real server work at scale?

Every variant runs the same workload: a grid of machines that tick every server tick,
pass energy to their neighbours and write blocks into the world (see [SPEC.md](SPEC.md)).
Every run is checked against a reference checksum before its timings are kept, so a
variant can't look fast by doing less work.

**Results: [docs/RESULTS.md](docs/RESULTS.md).** In short: plugin compute is close to free, but each
world write from a WASM plugin costs about 0.33 ms of tick time, almost all of it the host-call round
trip. A 31-line batched-write prototype takes 100 writes per tick from 30.6 ms to 2.2 ms.

## Variants

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
| `pumpkin/` | Pumpkin WASM plugins: `probe/` (tick timing) and the variants; `patches/bulk-write.py` adds `set-block-states` |
| `harness/psb.py` | Runs one configuration through the server console and writes JSON |
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
