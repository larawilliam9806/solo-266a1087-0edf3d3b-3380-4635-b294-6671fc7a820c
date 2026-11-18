#!/usr/bin/env python3
"""关键功能自测：只读约束、并发名额、超时、取消、截断、失败不留半份结果，
以及 DELETE 演练（隔离副本、触发器/外键、前后对比、失败无部分报告、名额共享）。

用法：
    python3 tests/selftest.py <base_url> <demo_db_path> <server_upload_dir>

需要被测服务已在运行（UPLOAD_DIR 指向 <server_upload_dir>）。
仅用标准库，可用 python3 -I 运行。
"""
import hashlib
import http.cookiejar
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

SLOW_SQL = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt "
    "WHERE x < 300000000) SELECT COUNT(*), SUM(x) FROM cnt"
)
DUP_SQL = (
    "SELECT sensor_id, ts, COUNT(*) AS n FROM sensor_samples "
    "GROUP BY sensor_id, ts HAVING COUNT(*) > 1 ORDER BY n DESC, sensor_id, ts"
)
# 去重 DELETE：每组保留最小 id，触发器会把被删行归档到 sample_audit
DEDUP_DELETE = (
    "WITH ranked AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY sensor_id, ts ORDER BY id) AS rn "
    "FROM sensor_samples) "
    "DELETE FROM sensor_samples WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"
)
SLOW_DELETE = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt "
    "WHERE x < 300000000) DELETE FROM sensor_samples WHERE id IN (SELECT x FROM cnt)"
)

failures = []
checks = [0]


def check(name, cond, detail=""):
    checks[0] += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Client:
    """带独立 cookie 会话的 HTTP 客户端。"""

    def __init__(self, base):
        self.base = base
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(self, method, path, payload=None):
        data = headers = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def upload(self, path, filename="demo.db", content=None):
        if content is None:
            with open(path, "rb") as f:
                content = f.read()
        boundary = "----selftest-boundary-9f2c"
        body = b"\r\n".join(
            [
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
                b"Content-Type: application/octet-stream",
                b"",
                content,
                f"--{boundary}--".encode(),
                b"",
            ]
        )
        req = urllib.request.Request(
            self.base + "/upload",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def run_query(self, sql):
        return self.call("POST", "/query", {"sql": sql})

    def run_rehearsal(self, sql):
        return self.call("POST", "/rehearsal", {"sql": sql})

    def wait_final(self, job_id, timeout=20.0, kind="query"):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st, j = self.call("GET", f"/{kind}/{job_id}")
            assert st == 200, f"status {st}"
            if j["status"] in ("done", "error", "timeout", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("任务未在预期时间内到达终态")

    def wait_rehearsal_final(self, job_id, timeout=20.0):
        return self.wait_final(job_id, timeout=timeout, kind="rehearsal")


def main():
    base, demo_db, upload_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    demo_hash = sha256(demo_db)
    c = Client(base)

    print("== 页面与上传 ==")
    with c.opener.open(base + "/", timeout=10) as resp:
        html = resp.read().decode()
    check("首页可访问且为排查页", resp.status == 200 and "只读 SQL" in html)

    st, j = c.upload(None, "evil.db", b"not a sqlite database at all........")
    check("非 SQLite 文件被拒绝", st == 400, f"got {st} {j}")

    st, j = c.upload(demo_db, "demo_sensors.db")
    check("演示库上传成功", st == 200 and j.get("ok"), f"got {st} {j}")

    st, j = c.call("GET", "/schema")
    tables = {t["name"]: t for t in j.get("tables", [])} if st == 200 else {}
    check("表结构包含 sensor_samples", "sensor_samples" in tables)
    if "sensor_samples" in tables:
        cols = {col["name"] for col in tables["sensor_samples"]["columns"]}
        check("列结构完整", {"id", "sensor_id", "ts", "value", "batch_id"} <= cols, str(cols))
        check("行数统计合理", (tables["sensor_samples"]["row_count"] or 0) > 12000)

    print("== 只读约束 ==")
    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    total_before = j["rows"][0][0] if j["status"] == "done" else None
    check("SELECT 正常执行", j["status"] == "done" and total_before, str(j))

    for bad in ["DELETE FROM sensor_samples", "UPDATE sensor_samples SET value=0",
                "PRAGMA user_version=1", "ATTACH DATABASE '/tmp/evil.db' AS e",
                "DROP TABLE sensor_samples", "INSERT INTO sensors VALUES ('X','x','y','z')"]:
        st, j = c.run_query(bad)
        check(f"非只读语句被 400 拒绝: {bad.split()[0]}", st == 400, f"got {st} {j}")

    st, j = c.run_query("WITH d AS (SELECT 1) DELETE FROM sensor_samples")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("WITH 伪装的 DELETE 被 authorizer 拦截", j["status"] == "error" and "not authorized" in j.get("error", ""), str(j))
    else:
        check("WITH 伪装的 DELETE 被 authorizer 拦截", False, f"got {st} {j}")

    st, j = c.run_query("SELECT load_extension('/tmp/evil.so')")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("load_extension 被拦截", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT 1; SELECT 2")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("多语句被拒绝", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    check("写尝试后数据未变", j["status"] == "done" and j["rows"][0][0] == total_before,
          f"before={total_before} after={j.get('rows')}")

    st, j = c.run_query("SELECT name, type FROM pragma_table_info('sensor_samples') ORDER BY cid")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("pragma 函数只读访问可用", j["status"] == "done" and j["row_count"] == 6, str(j)[:200])

    print("== 重复采样排查 SQL ==")
    st, j = c.run_query(DUP_SQL)
    j = c.wait_final(j["job_id"])
    check("重复汇总查询成功", j["status"] == "done", str(j)[:200])
    if j["status"] == "done":
        check("找到 270 组重复", j["row_count"] == 270, f"got {j['row_count']}")
        check("最大重复次数为 3", j["rows"][0][2] == 3, str(j["rows"][0]))
        check("未截断", j["truncated"] is False)

    print("== 行数上限与截断 ==")
    st, j = c.run_query(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5000) SELECT x FROM c"
    )
    j = c.wait_final(j["job_id"])
    check("超过 1000 行被截断", j["status"] == "done" and j["row_count"] == 1000 and j["truncated"] is True,
          f"rows={j.get('row_count')} truncated={j.get('truncated')}")

    print("== 失败不留半份结果 ==")
    st, j = c.run_query("SELECT * FROM no_such_table")
    j = c.wait_final(j["job_id"])
    check("错误查询无结果字段", j["status"] == "error" and "rows" not in j and "columns" not in j, str(j))

    print("== 超时自动终止 ==")
    t0 = time.monotonic()
    st, j = c.run_query(SLOW_SQL)
    check("慢查询被受理", st == 200, f"got {st} {j}")
    j = c.wait_final(j["job_id"])
    wall = time.monotonic() - t0
    check("慢查询 3s 左右自动终止", j["status"] == "timeout" and 2.0 <= wall <= 8.0,
          f"status={j['status']} wall={wall:.2f}s elapsed_ms={j['elapsed_ms']}")

    print("== 并发名额与取消 ==")
    c2, c3 = Client(base), Client(base)
    st, _ = c2.upload(demo_db, "demo_sensors.db")
    assert st == 200
    st, _ = c3.upload(demo_db, "demo_sensors.db")
    assert st == 200

    st1, r1 = c2.run_query(SLOW_SQL)
    st2, r2 = c3.run_query(SLOW_SQL)
    check("两条慢查询均被受理", st1 == 200 and st2 == 200, f"{st1} {st2}")
    time.sleep(0.6)
    st3, r3 = c.run_query("SELECT 1")
    check("第三条查询被 429 明确拒绝", st3 == 429 and "满" in r3.get("error", ""), f"got {st3} {r3}")

    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r1['job_id']}/cancel")
    cancel_wall = time.monotonic() - t0
    check("取消接口返回 cancelled 且响应迅速", j.get("status") == "cancelled" and cancel_wall < 2.5,
          f"status={j.get('status')} wall={cancel_wall:.2f}s")

    st, j = c.run_query("SELECT 1 AS ok")
    check("取消后名额已释放（新查询立即受理）", st == 200, f"got {st} {j}")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("新查询正常完成", j["status"] == "done" and j["rows"][0][0] == 1, str(j))

    j2 = c3.wait_final(r2["job_id"])
    check("另一条查询不受取消影响（自然超时终止）", j2["status"] == "timeout", j2["status"])

    print("== 手动取消进行中的查询 ==")
    st, r = c2.run_query(SLOW_SQL)
    check("慢查询再次被受理", st == 200, f"got {st}")
    time.sleep(0.7)
    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r['job_id']}/cancel")
    wall = time.monotonic() - t0
    check("取消生效且真实停止计算", j.get("status") == "cancelled" and wall < 2.5 and j.get("elapsed_ms", 9999) < 2500,
          f"status={j.get('status')} wall={wall:.2f}s elapsed={j.get('elapsed_ms')}ms")

    print("== DELETE 演练：语句预检 ==")
    for bad in ["SELECT * FROM sensor_samples",
                "UPDATE sensor_samples SET value=0",
                "INSERT INTO sensors VALUES ('X','x','y','z')",
                "DROP TABLE sensor_samples",
                "PRAGMA user_version=1",
                "WITH d AS (SELECT 1) SELECT * FROM d",
                "WITH d AS (SELECT 1) UPDATE sensor_samples SET value=0"]:
        st, j = c.run_rehearsal(bad)
        check(f"非 DELETE 演练被 400 拒绝: {bad.split()[0]}", st == 400, f"got {st} {j}")

    print("== DELETE 演练：成功并对比前后数据 ==")
    st, j = c.run_rehearsal(DEDUP_DELETE)
    check("去重演练被受理", st == 200, f"got {st} {j}")
    j = c.wait_rehearsal_final(j["job_id"])
    check("演练完成", j["status"] == "done", str(j)[:300])
    rep = j.get("report") or {}
    tables = {t["name"]: t for t in rep.get("tables", [])}
    ss = tables.get("sensor_samples", {})
    sa = tables.get("sample_audit", {})
    so = tables.get("sensors", {})
    check("sensor_samples 删除 300 行且无新增",
          ss.get("deleted_count") == 300 and ss.get("added_count") == 0,
          f"del={ss.get('deleted_count')} add={ss.get('added_count')}")
    check("sensor_samples 行数 13260 -> 12960",
          ss.get("row_count_before") == total_before and ss.get("row_count_after") == total_before - 300,
          f"{ss.get('row_count_before')} -> {ss.get('row_count_after')}")
    check("触发器影响计入：sample_audit 新增 300 行",
          sa.get("added_count") == 300 and sa.get("deleted_count") == 0,
          f"add={sa.get('added_count')} del={sa.get('deleted_count')}")
    check("sensors 表无变化", so.get("deleted_count") == 0 and so.get("added_count") == 0)
    check("DELETE 直接行数计入报告", rep.get("delete_rowcount") == 300, str(rep.get("delete_rowcount")))
    check("变化表统计正确", rep.get("changed_tables") == 2 and rep.get("table_count") == 3,
          f"changed={rep.get('changed_tables')} total={rep.get('table_count')}")
    ds = ss.get("deleted_samples", [])
    check("删除样例 1~20 条", 1 <= len(ds) <= 20, f"n={len(ds)}")
    check("样例含完整行、值类型与重复次数",
          all("values" in s and "types" in s and "count" in s for s in ds)
          and all(len(s["values"]) == 6 and len(s["types"]) == 6 for s in ds),
          str(ds[:1]))
    check("删除样例每行重复次数为 1", all(s["count"] == 1 for s in ds))
    check("值类型标注正确", all(s["types"][0] == "INTEGER" and s["types"][3] == "REAL" for s in ds),
          str(ds[:1]))
    check("审计表新增样例存在", 1 <= len(sa.get("added_samples", [])) <= 20)

    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    check("演练后上传原件行数未变", j["status"] == "done" and j["rows"][0][0] == total_before,
          f"before={total_before} after={j.get('rows')}")
    st, j = c.run_query("SELECT COUNT(*) AS n FROM sample_audit")
    j = c.wait_final(j["job_id"])
    check("上传原件审计表仍为空（变更已丢弃）", j["status"] == "done" and j["rows"][0][0] == 0, str(j.get("rows")))

    print("== DELETE 演练：约束失败 / 危险函数 / 多语句 ==")
    st, j = c.run_rehearsal("DELETE FROM sensors")
    j = c.wait_rehearsal_final(j["job_id"])
    check("外键约束失败为 error 且无部分报告",
          j["status"] == "error" and "report" not in j and "FOREIGN KEY" in j.get("error", ""), str(j)[:200])
    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensors")
    j = c.wait_final(j["job_id"])
    check("约束失败后原件 sensors 未变", j["status"] == "done" and j["rows"][0][0] == 6, str(j.get("rows")))

    st, j = c.run_rehearsal("DELETE FROM sensor_samples WHERE load_extension('/tmp/evil.so')")
    j = c.wait_rehearsal_final(j["job_id"])
    check("演练中 load_extension 被拦截且无报告", j["status"] == "error" and "report" not in j, str(j)[:200])

    st, j = c.run_rehearsal("DELETE FROM sensor_samples WHERE id < 0; DELETE FROM sensors")
    j = c.wait_rehearsal_final(j["job_id"])
    check("多语句演练失败且无报告", j["status"] == "error" and "report" not in j, str(j)[:200])

    st, j = c.run_rehearsal("DELETE FROM sensor_samples WHERE id IN (ATTACH DATABASE '/tmp/e.db' AS e)")
    j = c.wait_rehearsal_final(j["job_id"])
    check("演练无法触达库外文件（语法错误安全失败）", j["status"] == "error" and "report" not in j, str(j)[:200])

    print("== DELETE 演练：超时与取消 ==")
    t0 = time.monotonic()
    st, r = c.run_rehearsal(SLOW_DELETE)
    check("慢演练被受理", st == 200, f"got {st} {r}")
    j = c.wait_rehearsal_final(r["job_id"], timeout=15)
    wall = time.monotonic() - t0
    check("慢演练 3s 左右自动终止且无报告",
          j["status"] == "timeout" and "report" not in j and 2.0 <= wall <= 10.0,
          f"status={j['status']} wall={wall:.2f}s elapsed_ms={j['elapsed_ms']}")

    st, r = c.run_rehearsal(SLOW_DELETE)
    check("慢演练再次被受理", st == 200, f"got {st}")
    time.sleep(0.7)
    t0 = time.monotonic()
    st, j = c.call("POST", f"/rehearsal/{r['job_id']}/cancel")
    wall = time.monotonic() - t0
    check("演练取消生效、真实停止且无报告",
          j.get("status") == "cancelled" and "report" not in j and wall < 2.5,
          f"status={j.get('status')} wall={wall:.2f}s")

    print("== 查询与演练共享执行名额 ==")
    st1, r1 = c2.run_query(SLOW_SQL)
    st2, r2 = c3.run_rehearsal(SLOW_DELETE)
    check("一条查询 + 一条演练均被受理", st1 == 200 and st2 == 200, f"{st1} {st2}")
    time.sleep(0.6)
    st3, r3 = c.run_query("SELECT 1")
    check("第三个查询被 429 明确拒绝", st3 == 429 and "满" in r3.get("error", ""), f"got {st3} {r3}")
    st4, r4 = c.run_rehearsal("DELETE FROM sensor_samples")
    check("第三个演练被 429 明确拒绝", st4 == 429 and "满" in r4.get("error", ""), f"got {st4} {r4}")

    st, j = c2.call("POST", f"/query/{r1['job_id']}/cancel")
    check("查询取消成功", j.get("status") == "cancelled", j.get("status"))
    st, j = c3.call("POST", f"/rehearsal/{r2['job_id']}/cancel")
    check("演练取消成功", j.get("status") == "cancelled", j.get("status"))

    st, j = c.run_query("SELECT 1 AS ok")
    check("名额释放后新查询立即受理", st == 200, f"got {st} {j}")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("新查询正常完成", j["status"] == "done" and j["rows"][0][0] == 1, str(j))

    print("== DELETE 演练：含虚拟表的库被拒绝 ==")
    fts_dir = tempfile.mkdtemp(prefix="selftest-fts-")
    fts_path = os.path.join(fts_dir, "fts.db")
    fts_hash = None
    try:
        fc = sqlite3.connect(fts_path)
        fc.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
        fc.execute("INSERT INTO docs VALUES ('hello world')")
        fc.commit()
        fc.close()
        fts_hash = sha256(fts_path)
    except sqlite3.Error as exc:
        print(f"  SKIP  当前 SQLite 不支持 FTS5，跳过虚拟表检查（{exc}）")
    if fts_hash:
        c5 = Client(base)
        st, j = c5.upload(fts_path, "fts.db")
        check("含虚拟表的库可上传（只读查询不受影响）", st == 200 and j.get("ok"), f"got {st} {j}")
        st, j = c5.run_query("SELECT * FROM docs")
        j5 = c5.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
        check("含虚拟表的库只读查询照常", j5["status"] == "done" and j5["row_count"] == 1, str(j5)[:200])
        st, j = c5.run_rehearsal("DELETE FROM docs")
        check("含虚拟表的库演练被 400 拒绝", st == 400 and "虚拟表" in j.get("error", ""), f"got {st} {j}")

    print("== 数据库文件未被修改 ==")
    uploaded = [os.path.join(upload_dir, f) for f in os.listdir(upload_dir) if f.endswith(".db")]
    check("服务端存在上传副本", len(uploaded) >= 3, str(uploaded))
    expected = {demo_hash}
    if fts_hash:
        expected.add(fts_hash)
    for p in uploaded:
        check(f"副本哈希未变 {os.path.basename(p)}", sha256(p) in expected)
    leftovers = [f for f in os.listdir(upload_dir) if f.startswith("rehearsal-")]
    check("演练临时目录已全部清理", not leftovers, str(leftovers))

    print("== 内置演示库入口 ==")
    c4 = Client(base)
    st, j = c4.call("POST", "/use-demo")
    check("use-demo 可用", st == 200 and j.get("ok"), f"got {st} {j}")
    st, j = c4.call("GET", "/schema")
    check("演示库表结构可读", st == 200 and any(t["name"] == "sensor_samples" for t in j.get("tables", [])))

    print()
    print(f"共 {checks[0]} 项检查，失败 {len(failures)} 项")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
