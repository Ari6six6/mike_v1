# The Protocol: Flat Loop with the commit_changes Gate

The machine runs a **flat tool loop**. One turn = one LLM call. On each turn
the LLM may call any number of tools; their results feed back in the next
turn. The loop ends when the LLM calls `commit_changes` (staged writes are
flushed and the run exits) or when it replies without any tool calls
(natural exit, no commit). Maximum turns per run is `MAX_AGENT_TURNS` (60).

## Fundamental Truth

**Goal → Turns → `commit_changes` → Done**

User states a goal. The LLM iterates with full toolset until it has
something worth committing, then calls `commit_changes(summary=…)`. There
are no rooms, phases, or pass-phrases — `commit_changes` is the only
commit gate.

---

## The Three Kantian Questions (orientation, not enforcement)

The system prompt asks the model to keep three questions in mind across
its turns. They are a reasoning lens, not a state machine — the harness
does not gate tools by which question is being addressed.

1. **What can I know?** — read the filesystem, search prior tool history,
   establish the constraints.
2. **What should I do?** — implement the smallest correct action; test it
   before committing.
3. **What can I hope for?** — leave a clean state and a note on what the
   next run should pick up.

---

## Tools

Built-in tools registered in `michael/tools.py`:

| Tool | Auto-exec? | Behaviour |
|------|------------|-----------|
| `read_file` | yes | Read a file from the project |
| `list_dir` | yes | List a directory |
| `search_memory` | yes | Search past LLM responses for this project |
| `search_tools` | yes | Look up available tool schemas by name/keyword |
| `fetch_url` | yes | HTTP GET — read web content |
| `load_model` | yes | Switch to a different model profile mid-run |
| `forge_tool` | yes | Write a new tool to `<project>/tools/<name>.py` (available next run) |
| `write_file` | confirm | Stage a file write (committed on `commit_changes`) |
| `apply_patch` | confirm | Stage a unified-diff patch (committed on `commit_changes`) |
| `run_in_sandbox` | confirm | Execute Python in isolated podman (local or remote) |
| `run_shell` | confirm | Run a shell command in the project workspace |
| `commit_changes` | confirm | Flush staged writes, install deliverable if detected, exit |

Auto-exec tools run without a confirmation prompt. Confirm-tools render a
preview and wait for the user's `y/n` (configurable via `AUTO_EXEC_TOOLS`
in `michael/globals.py`).

### Dynamic tools

On every `michael run`, `_load_dynamic_tools` (michael/agent.py:35) scans
three directories and concatenates their `TOOL_SCHEMA` exports:

1. `<repo>/toolbox/` — bundled recon toolbox (lowest priority)
2. `~/.michael/toolbox/` — user-global tools
3. `<project>/tools/` — project-local tools (highest priority, overrides
   on name collision)

Tools written by `forge_tool` land in `<project>/tools/`. They are loaded
on the **next** `michael run`, not the current one — the registry is built
at the start of the loop.

---

## Tool Invention Format

To invent a tool, call `forge_tool` (or write the file directly with
`write_file`). Schema:

```python
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_port",
        "description": "Check if a TCP port is open on a host.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            "required": ["host", "port"],
        },
    },
}

def check_port(host: str, port: int) -> str:
    import socket
    try:
        with socket.create_connection((host, port), timeout=3):
            return "open"
    except OSError:
        return "closed"
```

The function name must match `TOOL_SCHEMA.function.name`. On the next
`michael run`, the tool appears in the LLM's tool list.

---

## Staging and Commit

Writes (`write_file`, `apply_patch`) go to a staging copy of the project
(michael/tools.py `PendingChanges`). They do not touch the real
filesystem until `commit_changes` is called. On commit:

1. Pre-change snapshots are saved to `~/.michael/projects/<slug>/trash/`
   for `michael undo`.
2. Staged files are synced into the project working tree.
3. If the project has a detectable deliverable (a top-level script that
   responds to `--help`), it is registered and a wrapper is installed at
   `~/.michael/bin/<slug>` (michael/agent.py:218-256).

A `KeyboardInterrupt` mid-run discards pending changes. A natural exit
without `commit_changes` also discards them.

---

## Events Logged

Per-run events appended to `~/.michael/projects/<slug>/events.jsonl`:

| Event | When |
|-------|------|
| `agent.started` | Loop start (with model, served name, sandbox label) |
| `prompt.sent` | User prompt enqueued |
| `assistant.message` | Each LLM response (optionally with full text if `log_responses`) |
| `tool.executed` | After any tool call, with name + brief summary |
| `agent.aborted` | Ctrl-C during the loop |
| `agent.ended` | Loop finished (one of: turns, committed, error, aborted) |
| `error` | Exception path |

Global events appended to `~/.michael/events.jsonl`:
`config.loaded`, `project.created`, `project.activated`,
`endpoint.discovered`, `gpu.poll`, `gpu.ready`, `gpu.stopped`,
`ssh.health`.

---

## Roadmap

Items previously described in earlier drafts of this protocol that are
**not implemented today** and are tracked as future work:

- **Four-Room Kantian Cycle** — gating tool access by phase
  (Epistemics→Ethics→Teleology→Completion), enforcing per-room turn caps,
  emitting `room.*` events, requiring a trailing-token `Ja` passcode in a
  dedicated completion room. The current implementation is a single flat
  loop; the rooms exist as a reasoning lens in the system prompt only.
- **Per-cycle dynamic tool reload** — currently dynamic tools load once at
  run start. Reloading between phases would let a tool forged earlier in
  the run be used later in the same run.
- **Browser-automation tool** — Playwright or similar so the agent can
  drive real web apps, not only do HTTP recon.
