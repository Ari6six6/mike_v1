"""delegate — send a code-generation task to the junior model.

Hermes writes a task description and a test snippet; this tool calls the
junior model (models.junior profile), extracts the generated code, appends
the test, runs the combined script in a subprocess, and feeds any failure
back to the junior for another attempt.  Loops up to max_tries times.

Config required:
    models.junior.endpoint          e.g. "http://localhost:11434/v1"
    models.junior.served_model_name e.g. "qwen2.5-coder:7b"
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
from typing import Any

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Delegate a code-generation task to the junior model. "
            "Write a precise task description and a Python test snippet. "
            "The junior generates code; the test is appended and run in a subprocess. "
            "On failure the traceback goes back to the junior. Loops until the test "
            "passes or max_tries is exhausted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Exact task for the junior: what to implement, expected function "
                        "signatures, inputs, outputs, edge cases.  Be precise — the junior "
                        "has no other context."
                    ),
                },
                "test_code": {
                    "type": "string",
                    "description": (
                        "Python code appended after the generated code to verify it. "
                        "Should raise AssertionError (or any exception) on failure and "
                        "exit cleanly on success.  May call any name the generated code defines."
                    ),
                },
                "max_tries": {
                    "type": "integer",
                    "description": "Max attempts before giving up (default 5).",
                },
            },
            "required": ["task", "test_code"],
        },
    },
}

_CODE_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    m = _CODE_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def delegate(task: str, test_code: str, max_tries: int = 5, **_: Any) -> str:
    from michael.backends import LLMClient
    from michael.config import Config

    cfg = Config.load()

    if "junior" not in cfg.models:
        return (
            "error: no 'junior' model profile found — add models.junior with "
            "endpoint and served_model_name to config.json"
        )
    profile = cfg.models["junior"]
    if not profile.endpoint:
        return (
            "error: models.junior.endpoint is not set — run `michael gpu up` or "
            "set it manually in config.json"
        )
    if not profile.served_model_name:
        return "error: models.junior.served_model_name is not set in config.json"

    client = LLMClient(profile.endpoint)
    timeout = float(profile.request_timeout_s or 120)

    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are a focused code-generation assistant. "
                "Return ONLY a Python code block inside ```python ... ``` fences. "
                "No explanation, no prose — just the code."
            ),
        },
        {"role": "user", "content": f"Task:\n{task}"},
    ]

    last_code = ""

    for attempt in range(1, max_tries + 1):
        try:
            resp = client.chat.completions.create(
                model=profile.served_model_name,
                messages=messages,
                timeout=timeout,
            )
        except Exception as exc:
            return f"error calling junior model: {exc}"

        raw = (resp.choices[0].content or "").strip()
        last_code = _extract_code(raw)

        combined = last_code + "\n\n" + test_code

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="/tmp/delegate_", delete=False
        ) as f:
            f.write(combined)
            tmp = f.name

        try:
            proc = subprocess.run(
                ["python3", tmp],
                capture_output=True,
                text=True,
                timeout=30,
            )
            returncode = proc.returncode
            out = (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            returncode = 1
            out = "execution timed out after 30 s"
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)

        if returncode == 0:
            return (
                f"PASS on attempt {attempt}/{max_tries}\n\n"
                f"```python\n{last_code}\n```"
            )

        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Attempt {attempt}/{max_tries} failed.\n\n"
                    f"Error output:\n{out or '(no output)'}\n\n"
                    "Fix the code and return only a ```python ... ``` block."
                ),
            }
        )

    return (
        f"FAIL — junior did not pass the test in {max_tries} attempts.\n\n"
        f"Last generated code:\n```python\n{last_code}\n```"
    )
