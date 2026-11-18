"""DELETE 演练执行器。

数据工程师在真正入库修复前，可以用它确认一条 DELETE（可带 WITH）连同库内
原有触发器、外键约束会改动哪些数据：

- 在**隔离副本**上执行（复制上传件到独立临时目录，原件不动），副本连接上
  显式 `PRAGMA foreign_keys = ON`，触发器随 DELETE 自然触发；
- 与只读查询**共享同一组执行名额**（信号量由上层注入），满额 submit() 返回 None；
- 超时 / 取消走 conn.interrupt()，真正停止 SQLite 正在执行的工作；
- 约束失败、超时、取消或任何内部错误：回滚事务、删除隔离副本，**不返回部分报告**；
- worker 在关闭连接、删除副本、释放名额之后才对外落定终态；
- 成功时按（值类型, 完整行, 重复次数）对比所有用户表的前后数据，逐表给出
  新增 / 删除行数与最多 sample_limit 条样例；UPDATE 自然体现为删旧行 + 增新行，
  触发器写入的其他表也一并计入。

SQL 约束：authorizer 只放行 DELETE 及其触发器 / 外键级联所需动作，拒绝
ATTACH / DDL / load_extension 等，演练 SQL 无法触达隔离库之外的文件。
"""
from __future__ import annotations

import collections
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from urllib.parse import quote

TERMINAL_STATES = frozenset({"done", "error", "timeout", "cancelled"})

_BLOCKED_FUNCTIONS = frozenset({"load_extension"})

# 演练期间放行的动作：DELETE 本体及其触发的触发器 / 外键级联所需的最小集合。
# 用户 SQL 经文本预检限定为单条 DELETE（可带 WITH），多语句由 sqlite3 自身拒绝；
# TRANSACTION 仅由 worker 自身的 BEGIN/ROLLBACK 触发（用户单条 DELETE 不可达）；
# PRAGMA 仅可能以表值函数形式出现在 WHERE 中（只读），语句级 PRAGMA 不可达；
# ATTACH / DETACH / CREATE / DROP / ALTER / SAVEPOINT 等一律拒绝，
# 因此演练 SQL 无法访问隔离库之外的文件，也无法加载扩展。
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,      # WHERE 子查询、触发器体内的 SELECT
        sqlite3.SQLITE_READ,        # 读取表/列
        sqlite3.SQLITE_FUNCTION,    # SQL 函数（load_extension 单独拦截）
        sqlite3.SQLITE_RECURSIVE,   # 递归 CTE / 视图展开
        sqlite3.SQLITE_DELETE,      # DELETE 本体、触发器 / 外键级联删除
        sqlite3.SQLITE_INSERT,      # 触发器写入（如审计表）
        sqlite3.SQLITE_UPDATE,      # 触发器更新、外键 ON DELETE SET NULL 等
        sqlite3.SQLITE_TRANSACTION, # worker 自己的 BEGIN / ROLLBACK
        sqlite3.SQLITE_PRAGMA,      # pragma 表值函数（只读）
    }
)


def rehearsal_authorizer(action, arg1, arg2, dbname, source):
    """SQLite authorizer：只放行演练所需动作，其余一律拒绝。"""
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# ---------------- SQL 文本预检（硬约束在 authorizer） ----------------

def _skip_ws_comments(s, i):
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i + 2)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j < 0:
                return n
            i = j + 2
        else:
            break
    return i


def _skip_quoted(s, i):
    """跳过引号串（'..'、".."、`..`、[..]），返回其后的下标。"""
    q = s[i]
    n = len(s)
    if q == "[":
        j = s.find("]", i + 1)
        return n if j < 0 else j + 1
    i += 1
    while i < n:
        if s[i] == q:
            if i + 1 < n and s[i + 1] == q:  # 双写引号转义
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _skip_parens(s, i):
    """跳过以 s[i]=='(' 开头的括号块（含嵌套、字符串、注释）。不配对返回 -1。"""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "'\"`[":
            i = _skip_quoted(s, i)
            continue
        if s.startswith("--", i) or s.startswith("/*", i):
            i = _skip_ws_comments(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _read_word(s, i):
    m = re.match(r"[A-Za-z0-9_]+", s[i:])
    if not m:
        return "", i
    return m.group(0).upper(), i + m.end()


def main_statement_keyword(sql):
    """返回主语句关键字（大写）；WITH 开头时跳过 CTE 列表取其后关键字。

    轻量词法扫描（识别字符串/标识符引号、注释、括号嵌套），无法解析时返回 ''。
    """
    i = _skip_ws_comments(sql, 0)
    kw, i = _read_word(sql, i)
    if kw != "WITH":
        return kw
    i = _skip_ws_comments(sql, i)
    w, i2 = _read_word(sql, i)
    if w == "RECURSIVE":
        i = i2
    while True:  # 逐个跳过 CTE：name [(cols)] AS [NOT] [MATERIALIZED] ( ... )
        i = _skip_ws_comments(sql, i)
        if i < len(sql) and sql[i] in "'\"`[":
            i = _skip_quoted(sql, i)
        else:
            w, i = _read_word(sql, i)
            if not w:
                return ""
        i = _skip_ws_comments(sql, i)
        if i < len(sql) and sql[i] == "(":  # CTE 列名列表
            i = _skip_parens(sql, i)
            if i < 0:
                return ""
            i = _skip_ws_comments(sql, i)
        w, i = _read_word(sql, i)
        if w != "AS":
            return ""
        i = _skip_ws_comments(sql, i)
        w, i2 = _read_word(sql, i)
        if w == "NOT":
            i = _skip_ws_comments(sql, i2)
            w, i2 = _read_word(sql, i)
            if w != "MATERIALIZED":
                return ""
            i = i2
        elif w == "MATERIALIZED":
            i = i2
        i = _skip_ws_comments(sql, i)
        if i >= len(sql) or sql[i] != "(":
            return ""
        i = _skip_parens(sql, i)
        if i < 0:
            return ""
        i = _skip_ws_comments(sql, i)
        if i < len(sql) and sql[i] == ",":
            i += 1
            continue
        w, _ = _read_word(sql, i)
        return w


# ---------------- 库结构检查 ----------------

def find_virtual_tables(db_path):
    """返回库中虚拟表名列表（无则空列表）。只读打开，不修改文件。"""
    uri = "file:" + quote(os.path.abspath(db_path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return [
            name
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
            if sql and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
        ]
    finally:
        conn.close()


# ---------------- 行级快照与对比 ----------------

# 比较口径：SQLite 存储类（值类型）+ 完整行值 + 重复次数。
# Python 驱动返回的类型与 SQLite 存储类一一对应，借此区分 1 / 1.0 / '1'。
_TYPE_NAMES = {
    "int": "INTEGER",
    "float": "REAL",
    "str": "TEXT",
    "bytes": "BLOB",
    "NoneType": "NULL",
}


def _row_key(row):
    return tuple((_TYPE_NAMES[type(v).__name__], v) for v in row)


def _json_safe(value):
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


class RehearsalError(Exception):
    """演练业务错误（如快照行数超限），按 error 终态返回。"""


class _Cancelled(Exception):
    """内部控制流：在阶段边界发现取消/超时请求。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _status_for(reason):
    return "timeout" if reason == "timeout" else "cancelled"


class RehearsalJob:
    """一次演练的生命周期：queued -> running -> done/error/timeout/cancelled"""

    def __init__(self, db_path, sql):
        self.id = uuid.uuid4().hex
        self.db_path = db_path
        self.sql = sql
        self.status = "queued"
        self.report = None
        self.error = None
        self.created_at = time.monotonic()
        self.started_at = None
        self.finished_at = None
        self.conn = None
        self.thread = None
        self.timer = None
        self.cancel_reason = None  # 'user' | 'timeout'
        self.lock = threading.Lock()

    def cancel(self, reason):
        """请求终止（reason: 'user'/'timeout'）。只打标记并中断连接，终态由 worker 落定。"""
        with self.lock:
            if self.status in TERMINAL_STATES:
                return False
            if self.cancel_reason is None:
                self.cancel_reason = reason
            conn = self.conn
        if conn is not None:
            try:
                conn.interrupt()  # 真正中止 SQLite 正在执行的语句
            except Exception:
                pass
        return True

    def public(self):
        with self.lock:
            end = self.finished_at if self.finished_at is not None else time.monotonic()
            data = {
                "id": self.id,
                "status": self.status,
                "sql": self.sql,
                "error": self.error,
                "elapsed_ms": int((end - self.started_at) * 1000) if self.started_at else 0,
            }
            # 只有完整成功的演练才携带报告；失败/超时/取消不返回部分报告
            if self.status == "done" and self.report is not None:
                data["report"] = self.report
            return data


class RehearsalManager:
    def __init__(
        self,
        slots,
        timeout=3.0,
        sample_limit=20,
        max_snapshot_rows=2_000_000,
        job_ttl=600,
        work_dir=None,
    ):
        self.slots = slots  # 与只读查询共享的执行名额
        self.timeout = timeout
        self.sample_limit = sample_limit
        self.max_snapshot_rows = max_snapshot_rows
        self.job_ttl = job_ttl
        self.work_dir = work_dir
        self._jobs = {}
        self._lock = threading.Lock()

    # ---------------- 对外接口 ----------------

    def submit(self, db_path, sql):
        """提交演练；名额已满时返回 None（不排队，由上层明确拒绝）。"""
        if not self.slots.acquire(blocking=False):
            return None
        try:
            self._prune()
            job = RehearsalJob(db_path, sql)
            with self._lock:
                self._jobs[job.id] = job
            t = threading.Thread(
                target=self._run, args=(job,), name=f"rehearsal-{job.id[:8]}", daemon=True
            )
            job.thread = t
            t.start()
            return job
        except Exception:
            self.slots.release()
            raise

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_and_wait(self, job_id, wait=10.0):
        """取消并等待 worker 真正退出后才返回，保证返回时工作已停止、名额已释放。"""
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel("user")
        t = job.thread
        if t is not None:
            t.join(timeout=wait)
        return job

    # ---------------- 内部实现 ----------------

    def _prune(self):
        now = time.monotonic()
        with self._lock:
            stale = [
                jid
                for jid, j in self._jobs.items()
                if j.finished_at is not None and now - j.finished_at > self.job_ttl
            ]
            for jid in stale:
                del self._jobs[jid]

    @staticmethod
    def _raise_if_cancelled(job):
        with job.lock:
            reason = job.cancel_reason
        if reason is not None:
            raise _Cancelled(reason)

    def _run(self, job):
        conn = None
        workdir = None
        outcome = ("error", {"error": "内部错误：演练未执行"})
        try:
            with job.lock:
                early = job.cancel_reason
                if early is None:
                    job.status = "running"
                    job.started_at = time.monotonic()
            if early is not None:
                outcome = (_status_for(early), {})
            else:
                # 计时从 worker 启动开始：复制、快照、执行、对比共用同一时间预算
                timer = threading.Timer(self.timeout, job.cancel, args=("timeout",))
                timer.daemon = True
                job.timer = timer
                timer.start()
                workdir = tempfile.mkdtemp(prefix="rehearsal-", dir=self.work_dir)
                copy_path = os.path.join(workdir, "work.db")
                shutil.copyfile(job.db_path, copy_path)  # 隔离副本，原件不动
                self._raise_if_cancelled(job)
                conn = sqlite3.connect(copy_path, isolation_level=None, check_same_thread=False)
                conn.enable_load_extension(False)
                conn.execute("PRAGMA foreign_keys = ON")  # 演练启用外键约束
                with job.lock:
                    job.conn = conn
                self._raise_if_cancelled(job)
                outcome = self._rehearse(job, conn)
        except _Cancelled as exc:
            outcome = (_status_for(exc.reason), {})
        except Exception as exc:  # 复制/连接失败等
            outcome = ("error", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            # 顺序很关键：先停计时器、回滚并关闭连接、删除副本、释放名额，
            # 最后才对外落定终态。
            if job.timer is not None:
                job.timer.cancel()
            with job.lock:
                job.conn = None
            if conn is not None:
                try:
                    conn.rollback()  # 兜底：任何未提交变更一律丢弃
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            self.slots.release()
            status, payload = outcome
            with job.lock:
                job.status = status
                job.error = payload.get("error")
                job.report = payload.get("report")
                job.finished_at = time.monotonic()

    def _rehearse(self, job, conn):
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            before = self._snapshot(conn, tables)
            self._raise_if_cancelled(job)
            conn.set_authorizer(rehearsal_authorizer)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(job.sql)  # DELETE 本体，触发器/外键随之生效
                # WITH 开头的 DELETE 的 cursor.rowcount 恒为 -1，改用 changes()
                # （仅统计语句本身、不含触发器写入）
                direct_rowcount = conn.execute("SELECT changes()").fetchone()[0]
                after = self._snapshot(conn, tables)
            finally:
                try:
                    conn.execute("ROLLBACK")  # 演练结束即丢弃全部变更
                except sqlite3.Error:
                    pass
            report = self._build_report(tables, before, after, direct_rowcount)
        except _Cancelled:
            raise
        except Exception as exc:
            with job.lock:
                reason = job.cancel_reason
            if reason is not None:
                return (_status_for(reason), {})
            return ("error", {"error": f"{type(exc).__name__}: {exc}"})
        with job.lock:
            reason = job.cancel_reason
        if reason is not None:
            # 取消/超时与执行完成发生竞态：按终止状态返回并丢弃报告
            return (_status_for(reason), {})
        return ("done", {"report": report})

    def _snapshot(self, conn, tables):
        """对所有用户表做行级多重集快照：{(类型, 值)元组: 出现次数}。"""
        snap = {}
        total = 0
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            cur = conn.execute(f"SELECT * FROM {quoted}")
            cols = [d[0] for d in cur.description]
            counter = collections.Counter()
            while True:
                rows = cur.fetchmany(8192)
                if not rows:
                    break
                for row in rows:
                    counter[_row_key(row)] += 1
                total += len(rows)
                if total > self.max_snapshot_rows:
                    raise RehearsalError(
                        f"用户表数据量超过演练快照上限（{self.max_snapshot_rows} 行）"
                    )
            snap[name] = (cols, counter)
        return snap

    def _build_report(self, tables, before, after, direct_rowcount):
        out = []
        changed = 0
        for name in tables:
            cols, b = before[name]
            _, a = after[name]
            deleted = b - a  # 多重集差：更新体现为删旧行 + 增新行
            added = a - b
            d_count = sum(deleted.values())
            a_count = sum(added.values())
            if d_count or a_count:
                changed += 1
            out.append(
                {
                    "name": name,
                    "columns": cols,
                    "row_count_before": sum(b.values()),
                    "row_count_after": sum(a.values()),
                    "deleted_count": d_count,
                    "added_count": a_count,
                    "deleted_samples": self._samples(deleted),
                    "added_samples": self._samples(added),
                    "deleted_samples_truncated": len(deleted) > self.sample_limit,
                    "added_samples_truncated": len(added) > self.sample_limit,
                }
            )
        return {
            "tables": out,
            "table_count": len(out),
            "changed_tables": changed,
            "delete_rowcount": direct_rowcount,
        }

    def _samples(self, counter):
        """每个不同行一条样例（含值、值类型、重复次数），最多 sample_limit 条。"""
        entries = sorted(
            counter.items(), key=lambda kv: repr([_json_safe(v) for _t, v in kv[0]])
        )
        return [
            {
                "values": [_json_safe(v) for _t, v in key],
                "types": [t for t, _v in key],
                "count": n,
            }
            for key, n in entries[: self.sample_limit]
        ]
