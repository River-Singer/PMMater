"""Focused checks for truthful, immutable Sunday Excel captures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import os
import unittest
import zipfile

from openpyxl import Workbook, load_workbook

from pm_core import archive
from pm_core.fixtures import base_data, CALENDAR_ID
from pm_core.store import Store
from pm_core.weekly import save_config as save_weekly_config


LOCAL_SUN = datetime(2026, 11, 8, 20, 5, tzinfo=ZoneInfo("America/Vancouver"))
SUN = LOCAL_SUN.astimezone(timezone.utc)
MON = SUN + timedelta(days=1)


def facts():
    data = base_data()
    data["people"] = [{"id": "PER-PM", "name": "测试项目经理", "active": True}]
    data["projects"] = [{
        "id": "PRJ-1", "name": "Excel 留档虚构测试", "po_id": "PER-PM", "pm_id": "PER-PM",
        "calendar_id": CALENDAR_ID, "baseline_start": "2026-10-01",
        "baseline_finish": "2026-12-20", "forecast_finish": "2026-12-20",
    }]
    data["wbs"] = [{
        "id": "WI-1", "title": "首次已提交事实", "scope": "project", "project_id": "PRJ-1",
        "work_type": "improvement", "status": "intake",
    }]
    # The archive must preserve all historical rows, outside the visible month.
    data["day_records"] = [{
        "id": "DAY-OLD", "person_id": "PER-PM", "calendar_id": CALENDAR_ID,
        "date": "2026-10-02", "period": "AM", "record_kind": "actual", "status": "onsite",
    }]
    return data


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "pm.sqlite3"
        self.out = self.root / "archives"
        with Store(self.db) as store:
            store.apply_batch(facts(), actor="fixture", now=SUN - timedelta(days=10))

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, at=SUN, **kwargs):
        with patch("pm_core.archive.utc_now", return_value=at):
            return archive.capture_archive(self.db, self.out, **kwargs)

    def tick(self, at=SUN, **kwargs):
        with patch("pm_core.archive.utc_now", return_value=at):
            return archive.run_due_archive(self.db, self.out, **kwargs)

    def retry(self, archive_id, at=MON):
        with patch("pm_core.archive.utc_now", return_value=at):
            return archive.retry_archive(self.db, archive_id, self.out)

    def source(self, name="current.xlsx", value="只存在 Excel 中，尚未导入"):
        path = self.root / name
        book = Workbook()
        book.active["A1"] = value
        book.save(path)
        book.close()
        # An extra ZIP member would be discarded by an open/save round trip.
        with zipfile.ZipFile(path, "a") as package:
            package.writestr("custom/original.bin", b"\x00\xffOriginal Excel bytes\x00")
        return path

    def count(self, table):
        with Store(self.db) as store:
            return store.conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]

    def update_task(self, title, at=MON):
        with Store(self.db) as store:
            row = store.data()["wbs"][0]
            store.apply_batch({"wbs": [{"id": row["id"], "version": row["version"], "title": title}]},
                              actor="PM", now=at)

    def test_config_defaults_inherit_weekly_zone_and_validate_versions(self):
        save_weekly_config(self.db, {"timezone": "Asia/Shanghai"})
        old = archive.get_archive_config(self.db)
        self.assertFalse(old["enabled"])
        self.assertEqual(old["timezone"], "Asia/Shanghai")
        self.assertEqual(old["sunday_time"], "20:00")
        self.assertIsNone(old["source_workbook"])
        for invalid in ({"enabled": "yes"}, {"timezone": "not/a_zone"}, {"sunday_time": "24:00"},
                        {"source_workbook": True}, {"source_workbook": "old.xls"}, {"unexpected": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                archive.save_archive_config(self.db, invalid)
        new = archive.save_archive_config(self.db, {"enabled": True, "source_workbook": "current.xlsx", "version": old["version"]})
        self.assertEqual(Path(new["source_workbook"]).resolve(), (self.root / "current.xlsx").resolve())
        with self.assertRaises(ValueError):
            archive.save_archive_config(self.db, {"version": old["version"], "enabled": False})
        with self.assertRaises(ValueError):
            archive.list_archives(self.db, limit=1001)

    def test_copy_keeps_exact_bytes_hash_and_does_not_import_or_snapshot_db(self):
        source = self.source()
        original = source.read_bytes()
        archive.save_archive_config(self.db, {"source_workbook": str(source)})
        before = self.count("event_log")
        result = self.capture()
        self.assertEqual(result["state"], "ready", result)
        self.assertEqual(result["mode"], "copy")
        self.assertEqual((self.out / result["filename"]).read_bytes(), original)
        self.assertEqual(result["sha256"], sha256(original).hexdigest())
        self.assertEqual(result["captured_at"], SUN.isoformat())
        self.assertIsNone(result["snapshot_id"])
        self.assertEqual(self.count("snapshots"), 0)
        self.assertEqual(self.count("event_log"), before)
        self.assertIsNone(result["scheduled_for"])
        self.assertIn("2026-11-08_manual_20261108T200500", result["filename"])

    def test_export_uses_project_calendar_and_actual_project_local_month(self):
        with Store(self.db) as store:
            store.apply_batch(base_data(timezone="UTC", calendar_id="AAA-OTHER"), actor="fixture",
                              now=SUN - timedelta(days=5))
            before = store.data()
        at = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)  # Dec 1 Shanghai / Nov 30 Vancouver.
        result = self.capture(at)
        self.assertEqual(result["state"], "ready", result)
        self.assertEqual(result["mode"], "export")
        self.assertEqual(result["calendar_id"], CALENDAR_ID)
        self.assertEqual(result["month"], "2026-12")
        self.assertIn("已提交", result["note"])
        with Store(self.db) as store:
            frozen = store.read_snapshot(result["snapshot_id"])
            self.assertEqual(frozen["data"], before)
            self.assertEqual(frozen["cutoff"], at.isoformat())
            self.assertEqual(frozen["event_watermark"], result["event_watermark"])
        book = load_workbook(self.out / result["filename"], read_only=True, data_only=True)
        try:
            values = [value for row in book["_meta"].iter_rows(values_only=True) for value in row if isinstance(value, str)]
            self.assertIn("DAY-OLD", "".join(values))
        finally:
            book.close()

    def test_scheduler_disabled_before_due_and_no_monday_backfill(self):
        self.assertEqual(self.tick(), [])
        archive.save_archive_config(self.db, {"enabled": True})
        self.assertEqual(self.tick(SUN - timedelta(minutes=6)), [])
        self.assertEqual(self.tick(MON), [])
        self.assertEqual(archive.list_archives(self.db), [])

    def test_same_local_sunday_idempotent_despite_utc_monday_and_time_change(self):
        archive.save_archive_config(self.db, {"enabled": True, "source_workbook": str(self.source())})
        first = self.tick()[0]
        self.assertEqual(first["state"], "ready", first)
        self.assertEqual(first["week_end"], "2026-11-08")
        self.assertEqual(first["scheduled_for"], LOCAL_SUN.replace(minute=0).isoformat())
        archive.save_archive_config(self.db, {"sunday_time": "19:30"})
        again = self.tick(SUN + timedelta(minutes=20))[0]
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["attempts"], 1)
        self.assertEqual(len(list(self.out.glob("*.xlsx"))), 1)
        manual = self.capture()
        self.assertNotEqual(manual["id"], first["id"])
        self.assertEqual(manual["kind"], "manual")

    def test_concurrent_ticks_claim_one_capture_and_one_file(self):
        archive.save_archive_config(self.db, {"enabled": True, "source_workbook": str(self.source())})
        with patch("pm_core.archive.utc_now", return_value=SUN):
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(lambda _: archive.run_due_archive(self.db, self.out), range(4)))
        ids = {result[0]["id"] for result in results}
        self.assertEqual(len(ids), 1)
        rows = archive.list_archives(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "ready", rows)
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertEqual(self.count("archive_attempts"), 1)
        self.assertEqual(len(list(self.out.glob("*.xlsx"))), 1)

    def test_missing_source_is_failed_not_fallback_and_explicit_retry_has_real_time(self):
        missing = self.root / "missing.xlsx"
        archive.save_archive_config(self.db, {"enabled": True, "source_workbook": str(missing)})
        failed = self.tick()[0]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["mode"], "copy")
        self.assertIn("不存在", failed["error"])
        self.assertIsNone(failed["captured_at"])
        self.assertIsNone(failed["sha256"])
        self.assertEqual(self.count("snapshots"), 0)
        self.source("missing.xlsx")
        self.assertEqual(self.tick(MON), [])
        retried = self.retry(failed["id"])
        self.assertEqual(retried["state"], "ready", retried)
        self.assertEqual(retried["captured_at"], MON.isoformat())
        self.assertEqual(retried["scheduled_for"], failed["scheduled_for"])
        self.assertFalse(retried["captured_on_scheduled_date"])
        self.assertTrue(retried["late_capture"])
        self.assertIn("2026-11-08_sunday_20261109T200500", retried["filename"])
        self.assertEqual([row["state"] for row in archive.get_archive(self.db, failed["id"])["history"]],
                         ["failed", "ready"])

    def test_copy_does_not_parse_or_rewrite_opaque_workbook_bytes(self):
        source = self.root / "protected.xlsx"
        opaque = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1opaque encrypted workbook fixture"
        source.write_bytes(opaque)
        archive.save_archive_config(self.db, {"source_workbook": str(source)})
        ready = self.capture()
        self.assertEqual(ready["state"], "ready", ready)
        self.assertEqual((self.out / ready["filename"]).read_bytes(), opaque)
        self.assertEqual(ready["sha256"], sha256(opaque).hexdigest())

    def test_copy_accepts_cross_api_ctime_difference_without_losing_identity_checks(self):
        source = self.source()
        original_stat = Path.stat

        def different_ctime(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == source:
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}
                fields['st_ctime_ns'] += 123456789
                return SimpleNamespace(**fields)
            return result

        with patch.object(Path, 'stat', different_ctime):
            result = archive._copy_source(source, self.root / 'copied.xlsx')
        self.assertEqual(result[0], sha256(source.read_bytes()).hexdigest())

    def test_copy_rejects_same_size_source_mutation_with_restored_mtime(self):
        source = self.root / 'changing.xlsx'
        source.write_bytes(b'first snapshot')
        previous = source.stat()
        real_sync = archive.os.fsync

        def mutate_after_copy(fd):
            real_sync(fd)
            source.write_bytes(b'later snapshot')
            os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))

        with patch('pm_core.archive.os.fsync', side_effect=mutate_after_copy):
            with self.assertRaisesRegex(ValueError, '复制期间发生变化'):
                archive._copy_source(source, self.root / 'copy.xlsx')

    def test_copy_rejects_path_replacement_with_identical_bytes(self):
        source = self.source()
        real_stat = Path.stat

        # A different inode at the original path represents atomic replacement;
        # mock only this metadata because Windows can lock open source files.
        def replaced_path(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == source and (self.root / 'copy.xlsx').exists():
                fields = {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}
                fields['st_ino'] += 1
                return SimpleNamespace(**fields)
            return result

        with patch.object(Path, 'stat', replaced_path):
            with self.assertRaisesRegex(ValueError, '复制期间发生变化'):
                archive._copy_source(source, self.root / 'copy.xlsx')

    def test_failed_export_retry_uses_frozen_facts_not_current_db(self):
        with patch("pm_core.archive.export_workbook", side_effect=OSError("模拟磁盘失败")):
            failed = self.capture()
        self.assertEqual(failed["state"], "failed")
        self.assertIsNotNone(failed["snapshot_id"])
        self.assertEqual(failed["captured_at"], SUN.isoformat())
        self.update_task("捕获之后的新事实")
        real_export = archive.export_workbook
        observed = {}

        def inspect_export(data, path, **kwargs):
            observed.update(data=data, **kwargs)
            return real_export(data, path, **kwargs)

        with patch("pm_core.archive.export_workbook", side_effect=inspect_export):
            retried = self.retry(failed["id"])
        self.assertEqual(retried["state"], "ready", retried)
        self.assertEqual(retried["snapshot_id"], failed["snapshot_id"])
        self.assertEqual(observed["data"]["wbs"][0]["title"], "首次已提交事实")
        self.assertEqual(observed["as_of"], SUN.isoformat())
        self.assertEqual(self.count("snapshots"), 1)
        self.assertEqual(retried["attempts"], 2)

    def test_retry_recovers_published_bytes_after_failure_without_overwriting(self):
        source = self.source()
        archive.save_archive_config(self.db, {"source_workbook": str(source)})
        original = source.read_bytes()
        publish = archive._publish_no_replace

        def interrupted_after_publish(stage, final, expected):
            publish(stage, final, expected)
            raise OSError("模拟发布后数据库提交前失败")

        with patch("pm_core.archive._publish_no_replace", side_effect=interrupted_after_publish):
            failed = self.capture()
        self.assertEqual(failed["state"], "failed")
        final = self.out / failed["filename"]
        mtime = final.stat().st_mtime_ns
        self.source(value="后来修改的 Excel 内容")
        ready = self.retry(failed["id"])
        self.assertEqual(ready["state"], "ready", ready)
        self.assertEqual(ready["captured_at"], SUN.isoformat())
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(final.stat().st_mtime_ns, mtime)
        self.assertEqual(ready["sha256"], sha256(original).hexdigest())
        self.assertEqual(ready["attempts"], 2)

    def test_different_existing_bytes_are_never_overwritten(self):
        archive.save_archive_config(self.db, {"source_workbook": str(self.source())})
        with patch("pm_core.archive._publish_no_replace", side_effect=OSError("模拟发布失败")):
            failed = self.capture()
        final = self.out / failed["filename"]
        final.write_bytes(b"existing history, must not overwrite")
        retried = self.retry(failed["id"])
        self.assertEqual(retried["state"], "failed")
        self.assertEqual(final.read_bytes(), b"existing history, must not overwrite")

    def test_future_historical_or_naive_clock_cannot_create_backdated_archives(self):
        for invalid in (SUN + timedelta(seconds=1), SUN - timedelta(days=7), SUN.replace(tzinfo=None)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.capture(now=invalid)
        self.assertEqual(archive.list_archives(self.db), [])
        # A same-day scheduler hint is never used as the capture timestamp.
        result = self.capture(now=SUN - timedelta(minutes=5))
        self.assertEqual(result["captured_at"], SUN.isoformat())

    def test_expired_running_attempt_is_recovered_without_a_second_slot(self):
        archive.save_archive_config(self.db, {"enabled": True, "source_workbook": str(self.source())})
        config = archive.get_archive_config(self.db)
        key = "sunday|2026-11-08|America/Vancouver"
        with patch("pm_core.archive.utc_now", return_value=SUN):
            pending, _ = archive._claim(self.db, self.out, config, "crashed-worker", SUN,
                                        occurrence_key=key, scheduled_for=LOCAL_SUN.replace(minute=0).isoformat())
        self.assertEqual(self.tick()[0]["state"], "running")
        recovered = self.tick(SUN + timedelta(seconds=archive.LEASE_SECONDS + 1))[0]
        self.assertEqual(recovered["id"], pending["id"])
        self.assertEqual(recovered["state"], "ready", recovered)
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual([row["state"] for row in archive.get_archive(self.db, pending["id"])["history"]],
                         ["abandoned", "ready"])
        self.assertEqual(len(list(self.out.glob("*.xlsx"))), 1)


if __name__ == "__main__":
    unittest.main()
