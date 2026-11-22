# 树木标本鉴定供数 API

基于 **FastAPI + SQLite** 的标本鉴定供数服务。同一件树木标本可以有多份鉴定，
名录历次换版可能**拆种**（一个物种代码 → 多个代码）或**并种**（多个代码 → 同一代码）。
服务按现行名录对每份**有效鉴定**逐版转换，取候选物种交集后给出入库结论。

## 一、业务规则与结论状态

- **登记**：提交 `标本号 + 名录版号 + 物种代码`，系统校验版本与代码确实存在，返回鉴定编号。
- **撤回**：按鉴定编号软撤回（保留历史记录），重复撤回返回 409。
- **发布新版**：请求体必须给出**前版每一个代码在新版的一个或多个去向**：
  - 拆种：一个源代码映射到多个目标代码；
  - 并种：多个源代码映射到同一个目标代码。
  - 缺映射、去向不在新版清单、版号重复等都会让整笔发布**事务回滚**，继续按原名录供数。
- **查询结论**（`GET /specimens/{标本号}/resolution`）：

| 状态 | 含义 | `resolved_species_code` |
|---|---|---|
| `unique`（候选唯一） | 所有有效鉴定逐版转换后的交集只有 1 个代码，可入库 | 给出该代码 |
| `review`（待复核） | 交集有多个代码 | `null` |
| `conflict`（鉴定冲突） | 交集为空 | `null` |
| `unidentified`（未鉴定） | 没有任何有效鉴定（从未登记或均已撤回） | `null` |

响应同时返回现行版号、每份有效鉴定的**逐版转换链**和候选代码，便于人工复核。

## 二、一致性设计（换版期间仍可登记/撤回）

- SQLite 开启 **WAL 模式**：写事务（发布新版）进行时，读请求不被阻塞。
- 每个查询在**单条连接、单个只读事务**内完成全部读取；SQLite 快照隔离保证
  本次查询看到的始终是同一已提交状态，响应字段 `snapshot_consistent=true`。
- 发布新版、登记、撤回均使用 `BEGIN IMMEDIATE` 显式事务 + `busy_timeout=30s`，
  写者之间排队等待；发布失败执行 `ROLLBACK`，旧版指针不变。
- 鉴定记录只存登记时的版本与代码，查询时沿版本父子链（v1→v2→v3…）逐段套用
  `code_mappings` 转换，因此**任何历史版本的鉴定都能追溯到现行版**。

## 三、目录结构

```
app/
  database.py    # SQLite 连接、WAL/外键/建表
  schemas.py     # 请求/响应模型与状态枚举
  services.py    # 换版事务、登记/撤回、逐版转换与交集解析
  main.py        # FastAPI 路由（9 个端点）
scripts/
  seed.py        # 幂等写入 v1 初始名录（容器启动自动执行）
  demo.py        # HTTP 端到端演示：拆种/并种/撤回/换版失败
tests/
  test_api.py          # 8 组接口测试，覆盖全部状态分支与回滚
  test_concurrency.py  # 快照隔离与写锁等待的直连验证
docker/entrypoint.sh
Dockerfile
docker-compose.yml
```

## 四、启动方式

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 服务: http://localhost:8000  文档: http://localhost:8000/docs
```

容器启动时自动建库并写入 v1 种子名录（5 个树种代码），数据保存在命名卷
`det-data` 中，重复启动不会重复播种。

### 本地 Python 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
DET_DB_PATH=./data/det.db python -m scripts.seed
DET_DB_PATH=./data/det.db uvicorn app.main:app --reload
```

### 一键演示（服务启动后另开终端）

```bash
python scripts/demo.py          # 自动走完拆种、并种、撤回、换版失败并内置断言
python -m pytest tests/ -q      # 自动化测试
```

## 五、在 /docs 手工演示拆种、并种、撤回

种子数据 v1 含代码：`PIN01 PIN02 QRC01 QRC02 CUN01`。

1. **登记鉴定**：`POST /identifications`
   - 标本 `T1` → v1 的 `PIN01`（后面演示拆种）
   - 标本 `T2` 分别登记 v1 的 `QRC01` 和 `QRC02`（后面演示并种消解冲突）
2. **查询**：`GET /specimens/T1/resolution` → `unique`，入库代码 `PIN01`；
   `T2` 两份鉴定无交集 → `conflict`。
3. **发布 v2**（`POST /versions`，体现拆种与并种）：

```json
{
  "version_code": "v2",
  "name": "第二版",
  "species": [
    {"species_code": "PIN01A", "scientific_name": "Pinus massoniana A"},
    {"species_code": "PIN01B", "scientific_name": "Pinus massoniana B"},
    {"species_code": "PIN02",  "scientific_name": "Pinus tabuliformis"},
    {"species_code": "QRC99",  "scientific_name": "Quercus sl"},
    {"species_code": "CUN01",  "scientific_name": "Cunninghamia lanceolata"}
  ],
  "mappings": {
    "PIN01": ["PIN01A", "PIN01B"],
    "PIN02": ["PIN02"],
    "QRC01": ["QRC99"],
    "QRC02": ["QRC99"],
    "CUN01": ["CUN01"]
  }
}
```

4. **再查询**：
   - `T1`：`PIN01` 拆成两个候选 → `review`，候选 `PIN01A/PIN01B`，不可入库；
   - `T2`：两条链并到 `QRC99`，交集唯一 → 由 `conflict` 变 `unique`，入库 `QRC99`。
5. **撤回**：`POST /identifications/{id}/withdraw` 撤回 `T1` 的鉴定后再查 →
   `unidentified`；给 `T1` 用现行版代码重新登记（如 v2 的 `PIN02`）→ 恢复 `unique`。
6. **换版失败**：再发一个 v2' 但 `mappings` 漏掉任一前版代码 → 返回 400，
   `GET /versions/current` 仍是 `v2`，查询结论完全不变。

## 六、主要端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/versions/bootstrap` | 初始化首个名录（无需映射） |
| POST | `/versions` | 发布新版（校验全部前版代码的去向，原子事务） |
| GET | `/versions` / `/versions/current` | 版本列表 / 现行版号 |
| GET | `/versions/{v}/species` | 某版物种清单 |
| GET | `/versions/{v}/mappings/outgoing` | 某版到后继版的拆种/并种去向 |
| POST | `/identifications` | 登记鉴定 |
| GET | `/identifications?specimen_no=&status=` | 鉴定列表/过滤 |
| POST | `/identifications/{id}/withdraw` | 按编号撤回 |
| GET | `/specimens/{no}/resolution` | 现行版号 + 候选交集结论 + 转换链 |

## 七、验证情况

- `pytest tests/test_api.py`：8 组全部通过，覆盖四种结论状态、拆种/并种、
  多跳转换（v1→v2→v3）、撤回与重复撤回、非法登记、发布失败回滚、版号冲突、并发写入。
- `tests/test_concurrency.py`：直连验证读事务快照不被并发换版破坏、写锁排队等待。
- `scripts/demo.py`：对真实 uvicorn 进程跑完整 HTTP 流程，全部断言通过。

## 八、已知限制 / 未完成项

- 版本链为**线性**模型（每版最多一个后继、一个父版），不支持分支名录；
  映射只在相邻版本间维护，跨多版由父子链逐段推导。
- 撤回为软删除，未提供鉴定修改接口（业务上以"撤回 + 重新登记"代替）。
- 未做鉴权/多租户与分页；名录、鉴定体量按单机 SQLite 规模设计。
- 当前环境没有 Docker 守护进程，compose 文件已通过 YAML/语法校验，
  但未能在本机实际 `docker compose up` 构建运行（本地已用等价 uvicorn 流程验证）。
