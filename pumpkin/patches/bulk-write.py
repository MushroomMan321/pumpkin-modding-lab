#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later  (patches GPL-3.0 Pumpkin server code)
"""Prototype patch for Pumpkin: `world.set-block-states`, a batched block write for WASM plugins.

Adds one v0.1 WIT method that applies a list of (position, state) writes inside a single
`pump_blocking` job, so N writes cost one host round trip instead of N. Each write is the same
`World::set_block_state` call the single-write method makes, so re-entrant events still fire
per write. Benchmark prototype only: not proposed upstream in this form.

  python3 bulk-write.py ~/psb/Pumpkin          # apply (idempotent)
"""

import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()


def patch(path, anchor, addition, after=True):
    p = root / path
    text = p.read_text()
    if addition.strip() in text:
        print(f"already patched: {path}")
        return
    if text.count(anchor) != 1:
        sys.exit(f"anchor not found exactly once in {path}: {anchor!r}")
    text = text.replace(anchor, anchor + addition if after else addition + anchor)
    p.write_text(text)
    print(f"patched: {path}")


patch(
    "crates/pumpkin-plugin-wit/v0.1/world.wit",
    "        set-block-state: func(pos: block-pos, state: u16, update-flags: block-flags);\n",
    "\n"
    "        /// Sets many block states in one call, applied in order. One host round trip for\n"
    "        /// the whole list; events fire per write exactly as with set-block-state.\n"
    "        set-block-states: func(changes: list<tuple<block-pos, u16>>, update-flags: block-flags);\n",
)

patch(
    "crates/pumpkin-wasm-host-v0_1/src/bindings.rs",
    '        "pumpkin:plugin/world@0.1.0.[method]world.set-block-state": async | store | trappable,\n',
    '        "pumpkin:plugin/world@0.1.0.[method]world.set-block-states": async | store | trappable,\n',
)

patch(
    "crates/pumpkin-wasm-host-v0_1/src/world.rs",
    """    async fn set_block_state(
        host: Access<'_, PluginHostState, Self>,
        world: Resource<World>,
        pos: WitBlockPos,
        state: u16,
        update_flags: WitBlockFlags,
    ) -> wasmtime::Result<()> {
        set_block_state_with_store(host, world, pos, state, update_flags).await
    }
""",
    """
    async fn set_block_states(
        mut host: Access<'_, PluginHostState, Self>,
        world: Resource<World>,
        changes: Vec<(WitBlockPos, u16)>,
        update_flags: WitBlockFlags,
    ) -> wasmtime::Result<()> {
        let mut writes = Vec::with_capacity(changes.len());
        for (pos, state) in changes {
            let Some(state_id) = BlockStateId::new(state) else {
                return Err(wasmtime::Error::msg("Invalid BlockStateId"));
            };
            writes.push((BlockPos::new(pos.x, pos.y, pos.z), state_id));
        }
        let internal_flags = from_wit_block_flags(update_flags);
        let (world, plugin) = world_and_plugin(host.get(), &world)?;

        plugin
            .store
            .pump_blocking(&mut host, move || {
                for (pos, state_id) in &writes {
                    world.set_block_state(pos, *state_id, internal_flags);
                }
            })
            .await
    }
""",
)
