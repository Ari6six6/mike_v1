"""Agent loop: flat tool loop with explicit commit_changes gate."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from typing import Any

import michael.globals as G
from michael.backends import (
    LocalPodmanBackend,
    _close_tunnel,
    _ensure_tunnel,
    _ping_endpoint,
    _require_endpoint,
    _restart_ollama_on_gpu,
    _restart_vllm_on_gpu,
    _ssh_preflight,
    llm_client,
    make_backend,
)
from michael.config import Config, ModelProfile
from michael.project import Project, append_event
import subprocess

from michael.tools import (
    PendingChanges,
    TOOLS,
    COMMIT_SENTINEL,
    commit_pending,
    dispatch_tool_call,
)
from michael.utils import build_header, load_scripture

_MAX_TOOL_RESULT_CHARS = 8_000


_TAGS_RE = re.compile(r'TOOL_TAGS\s*=\s*\[([^\]]+)\]')


def _file_passes_mode_filter(py_file: pathlib.Path, mode: str) -> bool:
    """Check TOOL_TAGS via text scan before importing — avoids import side-effects."""
    try:
        text = py_file.read_text(errors="replace")
    except OSError:
        return False
    m = _TAGS_RE.search(text)
    if m is None:
        return True  # no TOOL_TAGS = available in all modes
    raw = [t.strip().strip("'\"") for t in m.group(1).split(",")]
    return mode in raw


def _load_dynamic_tools(project_path: str, mode: str = "recon") -> list[dict[str, Any]]:
    """Load tool schemas from the global toolbox and the project-local tools/ dir.

    Bundled and global tools are filtered by TOOL_TAGS (text-scanned before
    import). Project-local tools/<project>/tools/ are always loaded — they are
    project-specific by design and not mode-gated.

    Global tools (~/.michael/toolbox/) load first; a project-local tool with
    the same name overrides the global one.
    """
    seen: dict[str, dict[str, Any]] = {}  # name → schema, later entries win
    bundled = pathlib.Path(__file__).parent.parent / "toolbox"
    global_box = pathlib.Path(G.GLOBAL_TOOLS_DIR)
    project_box = pathlib.Path(project_path) / "tools"

    for tools_dir, apply_filter in [
        (bundled, True),       # bundled: filter by TOOL_TAGS
        (global_box, True),    # user global: filter by TOOL_TAGS
        (project_box, False),  # project-local: always load
    ]:
        if not tools_dir.exists():
            continue
        for py_file in sorted(tools_dir.glob("*.py")):
            if apply_filter and not _file_passes_mode_filter(py_file, mode):
                continue
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                if hasattr(mod, "TOOL_SCHEMA"):
                    name = mod.TOOL_SCHEMA.get("function", {}).get("name", py_file.stem)
                    seen[name] = mod.TOOL_SCHEMA
            except Exception as exc:
                G.err.print(f"[dim]dynamic tool load failed ({py_file.name}): {exc}[/]")
    return list(seen.values())


def _is_conn_refused(exc: Exception) -> bool:
    for e in (exc, getattr(exc, '__cause__', None)):
        if e is None:
            continue
        if isinstance(e, ConnectionRefusedError):
            return True
        if isinstance(e, OSError) and getattr(e, 'errno', None) == 111:
            return True
    return False


def _probe_deliverable(project: Project, run_cmd: str) -> tuple[bool, str]:
    """Run the deliverable with --help; return (success, output)."""
    try:
        cp = subprocess.run(
            ["bash", "-c", f"{run_cmd} --help"],
            cwd=project.path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        out = (cp.stdout or "")[:500] + (cp.stderr or "")[:200]
        return cp.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def _write_news(project: Project, content: str) -> None:
    """Persist the agent's last response to NEWS.md for next-run continuity."""
    if not content:
        return
    p = pathlib.Path(project.path) / "NEWS.md"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n")
    except OSError:
        pass


def _rescue_staged(project: Project, pending: PendingChanges) -> None:
    """If the loop exits with uncommitted staged changes, prompt the user to save them."""
    if pending.stage_root is None or not pending.change_log:
        pending.discard()
        return
    import typer
    G.console.print(
        f"[yellow]⚠  {len(pending.change_log)} staged change(s) were never committed:[/]"
    )
    for entry in pending.change_log:
        path = entry.get("args", {}).get("path", "?")
        G.console.print(f"[dim]  {entry['tool']} → {path}[/]")
    if typer.confirm("Commit staged changes now?", default=True):
        from rich.panel import Panel
        from michael.project import detect_deliverable, register_deliverable
        commit_pending(project, pending)
        det = detect_deliverable(project)
        if det:
            deliverable, run_cmd = det
            ok, probe_out = _probe_deliverable(project, run_cmd)
            if ok:
                register_deliverable(project, deliverable, run_cmd)
                installed = G.MICHAEL_BIN_DIR / project.slug
                G.console.print(Panel(
                    f"[bold]{deliverable}[/]\n"
                    f"installed: [cyan]{installed}[/]\n\n"
                    f"[dim]{probe_out[:300]}[/]",
                    title="⚡ Committed + Delivered (rescued)",
                    border_style="green",
                ))
                return
        G.console.print(Panel("Done.", title="⚡ Committed (rescued)", border_style="green"))
    else:
        pending.discard()
        G.console.print("[dim]staged changes discarded[/]")


def _run_agent_loop(
    project: Project,
    cfg: Config,
    name: str,
    profile: ModelProfile,
    prompt: str,
    *,
    verb_label: str = "run",
) -> None:
    """Run one prompt through a flat tool loop. The LLM iterates freely with all
    tools available and calls commit_changes() when done."""
    endpoint = _require_endpoint(profile, name)
    _ssh_preflight(cfg)

    if cfg.gpu.ssh_host:
        _ensure_tunnel(cfg.gpu)
        if not _ping_endpoint(endpoint):
            if cfg.gpu.inference_backend == "vllm":
                G.console.print("[yellow]model server unreachable — restarting vLLM...[/]")
                _restart_vllm_on_gpu(cfg.gpu)
            else:
                G.console.print("[yellow]model server unreachable — restarting ollama...[/]")
                _restart_ollama_on_gpu(cfg.gpu)

    client = llm_client(endpoint, "", profile.enable_thinking)
    backend = make_backend(cfg)
    G.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    base_prompt = cfg.resolved_system_prompt()

    backend_label = (
        "remote-podman (vps)" if cfg.vps_active()
        else ("local-podman" if isinstance(backend, LocalPodmanBackend)
              else "no-sandbox")
    )
    G.console.print(
        f"[bold cyan]michael {verb_label}[/] [dim]project={project.slug}  "
        f"model={name}  sandbox={backend_label}[/]"
    )
    G.console.print(f"[dim]Flat loop · up to {G.MAX_AGENT_TURNS} turns · Ctrl-C aborts[/]")

    append_event(
        "agent.started",
        {"model": name, "served": profile.served_model_name, "sandbox": backend_label},
        project=project,
    )
    append_event(
        "prompt.sent",
        {"prompt": prompt, "model": name, "served": profile.served_model_name},
        project=project,
    )

    scripture = load_scripture(cfg.scripture_dir, mode=project.mode)
    header = build_header(project, base_prompt, scripture)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": header},
        {"role": "user", "content": prompt},
    ]
    pending = PendingChanges()
    dynamic = _load_dynamic_tools(project.path, mode=project.mode)
    if dynamic:
        names = ", ".join(d["function"]["name"] for d in dynamic if "function" in d)
        G.console.print(f"[dim]loaded {len(dynamic)} dynamic tool(s): {names}[/]")
    all_tools = TOOLS + dynamic

    last_content: str = ""
    try:
        for turn in range(1, G.MAX_AGENT_TURNS + 1):
            G.console.print(f"[dim]· turn {turn}[/]")
            for _attempt in range(2):
                try:
                    resp = client.chat.completions.create(
                        model=profile.served_model_name,
                        messages=messages,
                        tools=all_tools,
                        tool_choice="auto",
                        stream=False,
                        timeout=float(profile.request_timeout_s),
                    )
                    break
                except Exception as _conn_exc:
                    if _attempt == 0 and _is_conn_refused(_conn_exc) and cfg.gpu.ssh_host:
                        G.console.print("[yellow]LLM connection lost — reconnecting tunnel...[/]")
                        _close_tunnel()
                        _ensure_tunnel(cfg.gpu)
                    else:
                        raise
            choice = resp.choices[0]
            content = choice.content or ""
            if content:
                last_content = content

            if content:
                payload: dict[str, Any] = {"chars": len(content), "turn": turn}
                if cfg.log_responses:
                    payload["text"] = content
                append_event("assistant.message", payload, project=project)

            tool_calls = choice.tool_calls or []
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                # LLM responded without calling a tool — natural loop exit.
                if content:
                    G.console.print(content)
                _write_news(project, last_content)
                _rescue_staged(project, pending)
                append_event("agent.ended", {"model": name, "turns": turn}, project=project)
                return

            committed = False
            for tc in tool_calls:
                G.console.print(f"[dim]· tool {tc.name}[/]")
                try:
                    targs = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    targs = {}
                if tc.name in ("write_file", "apply_patch") and "path" in targs:
                    G.console.print(f"[dim]  → {targs['path']}[/]")
                result = dispatch_tool_call(
                    tc.name, targs, project, cfg, backend, pending
                )
                if result == COMMIT_SENTINEL:
                    committed = True
                    # Still append so the message list is well-formed if we continued.
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": "Changes committed."})
                else:
                    if len(result) > _MAX_TOOL_RESULT_CHARS:
                        result = result[:_MAX_TOOL_RESULT_CHARS] + f"\n… [truncated {len(result) - _MAX_TOOL_RESULT_CHARS} chars]"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            if committed:
                _write_news(project, last_content)
                from rich.panel import Panel
                from michael.project import detect_deliverable, register_deliverable

                det = detect_deliverable(project)
                if det:
                    deliverable, run_cmd = det
                    ok, probe_out = _probe_deliverable(project, run_cmd)
                    if ok:
                        register_deliverable(project, deliverable, run_cmd)
                        installed = G.MICHAEL_BIN_DIR / project.slug
                        G.console.print(Panel(
                            f"[bold]{deliverable}[/]\n"
                            f"installed: [cyan]{installed}[/]\n\n"
                            f"[dim]{probe_out[:300]}[/]\n\n"
                            f"[dim]Add to PATH: export PATH=\"{G.MICHAEL_BIN_DIR}:$PATH\"[/]",
                            title="⚡ Committed + Delivered",
                            border_style="green",
                        ))
                        append_event(
                            "tool.executed",
                            {"tool": "deliver", "summary": f"delivered {deliverable}", "run_cmd": run_cmd},
                            project=project,
                        )
                    else:
                        G.console.print(Panel(
                            f"[yellow]{deliverable}[/] — probe failed\n\n[dim]{probe_out[:400]}[/]\n\n"
                            "Run [bold]michael run '<fix the issue>'[/] to repair and re-deliver.",
                            title="⚡ Committed (verify failed)",
                            border_style="yellow",
                        ))
                else:
                    G.console.print(Panel("Done.", title="⚡ Committed", border_style="green"))

                append_event(
                    "agent.ended", {"model": name, "committed": True, "turns": turn},
                    project=project,
                )
                return

    except KeyboardInterrupt:
        pending.discard()
        G.err.print("\nturn aborted by user; pending changes discarded")
        append_event("agent.aborted", {}, project=project)
        append_event("agent.ended", {"model": name, "aborted": True}, project=project)
        return
    except Exception as exc:
        G.err.print(f"LLM error: {exc}")
        append_event("error", {"where": "agent_loop", "msg": str(exc)}, project=project)
        pending.discard()
        append_event("agent.ended", {"model": name, "error": True}, project=project)
        return

    # Reached max turns without commit_changes
    from rich.panel import Panel
    G.console.print(
        Panel(
            "Max turns reached without commit_changes being called.\n\n"
            "[dim]Run [bold]michael run '<what you need>'[/bold] to continue.[/dim]",
            title=f"⏸  Turn limit ({G.MAX_AGENT_TURNS})",
            border_style="yellow",
        )
    )
    _write_news(project, last_content)
    _rescue_staged(project, pending)
    append_event(
        "agent.ended",
        {"model": name, "turns": G.MAX_AGENT_TURNS, "committed": False},
        project=project,
    )
