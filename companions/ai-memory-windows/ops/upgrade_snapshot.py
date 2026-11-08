"""Private, verified snapshots of this Windows installation's custom wiring."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import zipfile

from windows_paths import USER, DATA, EXE, BACKUPS, archive_name
INSTALL = EXE.parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    root = BACKUPS / f"pre-upgrade-{stamp}"
    root.mkdir()
    selected = [
        DATA / "config.toml", DATA / "codex-project-map.json",
        USER / ".ai-memory.toml", USER / ".codex/config.toml",
        USER / ".codex/hooks.json", USER / ".codex/AGENTS.md",
        DATA / "auth-token", DATA / "auth-header", DATA / "auth.json",
    ]
    trees = [DATA / "ops", DATA / "auth-tokens", DATA / "hooks", INSTALL]
    for skill_root in [USER / ".agents/skills", USER / ".codex/skills"]:
        trees += list(skill_root.glob("ai-memory-*"))
    for tree in trees:
        if tree.is_dir():
            selected += [
                p for p in tree.rglob("*")
                if p.is_file() and not p.is_symlink()
                and "__pycache__" not in p.parts and p.suffix != ".lock"
            ]
    manifest = {}
    archive = root / "customization-and-program.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(selected)):
            if path.is_file():
                if path.is_symlink() or (path.stat().st_file_attributes & 0x400):
                    raise RuntimeError("Reparse point in backup selection")
                relative = archive_name(path)
                contents = path.read_bytes()
                manifest[relative] = hashlib.sha256(contents).hexdigest()
                bundle.writestr(relative, contents)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip():
            raise RuntimeError("Snapshot CRC verification failed")
        for relative, expected in manifest.items():
            if hashlib.sha256(bundle.read(relative)).hexdigest() != expected:
                raise RuntimeError("Snapshot hash verification failed")
    (root / "manifest.json").write_text(
        json.dumps({"created_utc": stamp, "files": manifest,
                    "archive_roots": {"data": str(DATA), "program": str(INSTALL),
                                      "home": str(USER)}}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"directory": str(root), "files": len(manifest),
                      "archive_sha256": digest(archive), "verified": True}))


def verify_core(archive):
    with tarfile.open(archive, "r:gz") as bundle:
        databases = [
            m for m in bundle.getmembers()
            if m.isfile() and m.name.endswith("db/memory.sqlite")
        ]
        if len(databases) != 1:
            raise RuntimeError("Expected one core backup database")
        with tempfile.TemporaryDirectory(dir=BACKUPS, prefix="verify-upgrade-") as temp:
            target = Path(temp) / "verify.sqlite"
            source = bundle.extractfile(databases[0])
            with source, target.open("wb") as stream:
                while chunk := source.read(1024 * 1024):
                    stream.write(chunk)
            connection = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Core backup SQLite check failed")
                counts = {
                    table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in ["pages", "sessions", "observations"]
                }
            finally:
                connection.close()
    print(json.dumps({"sqlite_quick_check": "ok", "counts": counts,
                      "sha256": digest(Path(archive))}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-core")
    args = parser.parse_args()
    if args.verify_core:
        verify_core(args.verify_core)
    else:
        snapshot()
