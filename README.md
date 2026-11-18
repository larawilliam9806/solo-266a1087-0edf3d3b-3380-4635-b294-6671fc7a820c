# 传感器入库排查 · 只读 SQL 工作台

数据工程师排查传感器入库任务重复采样问题的内部工具：在浏览器中上传 SQLite
落地库副本，查看表结构，执行 **只读** `SELECT` / `WITH` 查询，依据结果修正入库逻辑。

修复前还可以做 **DELETE 演练**：在隔离副本上启用外键、执行库内原有触发器，
按（值类型 + 完整行 + 重复次数）对比所有用户表的前后数据，逐表报告新增/删除行数
与样例，确认无误后再动真库。上传原件全程不受影响。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

页面上可直接点击 **「使用内置演示库」** 加载传感器演示数据（含故意制造的重复采样），
或 **「下载演示库」** 后走一遍真实上传流程。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
python3 demo/make_demo_db.py            # 生成 demo/demo_sensors.db
cd webapp && python3 app.py             # http://localhost:8000
```

生产模式（与镜像内一致）：

```bash
gunicorn --pythonpath webapp --bind 0.0.0.0:8000 --workers 1 --threads 16 app:app
```

> 执行名额是进程内信号量，**必须单 worker**（多线程没问题）。

## 功能与约束的对应关系

### 只读查询

| 需求 | 实现 |
| --- | --- |
| 上传 SQLite 副本 | `POST /upload`，校验 SQLite 文件头，按会话隔离存储，可随时清除 |
| 查看表结构 | `GET /schema`：表/视图/索引、列定义、行数 |
| 仅 SELECT / WITH | 文本预检（跳过注释取首关键字）+ SQLite authorizer 双层约束 |
| 不得修改原库 | 连接以 `file:...?mode=ro` 只读打开；authorizer 拒绝 INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA 写；自测含写尝试前后行数与文件哈希校验 |
| 不得访问其他文件 | authorizer 拒绝 `ATTACH`；无法通过 SQL 触达库外文件 |
| 不得加载扩展 | `enable_load_extension(False)` + authorizer 拦截 `load_extension()` 函数 |
| 用户可取消 | `POST /query/<id>/cancel`：`conn.interrupt()` 真正中断计算，join 线程后才响应 |
| 超 3 秒自动终止 | 每条查询启动定时器，超时走同一 interrupt 路径 |
| 失败不留半份结果 | 结果完整取回后才挂载；任何失败只返回错误信息 |
| 最多 1000 行并提示截断 | `fetchmany(1001)` 判断截断，页面显示截断横幅 |

### DELETE 演练

| 需求 | 实现 |
| --- | --- |
| 隔离副本、原件不动 | 复制上传件到独立临时目录（`rehearsal-*`），全部 SQL 只作用于副本；终态前删除副本 |
| 仅 DELETE（可带 WITH） | 轻量词法扫描跳过 CTE 列表取主语句关键字，非 DELETE 返回 400 |
| 启用外键、执行原有触发器 | 副本连接 `PRAGMA foreign_keys = ON`；触发器随 DELETE 自然触发 |
| 拒绝含虚拟表的库 | 提交时检查 `sqlite_master`，存在 `CREATE VIRTUAL TABLE` 即 400 |
| 前后对比口径 | 所有用户表按（值类型, 完整行, 重复次数）建多重集快照并做差；UPDATE 自然体现为删旧行+增新行，触发器写入的其他表一并计入 |
| 逐表报告 | 每表给出前/后行数、删除/新增行数、各最多 20 条样例（含值、值类型、重复次数） |
| 失败不留部分报告 | 约束失败/超时/取消/内部错误：回滚事务、删除副本，响应中只有错误信息 |
| 停止实际工作 | 超时/取消走 `conn.interrupt()`；计时从 worker 启动开始，覆盖复制、快照、执行、对比全程 |
| 不得访问库外文件/加载扩展 | authorizer 拒绝 ATTACH/DETACH/DDL/SAVEPOINT 等，`load_extension` 双重拦截 |
| 与查询合计最多 2 项 | 查询与演练共享同一信号量名额，满额均返回 429 |
| 释放名额后才显示终态 | worker 先关连接、删副本、释放名额，再落定终态 |

关键参数均可通过环境变量调整：`QUERY_TIMEOUT`（默认 3 秒，查询与演练共用）、
`MAX_CONCURRENT`（默认 2）、`MAX_ROWS`（默认 1000）、`MAX_UPLOAD_MB`（默认 300）、
`UPLOAD_TTL_HOURS`（默认 24）、`REHEARSAL_SAMPLE_LIMIT`（默认 20）、
`REHEARSAL_MAX_SNAPSHOT_ROWS`（默认 2000000，防止超大库撑爆内存）、
`SECRET_KEY`（生产必须覆盖）。

## 演示库与排查 SQL

- `demo/make_demo_db.py`：生成 `demo_sensors.db`（6 个传感器 × 6 小时 × 10 秒间隔，
  13260 行；重试批次 `B-20261110-RETRY` 造成 270 组重复采样，共 300 行多余）。
  库内附带 `AFTER DELETE` 触发器（被删采样归档到 `sample_audit`）与
  `sensor_samples.sensor_id → sensors.sensor_id` 外键，用于演示演练中的
  触发器影响与外键约束失败。
- `demo/find_duplicates.sql`：重复采样排查 SQL 五连——汇总、明细、按批次定位、
  待清理行预览（`ROW_NUMBER()` 去重）、修复后验证。页面内置了其中三条作为一键示例。
- 页面「慢查询」/「慢演练」示例（递归 CTE）用于演示 3 秒自动终止与手动取消。
- 演练示例「去重 DELETE」：在隔离副本上删除 300 行重复采样，报告中可见
  `sensor_samples` 减少 300 行、`sample_audit` 因触发器增加 300 行。

## 目录结构

```
webapp/
  app.py                # Flask 路由：上传/表结构/查询/取消/演练/演示库
  query_manager.py      # 只读查询执行器：名额、超时、取消、只读 authorizer、行数上限
  rehearsal_manager.py  # DELETE 演练执行器：隔离副本、外键+触发器、前后行级对比
  templates/index.html  # 单页面前端（无外部依赖）
demo/
  make_demo_db.py       # 演示库生成脚本（含审计触发器与外键）
  find_duplicates.sql   # 重复采样排查 SQL
tests/
  selftest.py           # 关键功能自测
Dockerfile  docker-compose.yml  requirements.txt
```

## 自测

```bash
# 1. 启动服务（任选一种方式），假设监听 8123
UPLOAD_DIR=/tmp/uploads DEMO_DB=/tmp/demo_sensors.db PORT=8123 python3 webapp/app.py
# 2. 另开终端运行
python3 tests/selftest.py http://127.0.0.1:8123 /tmp/demo_sensors.db /tmp/uploads
```

覆盖：上传校验、表结构、只读约束（含 `WITH` 伪装写、`ATTACH`、`load_extension`、
多语句）、重复采样 SQL、1000 行截断、失败无半份结果、3 秒自动终止、并发满额 429、
取消真实停止计算并释放名额、其他查询不受影响、库文件哈希不变；
以及 DELETE 演练：语句预检、去重演练前后对比（含触发器审计行、值类型/重复次数标注）、
原件不变、外键约束失败/超时/取消均无部分报告、与查询共享名额、含虚拟表的库被拒绝、
演练临时目录清理。

## 已知限制

- 会话与上传副本通过 cookie 关联，重启服务（默认 SECRET_KEY 变化）后需重新上传。
- 上传副本默认 24 小时后由后台线程清理（`UPLOAD_TTL_HOURS`），期间占用卷空间。
- 执行名额为进程内实现，不能多 worker / 多副本水平扩展（如需扩展应改为外部锁，如 Redis）。
- 查询结果集在内存中物化（上限 1001 行），不会流式输出；超大单行（如大 BLOB）以 hex 显示。
- 演练需要复制整个库并对所有用户表做行级快照，大库会受 3 秒时限与
  `REHEARSAL_MAX_SNAPSHOT_ROWS` 上限约束；演练报告在内存中构建。
