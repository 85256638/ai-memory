"""Activate ai-memory's independent ChatGPT OAuth provider after a real probe."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import activate_relay as maintenance

OPS = Path(__file__).resolve().parent
DATA = OPS.parent
def chatgpt_settings() -> dict:
    config = OPS / "chatgpt-provider.toml"
    source = tomllib.loads((config if config.is_file() else DATA / "config.toml").read_text("utf-8-sig"))
    model = source.get("llm_model", "")
    if not model or model.startswith("REPLACE_"):
        raise RuntimeError("Configure an available model in chatgpt-provider.toml first.")
    return {"llm_provider": "openai-oauth", "llm_model": model,
            "llm_reasoning_effort": source.get("llm_reasoning_effort", "medium")}


SETTINGS = chatgpt_settings()


def candidate_config() -> str:
    text = (DATA / "config.toml").read_text("utf-8-sig")
    boundary = re.search(r"(?m)^\s*\[", text)
    position = boundary.start() if boundary else len(text)
    root, rest = text[:position], text[position:]
    root = re.sub(r"(?m)^llm_base_url\s*=.*\r?\n?", "", root)
    for key, value in SETTINGS.items():
        pattern = rf"(?m)^{key}\s*=.*$"
        replacement = f"{key} = {json.dumps(value)}"
        if re.search(pattern, root):
            root = re.sub(pattern, lambda _: replacement, root)
        else:
            root += "\n" + replacement + "\n"
    root = root.replace(
        "# Relay key loaded by ops/service_entry.ps1.",
        "# Independent ChatGPT OAuth; ai-memory owns its data-dir auth.json.",
    ).replace(
        "# Relay setup preserved in ops.",
        "# Independent ChatGPT OAuth; ai-memory owns its data-dir auth.json.",
    )
    candidate = root + rest
    parsed = tomllib.loads(candidate)
    if any(parsed.get(k) != v for k, v in SETTINGS.items()) or "llm_base_url" in parsed:
        raise RuntimeError("ChatGPT candidate configuration validation failed.")
    return candidate


def preserve_relay() -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    folder = OPS / "provider-snapshots" / f"relay-{stamp}"
    folder.mkdir(parents=True)
    files = [
        "relay-provider.toml", "relay-api-key.txt", "load_relay_key.ps1",
        "activate_relay.py", "activate_relay.ps1", "service_entry.ps1",
        "start_native_host.ps1", "relay_diagnostics_gateway.py",
        "llm_task_diagnostics.py", "relay-diagnostics.enabled",
        "watch_ai_memory.py", "README.md",
    ]
    shutil.copy2(DATA / "config.toml", folder / "config.toml")
    if (DATA / "config.toml").read_bytes() != (folder / "config.toml").read_bytes():
        raise RuntimeError("Relay configuration backup verification failed.")
    for name in files:
        source = OPS / name
        if source.exists():
            shutil.copy2(source, folder / name)
    # Verify copied bytes without printing credentials or their digest.
    for name in files:
        source, copied = OPS / name, folder / name
        if source.exists() and hashlib.sha256(source.read_bytes()).digest() != (
                hashlib.sha256(copied.read_bytes()).digest()):
            raise RuntimeError("Relay backup verification failed.")
    (folder / "RESTORE.md").write_text(
        "# Private provider snapshot\n\n"
        "This local snapshot may contain credentials. Keep it private.\n"
        "Review relay-provider.toml, then run activate_relay.ps1 to test, "
        "back up, restart and verify the provider. Do not overwrite a future "
        "config.toml with this snapshot.\n", encoding="utf-8",
    )
    return folder


def preflight(candidate: str) -> None:
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("AI_MEMORY_LLM_") or name in (
                "LLM_API_KEY", "OPENCODE_API_KEY", "OPENAI_API_KEY"):
            environment.pop(name)
    # This provider reads DATA/auth.json and refreshes its own OAuth tokens.
    # It does not use CODEX_HOME, Codex's API key, or a versioned Codex binary.
    with tempfile.TemporaryDirectory(prefix="chatgpt-preflight-", dir=OPS) as scratch:
        config = Path(scratch) / "config.toml"
        config.write_text(candidate, encoding="utf-8")
        result = maintenance.run_cli([
            "--config", str(config), "llm-test", "--provider", SETTINGS["llm_provider"],
            "--model", SETTINGS["llm_model"], "--structured",
            "--prompt", "Return a JSON object with answer equal to OK.",
        ], environment)
    if result.returncode:
        # Do not persist arbitrary provider stderr or any credential fields.
        detail = (result.stderr + result.stdout).lower()
        reason = ("authentication rejected" if "401" in detail or "auth" in detail else
                  "request failed")
        raise RuntimeError(f"ChatGPT OAuth preflight {reason}; active service unchanged.")
    try:
        payload = json.loads(result.stdout[result.stdout.index("{"):])
        if payload.get("answer", "").strip().upper() != "OK":
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("ChatGPT OAuth structured-output check failed; service unchanged.") from None


def main() -> None:
    candidate = candidate_config()
    active = tomllib.loads((DATA / "config.toml").read_text("utf-8-sig"))
    if active.get("llm_provider") == SETTINGS["llm_provider"]:
        if any(active.get(k) != v for k, v in SETTINGS.items()) or "llm_base_url" in active:
            raise RuntimeError("A different ChatGPT OAuth configuration is active; no changes applied.")
        preflight(candidate)
        result = maintenance.run_cli(["llm-test", "--server"])
        if result.returncode:
            raise RuntimeError("Client probe passed, but the running service probe failed.")
        print("Independent ChatGPT OAuth is configured; client and server probes passed.")
        return
    if active.get("llm_provider") not in ("codex", "opencode", "openai-compat"):
        raise RuntimeError("Unexpected active provider; no changes applied.")
    if active.get("llm_provider") in ("opencode", "openai-compat"):
        backup = preserve_relay()
        print(f"Relay configuration and credential backup verified: {backup}", flush=True)
    print("Testing independent ChatGPT OAuth with a fixed structured prompt.", flush=True)
    preflight(candidate)
    print("ChatGPT OAuth structured probe passed; applying and verifying the live service.", flush=True)
    maintenance.apply(candidate, SETTINGS, label="Independent ChatGPT OAuth")
    print("ChatGPT OAuth activated. Codex's own login and relay setup are unchanged.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else
              f"Switch stopped ({type(error).__name__}); credentials not printed.")
        sys.exit(1)
