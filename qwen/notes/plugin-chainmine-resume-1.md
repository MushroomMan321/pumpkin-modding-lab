HANDOFF (written by the supervisor; the previous run was stopped at turn 135)

1. Done: pumpkin/plugin-chainmine/Cargo.toml (cdylib + rlib, pumpkin-plugin-api, tracing),
   src/plan.rs (planner, VarInt, wear, PRNG, tool string, tests) and src/lib.rs (plugin) exist.

2. Failing: cargo_check fails with four API mistakes in src/lib.rs. The fixes are in
   qwen/notes/pumpkin-plugin-api.md:
   - `CommandSender::Console` does not exist on `command::CommandSender`. To run a server
     command use `pumpkin_plugin_api::server::CommandSender::Console` (import it as `RunAs`).
   - `ConsumedArgs` has no `get`: use `args.get_value("name")`.
   - There is no `Arg::Integer`: integers arrive as `Arg::Num(Ok(Number::Int32(v)))`
     (`Number` is `pumpkin_plugin_api::command_wit::Number`); greedy strings as `Arg::Simple(s)`.
   - The main hand is `Hand::Right` (`pumpkin_plugin_api::common::Hand`); there is no `MainHand`.
     `set_component` takes a slice: `&bytes`.

3. Learned: the previous run spent 110 turns reading the API. Do not. Everything you need is in
   qwen/notes/pumpkin-plugin-api.md and in qwen/templates/plugin/src/lib.rs, a skeleton that
   compiles and ran on a real server using each of those calls.

4. Next: read qwen/notes/pumpkin-plugin-api.md, then src/lib.rs, fix the errors above,
   cargo_check until clean, then run_gate.
