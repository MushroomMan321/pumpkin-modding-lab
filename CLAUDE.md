# Notes for Claude

- **Keep `TASKS.md` current.** At the start of a session read it; when a task is finished, move it to
  Done with the date and the commit or result; add new tasks as they come up. Don't leave the session
  with finished work still listed as open.
- Host-specific setup (benchmark host, model endpoint, dashboard) is in `CLAUDE.local.md`, which is
  not committed. Never commit hostnames, LAN addresses, home paths or private tool paths; results
  JSON should use `~/` paths.
- Every benchmark run must reproduce the reference checksum (`workload/`); a variant that fails it
  has no valid timings.
- Results that go public are single runs until `TASKS.md` says repeats are done; say so.
- Commit and push only when the user asks. Commits end with `Assisted-by: Ada`; never add claude.ai
  session links.

## Writing tasks for the Qwen loop

Qwen spends its turns rediscovering the API when a task leaves that to it (the first chain-mine
run read for 110 turns and still got four API names wrong). So every task:

- Points at `qwen/notes/pumpkin-plugin-api.md` and a template that already compiles
  (`--template qwen/templates/plugin` for plugins). Before a task needs a call the sheet lacks,
  add it to the skeleton, compile it, and add it to the sheet.
- Has a gate checked end to end before the run, every stage: a stub built from the template, with
  a couple of unit tests, that fails the plugin checks and passes the rest. (The first chain-mine
  gate was checked from the smoke stage only, and its test stage could not link a cdylib+rlib
  crate; Qwen lost an escalation to it.)
- Says what to read first and "read the API source only to resolve a compiler error".

The loop nudges by itself when Qwen reads too long (fresh conversation, reading locked to its own
crate until a compile or gate error) and escalates only if that fails.

## Contributing to Pumpkin itself

Pumpkin's `AGENTS.md` applies to anything aimed at `Pumpkin-MC/Pumpkin`:

- A human opens every PR. Agents never open PRs or post comments, reviews or replies on GitHub.
- The person opening it play-tests with a real client and attaches a screenshot or recording for
  anything visible in game. One bug or feature per PR, based on current `master`.
- The description's last line is `> This description was drafted by an AI agent (<tool and model>).`
- Commit messages are conventional commits with a scope, subject line only, no tool trailers.

## Clean-room rule

FTB Ultimine (and other All Rights Reserved mods) may inspire a plugin's behaviour, but their source
must never be read, copied or given to the Qwen loop, and their names are not used. Work from
in-game behaviour and public documentation only. LGPL mods (for example FallingTree) may be ported,
and the port is then LGPL.
