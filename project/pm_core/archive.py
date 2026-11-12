"""Immutable Excel captures and the optional, local Sunday archive schedule.

The schedule is independent of Monday/Friday reporting.  A configured workbook
is copied byte for byte, without importing or rewriting it.  Otherwise a single
SQLite transaction freezes committed facts, which the normal Excel exporter
renders.  Retries use that saved capture whenever one exists; a source that was
never captured can only be read at the real retry time, never backdated.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo
import errno
import json
import os
import re
import shutil
import stat
import uuid

from .excel_io import export_workbook
from .project_report import single_project
from .store import Store, parse_datetime, utc_now


DEFAULT_ARCHIVE_CONFIG = {
    "enabled": False,
    "timezone": "America/Vancouver",
    "sunday_time": "20:00",
    "source_workbook": None,
}
LEASE_SECONDS = 300
COPY_NOTE = "按字节复制当前已保存的 Excel；不改写文件内容，也不自动导入数据库。"
EXPORT_NOTE = (
    "未配置 Excel 源文件：从事务一致快照导出当前已提交的数据库事实；"
    "未保存或未导入数据库的 Excel 编辑不在此快照中。"
)


def _open(db_path):
    if not Path(db_path).is_file():
        raise ValueError("数据库不存在；请先建立单项目工作区")
    return Store(db_path)


def ensure_archive_schema(store):
    """Add system tables without changing business tables or weekly behavior."""
    store.conn.executescript("""
        CREATE TABLE IF NOT EXISTS archive_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS excel_archives (
            id TEXT PRIMARY KEY,
            occurrence_key TEXT UNIQUE,
            kind TEXT NOT NULL CHECK(kind IN ('manual','sunday')),
            week_end TEXT NOT NULL,
            scheduled_for TEXT,
            timezone TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('copy','export')),
            source_workbook TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            captured_at TEXT,
            completed_at TEXT,
            state TEXT NOT NULL CHECK(state IN ('running','ready','failed')),
            snapshot_id TEXT REFERENCES snapshots(id),
            event_watermark INTEGER,
            filename TEXT UNIQUE,
            sha256 TEXT,
            size_bytes INTEGER,
            output_dir TEXT NOT NULL,
            stage_filename TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT,
            lease_until TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS archive_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_id TEXT NOT NULL REFERENCES excel_archives(id),
            attempt INTEGER NOT NULL,
            actor TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            state TEXT NOT NULL CHECK(state IN ('running','ready','failed','abandoned')),
            captured_at TEXT,
            filename TEXT,
            sha256 TEXT,
            error TEXT,
            UNIQUE(archive_id, attempt)
        );
    """)
    initial = dict(DEFAULT_ARCHIVE_CONFIG)
    # Inherit the existing weekly zone only when archive settings are first made.
    # Later edits to either schedule remain independent and explicit.
    if store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weekly_config'"
    ).fetchone():
        weekly = store.conn.execute("SELECT config_json FROM weekly_config WHERE id=1").fetchone()
        if weekly:
            initial["timezone"] = json.loads(weekly[0]).get("timezone", initial["timezone"])
    store.conn.execute(
        "INSERT OR IGNORE INTO archive_config VALUES(1,?,?,?)",
        (json.dumps(initial, ensure_ascii=False), 1, utc_now().isoformat()),
    )
    store.conn.commit()


def _config_values(config, db_path):
    values = {key: config.get(key, value) for key, value in DEFAULT_ARCHIVE_CONFIG.items()}
    if not isinstance(values["enabled"], bool):
        raise ValueError("留档启用状态必须为布尔值")
    if not isinstance(values["timezone"], str):
        raise ValueError("留档时区必须为 IANA 名称")
    try:
        ZoneInfo(values["timezone"])
    except (ValueError, KeyError) as exc:
        raise ValueError("留档时区无效") from exc
    if not isinstance(values["sunday_time"], str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", values["sunday_time"]
    ):
        raise ValueError("sunday_time 必须为24小时制 HH:MM")
    source = values["source_workbook"]
    if source is not None and not isinstance(source, str):
        raise ValueError("Excel 源路径必须为文本或空值")
    if isinstance(source, str) and source.strip():
        if "\x00" in source or len(source) > 4096:
            raise ValueError("Excel 源路径无效或过长")
        path = Path(source).expanduser()
        if path.suffix.lower() != ".xlsx":
            raise ValueError("当前 Excel 源文件须为 .xlsx；留档不会转换文件格式")
        # A Windows task can have a different cwd; anchor relative paths to DB.
        if not path.is_absolute():
            path = Path(db_path).resolve().parent / path
        values["source_workbook"] = str(path.resolve())
    else:
        values["source_workbook"] = None
    return values


def get_archive_config(db_path):
    with _open(db_path) as store:
        ensure_archive_schema(store)
        row = store.conn.execute("SELECT * FROM archive_config WHERE id=1").fetchone()
        return json.loads(row["config_json"]) | {
            "version": row["version"], "updated_at": row["updated_at"]
        }


def save_archive_config(db_path, patch):
    if not isinstance(patch, dict):
        raise ValueError("留档配置必须为对象")
    unknown = set(patch) - set(DEFAULT_ARCHIVE_CONFIG) - {"version", "updated_at"}
    if unknown:
        raise ValueError("未知留档配置字段：" + ", ".join(sorted(unknown)))
    with _open(db_path) as store:
        ensure_archive_schema(store)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            old = store.conn.execute("SELECT * FROM archive_config WHERE id=1").fetchone()
            version = patch.get("version")
            if version is not None and (
                isinstance(version, bool) or not isinstance(version, int) or version != old["version"]
            ):
                raise ValueError("留档配置已更新，请刷新后重试")
            values = _config_values(json.loads(old["config_json"]) | {
                key: value for key, value in patch.items() if key in DEFAULT_ARCHIVE_CONFIG
            }, db_path)
            stamp = utc_now().isoformat()
            version = old["version"] + 1
            store.conn.execute(
                "UPDATE archive_config SET config_json=?,version=?,updated_at=? WHERE id=1",
                (json.dumps(values, ensure_ascii=False), version, stamp),
            )
            store.conn.commit()
            return values | {"version": version, "updated_at": stamp}
        except Exception:
            store.conn.rollback()
            raise


def _clock(now, timezone_name):
    actual = utc_now().astimezone(timezone.utc)
    instant = parse_datetime(now) if isinstance(now, str) else (actual if now is None else now)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("留档时间必须包含时区")
    instant = instant.astimezone(timezone.utc)
    if instant > actual:
        raise ValueError("不能捕获未来的留档")
    zone = ZoneInfo(timezone_name)
    if instant.astimezone(zone).date() != actual.astimezone(zone).date():
        raise ValueError("只能捕获当前日期的事实，不能补造过去周日的留档")
    # now can select a scheduler check time. It never replaces the capture clock.
    return instant, actual


def _actor(actor):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError("操作者标签应为1至200字文本")
    return actor.strip()


def _row(store, archive_id):
    row = store.conn.execute("SELECT * FROM excel_archives WHERE id=?", (archive_id,)).fetchone()
    if row is None:
        raise ValueError("留档记录不存在")
    return row


def _metadata(row):
    result = dict(row)
    result.update(json.loads(result.pop("details_json")))
    for key in ("lease_token", "lease_until", "stage_filename", "output_dir"):
        result.pop(key, None)
    result["note"] = COPY_NOTE if result["mode"] == "copy" else EXPORT_NOTE
    result["late_capture"] = bool(
        result["scheduled_for"] and result["captured_at"]
        and parse_datetime(result["captured_at"]) > parse_datetime(result["scheduled_for"])
    )
    result["captured_on_scheduled_date"] = None
    if result["scheduled_for"] and result["captured_at"]:
        zone = ZoneInfo(result["timezone"])
        result["captured_on_scheduled_date"] = (
            parse_datetime(result["captured_at"]).astimezone(zone).date()
            == parse_datetime(result["scheduled_for"]).astimezone(zone).date()
        )
    return result


def get_archive(db_path, archive_id):
    with _open(db_path) as store:
        ensure_archive_schema(store)
        result = _metadata(_row(store, archive_id))
        result["history"] = [dict(row) for row in store.conn.execute(
            "SELECT * FROM archive_attempts WHERE archive_id=? ORDER BY attempt", (archive_id,)
        )]
        return result


def list_archives(db_path, limit=1000):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("留档列表数量须为1至1000")
    with _open(db_path) as store:
        ensure_archive_schema(store)
        return [_metadata(row) for row in store.conn.execute(
            "SELECT * FROM excel_archives ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
        )]


def _filename(record, captured_at):
    local = parse_datetime(captured_at).astimezone(ZoneInfo(record["timezone"]))
    stamp = local.strftime("%Y%m%dT%H%M%S%f%z")
    return f"{record['week_end']}_{record['kind']}_{stamp}_{record['id']}.xlsx"


def _owned(store, archive_id, token):
    row = _row(store, archive_id)
    if row["state"] != "running" or row["lease_token"] != token:
        raise ValueError("留档执行权已变化，请读取当前结果或等待下一次重试")
    return row


def _claim(db_path, out_dir, config, actor, actual, *, occurrence_key=None,
           scheduled_for=None, archive_id=None):
    output_dir = str(Path(out_dir).resolve())
    token = uuid.uuid4().hex
    stamp = actual.isoformat()
    with _open(db_path) as store:
        ensure_archive_schema(store)
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            old = _row(store, archive_id) if archive_id else (
                store.conn.execute("SELECT * FROM excel_archives WHERE occurrence_key=?", (occurrence_key,)).fetchone()
                if occurrence_key else None
            )
            if old:
                if old["output_dir"] != output_dir:
                    raise ValueError("重试须使用原留档目录，避免将历史文件与记录分离")
                if old["state"] == "ready" or (
                    old["state"] == "running" and old["lease_until"]
                    and parse_datetime(old["lease_until"]) > actual
                ):
                    store.conn.commit()
                    return dict(old), None
                archive_id = old["id"]
                if old["state"] == "running":
                    store.conn.execute(
                        "UPDATE archive_attempts SET state='abandoned',finished_at=?,error=? "
                        "WHERE archive_id=? AND attempt=? AND state='running'",
                        (stamp, "执行租约过期；由后续尝试恢复，未声明留档成功", archive_id, old["attempts"]),
                    )
                attempt = old["attempts"] + 1
                store.conn.execute(
                    "UPDATE excel_archives SET state='running',attempts=?,updated_at=?,error=NULL,"
                    "lease_token=?,lease_until=? WHERE id=?",
                    (attempt, stamp, token, (actual + timedelta(seconds=LEASE_SECONDS)).isoformat(), archive_id),
                )
            else:
                archive_id = "AR-" + uuid.uuid4().hex
                local = actual.astimezone(ZoneInfo(config["timezone"]))
                week_end = (local.date() + timedelta(days=6 - local.weekday())).isoformat()
                record = {
                    "id": archive_id, "occurrence_key": occurrence_key,
                    "kind": "sunday" if occurrence_key else "manual", "week_end": week_end,
                    "scheduled_for": scheduled_for, "timezone": config["timezone"],
                    "mode": "copy" if config["source_workbook"] else "export",
                    "source_workbook": config["source_workbook"], "actor": actor,
                    "created_at": stamp, "updated_at": stamp, "state": "running",
                    "output_dir": output_dir, "attempts": 1, "lease_token": token,
                    "lease_until": (actual + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                }
                names = list(record)
                store.conn.execute(
                    "INSERT INTO excel_archives(" + ",".join(names) + ") VALUES("
                    + ",".join("?" for _ in names) + ")", [record[key] for key in names],
                )
                attempt = 1
            store.conn.execute(
                "INSERT INTO archive_attempts(archive_id,attempt,actor,started_at,state) VALUES(?,?,?,?,'running')",
                (archive_id, attempt, actor, stamp),
            )
            result = dict(_row(store, archive_id))
            store.conn.commit()
            return result, token
        except Exception:
            store.conn.rollback()
            raise


def _export_snapshot(db_path, archive_id, token):
    with _open(db_path) as store:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = _owned(store, archive_id, token)
            if row["snapshot_id"]:
                snapshot = store.read_snapshot(row["snapshot_id"])
                store.conn.commit()
                return dict(row), snapshot["data"]
            data = store.data()
            project = single_project(data)
            calendar = next((cal for cal in data["calendars"] if cal["id"] == project["calendar_id"]), None)
            if calendar is None:
                raise ValueError("项目日历不存在，无法导出留档")
            captured = utc_now().astimezone(timezone.utc)
            latest = store.conn.execute("SELECT MAX(recorded_at) FROM event_log").fetchone()[0]
            if latest and parse_datetime(latest) > captured:
                raise ValueError("当前事实含捕获时间之后的更新，不能倒签 Excel 留档")
            watermark = store.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM event_log").fetchone()[0]
            snapshot_id = "SNAP-" + uuid.uuid4().hex
            stamp = captured.isoformat()
            details = {
                "calendar_id": calendar["id"], "project_timezone": calendar["timezone"],
                "month": captured.astimezone(ZoneInfo(calendar["timezone"])).strftime("%Y-%m"),
                "capture_started_at": stamp,
            }
            store.conn.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                (snapshot_id, "Excel 留档：当前已提交事实", stamp, stamp, watermark,
                 json.dumps(data, ensure_ascii=False)),
            )
            filename = _filename(row, stamp)
            store.conn.execute(
                "UPDATE excel_archives SET captured_at=?,snapshot_id=?,event_watermark=?,filename=?,"
                "details_json=?,updated_at=? WHERE id=?",
                (stamp, snapshot_id, watermark, filename, json.dumps(details, ensure_ascii=False), stamp, archive_id),
            )
            store.conn.execute(
                "UPDATE archive_attempts SET captured_at=?,filename=? WHERE archive_id=? AND attempt=?",
                (stamp, filename, archive_id, row["attempts"]),
            )
            result = dict(_row(store, archive_id))
            store.conn.commit()
            return result, data
        except Exception:
            store.conn.rollback()
            raise


def _digest(path):
    digest, size = sha256(), 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _file_identity(value):
    # Windows Python can report different ctime values through fstat and stat
    # for the same file. Compare portable identity fields across those APIs;
    # retain ctime separately when comparing the same open handle over time.
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _copy_source(source_path, destination):
    """Do not open/save through an Excel library: preserve the original bytes."""
    started = utc_now().astimezone(timezone.utc).isoformat()
    path = Path(source_path)
    if not path.is_file():
        raise ValueError(f"Excel 源文件不存在或不是普通文件：{path}")
    with path.open("rb") as source, destination.open("xb") as target:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Excel 源路径必须为普通文件")
        digest, size = sha256(), 0
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
        after = os.fstat(source.fileno())
        if _file_identity(before) != _file_identity(after) or before.st_ctime_ns != after.st_ctime_ns or (
            _file_identity(before) != _file_identity(path.stat()) or size != before.st_size
        ):
            raise ValueError("Excel 源文件在复制期间发生变化；请保存完成后重试")
        # A second byte check also catches in-place edits on filesystems with
        # coarse/preserved timestamps. Check identity again after that read.
        if _digest(path) != (digest.hexdigest(), size) or (
            _file_identity(before) != _file_identity(path.stat())
            or _file_identity(before) != _file_identity(os.fstat(source.fileno()))
            or before.st_ctime_ns != os.fstat(source.fileno()).st_ctime_ns
        ):
            raise ValueError("Excel 源文件在复制期间发生变化；请保存完成后重试")
    # A byte archive must also preserve signed/encrypted workbooks.  Do not
    # parse the source or imply that a successful copy validates its contents.
    return digest.hexdigest(), size, started, {
        "capture_started_at": started,
        "capture_completed_at": utc_now().astimezone(timezone.utc).isoformat(),
        "source_size_bytes": size,
        "source_modified_at": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
    }


def _prepare(db_path, record, token, stage, digest, size, captured, details):
    with _open(db_path) as store:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = _owned(store, record["id"], token)
            filename = row["filename"] or _filename(row, captured)
            store.conn.execute(
                "UPDATE excel_archives SET captured_at=?,filename=?,sha256=?,size_bytes=?,"
                "stage_filename=?,details_json=?,updated_at=? WHERE id=?",
                (captured, filename, digest, size, stage.name, json.dumps(details, ensure_ascii=False),
                 utc_now().isoformat(), record["id"]),
            )
            store.conn.execute(
                "UPDATE archive_attempts SET captured_at=?,filename=?,sha256=? WHERE archive_id=? AND attempt=?",
                (captured, filename, digest, record["id"], row["attempts"]),
            )
            result = dict(_row(store, record["id"]))
            store.conn.commit()
            return result
        except Exception:
            store.conn.rollback()
            raise


def _publish_no_replace(stage, final, expected):
    """Atomic link on local filesystems; exclusive creation on others."""
    if final.exists():
        if _digest(final) != expected:
            raise ValueError("同名历史留档已存在且校验值不同，禁止覆盖；请检查原文件")
        return
    try:
        os.link(stage, final)
    except FileExistsError:
        if _digest(final) != expected:
            raise ValueError("同名历史留档已存在且校验值不同，禁止覆盖")
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOSYS, errno.ENOTSUP}:
            raise
        created = False
        try:
            with final.open("xb") as target:
                created = True
                with stage.open("rb") as source:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            if created:
                final.unlink(missing_ok=True)
            raise
    if _digest(final) != expected:
        raise ValueError("留档输出校验失败，未声明成功")


def _finish(db_path, archive_id, token):
    with _open(db_path) as store:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = _owned(store, archive_id, token)
            root = Path(row["output_dir"])
            _publish_no_replace(root / row["stage_filename"], root / row["filename"],
                                (row["sha256"], row["size_bytes"]))
            stamp = utc_now().isoformat()
            store.conn.execute(
                "UPDATE excel_archives SET state='ready',completed_at=?,updated_at=?,error=NULL,"
                "lease_token=NULL,lease_until=NULL WHERE id=?", (stamp, stamp, archive_id),
            )
            store.conn.execute(
                "UPDATE archive_attempts SET state='ready',finished_at=?,captured_at=?,filename=?,sha256=?,error=NULL "
                "WHERE archive_id=? AND attempt=?",
                (stamp, row["captured_at"], row["filename"], row["sha256"], archive_id, row["attempts"]),
            )
            result = _metadata(_row(store, archive_id))
            store.conn.commit()
        except Exception:
            store.conn.rollback()
            raise
    try:
        (root / row["stage_filename"]).unlink(missing_ok=True)
    except OSError:
        pass  # A successful immutable file must not become failed on tmp cleanup.
    return result


def _failed(db_path, archive_id, token, error):
    with _open(db_path) as store:
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            row = _row(store, archive_id)
            if row["lease_token"] == token and row["state"] == "running":
                message, stamp = str(error)[:2000], utc_now().isoformat()
                store.conn.execute(
                    "UPDATE excel_archives SET state='failed',error=?,updated_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                    (message, stamp, archive_id),
                )
                store.conn.execute(
                    "UPDATE archive_attempts SET state='failed',finished_at=?,error=? WHERE archive_id=? AND attempt=?",
                    (stamp, message, archive_id, row["attempts"]),
                )
            result = _metadata(_row(store, archive_id))
            store.conn.commit()
            return result
        except Exception:
            store.conn.rollback()
            raise


def _produce(db_path, record, token):
    if token is None:
        return _metadata(record) | {"reused": True}
    stage = Path(record["output_dir"]) / f".{record['id']}-{token}.part.xlsx"
    prepared = False
    try:
        stage.parent.mkdir(parents=True, exist_ok=True)
        expected = (record["sha256"], record["size_bytes"])
        if record["sha256"] and record["filename"]:
            final = stage.parent / record["filename"]
            saved_stage = stage.parent / record["stage_filename"]
            if final.exists() or saved_stage.exists():
                existing = final if final.exists() else saved_stage
                if _digest(existing) != expected:
                    raise ValueError("已捕获的留档文件校验失败，禁止用当前内容覆盖历史")
                return _finish(db_path, record["id"], token)
        if record["mode"] == "export":
            record, data = _export_snapshot(db_path, record["id"], token)
            details = json.loads(record["details_json"])
            export_workbook(data, stage, month=details["month"], calendar_id=details["calendar_id"],
                            as_of=record["captured_at"])
            digest, size = _digest(stage)
            captured = record["captured_at"]
        else:
            digest, size, captured, details = _copy_source(record["source_workbook"], stage)
            if record["sha256"]:
                # Only identical bytes can restore a lost, already captured copy.
                if (digest, size) != expected:
                    raise ValueError("原留档副本丢失且源文件已变化；不能重建过去状态，请手工新建留档")
                captured, details = record["captured_at"], json.loads(record["details_json"])
        record = _prepare(db_path, record, token, stage, digest, size, captured, details)
        prepared = True
        return _finish(db_path, record["id"], token) | {"reused": False}
    except Exception as exc:
        if not prepared:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
        return _failed(db_path, record["id"], token, exc)


def capture_archive(db_path, out_dir, actor="local-user", now=None):
    """Capture now, independently of the Sunday slot or its enabled setting."""
    actor = _actor(actor)
    config = get_archive_config(db_path)
    _, actual = _clock(now, config["timezone"])
    record, token = _claim(db_path, out_dir, config, actor, actual)
    return _produce(db_path, record, token)


def retry_archive(db_path, archive_id, out_dir, actor="local-user", now=None):
    """Retry the original capture/source, retaining every attempt and real time.

    An export retry reads its saved DB snapshot. A copy retry uses saved bytes;
    if no copy was ever captured, it reads the source at the current retry time.
    The original scheduled_for stays a schedule label, never a capture cutoff.
    """
    actor = _actor(actor)
    previous = get_archive(db_path, archive_id)
    _, actual = _clock(now, previous["timezone"])
    record, token = _claim(db_path, out_dir, previous, actor, actual, archive_id=archive_id)
    return _produce(db_path, record, token)


def run_due_archive(db_path, out_dir, now=None):
    """Return [] when not due, otherwise one ready/running/failed slot record.

    A slot is Sunday + configured timezone, so changing its time or restarting
    cannot create a second archive for that local Sunday. Missed past Sundays
    are never backfilled. Existing failed captures can be retried explicitly.
    """
    config = get_archive_config(db_path)
    instant, actual = _clock(now, config["timezone"])
    if not config["enabled"]:
        return []
    local = instant.astimezone(ZoneInfo(config["timezone"]))
    if local.weekday() != 6:
        return []
    hour, minute = map(int, config["sunday_time"].split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    # Choose the first occurrence of an ambiguous clock time; a nonexistent
    # spring-forward time is normalized forward to its real UTC round trip.
    scheduled = scheduled.astimezone(timezone.utc).astimezone(ZoneInfo(config["timezone"]))
    if instant < scheduled.astimezone(timezone.utc):
        return []
    key = "sunday|" + local.date().isoformat() + "|" + config["timezone"]
    record, token = _claim(db_path, out_dir, config, "local-scheduler", actual,
                           occurrence_key=key, scheduled_for=scheduled.isoformat())
    return [_produce(db_path, record, token)]
