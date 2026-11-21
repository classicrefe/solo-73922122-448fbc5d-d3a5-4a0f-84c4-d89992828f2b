"""演示目录隔离 / 旧库升级 / 并发首次加载 / 失败回滚 / 页面区分 的自测。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import create_app, get_db

DAY = "2026-11-20"
BIZ = "2026-11-21"
AS_OF = "2026-11-21T06:00:00+00:00"

# 升级前旧库的 schema（没有 is_demo 列），用于构造真实旧库。
LEGACY_SCHEMA = """
CREATE TABLE catalogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE partitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id INTEGER NOT NULL REFERENCES catalogs(id),
    partition_hour TEXT NOT NULL,
    planned_deadline TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id INTEGER NOT NULL REFERENCES catalogs(id),
    name TEXT NOT NULL,
    offset_days INTEGER NOT NULL DEFAULT 1,
    grace_minutes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id INTEGER NOT NULL REFERENCES catalogs(id),
    catalog_version INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL REFERENCES checks(id),
    report_name TEXT NOT NULL,
    offset_days INTEGER NOT NULL,
    grace_minutes INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    watermark_hour TEXT,
    status TEXT NOT NULL,
    block_hour TEXT,
    block_reason TEXT,
    block_message TEXT,
    counts_json TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def full_day(day: str = DAY) -> list[dict]:
    return [partition(h, day=day) for h in range(24)]


def partition(hour: int, submitted_min: int = 55, revision: int = 1,
              day: str = DAY) -> dict:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    h = start + timedelta(hours=hour)
    return {
        "partition_hour": h.isoformat(),
        "planned_deadline": (h + timedelta(minutes=60)).isoformat(),
        "submitted_at": (h + timedelta(minutes=submitted_min)).isoformat(),
        "revision": revision,
    }


def new_catalog(client: TestClient, name: str) -> int:
    return client.post("/api/catalogs", json={"name": name}).json()["id"]


def assert_demo_check_ready(client: TestClient, catalog_id: int, demo: dict) -> dict:
    """加载演示数据后无需额外操作即可直接核查（按钮内部正是这样做的）。"""
    check = client.post(f"/api/catalogs/{catalog_id}/checks",
                        json={"business_date": demo["business_date"],
                              "as_of": demo["as_of"]})
    assert check.status_code == 201, check.text
    return check.json()


def seed_legacy_db(path: str) -> int:
    """旧库：已有普通目录（分区 + 日报 + 核查），以及一个旧版“演示目录”。"""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    now = "2026-11-20T00:00:00+00:00"
    conn.execute("INSERT INTO catalogs(name, version, created_at) VALUES (?,?,?)",
                 ("普通目录", 1, now))
    conn.execute("INSERT INTO partitions(catalog_id, partition_hour, planned_deadline,"
                 " submitted_at, revision, created_at) VALUES (1,?,?,?,1,?)",
                 ("2026-11-19T00:00:00+00:00", "2026-11-19T01:00:00+00:00",
                  "2026-11-19T00:55:00+00:00", now))
    conn.execute("INSERT INTO reports(catalog_id, name, offset_days, grace_minutes,"
                 " created_at) VALUES (1,'旧日报',1,30,?)", (now,))
    conn.execute("INSERT INTO checks(catalog_id, catalog_version, business_date, as_of,"
                 " created_at) VALUES (1,1,'2026-11-20','2026-11-20T06:00:00+00:00',?)",
                 (now,))
    conn.execute("INSERT INTO check_results(check_id, report_name, offset_days,"
                 " grace_minutes, window_start, window_end, watermark_hour, status,"
                 " block_hour, block_reason, block_message, counts_json, detail_json)"
                 " VALUES (1,'旧日报',1,30,'2026-11-19T00:00:00+00:00',"
                 " '2026-11-19T23:00:00+00:00',NULL,'BLOCKED','2026-11-19T00:00:00+00:00',"
                 " 'MISSING','旧记录','{}','[]')")
    # 旧版演示按钮按名称复用，因此旧库里很可能已有同名目录，身份无法确认。
    conn.execute("INSERT INTO catalogs(name, version, created_at) VALUES (?,?,?)",
                 ("演示目录", 3, now))
    conn.execute("INSERT INTO partitions(catalog_id, partition_hour, planned_deadline,"
                 " submitted_at, revision, created_at) VALUES (2,?,?,?,1,?)",
                 ("2026-11-19T05:00:00+00:00", "2026-11-19T06:00:00+00:00",
                  "2026-11-19T05:55:00+00:00", now))
    conn.execute("INSERT INTO reports(catalog_id, name, offset_days, grace_minutes,"
                 " created_at) VALUES (2,'旧演示依赖',1,30,?)", (now,))
    conn.execute("INSERT INTO checks(catalog_id, catalog_version, business_date, as_of,"
                 " created_at) VALUES (2,3,'2026-11-20','2026-11-20T06:00:00+00:00',?)",
                 (now,))
    conn.commit()
    conn.close()
    return 2


# ---------- 专用目录只重置自己 ----------

def test_demo_never_touches_normal_catalog_with_same_name(tmp_path):
    """普通目录叫“演示目录”时，加载演示数据不得改动它的分区/依赖/核查。"""
    path = str(tmp_path / "t.db")
    app = create_app(path)
    with TestClient(app) as client:
        cid = new_catalog(client, "演示目录")
        client.post(f"/api/catalogs/{cid}/partitions",
                    json={"partitions": full_day()})
        client.post(f"/api/catalogs/{cid}/reports",
                    json={"name": "我的日报", "offset_days": 1, "grace_minutes": 30})
        before = client.get(f"/api/catalogs/{cid}").json()
        check = client.post(f"/api/catalogs/{cid}/checks",
                            json={"business_date": BIZ, "as_of": AS_OF}).json()

        demo = client.post("/api/demo").json()
        demo2 = client.post("/api/demo").json()

        assert demo["catalog_id"] != cid
        assert demo2["catalog_id"] == demo["catalog_id"]
        assert demo["is_demo"] is True and demo["created"] is True
        assert demo2["created"] is False

        after = client.get(f"/api/catalogs/{cid}").json()
        assert after == before  # 名称、版本均未变化
        parts = client.get(f"/api/catalogs/{cid}/partitions").json()["partitions"]
        assert len(parts) == 24
        reports = client.get(f"/api/catalogs/{cid}/reports").json()["reports"]
        assert [r["name"] for r in reports] == ["我的日报"]
        checks = client.get(f"/api/catalogs/{cid}/checks").json()["checks"]
        assert checks[0]["stale"] is False
        detail = client.get(f"/api/checks/{check['id']}").json()
        assert detail["stale"] is False and detail["results"][0]["status"] == "READY"

        # 专用目录确实被重置了：再次加载后分区/依赖数量不累积
        demo_parts = client.get(
            f"/api/catalogs/{demo['catalog_id']}/partitions").json()["partitions"]
        assert len(demo_parts) == 49  # 前天 24 + 昨天 23（缺 18 点）+ 2 条修订/重复
        demo_reports = client.get(
            f"/api/catalogs/{demo['catalog_id']}/reports").json()["reports"]
        assert len(demo_reports) == 3


def test_demo_button_result_immediately_checkable(client):
    """页面在加载演示数据后立即对返回目录核查，无需手工选择。"""
    first = client.post("/api/demo").json()
    check = assert_demo_check_ready(client, first["catalog_id"], first)
    by_name = {r["report_name"]: r for r in check["results"]}
    assert by_name["运营日报"]["status"] == "BLOCKED"
    assert by_name["设备日报"]["block_reason"] == "LATE"
    assert by_name["前日回顾报"]["status"] == "READY"
    # 核查基于最新版本 -> 有效，历史列表里可直接查看
    checks = client.get(
        f"/api/catalogs/{first['catalog_id']}/checks").json()["checks"]
    assert checks[0]["stale"] is False


# ---------- 并发首次加载 ----------

def test_concurrent_first_load_creates_one_demo(tmp_path):
    """两个浏览器（各自连接）同时首次加载：只创建一个专用目录，其余复用。"""
    path = str(tmp_path / "t.db")
    app = create_app(path)
    with TestClient(app) as client:
        new_catalog(client, "演示目录")  # 同名普通目录不应被并发请求碰到
        cid = new_catalog(client, "其它")
        client.post(f"/api/catalogs/{cid}/partitions",
                    json={"partitions": full_day()})

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        client = TestClient(app)
        try:
            barrier.wait(timeout=10)
            r = client.post("/api/demo")
            assert r.status_code == 201, r.text
            results.append(r.json())
        except BaseException as exc:  # noqa: BLE001 - 记录后统一断言
            errors.append(exc)
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert {r["catalog_id"] for r in results} == {results[0]["catalog_id"]}
    assert sorted(r["created"] for r in results) == [False, True]

    conn = sqlite3.connect(path)
    try:
        demos = conn.execute(
            "SELECT id, name FROM catalogs WHERE is_demo = 1").fetchall()
        normals = conn.execute(
            "SELECT id FROM catalogs WHERE is_demo = 0 AND name = '演示目录'").fetchall()
        parts = conn.execute(
            "SELECT COUNT(*) FROM partitions WHERE catalog_id = ?",
            (demos[0][0],)).fetchone()[0]
        reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE catalog_id = ?",
            (demos[0][0],)).fetchone()[0]
    finally:
        conn.close()
    assert len(demos) == 1
    assert len(normals) == 1  # 普通同名目录原样保留
    assert parts == 49 and reports == 3  # 没有重复插入，分区与依赖一起更新

    # 重启（重新建 app）后继续复用同一目录
    app2 = create_app(path)
    with TestClient(app2) as client:
        again = client.post("/api/demo").json()
    assert again["catalog_id"] == demos[0][0]
    assert again["created"] is False


def test_restart_reuses_demo_and_keeps_history(tmp_path):
    path = str(tmp_path / "t.db")
    with TestClient(create_app(path)) as client:
        first = client.post("/api/demo").json()
        assert_demo_check_ready(client, first["catalog_id"], first)
        old_checks = client.get(
            f"/api/catalogs/{first['catalog_id']}/checks").json()["checks"]
        assert len(old_checks) == 1

    with TestClient(create_app(path)) as client:
        second = client.post("/api/demo").json()
        assert second["catalog_id"] == first["catalog_id"]
        assert second["created"] is False
        catalogs = client.get("/api/catalogs").json()["catalogs"]
        assert len([c for c in catalogs if c["is_demo"]]) == 1
        # 重置后旧核查仍然保留，但被标记为失效
        checks = client.get(
            f"/api/catalogs/{first['catalog_id']}/checks").json()["checks"]
        assert len(checks) == 1 and checks[0]["stale"] is True
        assert_demo_check_ready(client, second["catalog_id"], second)


# ---------- 旧库升级 ----------

def test_legacy_db_upgrade_preserves_unidentified_demo_dir(tmp_path):
    """升级旧库：身份无法确认的旧“演示目录”及其分区/依赖/核查全部保留，
    仍作为普通目录存在；同名新专用目录另行创建，两者可区分。"""
    path = str(tmp_path / "legacy.db")
    legacy_id = seed_legacy_db(path)
    assert legacy_id == 2

    with TestClient(create_app(path)) as client:
        catalogs = client.get("/api/catalogs").json()["catalogs"]
        by_id = {c["id"]: c for c in catalogs}
        assert by_id[2]["name"] == "演示目录" and by_id[2]["is_demo"] == 0
        assert by_id[1]["is_demo"] == 0
        # 旧数据未被改写
        assert by_id[2]["version"] == 3
        parts = client.get("/api/catalogs/2/partitions").json()["partitions"]
        assert len(parts) == 1
        reports = client.get("/api/catalogs/2/reports").json()["reports"]
        assert [r["name"] for r in reports] == ["旧演示依赖"]
        checks = client.get("/api/catalogs/2/checks").json()["checks"]
        assert len(checks) == 1 and checks[0]["stale"] is False
        detail = client.get("/api/checks/2").json()
        assert detail["business_date"] == "2026-11-20"

        demo = client.post("/api/demo").json()
        assert demo["created"] is True
        assert demo["catalog_id"] != 2

        # 旧目录原样；新专用目录带 is_demo 标记，同名但可区分
        catalogs = client.get("/api/catalogs").json()["catalogs"]
        names = sorted((c["name"], c["is_demo"]) for c in catalogs)
        assert names == [("普通目录", 0), ("演示目录", 0), ("演示目录", 1)]
        parts = client.get("/api/catalogs/2/partitions").json()["partitions"]
        assert len(parts) == 1
        reports = client.get("/api/catalogs/2/reports").json()["reports"]
        assert [r["name"] for r in reports] == ["旧演示依赖"]

        # 再次加载只重置专用目录
        demo2 = client.post("/api/demo").json()
        assert demo2["catalog_id"] == demo["catalog_id"]
        assert len(client.get("/api/catalogs/2/partitions").json()["partitions"]) == 1


# ---------- 失败回滚 ----------

def _snapshot(conn):
    def rows(sql):
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    return {
        "catalogs": rows("SELECT id, name, version, is_demo FROM catalogs ORDER BY id"),
        "partitions": rows("SELECT * FROM partitions ORDER BY id"),
        "reports": rows("SELECT * FROM reports ORDER BY id"),
    }


class _FailingExecutemany:
    """包装真实连接：对指定 SQL 的 executemany 抛错，其余操作透传。"""

    def __init__(self, real, marker: str):
        self._real = real
        self._marker = marker

    def __getattr__(self, name):
        return getattr(self._real, name)

    def executemany(self, sql, params):
        if self._marker in sql:
            raise sqlite3.OperationalError("simulated failure")
        return self._real.executemany(sql, params)


def _failing_db(path: str, marker: str):
    def dependency():
        conn = _FailingExecutemany(dbmod.connect(path), marker)
        try:
            yield conn
        finally:
            conn.close()
    return dependency


def test_failed_reload_keeps_previous_complete_demo(tmp_path):
    """重载时日报依赖写入失败：分区、依赖、目录版本完整回滚到上一次。"""
    path = str(tmp_path / "t.db")
    app = create_app(path)
    with TestClient(app, raise_server_exceptions=False) as client:
        demo = client.post("/api/demo").json()
        conn = sqlite3.connect(path)
        before = _snapshot(conn)
        conn.close()

        app.dependency_overrides[get_db] = _failing_db(path, "INSERT INTO reports")
        try:
            assert client.post("/api/demo").status_code == 500
        finally:
            app.dependency_overrides.clear()

        conn = sqlite3.connect(path)
        after = _snapshot(conn)
        conn.close()
        # 分区、日报依赖、目录版本全部保持上一次的完整状态
        assert after == before
        check = assert_demo_check_ready(client, demo["catalog_id"], demo)
        assert len(check["results"]) == 3


def test_failed_first_load_leaves_no_empty_demo(tmp_path):
    """首次加载在写分区时失败：连专用目录本身也不留下，可无副作用重试。"""
    path = str(tmp_path / "t.db")
    app = create_app(path)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.dependency_overrides[get_db] = _failing_db(path, "INSERT INTO partitions")
        try:
            assert client.post("/api/demo").status_code == 500
        finally:
            app.dependency_overrides.clear()

        assert client.get("/api/catalogs").json()["catalogs"] == []
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM catalogs").fetchone()[0] == 0
        conn.close()

        # 故障消除后重试：正常创建，分区与依赖都有内容
        demo = client.post("/api/demo").json()
        assert demo["created"] is True
        parts = client.get(
            f"/api/catalogs/{demo['catalog_id']}/partitions").json()["partitions"]
        reports = client.get(
            f"/api/catalogs/{demo['catalog_id']}/reports").json()["reports"]
        assert len(parts) == 49 and len(reports) == 3


# ---------- 数据库层约束 ----------

def test_single_demo_index_enforced(tmp_path):
    """部分唯一索引兜底：数据库层面不可能出现第二个专用演示目录。"""
    path = str(tmp_path / "t.db")
    dbmod.init_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO catalogs(name, version, is_demo, created_at)"
                     " VALUES ('演示目录', 0, 1, 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO catalogs(name, version, is_demo, created_at)"
                         " VALUES ('演示目录', 0, 1, 'x')")
        # 普通同名目录可以有任意多个
        conn.execute("INSERT INTO catalogs(name, version, is_demo, created_at)"
                     " VALUES ('演示目录', 0, 0, 'x')")
        conn.execute("INSERT INTO catalogs(name, version, is_demo, created_at)"
                     " VALUES ('演示目录', 0, 0, 'x')")
        conn.commit()
    finally:
        conn.close()
