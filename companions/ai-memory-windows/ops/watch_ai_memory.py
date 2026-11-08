"""Local Windows health checks; no remote telemetry or memory-content logging."""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import msvcrt
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import traceback
import urllib.error
import urllib.request
import winreg
import zipfile

import llm_task_diagnostics as task_diagnostics
import spool_image_guard

OPS = Path(__file__).resolve().parent
DATA = OPS.parent
from windows_paths import USER, EXE, BACKUPS, archive_name
PS = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
STATE = OPS / "status.json"
REQUIRED_HOOKS = {
    "preToolUse", "postToolUse", "preCompact", "sessionStart",
    "sessionEnd", "userPromptSubmit", "stop",
}
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def cli(*args: str, timeout: int = 25) -> dict:
    environment = os.environ.copy()
    # The desktop injects its bundled Codex into its own PATH, but scheduled
    # processes do not inherit that PATH. Resolve the installed desktop bundle
    # per run so a desktop update does not leave a pinned version directory.
    codex_bins = list((USER / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"))
    if codex_bins:
        codex = max(codex_bins, key=lambda item: item.stat().st_mtime)
        environment["PATH"] = str(codex.parent) + os.pathsep + environment.get("PATH", "")
    run = subprocess.run(
        [str(EXE), "--data-dir", str(DATA), *args],
        cwd=str(USER), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=NO_WINDOW, env=environment,
    )
    # Do not copy arbitrary CLI stderr/config contents to logs or notifications.
    # Recover deliberately exits 1 when it produced a valid partial-delivery
    # report. Preserve that report instead of aborting the whole health check.
    if args[0] == "recover" and run.returncode in (0, 1):
        try:
            report = json.loads(run.stdout)
            spool = report["spool"]
            valid = (
                isinstance(report["reachable"], bool)
                and isinstance(report["spool_incomplete"], bool)
                and isinstance(report["spool_lock_busy"], bool)
                and all(type(spool[key]) is int and spool[key] >= 0
                        for key in ("sent", "remaining", "dropped"))
                and type(report["journal_remaining"]) is int
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if valid:
            report["cli_exit_code"] = run.returncode
            return report
        raise RuntimeError("CLI_recover_report_invalid")
    if run.returncode:
        raise RuntimeError(f"CLI_{args[0]}_exit_{run.returncode}")
    if args[0] == "backup":
        return {"exit_ok": True}
    return json.loads(run.stdout)


def healthy_http() -> bool:
    # Explicitly bypass any system proxy for this loopback-only health check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:49374/healthz", timeout=4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def port_in_use() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 49374), timeout=2):
            return True
    except OSError:
        return False


def logon_registered() -> bool:
    expected = (
        f'"{PS}" -NoProfile -NonInteractive -WindowStyle Hidden '
        f'-File "{OPS / "service_entry.ps1"}"'
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            actual = winreg.QueryValueEx(key, "ai-memory")[0]
            # Windows paths and PowerShell option names ignore case; a native
            # scheduled process can spell SystemRoot as C:\WINDOWS.
            return isinstance(actual, str) and actual.casefold() == expected.casefold()
    except OSError:
        return False


def tray_running() -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenMutexW(0x00100000, False, r"Local\AiMemoryStatusTray")
    if handle:
        kernel.CloseHandle(handle)
        return True
    return False


def ensure_tray() -> str:
    if (OPS / "tray.disabled").exists():
        return "disabled_by_user"
    if not (OPS / "AI-Memory-Status.exe").is_file():
        return "not_installed"
    if tray_running():
        return "running"
    run = subprocess.run(
        [str(PS), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-File", str(OPS / "start_status_tray.ps1")],
        capture_output=True, timeout=18, creationflags=NO_WINDOW,
    )
    if run.returncode:
        return "start_failed"
    for _ in range(10):
        if tray_running():
            return "running"
        time.sleep(0.3)
    return "start_failed"


def start_native_host() -> None:
    # Win32_Process.Create starts the official detached service-host outside
    # Task Scheduler's process job. Never Start-Process a server from this task.
    run = subprocess.run(
        [str(PS), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-File", str(OPS / "start_native_host.ps1")],
        capture_output=True, timeout=18, creationflags=NO_WINDOW,
    )
    if run.returncode:
        raise RuntimeError("native_host_launch_failed")


def notify(title: str, text: str, warning: bool = False) -> bool:
    try:
        run = subprocess.run(
            [str(PS), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-File", str(OPS / "notify.ps1"), "-Title", title, "-Message", text,
             "-WarningLevel", "warning" if warning else "info"],
            capture_output=True, timeout=18, creationflags=NO_WINDOW,
        )
        return run.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def event(kind: str, detail: str) -> None:
    log = OPS / "events.jsonl"
    # Only diagnostic codes and times, never project text or credentials.
    if log.exists() and log.stat().st_size > 512_000:
        os.replace(log, OPS / "events.previous.jsonl")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": utc(), "kind": kind, "detail": detail}) + "\n")


def backup() -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d")
    archive = BACKUPS / f"daily-{stamp}.tar.gz"
    wiring = BACKUPS / f"daily-{stamp}-wiring.zip"
    marker = BACKUPS / f"daily-{stamp}-verified.json"
    if marker.exists():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if archive.is_file() and wiring.is_file():
            return saved
    # The CLI online SQLite backup is safe while capture is running.
    cli("backup", "--to", str(archive), timeout=100)
    with tarfile.open(archive, "r:gz") as bundle:
        databases = [m for m in bundle.getmembers()
                     if m.isfile() and m.name.endswith("db/memory.sqlite")]
        if len(databases) != 1:
            raise RuntimeError("backup_database_member_invalid")
        # Extract only the database bytes; never extract untrusted archive paths.
        with tempfile.TemporaryDirectory(prefix="verify-", dir=BACKUPS) as scratch:
            database = Path(scratch) / "verify.sqlite"
            with bundle.extractfile(databases[0]) as source, database.open("wb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup_sqlite_check_failed")
    # Official core backup excludes credentials and harness config. Supplement
    # those in this user/SYSTEM-only directory. This is NOT a full disk backup.
    paths = [
        USER / ".ai-memory.toml", USER / ".codex/config.toml",
        USER / ".codex/hooks.json", USER / ".codex/AGENTS.md",
        DATA / "codex-project-map.json", DATA / "auth-token", DATA / "auth-header",
        DATA / "auth.json",
    ]
    paths += [p for p in (DATA / "auth-tokens").rglob("*") if p.is_file()]
    for directory in (USER / ".agents/skills").glob("ai-memory-*"):
        paths += [p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()]
    paths += [p for p in OPS.iterdir() if p.suffix in (".py", ".ps1", ".cs", ".exe", ".ico")]
    paths += [OPS / name for name in ("README.md", "windows-settings.json",
              "relay-provider.toml", "chatgpt-provider.toml", "relay-api-key.txt")]
    with zipfile.ZipFile(wiring, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            if path.is_file() and not path.is_symlink():
                bundle.write(path, archive_name(path))
    with zipfile.ZipFile(wiring) as bundle:
        if bundle.testzip():
            raise RuntimeError("wiring_backup_crc_failed")
    result = {
        "time": utc(), "sqlite_quick_check": "ok",
        "archive": str(archive), "wiring": str(wiring),
        "core_bytes": archive.stat().st_size, "wiring_bytes": wiring.stat().st_size,
        "scope": "online-db-wiki-config plus protected auth and agent wiring",
    }
    write_json(marker, result)
    # Prune only files we generated, in complete verified daily sets. Keep 7.
    for old in sorted(BACKUPS.glob("daily-????????-verified.json"), reverse=True)[7:]:
        prefix = old.name.removesuffix("-verified.json")
        for name in (f"{prefix}.tar.gz", f"{prefix}-wiring.zip", old.name):
            candidate = BACKUPS / name
            if candidate.parent.resolve() == BACKUPS.resolve():
                candidate.unlink(missing_ok=True)
    return result


def llm_error_description(error_class: str | None, status: int | None) -> str:
    if status == 403:
        return "接口拒绝访问（权限、网关或安全策略需核查）"
    if status == 401 or error_class in ("auth", "not-configured"):
        return "认证或访问权限异常"
    if status == 429 or error_class == "rate_limited":
        return "额度或请求频率受限"
    if status is not None:
        return f"接口返回 HTTP {status}"
    return {
        "serde": "返回结果解析失败（JSON 或字段类型不符合要求）",
        "http": "HTTP 传输或响应解码失败",
        "empty-content": "接口没有返回可用正文",
        "truncated-response": "结构化结果达到输出上限后被截断",
        "unexpected-shape": "响应结构不符合接口要求",
        "schema": "结构化输出 schema 异常",
    }.get(error_class, "调用失败，具体原因尚未分类")


def llm_workload_diagnostics(providers: dict) -> tuple[list[str], list[str]]:
    lines, issues = [], []
    names = {"consolidation": "会话整理", "auto-improve": "自动学习"}
    for workload in providers.get("llm_workloads", []):
        name = names.get(workload.get("workload"))
        if not name or not workload.get("calls"):
            continue
        lines.append(
            f"{name}：成功 {workload.get('successes', 0)} / "
            f"调用 {workload.get('calls', 0)}，失败 {workload.get('errors', 0)}"
        )
        error_at, success_at = workload.get("last_error_at"), workload.get("last_success_at")
        if error_at and (not success_at or error_at > success_at):
            reason = llm_error_description(
                workload.get("last_error_class"), workload.get("last_error_status"))
            issues.append(f"{name}最近一次失败：{reason}；时间 {error_at}")
    return lines, issues


def auto_improve_failure_counts() -> dict:
    # Read only bounded class/status metadata; never read source observations
    # or page content. Failed claims remain actionable after another call wins.
    database = DATA / "db/memory.sqlite"
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=3)) as connection:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN c.attempts >= 3 THEN 1 ELSE 0 END), 0) "
            "FROM auto_improve_scheduler_claims c WHERE c.last_error IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM auto_improve_runs r "
            "WHERE r.workspace_id = c.workspace_id AND r.project_id = c.project_id "
            "AND r.session_id = c.session_id "
            "AND r.created_at >= COALESCE(c.last_failed_at, c.claimed_at))"
        ).fetchone()
    return {"failed": row[0], "parked": row[1]}


def save_status(state: dict) -> None:
    write_json(STATE, state)
    labels = {"healthy": "正常", "warning": "需要检查", "failed": "故障",
              "maintenance": "维护暂停"}
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        f"AI Memory：{labels.get(state.get('health'), '未知')}",
        f"本次检查：{now}",
        "计划每 5 分钟检查；电脑保持唤醒时，超过 10 分钟未刷新应检查监测任务。",
        f"服务版本：{state.get('server_version', '未确认')}",
        f"待补传事件：{state.get('spool_pending', '未确认')}",
        f"最近完整检查成功：{state.get('last_full_success', '尚无')}",
        f"最近自动恢复：{state.get('last_recovery', '尚无')}",
        f"7 个全局 Hook 已信任：{state.get('hooks_trusted', '未确认')}",
        f"整理模型：{state.get('llm_provider', '未确认')} / {state.get('llm_model', '未确认')}",
        f"整理模型状态：{state.get('llm_status', '未确认')}",
        f"模型认证：{state.get('llm_auth_mode', '随 provider 配置')}",
        f"模型最近成功调用：{state.get('llm_last_success', '尚无')}",
        f"会话结束自动整理：{state.get('consolidate_on_session_end', '未确认')}",
        f"失败的自动整理任务：{state.get('consolidation_failed', '未确认')}",
        f"右下角状态图标：{state.get('tray', '尚未确认')}",
        "图片事件保护：启用；超大内嵌图片先保存原件，再转换为简短文字事件",
        f"最近验证备份：{state.get('backup', {}).get('time', '尚无')}",
        f"提示：{'；'.join(state.get('issues', [])) or '运行正常'}",
        "",
        "网页：http://127.0.0.1:49374/web",
        "桌面“AI Memory 立即检查”会主动运行检查并显示结果。",
        "通知可能受 Windows 勿扰模式限制；这里保留检查及故障记录。",
        "有意停用时，先按维护说明暂停监测，避免被自动重启。",
    ]
    lines[11:11] = state.get("llm_workload_lines", []) + [
        f"自动学习失败待处理：{state.get('auto_improve_failed') if state.get('auto_improve_failed') is not None else '未确认'}；"
        f"其中暂停自动重试：{state.get('auto_improve_parked') if state.get('auto_improve_parked') is not None else '未确认'}（连续失败 3 次）",
        "模型调用计数为本次服务运行以来累计；成功调用不代表已消除其他任务的错误。",
    ]
    lines[lines.index(""):lines.index("")] = task_diagnostics.detail_lines(
        state.get("llm_failed_tasks", []))
    (OPS / "status.txt").write_text("\n".join(lines), encoding="utf-8-sig")


def check(force_notice: bool = False) -> int:
    previous = load_state()
    state = dict(previous)
    state.update(last_check=utc(), health="healthy", issues=[], llm_failed_tasks=[],
                 auto_improve_failed=None, auto_improve_parked=None, monitor_error=None,
                 spool_guard_error=None, recover_error=None)
    recovered = False
    try:
        state["tray"] = ensure_tray()
        if state["tray"] in ("start_failed", "not_installed"):
            state["issues"].append("状态托盘图标未能运行，请双击桌面“AI Memory 托盘图标”")
    except Exception:
        state["tray"] = "start_failed"
        state["issues"].append("状态托盘启动检查失败")
    if (OPS / "maintenance.flag").exists():
        state.update(health="maintenance", issues=["maintenance.flag 存在，自动恢复和备份暂停"])
        maintenance_age = time.time() - (OPS / "maintenance.flag").stat().st_mtime
        if maintenance_age > 7200:
            state["issues"].append("维护标记已超过 2 小时，请确认是否忘记恢复监测")
            if time.time() - previous.get("maintenance_alert_epoch", 0) > 21600:
                state["notification_api_ok"] = notify(
                    "AI Memory 维护尚未结束",
                    "维护标记已超过 2 小时，自动恢复仍暂停。详情见桌面“AI Memory 状态”。",
                    True,
                )
                if state["notification_api_ok"]:
                    state["maintenance_alert_epoch"] = time.time()
        save_status(state)
        return 0
    try:
        if not EXE.is_file():
            raise RuntimeError("程序文件缺失，请检查安装路径或 Windows 安全中心隔离记录")
        if not (DATA / "config.toml").is_file():
            raise RuntimeError("服务配置文件缺失")
        with (DATA / "config.toml").open("rb") as config_file:
            memory_config = tomllib.load(config_file)
        if memory_config.get("llm_base_url") == task_diagnostics.LOCAL_BASE:
            run = subprocess.run(
                [sys.executable, str(OPS / "relay_diagnostics_gateway.py"), "--ensure"],
                capture_output=True, timeout=30, creationflags=NO_WINDOW)
            state["llm_diagnostics_gateway"] = "ok" if run.returncode == 0 else "error"
            if run.returncode:
                state["issues"].append("本机模型诊断转发服务无法恢复，请检查 49376 端口")
        else:
            state["llm_diagnostics_gateway"] = "disabled"
        if not healthy_http():
            if port_in_use():
                raise RuntimeError("49374 端口有进程，但健康检查失败；未启动重复服务")
            event("repair_started", "loopback_health_unavailable")
            start_native_host()
            for _ in range(20):
                if healthy_http():
                    recovered = True
                    state["last_recovery"] = utc()
                    event("recovered", "native_service_host")
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("自动启动未能在 10 秒内恢复，请查看服务日志")
        try:
            status = cli("status", "--json")
        except Exception:
            raise RuntimeError("健康端点可达，但认证状态查询失败；请检查令牌文件或服务配置") from None
        state["server_version"] = status.get("version")
        state["client_version"] = status.get("client", {}).get("version")
        state["last_service_success"] = utc()
        state["logon_registered"] = logon_registered()
        if not state["logon_registered"]:
            state["issues"].append("Windows 登录启动项缺失或与本机安装不符")
        if state["server_version"] != state["client_version"]:
            state["issues"].append("程序与运行中服务版本不同，需要按维护说明重启")
        embedding = status.get("providers", {}).get("embedding", {})
        if embedding.get("status") in ("error", "failed", "unavailable"):
            state["issues"].append("本地向量检索服务异常，请检查模型文件及服务日志")
        llm = status.get("providers", {}).get("llm", {})
        with (DATA / "config.toml").open("rb") as config_file:
            memory_config = tomllib.load(config_file)
        expected_llm = {
            "provider": memory_config.get("llm_provider"),
            "model": memory_config.get("llm_model"),
        }
        state["llm_provider"] = llm.get("provider")
        state["llm_model"] = llm.get("model")
        state["llm_status"] = llm.get("status", "unknown")
        state["llm_auth_mode"] = (
            "独立 ChatGPT OAuth；由 ai-memory 管理刷新"
            if llm.get("provider") == "openai-oauth"
            else "Codex 登录文件；刷新依赖 Codex 程序"
            if llm.get("provider") == "codex"
            else "随 provider 配置"
        )
        # Keep only known diagnostic labels, never arbitrary provider messages
        # that might echo submitted content or credentials.
        error_message = str(llm.get("last_error_message") or "")
        auth_error = (
            llm.get("status") == "error"
            and (error_message.startswith("auth:")
                 or llm.get("last_error_status") == 401)
        )
        if auth_error and llm.get("provider") == "openai-oauth":
            state["issues"].append(
                "独立 ChatGPT 授权失败；请重新执行 ai-memory auth login openai-oauth "
                "后用后台 llm-test --server 核验，成功后仍需补跑失败任务")
        elif auth_error and llm.get("provider") == "codex":
            if "Could not start `codex app-server" in error_message:
                state["issues"].append("Codex 认证刷新程序无法启动；需核查程序路径及后台配置")
            elif "without replacing the access token" in error_message:
                state["issues"].append("Codex 未刷新 ChatGPT 凭据；需核查 API Key 与 ChatGPT 登录模式")
        state["llm_last_success"] = llm.get("last_success_at") or "尚无"
        workload_lines, workload_issues = llm_workload_diagnostics(status.get("providers", {}))
        state["llm_workload_lines"] = workload_lines
        state["issues"].extend(workload_issues)
        try:
            failed_tasks = task_diagnostics.pending_details(DATA, OPS)
            state["llm_failed_tasks"] = failed_tasks
            state["auto_improve_failed"] = len(failed_tasks)
            state["auto_improve_parked"] = sum(
                i.get("attempts", 0) >= 3 for i in failed_tasks)
            state["issues"][0:0] = [task_diagnostics.summary(i) for i in failed_tasks]
            if state["auto_improve_parked"]:
                state["issues"].append(
                    f"自动学习有 {state['auto_improve_parked']} 个任务连续失败 3 次，"
                    "已暂停自动重试；其他调用成功不会补回这些任务")
        except Exception as error:
            state["auto_improve_failed"] = None
            state["auto_improve_parked"] = None
            frame = traceback.extract_tb(error.__traceback__)[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}"
            state["monitor_error"] = {
                "component": "auto_improve_diagnostics",
                "exception": type(error).__name__, "location": location,
            }
            # A broken optional diagnostic must not abort core health checks
            # or turn stale counts into a claim of authentication failure.
            state["issues"].append(
                f"自动学习任务诊断异常：{type(error).__name__}（{location}）；"
                "失败任务数量暂未确认")
            event("task_diagnostics_failed", f"{type(error).__name__}@{location}")
        if any(expected_llm.values()) and (not all(expected_llm.values()) or any(
                llm.get(key) != value for key, value in expected_llm.items())):
            state["issues"].append("整理模型未启用或与配置文件不符；自动采集正常不代表知识整理可用")
        elif llm.get("status") in ("disabled", "error", "failed", "unavailable", "rate_limited") and not workload_issues:
            state["issues"].append(
                "模型最近调用异常：" + llm_error_description(
                    llm.get("last_error_class"), llm.get("last_error_status")))
        elif llm.get("last_error_at") and (
                not llm.get("last_success_at")
                or llm["last_error_at"] > llm["last_success_at"]) and not workload_issues:
            state["issues"].append("模型最近一次调用失败；请查看按任务分类的状态及服务日志")
        state["consolidate_on_session_end"] = bool(
            memory_config.get("consolidate_on_session_end"))
        if any(expected_llm.values()) and not state["consolidate_on_session_end"]:
            state["issues"].append("会话结束自动整理未启用")
        consolidation = status.get("consolidation", {})
        state["consolidation_failed"] = consolidation.get("failed", 0)
        if state["consolidation_failed"]:
            state["issues"].append("存在失败的自动整理任务，请查看服务状态及模型调用错误")
        spool = status.get("spool", {})
        # Repair only recognized oversized local inline-image records, under
        # the official drain lock and after a byte-verified private backup.
        repaired_images = False
        try:
            guard_report = spool_image_guard.repair_oversized_images()
            state["spool_guard_last_check"] = guard_report["checked_at"]
            state["spool_guard_lock_busy"] = guard_report["lock_busy"]
            if guard_report["repaired"] or guard_report["unhandled"]:
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
                report_path = OPS / "spool-guard-reports" / (stamp + ".json")
                report_path.parent.mkdir(exist_ok=True)
                write_json(report_path, guard_report)
                state["spool_guard_last_report"] = str(report_path)
            if guard_report["repaired"]:
                repaired_images = True
                state["spool_guard_last_repair"] = guard_report["checked_at"]
                state["spool_guard_repaired_total"] = (
                    state.get("spool_guard_repaired_total", 0)
                    + len(guard_report["repaired"]))
                event("spool_images_compacted", str(len(guard_report["repaired"])))
            if guard_report["unhandled"]:
                state["issues"].append(
                    f"有 {len(guard_report['unhandled'])} 项超大事件未自动处理；"
                    "原件保留，请查看补传诊断报告")
        except Exception as error:
            state["spool_guard_error"] = type(error).__name__
            state["issues"].append("图片事件补传保护异常：" + type(error).__name__)
            event("spool_guard_failed", type(error).__name__)
        if recovered or repaired_images or (
                spool.get("pending", 0) and spool.get("oldest_age_ms", 0) > 120_000):
            try:
                report = cli("recover", "--json", timeout=45)
                state["last_recover_report"] = report
                if report.get("spool_lock_busy"):
                    state["issues"].append("补传锁正被其他进程占用；下次巡检会重试")
                elif report.get("spool_incomplete") or report.get("journal_remaining", 0):
                    counts = report.get("spool", {})
                    state["issues"].append(
                        f"补传未完成：已发送 {counts.get('sent', 0)}，"
                        f"仍待发送 {counts.get('remaining', 0)}，"
                        f"本轮丢弃 {counts.get('dropped', 0)}；下次巡检会重试")
            except Exception as error:
                label = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                state["recover_error"] = label
                state["issues"].append("事件补传检查失败：" + label)
                event("recover_check_failed", label)
            # Always refresh counters even after an incomplete recovery.
            # This keeps Hook, backup and model checks independent.
            status = cli("status", "--json")
            spool = status.get("spool", {})
        state["spool_pending"] = spool.get("pending", 0)
        state["spool_oldest_age_ms"] = spool.get("oldest_age_ms", 0)
        if state["spool_pending"] and state["spool_oldest_age_ms"] > 300_000:
            state["issues"].append("本地事件队列积压超过 5 分钟")
        check_due = time.time() - state.get("doctor_checked_epoch", 0) > 1800
        if check_due or force_notice or recovered:
            report = cli("doctor", "--json", timeout=35)
            hooks = report.get("codex_hooks") or {}
            trusted = set(hooks.get("trusted", []))
            state["hooks_trusted"] = REQUIRED_HOOKS <= trusted and not hooks.get("skipped")
            state["hooks_probe_available"] = bool(hooks)
            state["doctor_checked_epoch"] = time.time()
        if not state.get("hooks_trusted"):
            state["issues"].append(
                "全局 Hook 缺少信任或未完整安装，请在 Codex /hooks 检查"
                if state.get("hooks_probe_available")
                else "无法向 Codex 验证 Hook 信任状态，请用 Codex /hooks 检查"
            )
        try:
            state["backup"] = backup()
        except Exception as error:
            state["issues"].append(f"自动备份失败：{type(error).__name__}")
            event("backup_failed", type(error).__name__)
        if state["issues"]:
            state["health"] = "warning"
    except Exception as error:
        state["health"] = "failed"
        # Controlled messages above or exception class only, no raw stderr.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        state["issues"].append(message)
        event("check_failed", message)
    if state["health"] == "healthy":
        state["last_full_success"] = utc()
        state["last_success"] = state["last_full_success"]
    issue_key = json.dumps(state["issues"], ensure_ascii=False)
    alert_due = (
        issue_key != previous.get("last_alert_key")
        or time.time() - previous.get("last_alert_epoch", 0) > 21600
    )
    if state["health"] != "healthy" and alert_due:
        message = "；".join(state["issues"])
        if len(message) > 800:
            message = message[:797] + "..."
        state["notification_api_ok"] = notify(
            "AI Memory 需要检查", message + "。桌面“AI Memory 状态”可查看详情。", True,
        )
        if state["notification_api_ok"]:
            state["last_alert_epoch"] = time.time()
            state["last_alert_key"] = issue_key
    elif recovered or previous.get("health") in ("failed", "warning"):
        state["notification_api_ok"] = notify(
            "AI Memory 已恢复", "服务已正常运行，已尝试补传本地事件。详情见桌面“AI Memory 状态”。",
        )
    elif force_notice:
        state["notification_api_ok"] = notify(
            "AI Memory 检查完成", "服务运行正常，详情见桌面“AI Memory 状态”。",
        )
    save_status(state)
    return 1 if state["health"] == "failed" else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    lock = (OPS / "watch.lock").open("a+b")
    try:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 0
        result = check(force_notice=args.show)
    finally:
        lock.close()
    if args.show:
        subprocess.Popen(
            [str(Path(os.environ["SystemRoot"]) / "System32/notepad.exe"), str(OPS / "status.txt")],
            creationflags=NO_WINDOW,
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
