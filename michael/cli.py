"""CLI commands, Typer bindings, and the interactive REPL."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Optional

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

import michael.globals as G

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
from michael.agent import _run_agent_loop
from michael.backends import (
    VastClient,
    _gpu_ssh_run,
    _ping_endpoint,
    _require_endpoint,
    _ssh_argv,
    _ssh_preflight,
    _start_ollama_cmd,
    gpu_port_forward_cmd,
    llm_client,
    make_backend,
    parse_vast_ssh_cmd,
)
from michael.config import Config, CONFIG_HELP, GpuConfig, make_stub_config
from michael.project import (
    Project,
    append_event,
    create_project,
    detect_deliverable,
    get_active_project,
    get_active_slug,
    iter_events,
    list_projects,
    load_catalog,
    register_deliverable,
    replay_global,
    require_active_project,
    set_active_slug,
    slugify,
)
from michael.agent import _load_dynamic_tools
from michael.tools import TOOLS, _list_trash, _undo_one, _dispatch_dynamic_tool_from_path
from michael.utils import (
    build_header,
    load_scripture,
    _prompt_history_lines,
    _action_log_lines,
)

app = typer.Typer(
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="michael — air-gapped AI control loop",
)

gpu_app = typer.Typer(help="GPU instance management (Ollama).", invoke_without_command=True)
app.add_typer(gpu_app, name="gpu")

SUPPORTED_MODELS = ["qwen2.5:72b", "qwen3:32b"]
_MODEL_MIN_DISK_GB: dict[str, int] = {"qwen2.5:72b": 55, "qwen3:32b": 22}

tools_app = typer.Typer(help="Inspect and run dynamic tools.")
app.add_typer(tools_app, name="tools")


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


_SHELL_MARKER = "# michael shell integration"
_SHELL_LINES = (
    "\n{marker}\n"
    "export PATH=\"{bin}:$PATH\"\n"
    "mcd() {{ cd \"$(michael path)\"; }}\n"
)


def _shell_profile() -> Optional[pathlib.Path]:
    shell = os.environ.get("SHELL", "")
    home = pathlib.Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        for name in (".bashrc", ".bash_profile"):
            p = home / name
            if p.is_file():
                return p
        return home / ".bashrc"
    return None


def _inject_shell_integration() -> str:
    profile = _shell_profile()
    if profile is None:
        return "[yellow]unknown shell — add manually:[/]\n  export PATH=\"{bin}:$PATH\"\n  mcd() {{ cd \"$(michael path)\"; }}".format(bin=G.MICHAEL_BIN_DIR)
    text = profile.read_text() if profile.is_file() else ""
    if _SHELL_MARKER in text:
        return f"[dim]shell integration already in {profile}[/]"
    profile.parent.mkdir(parents=True, exist_ok=True)
    with profile.open("a") as f:
        f.write(_SHELL_LINES.format(marker=_SHELL_MARKER, bin=G.MICHAEL_BIN_DIR))
    return f"[green]wrote shell integration → {profile}[/]\n[dim]run: source {profile}[/]"


def cmd_init() -> None:
    G.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    G.MICHAEL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    if not G.GLOBAL_CONFIG_PATH.is_file():
        make_stub_config().save()
        G.console.print(f"[green]wrote stub[/] {G.GLOBAL_CONFIG_PATH}")
    else:
        G.console.print(f"[dim]config exists[/] {G.GLOBAL_CONFIG_PATH}")
    append_event("config.loaded", {"path": str(G.GLOBAL_CONFIG_PATH)})
    shell_msg = _inject_shell_integration()
    G.console.print(shell_msg)
    G.console.print(
        Panel(
            "Edit ~/.michael/config.json — fill in:\n\n"
            "  [bold]vast_api_key[/]              your Vast.ai console API key\n"
            "  [bold]gpu.model_repo[/]            Ollama tag, e.g. 'qwen2.5:72b'\n\n"
            "[dim]Optional, for remote sandbox on the VPS:[/]\n"
            "  [bold]vps.host[/]                  VPS public IP/hostname\n"
            "  [bold]vps.user[/]                  ssh user (default: michael)\n"
            "  [bold]vps.ssh_key_path[/]          path to private key\n"
            "  [bold]vps.workspace_dir[/]         /home/michael/workspace\n\n"
            "[dim]Leave vps.host empty to run without sandbox.[/]",
            title="checklist",
            border_style="green",
        )
    )


def cmd_show() -> None:
    projects = list_projects()
    if not projects:
        G.console.print("0")
        return
    active = get_active_slug()
    table = Table(title=f"projects ({len(projects)})", border_style="cyan")
    table.add_column("active", justify="center")
    table.add_column("slug", style="bold")
    table.add_column("name")
    table.add_column("path")
    table.add_column("created")
    for p in projects:
        mark = "*" if p.slug == active else ""
        table.add_row(mark, p.slug, p.name, p.path, p.created_at)
    G.console.print(table)


def cmd_new(name: Optional[str]) -> None:
    if not name:
        name = (typer.prompt("name") or "").strip()
    if not name:
        G.err.print("name is required")
        return
    try:
        slug_preview = slugify(name)
    except G.MichaelError as e:
        G.err.print(str(e))
        return
    default_path = G.WORKBENCH_DIR / "codebases" / slug_preview
    path_str = typer.prompt("path", default=str(default_path))
    path = pathlib.Path(path_str).expanduser().resolve()
    proj = create_project(name, path)
    set_active_slug(proj.slug)
    append_event("project.activated", {"slug": proj.slug})
    G.console.print(f"[green]created[/] {proj.slug} at {proj.path}")
    G.console.print(f"[dim]workspace is empty — add your code there, then run: michael run <prompt>[/]")


def cmd_use(slug: str) -> None:
    proj = Project.load(slug)
    set_active_slug(proj.slug)
    append_event("project.activated", {"slug": proj.slug})
    G.console.print(f"[green]active[/] {proj.slug}")


def cmd_current() -> None:
    p = get_active_project()
    if not p:
        G.console.print("(no active project)")
        return
    G.console.print(f"{p.slug} — {p.name} — {p.path}")


def cmd_config() -> None:
    G.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    if not G.GLOBAL_CONFIG_PATH.is_file():
        make_stub_config().save()
    help_lines = [f"[bold]{k}[/] — {v}" for k, v in CONFIG_HELP.items()]
    G.console.print(
        Panel(
            "\n".join(help_lines),
            title=f"config: {G.GLOBAL_CONFIG_PATH}",
            border_style="green",
        )
    )
    current_text = G.GLOBAL_CONFIG_PATH.read_text()
    edited = typer.edit(current_text, extension=".json")
    if edited is None or edited == current_text:
        G.console.print("[dim]no changes[/]")
        return
    try:
        json.loads(edited)
    except json.JSONDecodeError as e:
        G.err.print(f"invalid JSON, not saved: {e}")
        return
    G.GLOBAL_CONFIG_PATH.write_text(edited)
    os.chmod(G.GLOBAL_CONFIG_PATH, 0o600)
    G.console.print("[green]config saved[/]")


def _prompt_model_selection(current: str) -> str:
    """Interactive numbered menu for model selection. Returns the chosen tag."""
    _labels = {
        "qwen2.5:72b": "large instruct, ~45 GB VRAM",
        "qwen3:32b": "coder, ~20 GB VRAM",
    }
    G.console.print("\n[bold]Available models:[/]")
    for i, tag in enumerate(SUPPORTED_MODELS, 1):
        marker = " [green]← current[/]" if tag == current else ""
        G.console.print(f"  [cyan]{i}.[/] {tag}  [dim]({_labels.get(tag, '')})[/]{marker}")

    default_idx = SUPPORTED_MODELS.index(current) + 1 if current in SUPPORTED_MODELS else 1
    raw = typer.prompt(f"Model", default=str(default_idx)).strip()
    try:
        idx = int(raw)
        if 1 <= idx <= len(SUPPORTED_MODELS):
            return SUPPORTED_MODELS[idx - 1]
    except ValueError:
        if raw in SUPPORTED_MODELS:
            return raw
    G.console.print(f"[yellow]invalid choice, keeping {current or SUPPORTED_MODELS[0]}[/]")
    return current or SUPPORTED_MODELS[0]


def _select_vast_instance(cfg: "Config", gpu: "GpuConfig") -> bool:
    """List Vast.ai instances and let user pick one. Populates gpu in-place.

    Returns True on success, False if user chose manual entry or API unavailable.
    """
    if not cfg.vast_api_key:
        return False
    try:
        vast = VastClient(cfg.vast_api_key)
        instances = vast.list()
        vast.close()
    except G.MichaelError as e:
        G.console.print(f"[dim]Vast.ai API unavailable ({e}) — falling back to manual SSH entry[/]")
        return False

    if not instances:
        G.console.print(
            "[dim]No instances found on Vast.ai — rent one from the console, then re-run `michael gpu`.[/]"
        )
        return False

    table = Table(title="Vast.ai Instances", border_style="cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("ID", style="bold")
    table.add_column("GPU")
    table.add_column("Status")
    table.add_column("IP / Host")
    for i, inst in enumerate(instances, 1):
        gpu_label = f"{inst.get('num_gpus', 1)}× {inst.get('gpu_name') or '?'}"
        ip = inst.get("public_ipaddr") or inst.get("ssh_host") or "?"
        status = inst.get("actual_status") or inst.get("status") or "?"
        table.add_row(str(i), str(inst.get("id", "?")), gpu_label, status, ip)
    G.console.print(table)

    raw = typer.prompt(f"Select instance [1-{len(instances)}] or 0 for manual SSH entry", default="1").strip()
    try:
        choice = int(raw)
    except ValueError:
        choice = 0
    if choice < 1 or choice > len(instances):
        return False

    inst = instances[choice - 1]
    ssh_port = inst.get("ssh_port")
    if not ssh_port:
        G.console.print("[dim]Instance API response missing ssh_port — falling back to manual entry[/]")
        return False

    gpu.vast_instance_id = str(inst["id"])
    gpu.ssh_host = inst.get("public_ipaddr") or inst.get("ssh_host") or ""
    gpu.ssh_port = int(ssh_port)
    gpu.ssh_user = inst.get("ssh_user") or "root"
    G.console.print(
        f"[green]Selected instance {gpu.vast_instance_id}[/] "
        f"({gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port})"
    )
    return True


def _manual_ssh_setup(cfg: "Config", gpu: "GpuConfig") -> None:
    """Prompt user for SSH command and auto-detect instance ID from API."""
    G.console.print(
        "[bold cyan]Paste the SSH command from your Vast.ai console[/] "
        "[dim](e.g. ssh root@1.2.3.4 -p 10022)[/]"
    )
    ssh_str = typer.prompt("SSH command").strip()
    user, host, port = parse_vast_ssh_cmd(ssh_str)
    gpu.ssh_user = user
    gpu.ssh_host = host
    gpu.ssh_port = port

    if cfg.vast_api_key:
        try:
            vast = VastClient(cfg.vast_api_key)
            for inst in vast.list():
                if inst.get("ssh_host") == host or inst.get("public_ipaddr") == host:
                    gpu.vast_instance_id = str(inst["id"])
                    G.console.print(f"[dim]auto-detected instance id: {gpu.vast_instance_id}[/]")
                    break
            vast.close()
        except G.MichaelError:
            pass

    G.console.print(f"[green]GPU config saved[/] ({gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port})")


def _boot_poll(gpu: "GpuConfig", max_boot: int = 300, poll_s: int = 10) -> None:
    """Poll SSH until the instance responds or timeout. Raises MichaelError on failure."""
    G.console.print("[dim]start requested — waiting for SSH to come up…[/]")
    elapsed = 0
    while elapsed < max_boot:
        time.sleep(poll_s)
        elapsed += poll_s
        try:
            cp = _gpu_ssh_run(gpu, "echo ok", timeout=60)
            if cp.returncode == 0:
                return
            reason = (cp.stderr or "").strip()[:120] or f"rc={cp.returncode}"
        except G.MichaelError as ssh_exc:
            reason = str(ssh_exc)[:120]
        G.console.print(f"[dim]· {elapsed}s — waiting for SSH ({reason})[/]")
    raise G.MichaelError(f"instance did not respond to SSH within {max_boot}s")


def _resume_known_instance(cfg: "Config", gpu: "GpuConfig") -> None:
    """Start a known Vast.ai instance (by vast_instance_id) and wait for SSH."""
    if not cfg.vast_api_key:
        G.console.print(
            f"[dim]no vast_api_key — assuming instance is already running, "
            f"connecting to {gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}…[/]"
        )
        cp = _gpu_ssh_run(gpu, "echo ok", timeout=60)
        if cp.returncode != 0:
            raise G.MichaelError(
                f"GPU unreachable: {cp.stderr.strip()[:200]}\n"
                "Check ssh_key_path in config or try ssh manually."
            )
        return

    G.console.print(f"[dim]checking instance {gpu.vast_instance_id} via Vast.ai API…[/]")
    try:
        vast = VastClient(cfg.vast_api_key)
        info = vast.get(gpu.vast_instance_id)
        vast.close()
    except G.MichaelError as e:
        if "404" in str(e) or "no_such_instance" in str(e):
            G.console.print("[yellow]Instance not found — it was probably destroyed. Clearing stale config…[/]")
            gpu.vast_instance_id = ""
            gpu.ssh_host = ""
            cfg.gpu = gpu
            cfg.save()
            if not _select_vast_instance(cfg, gpu):
                _manual_ssh_setup(cfg, gpu)
            cfg.gpu = gpu
            cfg.save()
            return
        raise G.MichaelError(f"Vast.ai API error: {e}") from e

    # Empty info = instance no longer exists (API returned 200 with no data)
    if not info:
        G.console.print("[yellow]Instance not found — it was probably destroyed. Clearing stale config…[/]")
        gpu.vast_instance_id = ""
        gpu.ssh_host = ""
        cfg.gpu = gpu
        cfg.save()
        if not _select_vast_instance(cfg, gpu):
            _manual_ssh_setup(cfg, gpu)
        cfg.gpu = gpu
        cfg.save()
        return

    status = info.get("actual_status") or info.get("status") or ""
    if status == "running":
        G.console.print(f"[dim]instance already running — reconnecting…[/]")
        try:
            cp = _gpu_ssh_run(gpu, "echo ok", timeout=60)
            ssh_ok = cp.returncode == 0
        except G.MichaelError:
            ssh_ok = False
        if not ssh_ok:
            raise G.MichaelError(
                f"SSH connection timed out or was refused ({gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}).\n\n"
                f"Add your public key to this instance in the Vast.ai console:\n"
                f"  Account → SSH Keys → paste contents of ~/.ssh/id_ed25519.pub\n\n"
                f"Then run `michael gpu` again."
            )
        return

    G.console.print(f"[dim]instance status: {status!r} — starting via Vast.ai API…[/]")
    try:
        vast = VastClient(cfg.vast_api_key)
        vast.start(gpu.vast_instance_id)
        vast.close()
    except G.MichaelError as e:
        if "404" in str(e) or "no_such_instance" in str(e):
            G.console.print("[yellow]Instance not found — it was probably destroyed. Clearing stale config…[/]")
            gpu.vast_instance_id = ""
            gpu.ssh_host = ""
            cfg.gpu = gpu
            cfg.save()
            if not _select_vast_instance(cfg, gpu):
                _manual_ssh_setup(cfg, gpu)
            cfg.gpu = gpu
            cfg.save()
            return
        raise G.MichaelError(f"failed to start instance: {e}") from e

    _boot_poll(gpu)


def _reconnect_ssh_only(cfg: "Config", gpu: "GpuConfig") -> None:
    """Reconnect when ssh_host is known but vast_instance_id is not."""
    G.console.print(f"[dim]connecting to {gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}…[/]")
    try:
        cp = _gpu_ssh_run(gpu, "echo ok", timeout=30)
        ok = cp.returncode == 0
    except G.MichaelError:
        ok = False

    if ok:
        if cfg.vast_api_key:
            try:
                vast = VastClient(cfg.vast_api_key)
                for inst in vast.list():
                    if inst.get("ssh_host") == gpu.ssh_host or inst.get("public_ipaddr") == gpu.ssh_host:
                        gpu.vast_instance_id = str(inst["id"])
                        G.console.print(f"[dim]re-detected instance id: {gpu.vast_instance_id}[/]")
                        break
                vast.close()
            except G.MichaelError:
                pass
        return

    G.console.print("[yellow]SSH unreachable — clearing stale config and re-selecting instance…[/]")
    gpu.ssh_host = ""
    gpu.ssh_port = 22
    gpu.ssh_user = "root"
    gpu.vast_instance_id = ""
    cfg.gpu = gpu
    cfg.save()
    if not _select_vast_instance(cfg, gpu):
        _manual_ssh_setup(cfg, gpu)
    cfg.gpu = gpu
    cfg.save()


def _run_gpu_setup_protocol(cfg: "Config", gpu: "GpuConfig") -> None:
    """Install ollama, start daemon, pull model, save endpoint, print port-forward."""
    # ── Verify SSH before doing anything else ──
    G.console.print(f"[dim]testing SSH connection to {gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}…[/]")
    try:
        cp = _gpu_ssh_run(gpu, "echo ok", timeout=30)
        ssh_ok = cp.returncode == 0
    except G.MichaelError:
        ssh_ok = False
    if not ssh_ok:
        raise G.MichaelError(
            f"SSH connection timed out or was refused ({gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}).\n\n"
            f"Add your public key to this instance in the Vast.ai console:\n"
            f"  Account → SSH Keys → paste contents of ~/.ssh/id_ed25519.pub\n\n"
            f"Then run `michael gpu` again."
        )

    # ── Install ollama if missing ──
    cp = _gpu_ssh_run(gpu, "command -v ollama >/dev/null && echo installed || echo missing")
    if "missing" in cp.stdout:
        G.console.print("[cyan]Installing ollama on the GPU (curl one-liner, ~10 s)…[/]")
        cp = _gpu_ssh_run(
            gpu,
            "curl -fsSL https://ollama.com/install.sh | sh",
            timeout=180,
        )
        if cp.returncode != 0:
            raise G.MichaelError(f"ollama install failed:\n{(cp.stderr or cp.stdout)[:500]}")
        G.console.print("[green]ollama installed[/]")

    # ── Ensure ollama daemon is running ──
    cp = _gpu_ssh_run(gpu, _start_ollama_cmd(gpu), timeout=60)
    pid = cp.stdout.strip().split("\n")[-1]
    if not pid.isdigit():
        raise G.MichaelError(
            f"ollama failed to launch (no PID returned)\n"
            f"stdout: {cp.stdout.strip()!r}\nstderr: {cp.stderr.strip()!r}"
        )
    G.console.print(f"[dim]ollama daemon started: pid={pid}[/]")

    time.sleep(2)
    cp = _gpu_ssh_run(
        gpu,
        f"kill -0 {pid} 2>/dev/null && echo alive || "
        "(echo dead; echo '--- /tmp/ollama.log ---'; cat /tmp/ollama.log 2>/dev/null | head -30)",
        timeout=60,
    )
    if "alive" not in cp.stdout:
        raise G.MichaelError(f"ollama died shortly after launch (pid={pid}):\n{cp.stdout.strip()}")

    # ── Wait for the endpoint to answer ──
    _max_wait_s = 60
    _elapsed = 0
    daemon_ready = False
    while _elapsed < _max_wait_s:
        time.sleep(2)
        _elapsed += 2
        cp = _gpu_ssh_run(
            gpu,
            f"curl -sf http://localhost:{gpu.gpu_port}/v1/models > /dev/null 2>&1 "
            f"&& echo ready || echo down",
            timeout=60,
        )
        if "ready" in cp.stdout:
            daemon_ready = True
            break
    if not daemon_ready:
        disk = _gpu_ssh_run(gpu, "df / | awk 'NR==2{print $5}'", timeout=60).stdout.strip()
        if disk.rstrip("%").isdigit() and int(disk.rstrip("%")) >= 95:
            raise G.MichaelError(
                f"GPU disk is full ({disk} used). Free space and retry:\n"
                f"  ssh -p {gpu.ssh_port} {gpu.ssh_user}@{gpu.ssh_host} "
                f"'rm -rf /root/.ollama/models/ && df -h /'"
            )
        diag = _gpu_ssh_run(
            gpu,
            "echo '--- /tmp/ollama.log ---'; cat /tmp/ollama.log 2>&1; "
            "echo '--- ollama processes ---'; ps -ef | grep -i ollama | grep -v grep; "
            "echo '--- port ---'; ss -tlnp 2>/dev/null | grep "
            f"{gpu.gpu_port} || netstat -tlnp 2>/dev/null | grep {gpu.gpu_port} "
            "|| echo '(nothing listening)'",
            timeout=60,
        ).stdout
        raise G.MichaelError(
            f"ollama daemon did not become ready within {_max_wait_s}s\n{diag.strip()}"
        )

    # ── Pull the model if not already present ──
    cp = _gpu_ssh_run(
        gpu,
        f"ollama list 2>/dev/null | awk 'NR>1 {{print $1}}' | grep -Fxq {gpu.model_repo!r} "
        f"&& echo present || echo missing",
        timeout=60,
    )
    if "missing" in cp.stdout:
        disk_kb = _gpu_ssh_run(gpu, "df / | awk 'NR==2{print $4}'", timeout=60).stdout.strip()
        min_gb = _MODEL_MIN_DISK_GB.get(gpu.model_repo, 30)
        if disk_kb.isdigit() and int(disk_kb) < min_gb * 1_000_000:
            avail_gb = int(disk_kb) // 1_000_000
            raise G.MichaelError(
                f"Not enough disk space to pull {gpu.model_repo} "
                f"(only ~{avail_gb} GB free, need ~{min_gb} GB). Free space and retry:\n"
                f"  ssh -p {gpu.ssh_port} {gpu.ssh_user}@{gpu.ssh_host} "
                f"'rm -rf /root/.ollama/models/ && df -h /'"
            )
        G.console.print(f"[cyan]Pulling model {gpu.model_repo} (this can take a while)…[/]")
        _gpu_ssh_run(
            gpu,
            "rm -f /tmp/ollama_pull.exit && "
            "( nohup bash -c "
            f"'ollama pull {gpu.model_repo} > /tmp/ollama_pull.log 2>&1; "
            "echo $? > /tmp/ollama_pull.exit' "
            "> /dev/null 2>&1 < /dev/null & ) && echo started",
            timeout=60,
        )
        _max_pull_s = 3600
        _poll_s = 15
        _elapsed = 0
        while _elapsed < _max_pull_s:
            time.sleep(_poll_s)
            _elapsed += _poll_s
            cp = _gpu_ssh_run(
                gpu, "cat /tmp/ollama_pull.exit 2>/dev/null || echo running", timeout=180
            )
            done = cp.stdout.strip()
            if done and done != "running":
                rc = int(done) if done.lstrip("-").isdigit() else 1
                if rc != 0:
                    tail = _gpu_ssh_run(
                        gpu, "tail -30 /tmp/ollama_pull.log 2>/dev/null", timeout=60
                    ).stdout
                    raise G.MichaelError(f"ollama pull failed (rc={rc}):\n{tail.strip()}")
                G.console.print(f"[green]model {gpu.model_repo} pulled[/]")
                break
            tail = _ANSI.sub("", _gpu_ssh_run(
                gpu, "tail -1 /tmp/ollama_pull.log 2>/dev/null", timeout=180
            ).stdout.strip().replace("\r", " "))
            G.console.print(
                f"[dim]· {_elapsed}s — {(tail[:120] + '…') if len(tail) > 120 else (tail or 'starting pull…')}[/]"
            )
            append_event("gpu.poll", {"elapsed_s": _elapsed, "phase": "pull"})
        else:
            raise G.MichaelError(
                f"ollama pull did not finish within {_max_pull_s}s. "
                "SSH in and tail /tmp/ollama_pull.log for the real status."
            )

    # ── Save endpoint into models.god so `michael run` works via port forward ──
    endpoint = f"http://localhost:{gpu.gpu_port}/v1"
    if "god" not in cfg.models:
        from michael.config import ModelProfile
        cfg.models["god"] = ModelProfile()
        cfg.default_model = cfg.default_model or "god"
    cfg.models["god"].endpoint = endpoint
    cfg.models["god"].served_model_name = gpu.model_repo
    cfg.save()
    append_event("gpu.ready", {"host": gpu.ssh_host, "model": gpu.model_repo, "endpoint": endpoint})

    pf_cmd = gpu_port_forward_cmd(gpu)
    G.console.print(
        Panel(
            f"[bold green]ollama is ready[/] — {gpu.model_repo}\n\n"
            f"[bold]Open a new terminal and run:[/]\n\n"
            f"  {pf_cmd}\n\n"
            f"[dim]Keep that terminal open. Then use:[/]\n"
            f"  michael run <your prompt>",
            title="port forward",
            border_style="green",
        )
    )


def cmd_gpu() -> None:
    """Smart GPU command: detects state, resumes or initialises, then runs setup protocol."""
    cfg = Config.load()
    gpu = cfg.gpu

    # Always ask which model to run
    gpu.model_repo = _prompt_model_selection(gpu.model_repo)
    cfg.gpu = gpu
    cfg.save()

    if gpu.vast_instance_id:
        _resume_known_instance(cfg, gpu)
    elif gpu.ssh_host:
        _reconnect_ssh_only(cfg, gpu)
    else:
        if not _select_vast_instance(cfg, gpu):
            _manual_ssh_setup(cfg, gpu)
        cfg.gpu = gpu
        cfg.save()

    _run_gpu_setup_protocol(cfg, gpu)


def cmd_gpu_up() -> None:
    """Legacy `gpu up`: ensure SSH config exists, then run setup protocol."""
    cfg = Config.load()
    gpu = cfg.gpu
    if not gpu.ssh_host:
        _manual_ssh_setup(cfg, gpu)
        cfg.gpu = gpu
        cfg.save()
    if gpu.vast_instance_id and cfg.vast_api_key:
        _resume_known_instance(cfg, gpu)
    else:
        G.console.print(f"[dim]connecting to {gpu.ssh_user}@{gpu.ssh_host}:{gpu.ssh_port}…[/]")
        cp = _gpu_ssh_run(gpu, "echo ok", timeout=60)
        if cp.returncode != 0:
            raise G.MichaelError(
                f"GPU unreachable: {cp.stderr.strip()[:200]}\n"
                "Check ssh_key_path in config or try ssh manually."
            )
    _run_gpu_setup_protocol(cfg, gpu)


def cmd_gpu_new() -> None:
    """Wipe per-instance GPU state and run smart `gpu` for a fresh GPU."""
    cfg = Config.load()
    if cfg.gpu.ssh_host or cfg.gpu.vast_instance_id:
        G.console.print(
            f"[dim]clearing previous GPU: {cfg.gpu.ssh_user}@{cfg.gpu.ssh_host}"
            f":{cfg.gpu.ssh_port} (instance {cfg.gpu.vast_instance_id or '—'})[/]"
        )
    cfg.gpu.ssh_host = ""
    cfg.gpu.ssh_port = 22
    cfg.gpu.ssh_user = "root"
    cfg.gpu.vast_instance_id = ""
    for profile in cfg.models.values():
        profile.endpoint = None
        profile.served_model_name = ""
    cfg.save()
    G.console.print("[green]gpu cleared[/]")
    cmd_gpu()


def cmd_gpu_down() -> None:
    cfg = Config.load()
    gpu = cfg.gpu
    if not gpu.ssh_host:
        raise G.MichaelError("no GPU configured — run `michael gpu up` first")

    # Stop ollama via SSH (best-effort — instance may already be off)
    cp = _gpu_ssh_run(
        gpu,
        "systemctl stop ollama 2>/dev/null || pkill -x ollama 2>/dev/null || true",
        timeout=60,
    )
    if cp.returncode == 0:
        G.console.print("[yellow]ollama stopped[/]")
    else:
        G.console.print("[dim]SSH unreachable — skipping ollama stop (instance likely already off)[/]")

    if gpu.vast_instance_id and cfg.vast_api_key:
        vast = VastClient(cfg.vast_api_key)
        try:
            vast.stop(gpu.vast_instance_id)
            G.console.print(f"[yellow]instance {gpu.vast_instance_id} stopped[/]")
            append_event("gpu.stopped", {"host": gpu.ssh_host, "instance_id": gpu.vast_instance_id})
        finally:
            vast.close()
    else:
        G.console.print("[dim]no vast_instance_id or vast_api_key — skipping API stop[/]")
        append_event("gpu.stopped", {"host": gpu.ssh_host})

    if "god" in cfg.models:
        cfg.models["god"].endpoint = None
    cfg.save()


def cmd_status() -> None:
    cfg = Config.load()
    state = replay_global()
    active = get_active_project()
    table = Table(title="michael status", border_style="cyan")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    table.add_row("active project", active.slug if active else "(none)")
    if cfg.vps_active():
        table.add_row("vps", f"{cfg.vps.user}@{cfg.vps.host}:{cfg.vps.port}")
        table.add_row("vps.workspace", cfg.vps.workspace_dir)
    else:
        table.add_row("vps", "[dim]not configured (no sandbox)[/]")

    table.add_row("default model", cfg.default_model or "[dim](first available)[/]")
    if not cfg.models:
        table.add_row("models", "[dim](none — edit config.json)[/]")
    for mname, profile in cfg.models.items():
        st = state.get("models", {}).get(mname, {})
        table.add_row(
            f"  {mname}",
            f"state={st.get('instance_state', 'unknown')}  "
            f"endpoint={st.get('endpoint') or profile.endpoint or '—'}",
        )

    table.add_row("errors (global)", str(state["errors"]))
    G.console.print(table)


def cmd_run(prompt: str) -> None:
    project = require_active_project()
    cfg = Config.load()
    name, profile = cfg.get_model()
    _run_agent_loop(project, cfg, name, profile, prompt, verb_label="run")


def cmd_log(tail: int) -> None:
    project = get_active_project()
    if project:
        events = iter_events(project.events_path)
        title = f"events (project: {project.slug})"
    else:
        events = iter_events(G.GLOBAL_EVENTS_PATH)
        title = "events (global)"
    if not events:
        G.console.print("[dim](no events)[/]")
        return
    last = events[-tail:] if tail > 0 else events
    table = Table(
        title=f"{title} — last {len(last)} of {len(events)}",
        border_style="cyan",
    )
    table.add_column("seq", style="bold", justify="right")
    table.add_column("ts")
    table.add_column("type")
    table.add_column("payload")
    for ev in last:
        payload = json.dumps(ev.get("payload", {}), ensure_ascii=False, sort_keys=True)
        if len(payload) > 80:
            payload = payload[:77] + "..."
        table.add_row(
            str(ev.get("seq", "?")),
            str(ev.get("ts", "?")),
            str(ev.get("type", "?")),
            payload,
        )
    G.console.print(table)


def cmd_inspect() -> None:
    project = require_active_project()
    cfg = Config.load()
    scripture = load_scripture(cfg.scripture_dir)
    header = build_header(project, cfg.resolved_system_prompt(), scripture)
    prompts = _prompt_history_lines(project)
    actions = _action_log_lines(project)
    G.console.print(f"\n[bold cyan]Project:[/] {project.name}  [dim]({project.slug})[/]")
    G.console.print(
        f"[dim]H1 prompts: {len(prompts)} · H3 tool calls: {len(actions)} · "
        f"context size: {len(header):,} chars[/]\n"
    )
    G.console.print(header)


def cmd_undo(list_only: bool = False, trash_id: Optional[str] = None) -> None:
    project = require_active_project()
    if list_only:
        entries = _list_trash(project)
        if not entries:
            G.console.print("(no trash)")
            return
        table = Table(
            title=f"trash for {project.slug} (newest last)",
            border_style="cyan",
        )
        table.add_column("trash_id", style="bold")
        table.add_column("ts")
        table.add_column("tool")
        table.add_column("delta")
        table.add_column("verify")
        for m in entries:
            d = m.get("delta", {}) or {}
            delta_summary = (
                f"+{len(d.get('added', []))} "
                f"~{len(d.get('modified', []))} "
                f"-{len(d.get('removed', []))}"
            )
            v = m.get("verify_rc")
            v_str = "—" if v is None else f"rc={v}"
            table.add_row(
                str(m.get("trash_id", "?")),
                str(m.get("ts", "?")),
                str(m.get("tool", "?")),
                delta_summary,
                v_str,
            )
        G.console.print(table)
        return
    metadata = _undo_one(project, trash_id)
    append_event(
        "tool.undone",
        {
            "trash_id": metadata.get("trash_id"),
            "tool": metadata.get("tool"),
            "summary": metadata.get("summary", ""),
        },
        project=project,
    )
    G.console.print(
        f"[green]undone[/] {metadata.get('tool')} ({metadata.get('trash_id')})"
    )


def cmd_sandbox(file: pathlib.Path, net: bool = False, timeout: int = 30) -> None:
    cfg = Config.load()
    _ssh_preflight(cfg)
    backend = make_backend(cfg)
    project = get_active_project()
    code = pathlib.Path(file).read_text()
    cp = backend.run(code, network=net, timeout_s=timeout, project=project)
    stdout_tail = "\n".join((cp.stdout or "").splitlines()[-80:])
    stderr_tail = "\n".join((cp.stderr or "").splitlines()[-40:])
    G.console.print(
        Panel(
            stdout_tail or "(empty)",
            title=f"stdout (rc={cp.returncode})",
            border_style="green" if cp.returncode == 0 else "red",
        )
    )
    if stderr_tail:
        G.console.print(Panel(stderr_tail, title="stderr", border_style="red"))


def cmd_ssh_test() -> None:
    cfg = Config.load()
    if not cfg.vps_active():
        raise G.MichaelError("vps.host is not configured")
    t0 = time.monotonic()
    cp = subprocess.run(
        _ssh_argv(cfg.vps) + ["echo ok && podman --version 2>/dev/null || true"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    dt = round(time.monotonic() - t0, 3)
    if cp.returncode != 0:
        append_event("ssh.health", {"host": cfg.vps.host, "ok": False, "stderr": cp.stderr[:200]})
        raise G.MichaelError(f"ssh failed in {dt}s: {cp.stderr.strip()[:200]}")
    append_event("ssh.health", {"host": cfg.vps.host, "ok": True, "duration_s": dt})
    G.console.print(
        Panel(
            cp.stdout.strip() or "(no output)",
            title=f"ssh ok in {dt}s — {cfg.vps.user}@{cfg.vps.host}",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Catalog / install / deliver commands
# ---------------------------------------------------------------------------


def cmd_catalog() -> None:
    catalog = load_catalog()
    if not catalog:
        G.console.print("[dim]catalog is empty — deliver a tool first[/]")
        return
    table = Table(title=f"tool catalog ({len(catalog)} tools)", border_style="cyan")
    table.add_column("slug", style="bold")
    table.add_column("description")
    table.add_column("installed", style="green")
    table.add_column("built_at", style="dim")
    for slug, entry in sorted(catalog.items()):
        table.add_row(
            slug,
            str(entry.get("description", "—"))[:60],
            str(entry.get("installed_as") or "—"),
            str(entry.get("built_at", "—"))[:19],
        )
    G.console.print(table)


def cmd_install(slug: Optional[str]) -> None:
    catalog = load_catalog()
    if not catalog:
        raise G.MichaelError("catalog is empty — no tools to install")
    if slug is None:
        proj = get_active_project()
        if not proj:
            raise G.MichaelError("no active project and no slug given")
        slug = proj.slug
    entry = catalog.get(slug)
    if not entry:
        raise G.MichaelError(f"tool {slug!r} not found in catalog")
    deliverable = entry.get("deliverable", "")
    if not deliverable:
        raise G.MichaelError(f"no deliverable path recorded for {slug!r}")
    src = pathlib.Path(deliverable).expanduser()
    if not src.is_file():
        raise G.MichaelError(f"deliverable not found: {src}")
    G.MICHAEL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    link = G.MICHAEL_BIN_DIR / slug
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(src)
    if not src.stat().st_mode & 0o111:
        src.chmod(src.stat().st_mode | 0o755)
    run_cmd = str(link)
    from michael.project import save_catalog
    catalog[slug]["installed_as"] = str(link)
    catalog[slug]["run_cmd"] = run_cmd
    save_catalog(catalog)
    G.console.print(
        Panel(
            f"[bold green]installed[/] {slug}\n"
            f"  symlink: {link} → {src}\n\n"
            f"Add to PATH:\n  export PATH=\"{G.MICHAEL_BIN_DIR}:$PATH\"",
            title="michael install",
            border_style="green",
        )
    )


def cmd_path() -> None:
    p = get_active_project()
    if not p:
        raise G.MichaelError("no active project")
    G.console.print(p.path)


def cmd_deliver() -> None:
    project = require_active_project()
    det = detect_deliverable(project)
    if not det:
        raise G.MichaelError("no deliverable detected in this project (look for main.py, app.py, *.sh, etc.)")
    deliverable, run_cmd = det
    register_deliverable(project, deliverable, run_cmd)
    G.console.print(
        Panel(
            f"[bold green]delivered[/] {deliverable}\n"
            f"installed: [cyan]{G.MICHAEL_BIN_DIR / project.slug}[/]\n\n"
            f"[dim]Add to PATH: export PATH=\"{G.MICHAEL_BIN_DIR}:$PATH\"[/]",
            title="michael deliver",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Tools workspace commands
# ---------------------------------------------------------------------------

_TOOL_DIR_LABELS = [
    ("bundled", pathlib.Path(__file__).parent.parent / "toolbox"),
    ("global",  pathlib.Path(G.GLOBAL_TOOLS_DIR)),
]


def _tool_search_dirs(project_path: str | None) -> list[tuple[str, pathlib.Path]]:
    dirs = list(_TOOL_DIR_LABELS)
    if project_path:
        dirs.append(("project", pathlib.Path(project_path) / "tools"))
    return dirs


def _parse_kv_args(tokens: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise typer.BadParameter(f"expected key=value, got {token!r}")
        k, _, v = token.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _find_tool_file(name: str, project_path: str | None) -> pathlib.Path | None:
    # Project-local takes priority, then global, then bundled.
    search = list(reversed(_tool_search_dirs(project_path)))
    for _label, d in search:
        candidate = d / f"{name}.py"
        if candidate.exists():
            return candidate
    return None


def cmd_tools_list() -> None:
    project_path: str | None = None
    try:
        project_path = require_active_project().path
    except G.MichaelError:
        pass

    import importlib.util as _ilu

    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    # Reverse priority so highest-priority entry wins display
    for label, d in reversed(_tool_search_dirs(project_path)):
        if not d.is_dir():
            continue
        for py_file in sorted(d.glob("*.py")):
            try:
                spec = _ilu.spec_from_file_location(py_file.stem, py_file)
                mod = _ilu.module_from_spec(spec)       # type: ignore[arg-type]
                spec.loader.exec_module(mod)             # type: ignore[union-attr]
                if not hasattr(mod, "TOOL_SCHEMA"):
                    continue
                fn_name = mod.TOOL_SCHEMA.get("function", {}).get("name", py_file.stem)
                if fn_name in seen:
                    continue
                seen.add(fn_name)
                desc = mod.TOOL_SCHEMA.get("function", {}).get("description", "")
                desc = desc.strip().splitlines()[0][:72] if desc else ""
                rows.append((fn_name, desc, label))
            except Exception as exc:
                G.err.print(f"[dim]skipped {py_file.name}: {exc}[/]")

    if not rows:
        G.console.print("[dim]no dynamic tools found[/]")
        return

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False, min_width=60)
    t.add_column("Name", style="cyan", no_wrap=True)
    t.add_column("Description", no_wrap=False)
    t.add_column("Source", style="dim", no_wrap=True)
    for name, desc, label in sorted(rows, key=lambda r: r[0]):
        t.add_row(name, desc, label)
    G.console.print(t)


def cmd_tools_run(name: str, kv_tokens: list[str]) -> None:
    project_path: str | None = None
    try:
        project_path = require_active_project().path
    except G.MichaelError:
        pass

    py_file = _find_tool_file(name, project_path)
    if py_file is None:
        raise G.MichaelError(f"tool {name!r} not found in any toolbox directory")

    try:
        args = _parse_kv_args(kv_tokens)
    except typer.BadParameter as e:
        raise G.MichaelError(str(e)) from e

    result = _dispatch_dynamic_tool_from_path(name, args, py_file)
    G.console.print(result)


def cmd_tools_show(name: str) -> None:
    project_path: str | None = None
    try:
        project_path = require_active_project().path
    except G.MichaelError:
        pass

    py_file = _find_tool_file(name, project_path)
    if py_file is None:
        raise G.MichaelError(f"tool {name!r} not found in any toolbox directory")

    from rich.syntax import Syntax
    G.console.print(f"[dim]{py_file}[/]")
    G.console.print(Syntax(py_file.read_text(), "python", line_numbers=True))


# ---------------------------------------------------------------------------
# Typer command bindings
# ---------------------------------------------------------------------------


@app.command(name="init")
def init_cmd() -> None:
    """Write stub config, create workbench dirs, inject shell integration. Idempotent."""
    cmd_init()


@app.command(name="show")
def show_cmd() -> None:
    """List projects."""
    cmd_show()


@app.command(name="new")
def new_cmd(
    name: Optional[str] = typer.Argument(None, help="Project name."),
) -> None:
    """Create a new project."""
    cmd_new(name)


@app.command(name="use")
def use_cmd(slug: str = typer.Argument(...)) -> None:
    """Set the active project."""
    cmd_use(slug)


@app.command(name="current")
def current_cmd() -> None:
    """Print the active project."""
    cmd_current()


@app.command(name="config")
def config_cmd() -> None:
    """Open the global config file in $EDITOR (with help panel)."""
    cmd_config()


@gpu_app.callback(invoke_without_command=True)
def gpu_callback(ctx: typer.Context) -> None:
    """Smart GPU management: detect state, resume or initialise, run setup protocol."""
    if ctx.invoked_subcommand is None:
        cmd_gpu()


@gpu_app.command("up")
def gpu_up_cmd() -> None:
    """Start the Vast.ai instance, install ollama if missing, pull the model, print port-forward."""
    cmd_gpu_up()


@gpu_app.command("new")
def gpu_new_cmd() -> None:
    """Swap to a new GPU: clears the cached SSH/instance state then runs `gpu`."""
    cmd_gpu_new()


@gpu_app.command("down")
def gpu_down_cmd() -> None:
    """Stop ollama and pause the Vast.ai instance via API."""
    cmd_gpu_down()


@app.command(name="status")
def status_cmd() -> None:
    """Show derived state from the event log."""
    cmd_status()


@app.command(name="run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run_cmd(
    prompt: list[str] = typer.Argument(None, help="Prompt — every word after 'run' is the prompt."),
) -> None:
    """Run the agent on a prompt. Everything after 'run' is the prompt.

    Example: michael run fix the auth bug in login.py
    """
    text = " ".join(prompt or []).strip()
    if not text:
        G.err.print("michael run requires a prompt. Example: michael run fix the login bug")
        raise typer.Exit(1)
    cmd_run(text)


@app.command(name="log")
def log_cmd(
    tail: int = typer.Option(20, "--tail", "-n", help="How many events to show."),
) -> None:
    """Show the project event log (or global if no project active)."""
    cmd_log(tail)


@app.command(name="inspect")
def inspect_cmd() -> None:
    """Print the full H1–H4 context package the model will receive on the next run."""
    cmd_inspect()


@app.command(name="sandbox")
def sandbox_cmd(
    file: pathlib.Path = typer.Argument(..., exists=True, readable=True),
    net: bool = typer.Option(False, "--net", help="Allow bridge networking."),
    timeout: int = typer.Option(30, help="Wall-clock timeout in seconds."),
) -> None:
    """Run a Python file in the sandbox (local or VPS depending on config)."""
    cmd_sandbox(file, net, timeout)


@app.command(name="undo")
def undo_cmd(
    list_only: bool = typer.Option(False, "--list", "-l", help="List trash entries."),
    trash_id: Optional[str] = typer.Argument(None, help="Specific trash id to undo."),
) -> None:
    """Restore the most recent (or named) staged change."""
    cmd_undo(list_only=list_only, trash_id=trash_id)


@app.command(name="ssh-test")
def ssh_test_cmd() -> None:
    """Verify the VPS is reachable and report the SSH handshake time."""
    cmd_ssh_test()


@app.command(name="catalog")
def catalog_cmd() -> None:
    """List all delivered tools in the global catalog."""
    cmd_catalog()


@app.command(name="path")
def path_cmd() -> None:
    """Print the active project's workspace path (useful for cd $(michael path))."""
    cmd_path()


@app.command(name="deliver")
def deliver_cmd() -> None:
    """Detect, register, and install the active project's deliverable."""
    cmd_deliver()


@app.command(name="install", hidden=True)
def install_cmd(
    slug: Optional[str] = typer.Argument(None, help="Tool slug to reinstall."),
) -> None:
    """Reinstall a delivered tool's wrapper script (repair command)."""
    cmd_install(slug)


@tools_app.command(name="list")
def tools_list_cmd() -> None:
    """List all dynamic tools across bundled, global, and project toolboxes."""
    cmd_tools_list()


@tools_app.command(
    name="run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tools_run_cmd(
    name: str = typer.Argument(..., help="Tool name to invoke."),
    ctx: typer.Context = typer.Option(None, hidden=True),
) -> None:
    """Run a dynamic tool by name. Pass arguments as key=value pairs."""
    cmd_tools_run(name, ctx.args if ctx else [])


@tools_app.command(name="show")
def tools_show_cmd(
    name: str = typer.Argument(..., help="Tool name to inspect."),
) -> None:
    """Print the source code of a dynamic tool."""
    cmd_tools_show(name)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

REPL_COMMANDS = {
    "project", "new", "run", "gpu", "config", "init",
    "tools", "quit", "exit", "help",
}


def _config_is_unset() -> bool:
    if not G.GLOBAL_CONFIG_PATH.is_file():
        return True
    try:
        cfg = Config.load()
    except G.MichaelError:
        return True
    if not cfg.vast_api_key:
        return True
    return not any(p.vast_instance_id for p in cfg.models.values())


class MichaelCompleter(Completer):
    """Tab-completion for the REPL."""

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        at_boundary = text.endswith(" ") or not text

        if not words or (len(words) == 1 and not at_boundary):
            prefix = words[0] if words else ""
            for cmd in sorted(REPL_COMMANDS):
                if cmd.startswith(prefix):
                    yield Completion(cmd, start_position=-len(prefix))
            return

        head = words[0]
        if head == "project":
            prefix = words[1] if len(words) > 1 and not at_boundary else ""
            for p in list_projects():
                if p.slug.startswith(prefix):
                    yield Completion(p.slug, start_position=-len(prefix))
            return


def repl() -> None:
    G.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(G.REPL_HISTORY_PATH)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=MichaelCompleter(),
        complete_while_typing=False,
    )
    G.console.print("[bold cyan]michael[/] [dim]— event-sourced LLM loop[/]")
    if _config_is_unset():
        G.console.print(
            "[yellow]setup required[/] [dim]type: config[/]"
        )
    while True:
        try:
            line = session.prompt("michael> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            continue
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        try:
            dispatch_repl(line)
        except G.MichaelError as e:
            G.err.print(f"michael: {escape(str(e))}")
        except typer.Abort:
            G.err.print("aborted")
        except KeyboardInterrupt:
            G.err.print("interrupted")


def _opt_value(rest: list[str], *flags: str) -> Optional[str]:
    for f in flags:
        if f in rest:
            i = rest.index(f)
            if i + 1 < len(rest):
                return rest[i + 1]
    return None


def dispatch_repl(line: str) -> None:
    try:
        parts = shlex.split(line)
    except ValueError as e:
        G.err.print(f"parse error: {e}")
        return
    if not parts:
        return
    cmd, rest = parts[0], parts[1:]

    if cmd == "help":
        G.console.print(
            "commands:\n"
            "  run <prompt>                      run the agent on a prompt\n"
            "  project [slug]                    select/list projects\n"
            "  new [name]                        create new project\n"
            "  up / down                         start/stop GPU (legacy — needs config.json)\n"
            "  gpu up / gpu down                 start instance, install ollama, pull model\n"
            "  tools list                        list all dynamic tools\n"
            "  tools run <name> [key=value ...]  run a dynamic tool directly\n"
            "  tools show <name>                 print tool source\n"
            "  catalog                           list all delivered tools\n"
            "  path                              print active project workspace path\n"
            "  deliver                           detect + install active project's deliverable\n"
            "  config                            edit config\n"
            "  init                              initialize config + shell integration\n"
            "  upgrade                           git pull + re-apply shell integration\n"
            "  exit / quit                       exit michael"
        )
        return

    if cmd == "init":
        cmd_init()
    elif cmd == "config":
        cmd_config()
    elif cmd == "project":
        if rest:
            cmd_use(rest[0])
        else:
            cmd_show()
    elif cmd == "new":
        name = " ".join(rest) if rest else None
        cmd_new(name)
    elif cmd == "run":
        if not rest:
            G.err.print("run requires a prompt. Example: run fix the auth bug")
            return
        cmd_run(" ".join(rest))
    elif cmd == "gpu":
        sub = rest[0] if rest else ""
        if sub == "up":
            cmd_gpu_up()
        elif sub == "new":
            cmd_gpu_new()
        elif sub == "down":
            cmd_gpu_down()
        else:
            G.err.print("usage: gpu up | gpu new | gpu down")
    elif cmd == "tools":
        sub = rest[0] if rest else "list"
        if sub == "list":
            cmd_tools_list()
        elif sub == "run":
            if len(rest) < 2:
                G.err.print("usage: tools run <name> [key=value ...]")
            else:
                cmd_tools_run(rest[1], rest[2:])
        elif sub == "show":
            if len(rest) < 2:
                G.err.print("usage: tools show <name>")
            else:
                cmd_tools_show(rest[1])
        else:
            G.err.print("usage: tools list | tools run <name> [key=value ...] | tools show <name>")
    elif cmd == "catalog":
        cmd_catalog()
    elif cmd == "path":
        cmd_path()
    elif cmd == "deliver":
        cmd_deliver()
    else:
        G.err.print(f"unknown command: {cmd!r}. try 'help'.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    G.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        if len(sys.argv) == 1:
            repl()
        else:
            app()
    except G.MichaelError as e:
        G.err.print(f"michael: {escape(str(e))}")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        G.err.print(f"command failed (exit {e.returncode})")
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        G.err.print("interrupted")
        sys.exit(130)
