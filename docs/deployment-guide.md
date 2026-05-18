# Project Michael — Full Deployment Guide

From a fresh Termux install with an old copy of Michael, through a clean VPS, to a running
Vast.ai GPU endpoint. Follow the phases in order — each one depends on the last.

---

## Prerequisites

Before you start, have these in hand:

| Item | Where to get it |
|------|----------------|
| Termux with storage access | F-Droid or Google Play |
| VPS public IP, root SSH access | Your VPS provider console |
| Vast.ai account + API key | console.vast.ai → Account → API Keys |

---

## Phase 1 — Termux: upgrade from old install

### 1.1 — Update Termux packages

```bash
pkg update && pkg upgrade -y
```

### 1.2 — Get the latest code

If you already have a clone, pull the latest:

```bash
cd ~/project_michael
git pull origin main
```

If your old clone is stale or broken, re-clone cleanly:

```bash
cd ~
rm -rf project_michael
git clone https://github.com/ari6six6/project_michael project_michael
cd project_michael
```

### 1.3 — Run the Termux bootstrap

```bash
bash bootstrap_termux.sh
```

This script is idempotent — safe to run over an existing install. It:
- Installs `python`, `openssh`, `git`, `rsync`, `coreutils`, `nano` via `pkg`
- Installs all Python deps from `requirements.txt` via pip (uses `httpx`, not the OpenAI SDK — no Rust compiler needed)
- Creates `~/.michael/` state directory
- Requires `~/.ssh/id_ed25519` to already exist — bootstrap does **not** generate keys. If missing, it errors out with the `ssh-keygen` command for you to run.
- Installs the `michael` wrapper at `$PREFIX/bin/michael`
- Runs `michael init` to create a stub `~/.michael/config.json`

### 1.4 — Verify

```bash
michael --help
```

You should see the full command list. If you get `command not found`, restart your Termux session and try again.

### 1.5 — Copy your SSH public key

You will need this in Phase 2:

```bash
cat ~/.ssh/id_ed25519.pub
```

Copy the entire output line. It starts with `ssh-ed25519 AAAA…`.

> **Backup tip:** Also copy `~/.ssh/id_ed25519` (the private key) to a secure location.
> After Phase 2, SSH password auth is disabled on the VPS. Losing this key means losing access.

---

## Phase 2 — VPS: bootstrap from scratch

All commands in this phase run on the VPS as **root** unless noted.

### 2.1 — SSH in as root

```bash
ssh root@<vps-ip>
```

### 2.2 — Clone the repo

```bash
git clone https://github.com/ari6six6/project_michael /opt/project_michael
cd /opt/project_michael
```

### 2.3 — Run the VPS bootstrap

```bash
bash bootstrap.sh
```

This takes a few minutes. It runs 11 numbered steps:

| Step | What it does |
|------|-------------|
| 1 | Installs UFW, fail2ban, podman, python3, git, tmux, jq, chrony |
| 2 | Sets timezone, locale, NTP |
| 3 | Enables automatic security updates |
| 4 | Creates the `michael` user with sudo access |
| 5 | Configures UFW: deny-all-in, allow SSH only |
| 6 | Hardens SSH: pubkey-only, no passwords, no root login |
| 7 | Configures fail2ban for SSH rate-limiting |
| 8 | Applies kernel hardening (sysctl, AppArmor) |
| 9 | Builds the `michael-sandbox:alpine` container image |
| 10 | Creates Python venv, installs deps, installs `/usr/local/bin/michael` wrapper |
| 11 | Creates `/home/michael/workspace` for remote sandboxes |

> **Warning from the script:** It will print an alert if `/root/.ssh/authorized_keys` is
> missing or empty. Keep your root SSH session open until you have confirmed you can SSH in
> as `michael` with your key (step 2.5 below). Do not close the root session prematurely.

### 2.4 — Add your Termux public key to the `michael` user

Still as root on the VPS:

```bash
mkdir -p /home/michael/.ssh
echo "ssh-ed25519 AAAA...  (paste the full line from Phase 1 step 1.5)" \
  >> /home/michael/.ssh/authorized_keys
chmod 700 /home/michael/.ssh
chmod 600 /home/michael/.ssh/authorized_keys
chown -R michael:michael /home/michael/.ssh
```

### 2.5 — Verify SSH access from Termux

Back in Termux (keep root session open as fallback):

```bash
ssh michael@<vps-ip>
```

If you get a shell prompt without a password prompt, Phase 2 is complete. Exit back to Termux.

### 2.6 — Confirm the sandbox image was built

```bash
ssh michael@<vps-ip> 'podman images'
```

Expected output includes a line like:

```
localhost/michael-sandbox  alpine  <hash>  ...
```

If it is missing, re-run step 9 manually:

```bash
ssh michael@<vps-ip> "cd /opt/project_michael && podman build -t michael-sandbox:alpine -f Dockerfile.sandbox ."
```

---

## Phase 3 — Vast.ai: rent a GPU

Michael drives Ollama over SSH — you do **not** need to configure an on-start command
or anything else in the Vast console. Just rent a GPU with a PyTorch / CUDA template
and stop here; Phase 5 (`michael gpu up`) installs Ollama with a curl one-liner,
pulls the model, and caches the endpoint.

### 3.1 — Pick a GPU

The default model is `qwen2.5:72b` (configured in `gpu.model_repo`), which needs
≥45 GB VRAM. Rent accordingly (A100 80 GB, H100, or A6000-class). If you want a
different model, set `gpu.model_repo` in Phase 4 before running `gpu up` — any
Ollama tag works (`llama3.1:70b`, `qwen2.5-coder:32b`, etc.).

### 3.2 — Rent

1. Go to **console.vast.ai → Search**
2. Filter by your target GPU
3. Pick any PyTorch / CUDA template. No on-start command needed.
4. Click **Rent**

### 3.3 — Copy the SSH command

Once the instance is up, the Vast console shows an SSH command like
`ssh root@1.2.3.4 -p 10022`. Copy it as-is — Phase 5 will paste it into a prompt and
parse host/user/port automatically.

---

## Phase 4 — Termux: wire up the config

### 4.1 — Open config

```bash
michael config
```

This opens `~/.michael/config.json` in `$EDITOR` (defaults to `nano`).

### 4.2 — Fill in the required fields

You only need three things by hand: the Vast API key (so `gpu up` can auto-detect your
instance ID and pause/resume it), the VPS host, and optionally a non-default model. The
GPU SSH details are filled in for you by Phase 5 from the SSH command you paste.

```json
{
  "vast_api_key": "<your Vast.ai API key>",
  "vps": {
    "host": "<vps-ip>"
  },
  "gpu": {
    "model_repo": "qwen2.5:72b"
  }
}
```

Defaults for everything else (`vps.user=michael`, `vps.ssh_key_path=~/.ssh/id_ed25519`,
`vps.workspace_dir=/home/michael/workspace`, `gpu.ssh_user=root`, `gpu.gpu_port=11434`)
match the bootstrap layout — override them only if your setup differs. Save and exit
(`Ctrl+X` → `Y` → `Enter` in nano).

### 4.3 — Verify VPS connectivity

```bash
michael ssh-test
```

Expected: `✓ VPS reachable  handshake Xms`

If it fails: re-check `vps.host`, `vps.user`, and that your SSH key is in
`/home/michael/.ssh/authorized_keys` on the VPS.

---

## Phase 5 — Start the GPU and pull the model

```bash
michael gpu up
```

On first run, Michael will prompt for the Vast SSH command (paste the one you copied
in Phase 3.3). After that:

1. SSHes into the GPU and starts the Vast instance via the API
2. Checks if Ollama is installed; runs `curl -fsSL https://ollama.com/install.sh | sh`
   if missing (~10 s on a fresh PyTorch image)
3. Starts the Ollama daemon (systemd if available, nohup fallback) and waits for the
   endpoint to answer
4. Checks if the model is already pulled; if not, runs `ollama pull <tag>` in the
   background with progress streamed back via `/tmp/ollama_pull.log`
5. Caches `endpoint` and `served_model_name` in `~/.michael/config.json` and prints
   the SSH port-forward command to run in a second terminal

**Expected total time on a fresh instance:**
- Ollama install: ~10 s
- First model pull: 5–20 minutes depending on Vast network and model size
- Subsequent `gpu up` runs reuse the cached model on disk — seconds, not minutes

When ready you'll see:
```
✓ endpoint ready: http://<ip>:<port>/v1
```

> **Termux tip:** For long waits, run `termux-wake-lock` first to prevent Android from
> suspending the session.

---

## Phase 6 — Create a project and run the agent

### 6.1 — Create a project

```bash
michael new myproject
michael use myproject
michael current        # should print: myproject
```

### 6.2 — Run a basic prompt

```bash
michael run hello, what can you do?
```

What happens:
1. Michael packages the four-header context (H1: your prompts, H2: filesystem snapshot,
   H3: tool call history, H4: protocol bible) and sends it to the Ollama endpoint
2. The LLM iterates privately — you see dim status lines for each turn
3. When the LLM calls `commit_changes(summary=…)`, staged writes are flushed to the project,
   the trash dir is updated for `michael undo`, and any detectable deliverable is installed
4. The terminal prints what was applied and exits

### 6.3 — Use the sandbox

```bash
michael run write a Python script that prints the first 20 primes, run it in the sandbox, and show me the output
```

The LLM will:
- Call `write_file` to stage the script
- Call `run_in_sandbox` — which SSHes to the VPS and runs it in the `michael-sandbox:alpine` container
- Read the output and decide if it's correct
- Call `commit_changes(summary=…)` once satisfied → script is committed and the run exits

### 6.4 — View the event log

```bash
michael log           # last 20 events
michael log --tail 50 # last 50 events
```

---

## Phase 7 — Everyday lifecycle

| Goal | Command |
|------|---------|
| Start the GPU for a session | `michael gpu up` |
| Stop the GPU (save credits) | `michael gpu down` |
| Check current state | `michael status` |
| Full agent run | `michael run <your prompt>` |
| Run Python in isolated sandbox | `michael sandbox script.py` |
| Revert last committed change | `michael undo` |
| See revertible changes | `michael undo --list` |
| List all projects | `michael show` |
| Switch project | `michael use <slug>` |
| Interactive REPL | `michael` (no args) |

---

## Troubleshooting

### `michael gpu up` times out (90 min)
- Check the Vast console — is the instance actually running?
- SSH into the Vast instance manually and inspect `/tmp/ollama.log` (or `journalctl -u ollama`) and `/tmp/ollama_pull.log` for the real error
- Confirm the model in `gpu.model_repo` fits in the rented VRAM
- Confirm `nvidia-smi` works on the instance (the PyTorch template should make it work)

### `michael ssh-test` fails
- Confirm `vps.host` is the correct IP
- Confirm your pubkey is in `/home/michael/.ssh/authorized_keys` on the VPS
- Confirm UFW allows your SSH port: `ssh root@<vps> ufw status`

### `run_in_sandbox` fails with "sandbox unavailable"
- Verify `vps.host` is set in config
- Run `michael ssh-test` to confirm VPS is reachable
- Confirm the sandbox image exists: `ssh michael@<vps> podman images`

### LLM never calls `commit_changes`
- The model may need more context or a more specific prompt
- Check `michael log` to see LLM responses
- If the run hits `MAX_AGENT_TURNS` (60) without committing, pending changes are
  discarded; run again with a tighter prompt to continue
- If the run hits `MAX_AGENT_TURNS` (60) without committing, pending changes are discarded;
  run again with a tighter prompt to continue

### Vast.ai endpoint changes after restart
- Run `michael gpu up` after each instance restart — it re-checks Ollama and re-caches the endpoint
- The old cached endpoint in `config.json` becomes invalid when the instance stops

---

## Security notes

- `~/.michael/config.json` is created `chmod 600` — it contains your Vast API key
- SSH password auth is disabled on the VPS after bootstrap — pubkey only
- The sandbox container runs as an unprivileged user, with no network (by default), read-only
  root, memory/CPU/PID limits, and all capabilities dropped
- `commit_changes` is a confirmable tool — Michael prompts y/n before flushing staged
  writes. Auto-exec tools (`read_file`, `list_dir`, `search_memory`, `fetch_url`,
  `search_tools`, `forge_tool`, `load_model`) run without a prompt; review their summaries
  in the dim status output
