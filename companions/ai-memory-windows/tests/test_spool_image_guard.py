"""Focused regression checks for image preservation, scope, and drain locking."""
import copy
import base64
import json
import msvcrt
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

OPS = Path(__file__).resolve().parent.parent / "ops"
sys.path.insert(0, str(OPS))
import spool_image_guard as guard
import watch_ai_memory as watch


def example():
    return {
        "url": ("http://127.0.0.1:49374/hook?event=post-tool-use&agent=codex"
                "&workspace=example&project=test&ingest_key=test-original-key"),
        "auth_mode": "static", "token": "synthetic-placeholder-only",
        "created_ms": 1794127812392, "attempts": 0,
        "body": json.dumps({
            "session_id": "00000000-0000-0000-0000-000000000001",
            "tool_name": "view_image", "tool_input": {"path": "synthetic.png"},
            "_ai_memory_usage_calls": [{"input_tokens": 12}],
            "tool_response": [
                {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 9000000},
                {"type": "text", "text": "Retain this ordinary text exactly."},
                {"type": "input_image", "image_url": "https://example.invalid/image.png"},
            ],
        }),
    }


class ImageGuardTests(unittest.TestCase):
    def test_preserves_identity_auth_usage_and_non_image_content(self):
        original = example()
        untouched = copy.deepcopy(original)
        changed, facts = guard.compact_entry(original)
        self.assertEqual(original, untouched)
        self.assertEqual(facts["images_replaced"], 1)
        self.assertEqual({k: v for k, v in original.items() if k != "body"},
                         {k: v for k, v in changed.items() if k != "body"})
        before, after = json.loads(original["body"]), json.loads(changed["body"])
        self.assertEqual(before["tool_response"][1:], after["tool_response"][1:])
        self.assertEqual({k: v for k, v in before.items() if k != "tool_response"},
                         {k: v for k, v in after.items() if k != "tool_response"})
        self.assertNotIn("data:image", after["tool_response"][0]["text"])
        self.assertLess(len(guard.encoded(changed)), guard.TRIGGER_BYTES)

    def test_rejects_remote_and_unrecognized_oversize(self):
        value = example()
        value["url"] = value["url"].replace("127.0.0.1", "example.invalid")
        with self.assertRaisesRegex(ValueError, "unexpected_destination"):
            guard.compact_entry(value)
        value = example()
        body = json.loads(value["body"])
        body["tool_response"] = [{"type": "text", "text": "A" * 9000000}]
        value["body"] = json.dumps(body)
        with self.assertRaisesRegex(ValueError, "no_recognized"):
            guard.compact_entry(value)

    def test_octet_stream_requires_view_image_and_raster_signature(self):
        value = example()
        body = json.loads(value["body"])
        body["tool_response"][0]["image_url"] = (
            "data:application/octet-stream;base64,"
            + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode()
        )
        value["body"] = json.dumps(body)
        changed, facts = guard.compact_entry(value)
        self.assertEqual(facts["images_replaced"], 1)
        self.assertIn("image/png", json.loads(changed["body"])["tool_response"][0]["text"])
        body["tool_name"] = "unknown_tool"
        value["body"] = json.dumps(body)
        with self.assertRaisesRegex(ValueError, "no_recognized"):
            guard.compact_entry(value)
        body["tool_name"] = "view_image"
        body["tool_response"][0]["image_url"] = (
            "data:application/octet-stream;base64,"
            + base64.b64encode(b"not-an-image" * 20).decode()
        )
        value["body"] = json.dumps(body)
        with self.assertRaisesRegex(ValueError, "no_recognized"):
            guard.compact_entry(value)

    def test_backups_exact_original_and_second_pass_does_nothing(self):
        with tempfile.TemporaryDirectory() as scratch:
            data = Path(scratch)
            spool = data / "hook-spool"
            spool.mkdir()
            path = spool / "synthetic.json"
            contents = guard.encoded(example())
            path.write_bytes(contents)
            result = guard.repair_oversized_images(data, data / "originals")
            self.assertEqual(len(result["repaired"]), 1)
            archive = Path(result["repaired"][0]["original_backup"])
            self.assertEqual(archive.read_bytes(), contents)
            self.assertLess(path.stat().st_size, guard.TRIGGER_BYTES)
            again = guard.repair_oversized_images(data, data / "originals")
            self.assertEqual(again["repaired"], [])
            self.assertEqual(again["unhandled"], [])

    def test_unsupported_file_remains_byte_exact(self):
        with tempfile.TemporaryDirectory() as scratch:
            data = Path(scratch)
            (data / "hook-spool").mkdir()
            path = data / "hook-spool/unknown.json"
            contents = json.dumps({"arbitrary": "x" * 9000000}).encode()
            path.write_bytes(contents)
            result = guard.repair_oversized_images(data, data / "originals")
            self.assertEqual(result["repaired"], [])
            self.assertEqual(len(result["unhandled"]), 1)
            self.assertEqual(path.read_bytes(), contents)

    def test_windows_lock_refuses_concurrent_repair_without_touching_events(self):
        # Use a temporary spool; never acquire the installed service's lock.
        with tempfile.TemporaryDirectory() as scratch:
            data = Path(scratch)
            spool = data / "hook-spool"
            spool.mkdir()
            event = spool / "synthetic.json"
            original = guard.encoded(example())
            event.write_bytes(original)
            with (spool / ".drain.lock").open("a+b") as lock:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    report = guard.repair_oversized_images(data, data / "originals")
                    self.assertTrue(report["lock_busy"])
                    self.assertEqual(report["repaired"], [])
                    self.assertEqual(event.read_bytes(), original)
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class RecoverReportTests(unittest.TestCase):
    def test_partial_exit_report_is_preserved(self):
        report = {"reachable": True, "spool_incomplete": True, "spool_lock_busy": False,
                  "spool": {"sent": 2, "remaining": 1, "dropped": 0},
                  "journal_remaining": 0}
        with patch.object(watch.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=json.dumps(report))):
            result = watch.cli("recover", "--json")
        self.assertEqual(result["spool"]["remaining"], 1)
        self.assertTrue(result["spool_incomplete"])
        self.assertEqual(result["cli_exit_code"], 1)

    def test_invalid_report_does_not_become_success(self):
        with patch.object(watch.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout='{"unknown": "bad"}')):
            with self.assertRaisesRegex(RuntimeError, "report_invalid"):
                watch.cli("recover", "--json")

    def test_other_cli_errors_still_fail(self):
        with patch.object(watch.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "CLI_status_exit_1"):
                watch.cli("status", "--json")


if __name__ == "__main__":
    unittest.main()
