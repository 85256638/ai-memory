"""Adversarial checks for paths, private snapshots and configured relay routing."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

OPS = Path(__file__).resolve().parent.parent / "ops"
sys.path.insert(0, str(OPS))
import llm_task_diagnostics as diagnostics
import upgrade_snapshot as snapshots
import windows_paths as paths


class PortabilityTests(unittest.TestCase):
    def test_archive_roots_support_custom_data_and_program_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user, data, program = root / "home", root / "custom-data", root / "program"
            with patch.object(paths, "USER", user), patch.object(paths, "DATA", data), patch.object(paths, "EXE", program / "ai-memory.exe"):
                self.assertEqual(paths.archive_name(data / "ops/test.py"), "data/ops/test.py")
                self.assertEqual(paths.archive_name(program / "ai-memory.exe"), "program/ai-memory.exe")
                self.assertEqual(paths.archive_name(user / ".codex/hooks.json"), "home/.codex/hooks.json")
                with self.assertRaises(ValueError):
                    paths.archive_name(root / "outside/credentials.txt")

    def test_settings_reject_relative_or_non_string_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            ops = Path(temporary)
            for value in ("relative/private", 123):
                (ops / "windows-settings.json").write_text(json.dumps({"backups": value}))
                with patch.object(paths, "OPS", ops), self.assertRaises(ValueError):
                    paths.read_settings()
            (ops / "windows-settings.json").write_text(json.dumps({"backups": str(ops / "private")}))
            with patch.object(paths, "OPS", ops):
                self.assertEqual(paths.read_settings()["backups"], str(ops / "private"))

    def test_relay_rejects_credentials_redirect_targets_and_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            ops = Path(temporary)
            bad = [
                "http://relay.invalid/v1", "https://secret@relay.invalid/v1",
                "https://relay.invalid/v1?api_key=synthetic",
                "https://relay.invalid/v1#secret",
                "https://relay.example.com/v1", "https://127.0.0.1/v1",
            ]
            with patch.object(diagnostics, "OPS", ops):
                for url in bad:
                    (ops / "relay-provider.toml").write_text(f"llm_base_url = {json.dumps(url)}\n")
                    with self.assertRaises(ValueError, msg=url):
                        diagnostics.upstream_base()
                (ops / "relay-provider.toml").write_text('llm_base_url = "https://relay.invalid/v1/"\n')
                self.assertEqual(diagnostics.upstream_base(), "https://relay.invalid/v1")

    def test_snapshot_preserves_custom_code_and_auth_bytes_without_echoing_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user, data, install, backups = [root / name for name in ("home", "data", "program", "backups")]
            for folder in (user, data / "ops", install, backups):
                folder.mkdir(parents=True, exist_ok=True)
            (data / "ops/custom.py").write_text("print('synthetic source')\n")
            (data / "auth.json").write_text('{"synthetic": "private-fixture-only"}')
            (install / "ai-memory.exe").write_bytes(b"synthetic executable")
            def archive_name(path):
                for folder, label in ((data, "data"), (install, "program"), (user, "home")):
                    try:
                        return f"{label}/{path.relative_to(folder).as_posix()}"
                    except ValueError:
                        pass
                raise ValueError
            with patch.multiple(snapshots, USER=user, DATA=data, INSTALL=install, BACKUPS=backups), patch.object(snapshots, "archive_name", archive_name), patch("sys.stdout", new_callable=io.StringIO) as output:
                snapshots.snapshot()
                report = json.loads(output.getvalue())
            self.assertTrue(report["verified"])
            self.assertNotIn("private-fixture-only", json.dumps(report))
            with zipfile.ZipFile(Path(report["directory"]) / "customization-and-program.zip") as bundle:
                self.assertEqual(bundle.read("data/auth.json"), (data / "auth.json").read_bytes())
                self.assertIn("data/ops/custom.py", bundle.namelist())

    def test_core_verification_does_not_extract_archive_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "fixture.sqlite"
            connection = sqlite3.connect(database)
            for table in ("pages", "sessions", "observations"):
                connection.execute(f"CREATE TABLE {table} (id INTEGER)")
                connection.execute(f"INSERT INTO {table} VALUES (1)")
            connection.commit()
            connection.close()
            archive = root / "core.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(database, arcname="backup/db/memory.sqlite")
                member = tarfile.TarInfo("../../escape.txt")
                member.size = 4
                bundle.addfile(member, io.BytesIO(b"nope"))
            with patch.object(snapshots, "BACKUPS", root), patch("sys.stdout", new_callable=io.StringIO) as output:
                snapshots.verify_core(archive)
                self.assertEqual(json.loads(output.getvalue())["sqlite_quick_check"], "ok")
            self.assertFalse((root.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
