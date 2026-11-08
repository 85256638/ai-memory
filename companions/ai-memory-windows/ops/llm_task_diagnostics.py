"""Task-specific, bounded diagnostics; never persist prompts or credentials."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

OPS = Path(__file__).resolve().parent
DATA = OPS.parent
LOCAL_BASE = "http://127.0.0.1:49376/v1"
def upstream_base() -> str:
    """Read only the locally configured, fixed HTTPS destination."""
    import tomllib
    from urllib.parse import urlsplit
    settings = tomllib.loads((OPS / "relay-provider.toml").read_text("utf-8-sig"))
    value = settings["llm_base_url"].rstrip("/")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.hostname.endswith("example.com")
            or url.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Configure a real HTTPS relay URL without credentials or query parameters.")
    return value
PREFIX = "Review one completed ai-memory session and propose only durable wiki edits.\nSession: "


def readonly(data: Path):
    return closing(sqlite3.connect(
        f"{(data / 'db/memory.sqlite').as_uri()}?mode=ro", uri=True, timeout=3))


def task_from_request(body: bytes, data: Path = DATA) -> dict | None:
    try:
        payload = json.loads(body)
        message = next(m for m in payload["messages"] if m.get("role") == "user")
        text = message["content"]
        if not isinstance(text, str) or not text.startswith(PREFIX):
            return None
        session = str(uuid.UUID(text[len(PREFIX):].splitlines()[0].strip()))
        with readonly(data) as connection:
            row = connection.execute(
                "SELECT w.name,p.name FROM sessions s "
                "JOIN workspaces w ON w.id=s.workspace_id "
                "JOIN projects p ON p.id=s.project_id AND p.workspace_id=s.workspace_id "
                "WHERE s.id=?", (uuid.UUID(session).bytes,)).fetchone()
        if row:
            return {"workspace": row[0], "project": row[1], "session_id": session,
                    "workload": "auto-improve"}
    except (ValueError, KeyError, TypeError, StopIteration, OSError, sqlite3.Error):
        pass
    return None


def safe_text(value: object, secret: str = "") -> str:
    text = str(value)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)\bBearer\s+\S+|\b(?:sk-|aim_)[A-Za-z0-9_-]+",
                  "[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|authorization)\s*[=:]\s*[^ ,;]+",
                  r"\1=[REDACTED]", text)
    # A provider may echo submitted text. Keep only one bounded error line.
    return " ".join(text.split())[:400]


def classify(status: int, body: bytes, secret: str = "",
             content_type: str = "") -> dict | None:
    """Extract the provider error, never an entire response or request."""
    text = body.decode("utf-8", errors="replace")
    error = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            response = parsed.get("response") or {}
            if not error and parsed.get("type") == "response.failed":
                error = response.get("error") or {"message": "SSE response.failed"}
    except (ValueError, AttributeError):
        pass
    # Some relays mistakenly send SSE even to a nonstream Chat request.
    if not error and ("text/event-stream" in content_type or "response.failed" in text):
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                item = json.loads(line[5:].strip())
                if isinstance(item, dict) and item.get("type") == "response.failed":
                    error = (item.get("response") or {}).get("error") or {
                        "message": "SSE response.failed"}
                    break
            except (ValueError, AttributeError):
                continue
    if 200 <= status < 300 and not error:
        return None
    if isinstance(error, dict):
        message = str(error.get("message", ""))
        code = safe_text(error.get("code") or error.get("type") or "", secret)
        categories = error.get("flagged_categories") or []
    else:
        message = str(error) if error else text
        code, categories = "", []
    lower = message.lower()
    if "possible cybersecurity risk" in lower:
        reason = "上游内容审核拒绝：疑似网络安全风险（上游判定）"
        message = "This chat was flagged for possible cybersecurity risk"
        code = code or "cybersecurity_risk"
    elif "sexual" in categories and (
            code == "content_policy_violation" or "flagged" in lower):
        reason = "上游内容审核拒绝：sexual 类别（上游判定）"
        message = "Request flagged by upstream moderation; category: sexual"
    elif code == "content_policy_violation" or "flagged by omni-moderation" in lower:
        reason = "上游内容审核拒绝"
    elif status == 401:
        reason = "上游认证失败"
    elif status == 429:
        reason = "上游额度或请求频率受限"
    elif "response.failed" in text:
        reason = "上游生成失败（SSE response.failed）"
    elif status == 502 and code == "local_transport_error":
        reason = "本机到中转站的网络连接失败"
    else:
        reason = f"上游返回 HTTP {status}" if status >= 300 else "上游响应包含错误"
    return {"upstream_http_status": status, "error_code": code,
            "reason": reason, "message": safe_text(message or reason, secret)}


def store_failure(task: dict, failure: dict, ops: Path = OPS,
                  observed_at: str | None = None,
                  source: str = "local-relay-diagnostics") -> None:
    folder = ops / "llm-task-errors"
    folder.mkdir(exist_ok=True)
    session = str(uuid.UUID(task["session_id"]))
    value = dict(task, **failure, source=source, observed_at=observed_at or
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"))
    path = folder / f"{session}.json"
    temporary = path.with_name(f"{session}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def micros(value: str) -> int:
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def scheduler_reason(error: str) -> str:
    error_class = re.search(r"class=([a-z-]+)", error)
    status = re.search(r"status=(\d+)", error)
    if error_class and error_class[1] == "serde":
        prefix = "返回结果解析失败"
    elif status:
        prefix = f"接口返回 HTTP {status[1]}"
    elif error_class and error_class[1] == "http":
        prefix = "HTTP 传输或响应解码失败"
    else:
        prefix = "任务执行失败"
    return prefix + "；历史记录未保留上游完整原因"


def pending_details(data: Path = DATA, ops: Path = OPS) -> list[dict]:
    """Resolve a failure only against a later persisted run of the same task."""
    cached = {}
    for path in (ops / "llm-task-errors").glob("*.json"):
        try:
            item = json.loads(path.read_text("utf-8"))
            key = (item["workspace"], item["project"],
                   str(uuid.UUID(item["session_id"])))
            item["failed_at_us"] = micros(item["observed_at"])
            cached[key] = item
        except (OSError, ValueError, KeyError, TypeError):
            continue
    labels = {}
    try:
        labels = {(i["workspace"], i["project"]): i["label"]
                  for i in json.loads((data / "codex-project-map.json").read_text("utf-8-sig"))}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    details = {}
    with readonly(data) as connection:
        rows = connection.execute(
            "SELECT w.name,p.name,c.session_id,c.attempts,c.last_error,c.last_failed_at,c.claimed_at "
            "FROM auto_improve_scheduler_claims c "
            "JOIN workspaces w ON w.id=c.workspace_id "
            "JOIN projects p ON p.id=c.project_id AND p.workspace_id=c.workspace_id "
            "WHERE c.last_error IS NOT NULL").fetchall()
        for workspace, project, session, attempts, error, failed_at, claimed_at in rows:
            session_id = str(uuid.UUID(bytes=session))
            key = (workspace, project, session_id)
            # Retrying clears last_failed_at but retains the historical error.
            # A persisted run after claimed_at resolves that retry; the claim
            # row itself is not removed after success.
            reference_at = failed_at if failed_at is not None else claimed_at
            details[key] = {
                "workspace": workspace, "project": project, "session_id": session_id,
                "attempts": attempts, "failed_at_us": reference_at,
                "scheduler_retry_claimed": failed_at is None,
                "scheduler_error": safe_text(error), "workload": "auto-improve",
                "reason": scheduler_reason(error),
                "observed_at": (dt.datetime.fromtimestamp(
                    failed_at / 1_000_000, dt.timezone.utc).isoformat()
                    if failed_at is not None else "历史失败时间未保留"),
                "claimed_at": dt.datetime.fromtimestamp(
                    claimed_at / 1_000_000, dt.timezone.utc).isoformat(),
            }
        for key, item in cached.items():
            existing = details.get(key, {})
            # The relay sees a failure just before the scheduler persists it.
            # Do not attach an old moderation error to a much later failure.
            if item["failed_at_us"] >= existing.get("failed_at_us", 0) - 30_000_000:
                details[key] = dict(existing, **item)
                details[key]["failed_at_us"] = max(
                    item["failed_at_us"], existing.get("failed_at_us", 0))
        pending = []
        for (workspace, project, session_id), item in details.items():
            later_run = connection.execute(
                "SELECT 1 FROM auto_improve_runs r "
                "JOIN workspaces w ON w.id=r.workspace_id "
                "JOIN projects p ON p.id=r.project_id AND p.workspace_id=r.workspace_id "
                "WHERE w.name=? AND p.name=? AND r.session_id=? AND r.created_at>=? LIMIT 1",
                (workspace, project, uuid.UUID(session_id).bytes,
                 item["failed_at_us"])).fetchone()
            if later_run:
                continue
            item["project_label"] = labels.get((workspace, project), project)
            item["retry_state"] = ("调度器已重新领取，尚未确认完成"
                                   if item.get("scheduler_retry_claimed")
                                   else "暂停自动重试" if item.get("attempts", 0) >= 3
                                   else "尚未完成成功学习")
            pending.append(item)
    return sorted(pending, key=lambda i: i["failed_at_us"], reverse=True)


def summary(item: dict) -> str:
    status = item.get("upstream_http_status")
    suffix = f"HTTP {status}，" if status else ""
    return (f"{item['project_label']} [{item['workspace']}/{item['project']}]："
            f"{suffix}{item['reason']}")


def detail_lines(items: list[dict]) -> list[str]:
    lines = []
    if items:
        lines = ["", "自动学习失败任务明细："]
    for item in items:
        lines += [
            summary(item),
            f"  会话：{item['session_id']}；状态：{item['retry_state']}；"
            f"最近记录：{item['observed_at']}",
        ]
        if item.get("message"):
            lines.append(f"  上游说明：{item['message']}")
        if item.get("scheduler_error"):
            lines.append(f"  调度器历史记录：{item['scheduler_error']}")
        if item.get("scheduler_retry_claimed"):
            lines.append(f"  最近重新领取：{item['claimed_at']}")
    if items:
        lines.append("内容审核类别是上游判定，可能误判；不是对项目内容的事实认定。")
    return lines
