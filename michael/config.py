"""Config dataclasses: ModelProfile, VpsConfig, SandboxConfig, Config."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import michael.globals as G


@dataclass
class ModelProfile:
    """One model served behind an OpenAI-compatible endpoint (Ollama)."""

    vast_instance_id: str = ""
    served_model_name: str = ""  # the tag to send in API requests, e.g. "qwen2.5:72b"
    request_timeout_s: int = 120
    endpoint: Optional[str] = None
    enable_thinking: bool = False


@dataclass
class VpsConfig:
    """Remote VPS that runs rootless Podman for sandbox execution."""

    host: str = ""
    port: int = 22
    user: str = "michael"
    ssh_key_path: str = "~/.ssh/id_ed25519"
    workspace_dir: str = "/home/michael/workspace"
    control_persist: str = "10m"


@dataclass
class SandboxConfig:
    image: str = "michael-sandbox:alpine"
    memory_mb: int = 384
    cpus: float = 1.5
    pids: int = 128
    timeout_s: int = 30


@dataclass
class GpuConfig:
    """Direct SSH + Ollama config for a rented GPU."""

    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key_path: str = "~/.ssh/id_ed25519"
    vast_instance_id: str = ""
    model_repo: str = "qwen2.5:72b"  # Ollama tag; also supported: "qwen3:32b"
    gpu_port: int = 11434  # Ollama's default OpenAI-compatible port


@dataclass
class Config:
    vast_api_key: str = ""
    models: dict[str, ModelProfile] = field(default_factory=dict)
    default_model: str = ""
    vps: VpsConfig = field(default_factory=VpsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    gpu: GpuConfig = field(default_factory=GpuConfig)
    system_prompt: str = G.DEFAULT_SYSTEM_PROMPT
    system_prompt_file: str = ""
    log_responses: bool = True
    boot_poll_s: int = 10
    scripture_dir: str = "scripture"
    workbench_root: str = "~/workbench"

    @classmethod
    def load(cls) -> "Config":
        data: dict[str, Any] = {}
        if G.GLOBAL_CONFIG_PATH.is_file():
            try:
                data = json.loads(G.GLOBAL_CONFIG_PATH.read_text())
            except json.JSONDecodeError as e:
                raise G.MichaelError(f"config.json is not valid JSON: {e}") from e

        if v := os.environ.get("VAST_API_KEY"):
            data["vast_api_key"] = v
        if v := os.environ.get("MICHAEL_DEFAULT_MODEL"):
            data["default_model"] = v

        models_raw = data.pop("models", {}) or {}
        models: dict[str, ModelProfile] = {}
        valid_mp = set(ModelProfile.__dataclass_fields__)
        for name, prof in models_raw.items():
            if isinstance(prof, dict):
                models[name] = ModelProfile(**{k: v for k, v in prof.items() if k in valid_mp})

        vps_raw = data.pop("vps", None) or {}
        valid_vps = set(VpsConfig.__dataclass_fields__)
        vps = VpsConfig(**{k: v for k, v in vps_raw.items() if k in valid_vps})

        sb_raw = data.pop("sandbox", None) or {}
        valid_sb = set(SandboxConfig.__dataclass_fields__)
        sandbox = SandboxConfig(**{k: v for k, v in sb_raw.items() if k in valid_sb})

        gpu_raw = data.pop("gpu", None) or {}
        valid_gpu = set(GpuConfig.__dataclass_fields__)
        gpu = GpuConfig(**{k: v for k, v in gpu_raw.items() if k in valid_gpu})

        valid = set(cls.__dataclass_fields__) - {"models", "vps", "sandbox", "gpu"}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(models=models, vps=vps, sandbox=sandbox, gpu=gpu, **clean)

    def save(self) -> None:
        G.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        G.GLOBAL_CONFIG_PATH.write_text(
            json.dumps(_diff_from_default(self, Config()), indent=2, sort_keys=True)
        )
        os.chmod(G.GLOBAL_CONFIG_PATH, 0o600)

    def get_model(self, name: Optional[str] = None) -> tuple[str, ModelProfile]:
        if not self.models:
            raise G.MichaelError(
                "no model profiles configured — run `config` and add a 'models' entry"
            )
        chosen = name or self.default_model or next(iter(self.models))
        if chosen not in self.models:
            raise G.MichaelError(
                f"unknown model profile: {chosen!r}. Available: {sorted(self.models)}"
            )
        return chosen, self.models[chosen]

    def resolved_system_prompt(self) -> str:
        if self.system_prompt_file:
            p = pathlib.Path(self.system_prompt_file).expanduser()
            if p.is_file():
                return p.read_text()
        return self.system_prompt

    def vps_active(self) -> bool:
        return bool(self.vps and self.vps.host)


def _diff_from_default(obj: Any, default: Any) -> Any:
    """Recursively prune a dataclass against a default instance of the same type.

    Returns a dict containing only fields whose values differ from the default.
    Nested dataclasses are pruned recursively; if a nested object equals its
    default entirely, it is omitted. Dicts whose values are dataclasses (e.g.
    Config.models) keep their keys but each value is pruned individually.
    """
    if not dataclasses.is_dataclass(obj):
        return obj
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        cur = getattr(obj, f.name)
        dflt = getattr(default, f.name)
        if dataclasses.is_dataclass(cur) and dataclasses.is_dataclass(dflt):
            pruned = _diff_from_default(cur, dflt)
            if pruned:
                out[f.name] = pruned
        elif isinstance(cur, dict):
            pruned_dict: dict[str, Any] = {}
            for k, v in cur.items():
                if dataclasses.is_dataclass(v):
                    pruned_dict[k] = _diff_from_default(v, type(v)())
                else:
                    pruned_dict[k] = v
            if pruned_dict != dflt:
                out[f.name] = pruned_dict
        elif cur != dflt:
            out[f.name] = cur
    return out


def make_stub_config() -> Config:
    """Minimal starting point: one empty 'god' model profile so the agent loop
    has something to dispatch to. Every other field uses its dataclass default;
    save-time pruning keeps the on-disk file to just what the user (or
    `michael gpu up`) has actually written.
    """
    return Config(models={"god": ModelProfile()}, default_model="god")


CONFIG_HELP: dict[str, str] = {
    "vast_api_key": "Vast.ai console API key.",
    "default_model": "Profile name to use (default: 'god').",
    "models.god.vast_instance_id": "Numeric ID of the rented GPU instance.",
    "models.god.served_model_name": "Model tag to send in API requests, e.g. 'qwen2.5:72b'. Auto-filled by `michael gpu up` from gpu.model_repo.",
    "models.god.request_timeout_s": "LLM request timeout (seconds).",
    "gpu.model_repo": "Ollama model tag, e.g. 'qwen2.5:72b' or 'llama3.1:70b'.",
    "gpu.gpu_port": "Ollama OpenAI-compat port on the GPU (default 11434).",
    "vps.host": "VPS public IP/hostname (empty = no remote sandbox).",
    "vps.user": "SSH user (default: michael).",
    "vps.ssh_key_path": "Path to private key (default: ~/.ssh/id_ed25519).",
    "vps.workspace_dir": "Default workspace dir on the VPS.",
    "sandbox.image": "Tag of the sandbox image built by bootstrap.sh.",
    "sandbox.memory_mb": "Sandbox memory cap in MB.",
    "sandbox.cpus": "Sandbox CPU cap.",
    "sandbox.pids": "Sandbox PID cap.",
    "sandbox.timeout_s": "Default sandbox timeout (seconds).",
    "system_prompt": "Default system prompt for the agent loop.",
    "system_prompt_file": "If set, read system prompt from this file.",
    "log_responses": "If true, log full LLM responses to events.jsonl.",
    "boot_poll_s": "Poll interval while waiting for the inference server to come up.",
    "scripture_dir": "Path to scripture files (relative to repo root, default 'scripture').",
}
