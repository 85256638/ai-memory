"""Repair oversized local image events without discarding their original bytes.

ai-memory 3.3.2 can spool an uncapped Codex tool image, while its drainer
refuses to load files over 10 MiB. This local maintenance guard changes only
recognized inline-image blocks in oversized post-tool-use events. Delivery
still belongs to the official CLI with the original scope and ingest key.
"""
from __future__ import annotations

import copy
import base64
import binascii
import datetime as dt
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

DATA = Path(__file__).resolve().parent.parent
from windows_paths import BACKUPS as BACKUP_ROOT
BACKUPS = BACKUP_ROOT / "spool-image-originals"
TRIGGER_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_REPAIRS = 8
IMAGE_HEADER = re.compile(
    r"data:(image/[a-z0-9.+-]+|application/octet-stream);base64,", re.IGNORECASE)


def image_format(source: str, match, tool: str) -> str | None:
    declared = match[1].lower()
    if declared.startswith("image/"):
        return declared
    # Codex view_image currently labels its image bytes application/octet-stream.
    # Admit that case only for this image tool and a recognized raster signature.
    if tool != "view_image":
        return None
    try:
        prefix = base64.b64decode(source[match.end():match.end() + 64], validate=True)
    except (binascii.Error, ValueError):
        return None
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"BM"):
        return "image/bmp"
    return None


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def has_reparse(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def compact_entry(original: dict) -> tuple[dict, dict]:
    """Return a copy; reject unexpected shapes and remote/profile deliveries."""
    url = urlsplit(original["url"])
    query = parse_qs(url.query)
    if (url.scheme, url.hostname, url.port, url.path) != (
            "http", "127.0.0.1", 49374, "/hook"):
        raise ValueError("unexpected_destination")
    if query.get("event") != ["post-tool-use"] or query.get("agent") != ["codex"]:
        raise ValueError("unsupported_event")
    if original.get("profile") is not None:
        raise ValueError("profile_bound_entry")
    body = json.loads(original["body"])
    response = body.get("tool_response")
    if not isinstance(response, list):
        raise ValueError("unsupported_response")
    new_body = copy.deepcopy(body)
    count = 0
    removed_chars = 0
    result = []
    for item in response:
        if not isinstance(item, dict) or item.get("type") not in (
                "input_image", "image_url", "image"):
            result.append(item)
            continue
        source = item.get("image_url")
        source = source.get("url") if isinstance(source, dict) else source
        match = IMAGE_HEADER.match(source) if isinstance(source, str) else None
        media_type = image_format(source, match, body.get("tool_name", "")) if match else None
        if not media_type:
            result.append(item)
            continue
        # Replacing a recognized image content block avoids keeping a partial
        # data URI or exposing its prefix as a textual memory excerpt.
        count += 1
        removed_chars += len(source)
        result.append({
            "type": "text",
            "text": (
                "[ai-memory local spool repair: inline image omitted from "
                f"memory transport; format={media_type}, "
                f"encoded_characters={len(source)}; "
                "the complete original event is retained in the private local backup.]"
            ),
        })
    if not count:
        raise ValueError("no_recognized_inline_image")
    new_body["tool_response"] = result
    updated = dict(original, body=encoded(new_body).decode("utf-8"))
    # Everything outside the result blocks, including usage metadata, scope
    # hints, URLs, credentials and the original idempotency identity is intact.
    assert {k: v for k, v in updated.items() if k != "body"} == {
        k: v for k, v in original.items() if k != "body"}
    assert {k: v for k, v in new_body.items() if k != "tool_response"} == {
        k: v for k, v in body.items() if k != "tool_response"}
    if len(encoded(updated)) >= TRIGGER_BYTES:
        raise ValueError("still_oversized")
    return updated, {"images_replaced": count, "image_uri_characters": removed_chars}


def secure_directory(path: Path) -> None:
    """New backup directories inherit the already-private backup root ACL."""
    root = DATA.parent / "ai-memory-backups"
    if path.parent != root or has_reparse(root):
        raise RuntimeError("unexpected_backup_parent")
    path.mkdir(exist_ok=True)
    if has_reparse(path):
        raise RuntimeError("reparse_backup_directory")


def repair_oversized_images(data: Path = DATA, backups: Path = BACKUPS) -> dict:
    # Alternate data and backup roots are accepted only for isolated tests.
    spool = data / "hook-spool"
    report = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "repaired": [], "unhandled": [], "lock_busy": False}
    candidates = []
    for path in spool.glob("*.json"):
        if not has_reparse(path) and path.stat().st_size >= TRIGGER_BYTES:
            candidates.append(path)
    if not candidates:
        return report
    with (spool / ".drain.lock").open("a+b") as lock:
        lock.seek(0)
        deadline = time.monotonic() + 2
        while True:
            try:
                # Windows byte-range locking intersects fs2's native whole-file
                # drain lock. Verified against the official recover command.
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    report["lock_busy"] = True
                    return report
                time.sleep(0.05)
        try:
            if data == DATA and backups == BACKUPS:
                secure_directory(backups)
            else:
                backups.mkdir(exist_ok=True)
            archive_bytes = sum(p.stat().st_size for p in backups.glob("*.original"))
            for path in candidates[:MAX_REPAIRS]:
                item = {"file": path.name, "original_bytes": path.stat().st_size}
                try:
                    if item["original_bytes"] > MAX_READ_BYTES:
                        raise ValueError("over_local_read_ceiling")
                    if archive_bytes + item["original_bytes"] > MAX_ARCHIVE_BYTES:
                        raise ValueError("private_backup_capacity_reached")
                    original_bytes = path.read_bytes()
                    original = json.loads(original_bytes)
                    updated, details = compact_entry(original)
                    digest = hashlib.sha256(original_bytes).hexdigest()
                    archive = backups / (digest + ".original")
                    if not archive.exists():
                        # Create a fresh file in the protected directory;
                        # copy2 would also copy the source's metadata.
                        with archive.open("xb") as stream:
                            stream.write(original_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        archive_bytes += len(original_bytes)
                    if archive.read_bytes() != original_bytes:
                        raise RuntimeError("original_backup_mismatch")
                    if path.read_bytes() != original_bytes:
                        raise RuntimeError("entry_changed_during_repair")
                    replacement = encoded(updated)
                    temporary = spool / (path.name + "." + uuid.uuid4().hex + ".guard")
                    try:
                        with temporary.open("xb") as stream:
                            stream.write(replacement)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    # Metadata only: never URLs, credentials, image bytes,
                    # tool inputs, or project contents in maintenance reports.
                    item.update(details, replacement_bytes=len(replacement),
                                original_sha256=digest, original_backup=str(archive))
                    report["repaired"].append(item)
                except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                    known = {
                        "unexpected_destination", "unsupported_event", "profile_bound_entry",
                        "unsupported_response", "no_recognized_inline_image",
                        "still_oversized", "over_local_read_ceiling",
                        "private_backup_capacity_reached", "original_backup_mismatch",
                        "entry_changed_during_repair",
                    }
                    item["reason"] = str(error) if str(error) in known else type(error).__name__
                    report["unhandled"].append(item)
            if len(candidates) > MAX_REPAIRS:
                report["unhandled"].append({"reason": "per_pass_limit",
                                             "remaining": len(candidates) - MAX_REPAIRS})
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return report


if __name__ == "__main__":
    print(json.dumps(repair_oversized_images(), ensure_ascii=True, indent=2))
