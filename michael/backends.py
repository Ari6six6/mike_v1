"""SSH helpers, Vast.ai client, LLM client, and sandbox backends."""
from __future__ import annotations

import atexit
import dataclasses
import json as _json
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

import httpx

import michael.globals as G
from michael.config import Config, GpuConfig, ModelProfile, SandboxConfig, VpsConfig

if TYPE_CHECKING:
    from michael.project import Project


# ---------------------------------------------------------------------------
# Event helpers (lazy import to avoid circular dependency)
# ---------------------------------------------------------------------------


def append_event(event_type: str, payload: dict, *, project=None) -> None:
    from michael.project import append_event as _ae
    _ae(event_type, payload, project=project)


# ---------------------------------------------------------------------------
# VPS SSH helpers
# ---------------------------------------------------------------------------


def _ssh_argv(vps: VpsConfig) -> list[str]:
    args = [
        "ssh",
        "-o", f"ControlMaster=auto",
        "-o", f"ControlPath={G.STATE_DIR}/ssh-%r@%h:%p.sock",
        "-o", f"ControlPersist={vps.control_persist}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-i", os.path.expanduser(vps.ssh_key_path),
        "-p", str(vps.port),
        f"{vps.user}@{vps.host}",
    ]
    return args


def _ssh_preflight(cfg: Config) -> None:
    if not cfg.vps_active():
        return
    cp = subprocess.run(
        _ssh_argv(cfg.vps) + ["podman --version"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if cp.returncode != 0:
        raise G.MichaelError(
            f"podman not available on VPS {cfg.vps.host}. "
            "Run `bash bootstrap.sh` on the VPS to install it."
        )


# ---------------------------------------------------------------------------
# GPU SSH helpers
# ---------------------------------------------------------------------------


def parse_vast_ssh_cmd(ssh_str: str) -> tuple[str, str, int]:
    """Parse a Vast.ai SSH string and return (user, host, port)."""
    try:
        tokens = shlex.split(ssh_str)
    except ValueError as e:
        raise G.MichaelError(f"could not parse SSH command: {e}") from e

    tokens = [t for t in tokens if t != "ssh"]
    port = 22
    user_host: Optional[str] = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-p" and i + 1 < len(tokens):
            try:
                port = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if tok.startswith("-p") and len(tok) > 2:
            try:
                port = int(tok[2:])
            except ValueError:
                pass
            i += 1
            continue
        if tok.startswith("-") and len(tok) == 2:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if "@" in tok:
            user_host = tok
        i += 1

    if not user_host:
        raise G.MichaelError(
            f"could not find user@host in SSH command: {ssh_str!r}"
        )
    parts = user_host.split("@", 1)
    return parts[0], parts[1], port


def _gpu_ssh_argv(gpu: GpuConfig) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={G.GPU_KNOWN_HOSTS_PATH}",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-i", os.path.expanduser(gpu.ssh_key_path),
        "-p", str(gpu.ssh_port),
        f"{gpu.ssh_user}@{gpu.ssh_host}",
    ]


def _gpu_ssh_run(
    gpu: GpuConfig, cmd: str, *, timeout: int = 60
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _gpu_ssh_argv(gpu) + [cmd],
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise G.MichaelError(f"GPU command timed out after {timeout}s")


def gpu_port_forward_cmd(gpu: GpuConfig) -> str:
    key = os.path.expanduser(gpu.ssh_key_path)
    return (
        f"ssh -p {gpu.ssh_port} {gpu.ssh_user}@{gpu.ssh_host} "
        f"-L {gpu.gpu_port}:localhost:{gpu.gpu_port} "
        f"-N -o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile={G.GPU_KNOWN_HOSTS_PATH} -i {key}"
    )


def _start_ollama_cmd(gpu: GpuConfig) -> str:
    """Background ollama serve detached from the SSH session, print its PID.

    Three statements, semicolon-separated, no chaining cleverness:
      1. pkill any existing ollama serve (ignored if none)
      2. touch the log so we can prove the redirect ran even if ollama crashes
      3. nohup the daemon with full std-stream redirection, echo the PID
    The caller is responsible for verifying the PID is still alive after a
    short sleep — that's done in a separate SSH call so SSH session timing
    can't affect the verification.
    """
    return (
        "pkill -x ollama 2>/dev/null; "
        "touch /tmp/ollama.log; "
        f"OLLAMA_HOST=0.0.0.0:{gpu.gpu_port} "
        "nohup ollama serve >/tmp/ollama.log 2>&1 </dev/null & "
        "echo $!"
    )


# Resolve a Python interpreter on the GPU. Vast.ai images commonly ship only
# `python3` (no bare `python`), so hardcoding `python` makes the launch die
# with "nohup: failed to run command 'python'". On top of that, the Vast.ai
# PyTorch template installs the CUDA torch stack into a specific env (conda or
# a venv) that a non-interactive SSH shell does not put on PATH — so we prefer
# the interpreter that can already `import torch` (vLLM needs it, and reusing
# the prebuilt CUDA torch avoids pip pulling a mismatched build), falling back
# to the first usable python. Prepend this to any remote command that needs the
# interpreter and reference it as "$PY"; installing AND launching with the same
# "$PY" guarantees vLLM is importable where we start it. POSIX-sh compatible.
_GPU_PY = (
    'PY=""; '
    'for _c in python3 python /opt/conda/bin/python /venv/main/bin/python '
    '/usr/local/bin/python3 /usr/bin/python3; do '
    '_p="$(command -v "$_c" 2>/dev/null)"; '
    '[ -z "$_p" ] && [ -x "$_c" ] && _p="$_c"; '
    '[ -z "$_p" ] && continue; '
    '[ -z "$PY" ] && PY="$_p"; '
    'if "$_p" -c "import torch" >/dev/null 2>&1; then PY="$_p"; break; fi; '
    'done; '
)


def _stop_vllm_cmd() -> str:
    """Kill any running vLLM server.

    This MUST run in its own SSH session — never chained ahead of the launch
    command. `pkill -f` matches against the *full command line* of every
    process, and the launch command's own argv contains the server module
    path, so chaining `pkill -f` with the launch makes pkill kill the very
    shell that is about to `echo $!` — the bug behind "vLLM failed to launch
    (no PID returned)". The `[v]llm` bracket is the standard self-exclusion
    trick: the regex matches a real `vllm.…` process but not this command's
    own argv (which contains the literal text `[v]llm.…`). The trailing
    `true` keeps the exit status 0 when no server was running.
    """
    return "pkill -f '[v]llm.entrypoints.openai.api_server' 2>/dev/null; true"


def _gpu_vllm_dtype(gpu: GpuConfig) -> Optional[str]:
    """Return 'half' for pre-Ampere GPUs (compute capability < 8.0), else None.

    Qwen3 and most modern checkpoints default to bfloat16, which vLLM refuses on
    Turing/Volta cards (cc < 8.0) — the engine raises "Engine core
    initialization failed" within seconds of launch. Those GPUs do support
    float16, so we force `--dtype half` there. On Ampere+ we return None and let
    vLLM keep the checkpoint's native dtype.
    """
    cp = _gpu_ssh_run(
        gpu,
        "nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1",
        timeout=30,
    )
    raw = cp.stdout.strip().split("\n")[-1].strip()
    try:
        cap = float(raw)
    except ValueError:
        return None
    return "half" if 0 < cap < 8.0 else None


def _start_vllm_cmd(gpu: GpuConfig, ngpu: int = 1, dtype: Optional[str] = None) -> str:
    """Background vLLM api_server detached from the SSH session, print its PID.

    Two statements, semicolon-separated:
      1. touch the log so we can prove the redirect ran even if vllm crashes
      2. nohup the server with full std-stream redirection, echo the PID

    `dtype`, when given, is passed as `--dtype` (e.g. "half" for pre-Ampere
    GPUs that can't run bfloat16 — see `_gpu_vllm_dtype`).

    Killing a prior server is intentionally NOT done here — call
    `_stop_vllm_cmd` in a separate SSH session first. See `_stop_vllm_cmd`
    for why the two must never be chained in one shell.
    """
    dtype_flag = f"--dtype {dtype} " if dtype else ""
    return (
        "touch /tmp/vllm.log; "
        + _GPU_PY +
        f'nohup "$PY" -m vllm.entrypoints.openai.api_server '
        f"--model {gpu.model_repo} "
        f"--port {gpu.gpu_port} "
        f"--host 0.0.0.0 "
        f"--tensor-parallel-size {ngpu} "
        f"{dtype_flag}"
        f">/tmp/vllm.log 2>&1 </dev/null & "
        "echo $!"
    )


def _restart_vllm_on_gpu(gpu: GpuConfig, *, poll_timeout_s: int = 1800) -> None:
    """Restart the vLLM server on the GPU and poll until ready.

    Default timeout is 30 minutes because the first start downloads the model
    from HuggingFace before the endpoint becomes healthy.
    """
    G.console.print("[yellow]restarting vLLM on GPU...[/]")
    ngpu_cp = _gpu_ssh_run(
        gpu,
        "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l",
        timeout=30,
    )
    ngpu_str = ngpu_cp.stdout.strip()
    ngpu = int(ngpu_str) if ngpu_str.isdigit() and int(ngpu_str) > 0 else 1

    dtype = _gpu_vllm_dtype(gpu)
    _gpu_ssh_run(gpu, _stop_vllm_cmd(), timeout=30)
    cp = _gpu_ssh_run(gpu, _start_vllm_cmd(gpu, ngpu, dtype), timeout=60)
    pid = cp.stdout.strip().split("\n")[-1]
    if not pid.isdigit():
        raise G.MichaelError(
            f"vLLM failed to launch (no PID returned)\n"
            f"stdout: {cp.stdout.strip()!r}\nstderr: {cp.stderr.strip()!r}"
        )
    elapsed = 0
    while elapsed < poll_timeout_s:
        time.sleep(15)
        elapsed += 15
        cp = _gpu_ssh_run(
            gpu,
            f"curl -sf http://localhost:{gpu.gpu_port}/v1/models > /dev/null 2>&1 "
            f"&& echo ready || echo down",
            timeout=60,
        )
        if "ready" in cp.stdout:
            G.console.print("[green]vLLM is ready[/]")
            return
        # Fail fast if the engine died instead of waiting out the full timeout.
        live = _gpu_ssh_run(
            gpu, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", timeout=30
        )
        if "alive" not in live.stdout:
            log = _gpu_ssh_run(gpu, "tail -40 /tmp/vllm.log 2>&1", timeout=30).stdout
            raise G.MichaelError(
                f"vLLM engine exited during startup (pid={pid}):\n{log.strip()}"
            )
        tail_cp = _gpu_ssh_run(gpu, "tail -2 /tmp/vllm.log 2>/dev/null", timeout=30)
        tail_line = tail_cp.stdout.strip().replace("\r", " ")
        G.console.print(f"[dim]· {elapsed}s — {tail_line[:120] or 'waiting for vLLM…'}[/]")
    raise G.MichaelError(
        f"vLLM did not come up within {poll_timeout_s}s — "
        "check /tmp/vllm.log on the GPU"
    )


def _restart_ollama_on_gpu(gpu: GpuConfig, *, poll_timeout_s: int = 300) -> None:
    """Restart the ollama daemon on the GPU and poll until ready."""
    G.console.print("[yellow]restarting ollama on GPU...[/]")
    cp = _gpu_ssh_run(gpu, _start_ollama_cmd(gpu), timeout=60)
    pid = cp.stdout.strip().split("\n")[-1]
    if not pid.isdigit():
        raise G.MichaelError(
            f"ollama failed to launch (no PID returned)\n"
            f"stdout: {cp.stdout.strip()!r}\nstderr: {cp.stderr.strip()!r}"
        )
    elapsed = 0
    while elapsed < poll_timeout_s:
        time.sleep(5)
        elapsed += 5
        cp = _gpu_ssh_run(
            gpu,
            f"curl -sf http://localhost:{gpu.gpu_port}/v1/models > /dev/null 2>&1 "
            f"&& echo ready || echo down",
            timeout=60,
        )
        if "ready" in cp.stdout:
            G.console.print("[green]ollama is ready[/]")
            return
    raise G.MichaelError(
        f"ollama did not come up within {poll_timeout_s}s — "
        "check /tmp/ollama.log or `journalctl -u ollama` on the GPU"
    )


_tunnel_proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]


def _close_tunnel() -> None:
    global _tunnel_proc
    if _tunnel_proc is not None:
        try:
            _tunnel_proc.terminate()
        except Exception:
            pass
        _tunnel_proc = None


def _ensure_tunnel(gpu: GpuConfig) -> None:
    """Auto-spawn SSH port-forward if the model endpoint is not locally reachable.

    Safe to call on every `michael run` — if the tunnel is already up (user-managed
    or from a previous auto-start) it returns immediately. When Termux is killed and
    restarted, this re-establishes the tunnel without any manual step.
    """
    global _tunnel_proc
    if not gpu.ssh_host:
        return
    endpoint = f"http://localhost:{gpu.gpu_port}/v1"
    if _ping_endpoint(endpoint):
        return  # already reachable — nothing to do
    G.console.print("[yellow]tunnel not detected — starting SSH port-forward...[/]")
    key = os.path.expanduser(gpu.ssh_key_path)
    _tunnel_proc = subprocess.Popen(
        [
            "ssh", "-p", str(gpu.ssh_port), f"{gpu.ssh_user}@{gpu.ssh_host}",
            "-L", f"{gpu.gpu_port}:localhost:{gpu.gpu_port}",
            "-N",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={G.GPU_KNOWN_HOSTS_PATH}",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-i", key,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(_close_tunnel)
    for _ in range(15):
        time.sleep(2)
        if _ping_endpoint(endpoint):
            G.console.print("[green]tunnel up[/]")
            return
    _close_tunnel()
    raise G.MichaelError(
        "SSH tunnel failed to come up after 30s — check gpu.ssh_host / ssh_key_path in config"
    )


# ---------------------------------------------------------------------------
# Vast.ai API client
# ---------------------------------------------------------------------------


class VastClient:
    base = "https://console.vast.ai/api/v0"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise G.MichaelError("vast_api_key is not set")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def _wrap(self, fn_name: str, request) -> Any:
        try:
            r = request()
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            body = (e.response.text or "")[:200]
            msg = f"vast {fn_name}: HTTP {e.response.status_code} — {body}"
            append_event("error", {"where": fn_name, "msg": msg})
            raise G.MichaelError(msg) from e
        except httpx.HTTPError as e:
            msg = f"vast {fn_name}: {e}"
            append_event("error", {"where": fn_name, "msg": msg})
            raise G.MichaelError(msg) from e

    def get(self, inst_id: str | int) -> dict[str, Any]:
        data = self._wrap(
            "get",
            lambda: self._client.get(f"{self.base}/instances/{inst_id}/"),
        )
        return data.get("instances", {}) or {}

    def start(self, inst_id: str | int) -> dict[str, Any]:
        return self._wrap(
            "start",
            lambda: self._client.put(
                f"{self.base}/instances/{inst_id}/", json={"state": "running"}
            ),
        )

    def stop(self, inst_id: str | int) -> dict[str, Any]:
        return self._wrap(
            "stop",
            lambda: self._client.put(
                f"{self.base}/instances/{inst_id}/", json={"state": "stopped"}
            ),
        )

    def list(self) -> list[dict[str, Any]]:
        data = self._wrap("list", lambda: self._client.get(f"{self.base}/instances/"))
        return data.get("instances", []) or []

    def endpoint_for(self, inst_id: str | int, port: int) -> Optional[str]:
        info = self.get(inst_id)
        if not info:
            return None
        ip = info.get("public_ipaddr") or info.get("ssh_host")
        if not ip:
            return None
        return f"http://{ip}:{port}/v1"


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _ToolCall:
    id: str
    name: str
    arguments: str


@dataclasses.dataclass
class _Choice:
    content: Optional[str]
    tool_calls: Optional[list[_ToolCall]]
    finish_reason: Optional[str]


@dataclasses.dataclass
class _CompletionResponse:
    choices: list[_Choice]
    usage: Optional[dict]


class _Completions:
    def __init__(
        self, endpoint: str, http: httpx.Client, headers: dict, enable_thinking: bool = False
    ) -> None:
        self._endpoint = endpoint
        self._http = http
        self._headers = headers
        self._enable_thinking = enable_thinking

    def create(
        self,
        *,
        model: str,
        messages: list,
        tools: Optional[list] = None,
        tool_choice: Optional[Any] = None,
        stream: bool = False,
        timeout: float = 60.0,
        stream_options: Optional[dict] = None,
        **_kw: Any,
    ) -> Any:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if tools:
            body["tools"] = tools
        if self._enable_thinking and not any(m.get("role") == "tool" for m in messages):
            body["chat_template_kwargs"] = {"enable_thinking": True}
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if stream and stream_options:
            body["stream_options"] = stream_options
        if stream:
            return self._stream_iter(body, timeout)
        r = self._http.post(
            f"{self._endpoint}/chat/completions",
            json=body,
            timeout=timeout,
        )
        r.raise_for_status()
        try:
            data = r.json()
        except Exception as exc:
            raise G.MichaelError(
                f"model server returned non-JSON response: {exc} — body: {r.text[:200]!r}"
            ) from exc
        return self._parse_response(data)

    def _stream_iter(self, body: dict, timeout: float):
        client = httpx.Client(
            headers=self._headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        try:
            with client.stream(
                "POST", f"{self._endpoint}/chat/completions", json=body
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        try:
                            yield self._parse_chunk(_json.loads(line[6:]))
                        except (_json.JSONDecodeError, KeyError):
                            continue
        finally:
            client.close()

    def _parse_response(self, data: dict) -> _CompletionResponse:
        choices = []
        for c in data.get("choices", []):
            m = c.get("message", {})
            tcs = m.get("tool_calls") or []
            tool_calls: Optional[list] = [
                _ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", ""),
                )
                for tc in tcs
            ] or None
            choices.append(
                _Choice(
                    content=m.get("content"),
                    tool_calls=tool_calls,
                    finish_reason=c.get("finish_reason"),
                )
            )
        return _CompletionResponse(choices=choices, usage=data.get("usage"))

    def _parse_chunk(self, data: dict) -> _Choice:
        c = data.get("choices", [{}])[0]
        delta = c.get("delta", {})
        tcs = delta.get("tool_calls") or []
        tool_calls: Optional[list] = [
            _ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments", ""),
            )
            for tc in tcs
        ] or None
        return _Choice(
            content=delta.get("content"),
            tool_calls=tool_calls,
            finish_reason=c.get("finish_reason"),
        )


class _ChatCompletions:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions


class LLMClient:
    def __init__(self, endpoint: str, api_key: str = "", enable_thinking: bool = False) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(headers=headers, timeout=120)
        _completions = _Completions(endpoint, self._http, headers, enable_thinking)
        self.chat = _ChatCompletions(_completions)

    def close(self) -> None:
        self._http.close()


def llm_client(endpoint: str, api_key: str = "", enable_thinking: bool = False) -> LLMClient:
    return LLMClient(endpoint, api_key, enable_thinking)


def _require_endpoint(profile: ModelProfile, name: str) -> str:
    if not profile.endpoint:
        raise G.MichaelError(
            f"model '{name}' has no endpoint — run `michael up` or `michael gpu up` first"
        )
    return profile.endpoint


def _ping_endpoint(endpoint: str, *, timeout_s: float = 5.0) -> bool:
    try:
        r = httpx.get(f"{endpoint}/models", timeout=timeout_s)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sandbox backends
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int


class SandboxBackend(ABC):
    @abstractmethod
    def run(
        self,
        code: str,
        *,
        network: bool = False,
        timeout_s: int = 30,
        project=None,
    ) -> SandboxResult:
        ...


class LocalPodmanBackend(SandboxBackend):
    def __init__(self, cfg: SandboxConfig) -> None:
        self._cfg = cfg

    def run(
        self,
        code: str,
        *,
        network: bool = False,
        timeout_s: int = 30,
        project=None,
    ) -> SandboxResult:
        cfg = self._cfg
        if not shutil.which("podman"):
            raise G.MichaelError(
                "podman not found on PATH. "
                "Install podman locally, or configure a remote VPS sandbox:\n"
                "  michael config  →  set vps.host to your VPS IP"
            )
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False
        ) as f:
            f.write(code)
            tmp = f.name
        try:
            cmd = [
                "podman", "run", "--rm",
                "--memory", f"{cfg.memory_mb}m",
                "--cpus", str(cfg.cpus),
                "--pids-limit", str(cfg.pids),
                "--network", "bridge" if network else "none",
                "-v", f"{tmp}:/sandbox/script.py:ro",
                cfg.image,
                "python3", "/sandbox/script.py",
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                )
                return SandboxResult(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )
            except subprocess.TimeoutExpired:
                raise G.MichaelError(f"sandbox timed out after {timeout_s}s") from None
            except FileNotFoundError:
                raise G.MichaelError("podman not found — cannot run sandbox locally.") from None
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)


class SubprocessBackend(SandboxBackend):
    """No-isolation fallback: runs code directly with the host python3."""

    def __init__(self, timeout_s: int) -> None:
        self._timeout_s = timeout_s

    def run(
        self,
        code: str,
        *,
        network: bool = False,
        timeout_s: int = 30,
        project=None,
    ) -> SandboxResult:
        effective = timeout_s or self._timeout_s
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp = f.name
        try:
            try:
                result = subprocess.run(
                    ["python3", tmp],
                    capture_output=True,
                    text=True,
                    timeout=effective,
                    check=False,
                )
                return SandboxResult(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )
            except subprocess.TimeoutExpired:
                raise G.MichaelError(f"sandbox timed out after {effective}s") from None
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)


class RemotePodmanBackend(SandboxBackend):
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def _stage(self, code: str, remote_path: str) -> None:
        cfg = self._cfg
        vps = cfg.vps
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False
        ) as f:
            f.write(code)
            tmp = f.name
        try:
            scp_cmd = [
                "scp",
                "-o", f"ControlMaster=auto",
                "-o", f"ControlPath={G.STATE_DIR}/ssh-%r@%h:%p.sock",
                "-o", f"ControlPersist={vps.control_persist}",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-i", os.path.expanduser(vps.ssh_key_path),
                "-P", str(vps.port),
                tmp,
                f"{vps.user}@{vps.host}:{remote_path}",
            ]
            cp_stage = subprocess.run(
                scp_cmd, capture_output=True, text=True, timeout=30, check=False
            )
            if cp_stage.returncode != 0:
                raise G.MichaelError(f"remote stage failed: {cp_stage.stderr[:200]}")
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)

    def run(
        self,
        code: str,
        *,
        network: bool = False,
        timeout_s: int = 30,
        project=None,
    ) -> SandboxResult:
        cfg = self._cfg
        _ssh_preflight(cfg)
        vps = cfg.vps
        sb = cfg.sandbox
        run_id = uuid.uuid4().hex[:8]
        remote_script = f"{vps.workspace_dir}/run_{run_id}.py"
        self._stage(code, remote_script)
        net_flag = "bridge" if network else "none"
        podman_cmd = (
            f"podman run --rm "
            f"--memory {sb.memory_mb}m "
            f"--cpus {sb.cpus} "
            f"--pids-limit {sb.pids} "
            f"--network {net_flag} "
            f"-v {remote_script}:/sandbox/script.py:ro "
            f"{sb.image} python3 /sandbox/script.py"
        )
        try:
            cp = subprocess.run(
                _ssh_argv(vps) + [podman_cmd],
                capture_output=True,
                text=True,
                timeout=timeout_s + 15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise G.MichaelError(f"sandbox timed out after {timeout_s}s") from None
        cleanup_cmd = f"rm -f {remote_script}"
        subprocess.run(
            _ssh_argv(vps) + [cleanup_cmd],
            capture_output=True, timeout=10, check=False,
        )
        return SandboxResult(
            stdout=cp.stdout,
            stderr=cp.stderr,
            returncode=cp.returncode,
        )


def make_backend(cfg: Config) -> SandboxBackend:
    if cfg.sandbox.passthrough:
        return SubprocessBackend(cfg.sandbox.timeout_s)
    if cfg.vps_active():
        return RemotePodmanBackend(cfg)
    return LocalPodmanBackend(cfg.sandbox)
