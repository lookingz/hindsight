"""Tests for the parts of backup-cn.py that need neither docker nor a network.

Run from anywhere: python3 -m unittest discover -s docker/standalone/backup -p "test_*.py"
"""
import datetime as dt
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path

_loader = importlib.machinery.SourceFileLoader("backup_cn", str(Path(__file__).with_name("backup-cn.py")))
backup = importlib.util.module_from_spec(importlib.util.spec_from_loader("backup_cn", _loader))
_loader.exec_module(backup)


class UploadIntervalTest(unittest.TestCase):
    """`UPLOAD_EVERY_N_DAYS` against the marker a successful upload leaves behind."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = {"BACKUP_DIR": tmp.name, "UPLOAD_EVERY_N_DAYS": "1"}

    def uploaded_at(self, when):
        (Path(self.cfg["BACKUP_DIR"]) / ".last-upload").write_text(when.isoformat(timespec="seconds") + "\n")

    def test_never_uploaded_is_due(self):
        self.assertEqual(backup.due_for_upload(self.cfg)[0], True)

    def test_uploaded_earlier_today_is_not_due_again(self):
        self.uploaded_at(dt.datetime.combine(dt.datetime.now().date(), dt.time(0, 0)))
        self.assertEqual(backup.due_for_upload(self.cfg)[0], False)

    def test_yesterday_counts_as_a_day_even_under_24_hours_ago(self):
        # The night's run can start earlier than the previous night's upload finished.
        self.uploaded_at(dt.datetime.combine(dt.datetime.now().date() - dt.timedelta(days=1), dt.time(23, 59)))
        self.assertEqual(backup.due_for_upload(self.cfg)[0], True)

    def test_every_two_days_waits_for_the_second_calendar_day(self):
        self.cfg["UPLOAD_EVERY_N_DAYS"] = "2"
        today = dt.datetime.now().date()
        self.uploaded_at(dt.datetime.combine(today - dt.timedelta(days=1), dt.time(2, 0)))
        self.assertEqual(backup.due_for_upload(self.cfg)[0], False)
        self.uploaded_at(dt.datetime.combine(today - dt.timedelta(days=2), dt.time(23, 59)))
        self.assertEqual(backup.due_for_upload(self.cfg)[0], True)

    def test_unreadable_marker_means_never_uploaded(self):
        (Path(self.cfg["BACKUP_DIR"]) / ".last-upload").write_text("not a date\n")
        self.assertEqual(backup.due_for_upload(self.cfg)[0], True)


if __name__ == "__main__":
    unittest.main()
