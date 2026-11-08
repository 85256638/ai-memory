"""Apply the prepared relay only after a credential-isolated structured probe."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request

import llm_task_diagnostics as task_diagnostics

OPS = Path(__file__).resolve().parent
DATA = OPS.parent
CONFIG = DATA / "config.toml"
from windows_paths import EXE
PS = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def prepared_config() -> tuple[str, dict]:
    settings = tomllib.loads((OPS / "relay-provider.toml").read_text("utf-8-sig"))
    required = {"llm_provider", "llm_base_url", "llm_model", "llm_reasoning_effort"}
    if set(settings) != required or settings["llm_provider"] not in ("opencode", "openai-compat"):
        raise RuntimeError("Prepared provider settings are invalid.")
    if settings["llm_base_url"] != task_diagnostics.upstream_base():
        raise RuntimeError("Prepared relay URL differs from the configured HTTPS URL.")
    if ((OPS / "relay-diagnostics.enabled").exists()
            and settings["llm_provider"] == "openai-compat"):
        settings["llm_base_url"] = task_diagnostics.LOCAL_BASE
    text = CONFIG.read_text("utf-8-sig")
    root_end = re.search(r"(?m)^\s*\[", text)
    boundary = root_end.start() if root_end else len(text)
    root, rest = text[:boundary], text[boundary:]
    for key, value in settings.items():
        line = f"{key} = {json.dumps(value)}"
        pattern = rf"(?m)^{key}\s*=.*$"
        if re.search(pattern, root):
            root = re.sub(pattern, lambda _: line, root)
        else:
            root += "\n" + line + "\n"
    root = root.replace(
        "# Codex retains ownership of authentication.",
        "# Relay key loaded by ops/service_entry.ps1.",
    )
    candidate = root + rest
    parsed = tomllib.loads(candidate)
    if any(parsed.get(key) != value for key, value in settings.items()):
        raise RuntimeError("Prepared TOML validation failed.")
    return candidate, settings


def run_cli(arguments: list[str], environment: dict | None = None,
            timeout: int = 330) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(EXE), "--data-dir", str(DATA), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW, env=environment,
    )


def probe(candidate: str, settings: dict, key: str,
          base_url: str | None = None) -> None:
    environment = os.environ.copy()
    # Do not let the launching desktop's LLM overrides replace the candidate.
    for name in list(environment):
        if name.startswith("AI_MEMORY_LLM_"):
            environment.pop(name)
    key_variable = "LLM_API_KEY" if settings["llm_provider"] == "openai-compat" else "OPENCODE_API_KEY"
    environment[key_variable] = key
    with tempfile.TemporaryDirectory(prefix="relay-probe-", dir=OPS) as temporary:
        probe_config = Path(temporary) / "config.toml"
        probe_config.write_text(candidate, encoding="utf-8")
        result = run_cli([
            "--config", str(probe_config), "llm-test",
            "--provider", settings["llm_provider"], "--model", settings["llm_model"],
            "--base-url", base_url or settings["llm_base_url"],
            "--structured", "--prompt", "Return a JSON object with answer equal to OK.",
        ], environment)
    # Captured provider errors can contain arbitrary upstream content. Never
    # print or persist them, or place the key in a command-line argument.
    if result.returncode:
        raise RuntimeError(
            "Structured relay probe failed. Check the API key, relay access, "
            "model availability and strict JSON-schema support. Service unchanged."
        )
    try:
        payload = json.loads(result.stdout[result.stdout.index("{"):])
        if payload.get("answer", "").strip().upper() != "OK":
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Structured probe returned an unexpected result. Service unchanged.") from None


def start_host() -> None:
    result = subprocess.run(
        [str(PS), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-File", str(OPS / "start_native_host.ps1")],
        capture_output=True, timeout=20, creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Native service launch failed.")


def wait_online() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    until = time.monotonic() + 25
    while time.monotonic() < until:
        try:
            with opener.open("http://127.0.0.1:49374/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError("Service did not become healthy within 25 seconds.")


def stop_host() -> None:
    host_file = DATA / "service-host.json"
    if not host_file.exists():
        # A separately managed serve process must not be treated as our host.
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:49374/healthz", timeout=2):
                raise RuntimeError("Service is online without its host record; no stop attempted.")
        except OSError:
            return
    (DATA / "service-host.stop").write_text("relay-provider-change", encoding="ascii")
    until = time.monotonic() + 20
    while host_file.exists() and time.monotonic() < until:
        time.sleep(0.2)
    if host_file.exists():
        raise RuntimeError("Host did not stop cooperatively; no forced termination attempted.")


def apply(candidate: str, settings: dict, label: str = "Relay") -> None:
    flag = OPS / "maintenance.flag"
    with flag.open("x", encoding="ascii") as stream:
        stream.write("relay-provider-change")
    original = CONFIG.read_bytes()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = OPS / f"config-before-relay-{stamp}.toml"
    changed = False
    stopped = False
    try:
        backup.write_bytes(original)
        stop_host()
        stopped = True
        temporary = CONFIG.with_suffix(".relay.tmp")
        temporary.write_text(candidate, encoding="utf-8")
        os.replace(temporary, CONFIG)
        changed = True
        start_host()
        wait_online()
        result = run_cli(["status", "--json"], timeout=25)
        state = json.loads(result.stdout) if result.returncode == 0 else {}
        llm = state.get("providers", {}).get("llm", {})
        if (llm.get("provider"), llm.get("model")) != (
                settings["llm_provider"], settings["llm_model"]):
            raise RuntimeError("Running service provider/model verification failed.")
        result = run_cli(["llm-test", "--server"])
        if result.returncode:
            raise RuntimeError("The restarted service's own provider probe failed.")
        report = json.loads(result.stdout[result.stdout.index("{"):])
        if report.get("ok") is not True:
            raise RuntimeError("The server did not confirm a successful provider probe.")
        print(f"{label} activated. Structured output and the running service probe passed.")
    except Exception:
        if changed:
            try:
                stop_host()
                CONFIG.write_bytes(original)
                start_host()
                wait_online()
                print("Activation failed; the previous configuration and service were restored.")
            except Exception:
                print(f"Automatic rollback needs attention. Previous config: {backup}")
        elif stopped:
            start_host()
        raise
    finally:
        flag.unlink(missing_ok=True)
        subprocess.run(
            [sys.executable, str(OPS / "watch_ai_memory.py")],
            capture_output=True, timeout=120, creationflags=NO_WINDOW,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Validate prepared config without a key or network request.")
    args = parser.parse_args()
    candidate, settings = prepared_config()
    if args.check:
        print("Prepared configuration is valid. Active service configuration unchanged.")
        return 0
    key = (OPS / "relay-api-key.txt").read_text("utf-8-sig").strip()
    if not key or key == "PASTE_API_KEY_HERE" or any(c.isspace() for c in key):
        raise RuntimeError("Replace relay-api-key.txt with the API key only, on one line.")
    print("Testing the relay with a fixed synthetic prompt; no memory content is sent.")
    # Verify the configured remote relay before any production restart. The
    # local diagnostic route is separately verified by the live server probe.
    probe(candidate, settings, key, base_url=task_diagnostics.upstream_base())
    print("Structured probe passed. Applying configuration and restarting the service.")
    apply(candidate, settings)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Never expose an arbitrary exception containing request headers or keys.
        print(str(error) if isinstance(error, RuntimeError) else
              f"Activation stopped ({type(error).__name__}); no credentials printed.")
        sys.exit(1)
