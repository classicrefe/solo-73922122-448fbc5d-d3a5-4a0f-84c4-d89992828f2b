# 数据产品新鲜度核查

FastAPI + SQLite 实现的小时级分区新鲜度核查网页：数据工程师上传设备遥测目录的
分区提交记录（业务日期/计划截止/实际提交/修订号），并登记下游日报的分区偏移与
宽限分钟；系统基于**同一目录快照**计算每个日报在截至时刻可安全使用的最新水位，
区分迟到、缺失、重复提交、已被新修订替代的分区，并给出阻断日报的最早原因。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据持久化在命名卷 `freshness-data` 中（容器内 `/data/freshness.db`），
并带有健康检查；`docker compose ps` 显示健康后即可访问。

> 想验证旧库升级时，可把旧库文件复制进命名卷后再启动，例如：
> `docker compose run --rm -v "$PWD/data:/seed:ro" web cp /seed/freshness.db /data/freshness.db`。

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

## 使用流程

1. 点击 **加载演示数据**：确保存在唯一的**专用演示目录**（`is_demo` 标记），
   在其中重建“演示目录”（含跨日、迟到、重复提交、修订替代、缺失、截至前未提交等
   场景）并自动运行一次核查。该按钮**只重置专用演示目录**，绝不会改动用户自建的
   普通目录——即使普通目录也叫“演示目录”；页头徽标与目录下拉会区分两者；
2. 或自行 **新建目录** → 上传分区 JSON → 添加日报依赖（偏移天数 + 宽限分钟）
   → 选择业务日期与截至时刻 → **运行核查**；
3. 结果卡片展示每个日报的就绪/阻断状态、安全水位、最早阻断原因与逐小时明细；
4. 目录或日报依赖一旦更新，历史核查自动标记为 **已失效**。

分区上传 JSON 示例：

```json
[
  {"partition_hour": "2026-11-20T13:00:00Z",
   "planned_deadline": "2026-11-20T14:00:00Z",
   "submitted_at": "2026-11-20T14:50:00Z",
   "revision": 1}
]
```

### 演示目录语义

- 分区与日报依赖在**同一个事务**内一起更新（先删后插，版本 +1）；任何一步失败
  整体回滚：已有演示数据保持上一次的完整状态，首次失败则不会留下空目录；
- 并发首次加载（多个浏览器 / 多个进程）通过 `BEGIN IMMEDIATE` 写锁串行化，
  配合 `catalogs(is_demo) WHERE is_demo = 1` 的部分唯一索引，全库只会有一个
  专用演示目录；重启后继续复用；
- 升级已有 SQLite 库时只新增 `is_demo` 列与索引；旧库里名为“演示目录”的目录
  无法确认来源，因此**一律保留为普通目录**，其分区、日报依赖与核查记录原样保留。
  新专用目录随后单独创建，二者同名但可区分。

## 判定语义

- 日报（业务日期 D，偏移 k）依赖分区日期 **D−k** 的 24 个整点分区（UTC）；
- 每个分区只有**最高修订号**生效，低修订标记为“已被新修订替代”，不参与判定；
- 最高修订出现多条 → **重复提交**，阻断；
- 生效提交晚于截至时刻 → **截至前未提交**，阻断；
- 生效提交晚于 计划截止+宽限 → **迟到超宽限**，阻断；宽限内迟到则可用但标记；
- **安全水位** = 从窗口起点开始连续可用分区的最后一个小时；起点即阻断时水位为空；
- **最早阻断原因** = 窗口内第一个不可用分区的原因（MISSING / PENDING / DUPLICATE / LATE）。

所有时间在服务端归一化为 UTC 存储与比较；页面输入的本地时间会转换为 UTC。

## 自测

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

覆盖：跨日窗口（offset=1/2）、超宽限/宽限内迟到、重复修订阻断、修订替代不阻断、
缺失、截至前未提交、目录更新后旧核查失效、演示数据故事线、输入校验；以及
演示目录隔离（同名普通目录不受影响）、旧库升级保留、并发首次加载、失败回滚与
数据库唯一约束。

> 测试通过 `tests/conftest.py` 把默认 `DB_PATH` 指向临时文件，仓库自带的
> `data/freshness.db` 不会被就地迁移。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/catalogs` | 新建目录 |
| GET | `/api/catalogs` | 目录列表（含版本号） |
| POST | `/api/catalogs/{id}/partitions` | 批量上传分区（目录版本 +1） |
| GET | `/api/catalogs/{id}/partitions` | 分区列表（含迟到/重复/被替代标记） |
| POST | `/api/catalogs/{id}/reset` | 清空分区 |
| POST/GET/DELETE | `/api/catalogs/{id}/reports[/{rid}]` | 日报依赖管理（变更使旧核查失效） |
| POST | `/api/catalogs/{id}/checks` | 运行核查（业务日期 + 截至时刻） |
| GET | `/api/catalogs/{id}/checks` | 核查历史（含 stale 标记） |
| GET | `/api/checks/{check_id}` | 核查详情（逐小时明细） |
| POST | `/api/demo` | 创建/重置**专用演示目录**（返回 `catalog_id`、`is_demo`、`created`、核查参数） |

交互式 API 文档：`http://localhost:8000/docs`
