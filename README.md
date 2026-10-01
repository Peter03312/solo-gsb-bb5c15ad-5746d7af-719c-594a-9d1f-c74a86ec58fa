# 路跑分段计时裁决服务（Road Race Split Timing）

基于 **Python 3.12 / FastAPI / SQLAlchemy 2 / PostgreSQL 16** 的分段计时裁决 HTTP 服务。

- 赛事创建时录入起跑时间、截止时间、**有序检查点与终点**、每个分段（起跑→首点、之后各段）的
  **非负耗时闭区间** `[min_seconds, max_seconds]`、选手↔芯片绑定；**配置创建后固定**，不提供修改接口。
- 扫描记录包含唯一编号 `scan_no`、芯片、点位、发生时间，**可乱序提交**。
  - 同编号同内容重传 → 幂等（HTTP 200，`replayed=true`）；
  - 同编号异内容 → `409` 冲突；
  - 未知芯片 / 未知点位 → `404` 拒收；封存后提交 → `409`。
  - 扫描始终保存设备原始 `occurred_at`，重放/冲突/撤销语义与偏移无关。
- **点位时钟偏移**：赛前及未封存期间，工作人员可为每个赛事×点位设置/替换
  **有符号整数秒**偏移（未设置视为 0）；查询返回各点当前偏移，未知赛事/点位
  `404`、非整数偏移 `422`、封存后变更 `409`。
- **赛中换芯**：未封存期间，工作人员可为选手登记换芯——提交该选手、一枚**本赛事
  从未使用过**的新芯片号与生效时间；同一选手可多次换芯，生效时间必须落在
  `[起跑, 截止]` 闭窗口内且严格晚于该选手上次生效时间。
  - 芯片有效期为**半开时段**：旧芯片在生效时刻之前有效，新芯片自生效时刻起有效；
    最新芯片持续有效。扫描一律按**设备原始发生时间**匹配当时有效的芯片，乱序/
    延迟补传也按同一规则；落在任何有效时段之外的扫描以
    `409 scan_outside_chip_period` **明确拒收**（未知芯片仍为 `404`）。
  - 同 `scan_no` 的重传/冲突约定不变，且优先于时段判定（已接收编号不会因后续换芯
    被重新解释）。
  - 换芯若会使任一**已接收扫描（含已撤销记录）**落入无效时段，则**整体拒绝**
    （`409 chip_replacement_conflict`，响应 `conflicts` 列出冲突扫描），绝不改写
    原始芯片号、扫描编号或既有归属。
  - 换芯成功后暂定成绩按原规则重算；`used_scans` 同时保留每条扫描提交时的原始
    芯片号；封存后不得换芯，时间线与成绩快照一并冻结。
- 裁决规则：按点位顺序每个点取一条扫描，将每条扫描的**原始时间加该点偏移**
  得到校准时间后，要求
  - 相邻所用扫描的**校准时间严格递增**；
  - 每段（校准）耗时落在对应**闭区间**内；
  - 终点校准时间不晚于截止时间。
  - 在所有合法完整路径中选**校准完赛时间最早**的一条；
    完赛时间相同则取扫描编号序列**字典序最小**；无合法完整路径记为 `did_not_finish`。
  - 成绩同时保留每条扫描的原始 `occurred_at` 与校准后 `calibrated_at`
    （及 `offset_seconds`）；`finish_time`/`total_seconds`/`rank` 均基于校准时间。
- 偏移变更立即使暂定成绩按**同一批已提交扫描与当前偏移版本**重算（结果不落地、
  按需计算）；扫描提交/撤销、偏移变更、**换芯登记**与封存共用赛事行锁串行。
  封存时连同偏移版本与芯片时间线冻结一致快照，此后拒绝一切变更，**已封存快照
  不受后续请求影响**。
- `docker compose up --build` 一键启动（应用启动时自动建表，含数据库连接重试）。

---

## 1. 一键启动

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://127.0.0.1:8000> |
| 交互式文档（Swagger UI） | <http://127.0.0.1:8000/docs> |
| OpenAPI 描述 | <http://127.0.0.1:8000/openapi.json> |
| 健康检查 | `GET http://127.0.0.1:8000/health` |
| PostgreSQL | `127.0.0.1:5432`（库 `race_timing`，用户/密码 `racer/racer`） |

停止并清空数据：

```bash
docker compose down -v
```

> 表结构在应用启动时通过 SQLAlchemy `create_all` 自动创建（`app/database.py::init_db`），
> Compose 中配置了 Postgres 健康检查与应用侧连接重试，因此无需手动初始化数据库。

### 本地（无 Docker）运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://racer:racer@127.0.0.1:5432/race_timing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://racer:racer@db:5432/race_timing` | SQLAlchemy/psycopg2 连接串 |

时间一律使用带时区的 ISO-8601（建议 UTC，如 `2026-10-01T08:00:00+00:00`）；
分段耗时单位为**秒**，数据库以 `NUMERIC(12,3)` 存储（毫秒精度）。

---

## 2. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/races` | 创建赛事（配置固定）；非法时窗/区间/重复芯片等返回 `422` |
| `GET` | `/races/{race_id}` | 查询赛事配置 |
| `POST` | `/races/{race_id}/scans` | 提交单条扫描（可乱序）；幂等重传 200 / 新建 201 / 冲突 409 |
| `POST` | `/races/{race_id}/scans/batch` | 批量提交，逐条返回状态码（同一事务） |
| `DELETE` | `/races/{race_id}/scans/{scan_no}` | 封存前撤销扫描（暂定成绩随后重算） |
| `GET` | `/races/{race_id}/offsets` | 查询各点当前整数秒偏移（未设置返回 0） |
| `PUT` | `/races/{race_id}/points/{point}/offset` | 设置/替换某点有符号整数秒偏移 |
| `POST` | `/races/{race_id}/chip-replacements` | 赛中登记一次换芯（新芯片+生效时间）；冲突/复用/非法时间明确报错 |
| `GET` | `/races/{race_id}/runners/{runner_id}/chip-timeline` | 查询选手换芯时间线及各芯片半开时段 |
| `GET` | `/races/{race_id}/results` | 全部选手成绩（封存前实时暂定，封存后取快照） |
| `GET` | `/races/{race_id}/runners/{runner_id}/result` | 单个选手成绩 |
| `POST` | `/races/{race_id}/seal` | 封存：冻结一致快照（含偏移版本）；重复封存幂等 |
| `GET` | `/health` | 健康检查 |

### 2.1 创建赛事

```jsonc
POST /races
{
  "name": "City 10K 2026",
  "start_time": "2026-10-01T08:00:00+00:00",
  "cutoff_time": "2026-10-01T09:00:00+00:00",
  "points": [
    {"code": "C1", "name": "Checkpoint 1"},
    {"code": "C2", "name": "Checkpoint 2"},
    {"code": "FIN", "name": "Finish", "is_finish": true}
  ],
  "segments": [
    {"min_seconds": 300, "max_seconds": 1800},   // START -> C1
    {"min_seconds": 300, "max_seconds": 1800},   // C1 -> C2
    {"min_seconds": 300, "max_seconds": 1800}    // C2 -> FIN
  ],
  "runners": [
    {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
    {"bib": "D04", "name": "Dan",   "chip": "CHIP-D"}
  ]
}
```

校验（不通过返回 `422`）：

- `cutoff_time > start_time`（无效时窗拒收）；
- 点代码非空、不重复（最后一个有序点即终点；`START` 为保留字）；
- `segments` 数量必须等于点数（起跑→首点一段，其后相邻点各一段）；
- 每个区间 `0 <= min_seconds <= max_seconds`（非法区间拒收）；
- `bib` 与 `chip` 在赛事内唯一（重复芯片/号码拒收）。

### 2.2 提交扫描

```jsonc
POST /races/{race_id}/scans
{"scan_no": "A-010", "chip": "CHIP-A", "point": "C1",
 "occurred_at": "2026-10-01T08:10:00+00:00"}
```

- 扫描可乱序提交（先发终点、后发检查点也可以）。
- 系统按**设备原始发生时间** `occurred_at` 匹配该选手当时有效的芯片时段（见
  2.4）：提交的芯片必须在该原始时刻对该选手有效。旧芯片时段外、新芯片尚未生效
  或已失效的扫描 → `409 scan_outside_chip_period` **明确拒收**，`detail` 会指出
  该时刻实际有效的芯片；乱序补传同样按原始时间判定。芯片在本赛事从未登记
  （非任何选手的初始/换芯芯片）→ `404 unknown_chip`。
- `scan_no` 在赛事内唯一。**同编号重传/冲突约定保持不变，且优先于芯片时段判定**：
  同 `scan_no` 且 `chip/point/occurred_at` 完全相同 → HTTP `200` 且
  `replayed=true`；内容不同 → `409 scan_conflict`。即一条已被接收的编号不会因为
  之后换芯而被重新解释或拒收；该编号若此前已被撤销，同内容重传仍返回该记录
  （`revoked=true`），新观测请使用新编号。
- 赛事已封存 → `409 race_sealed`。

批量接口 `POST /races/{race_id}/scans/batch` 的请求体为 `{"scans":[ ... ]}`，
响应为与输入等长、同序的数组，每项带独立 `status_code` / `error` / `detail` / `scan`。

### 2.3 点位时钟偏移

设备时钟可能与标准时间偏离。工作人员在赛前及未封存期间，按**赛事 × 点位**设置
有符号整数秒偏移；扫描的原始 `occurred_at` 永远原样保存，裁决时使用
`calibrated_at = occurred_at + offset_seconds`。

查询各点当前偏移（按点位顺序，未设置的点返回 `0`、`configured=false`）：

```jsonc
GET /races/{race_id}/offsets
{
  "race_id": "...",
  "offset_version": 2,
  "offsets": [
    {"point": "C1", "offset_seconds": 0,  "configured": false, "updated_at": null},
    {"point": "FIN", "offset_seconds": -60, "configured": true,
     "updated_at": "2026-09-30T11:20:03.412000+00:00"}
  ]
}
```

设置/替换单点偏移（首次设置 `replaced=false`，再次设置为 `replaced=true`）：

```jsonc
PUT /races/{race_id}/points/FIN/offset
{"offset_seconds": -60}
```

- `offset_seconds` 必须是**整数**（有符号，正/负/零均可）；`1.5`、`1.0`、`"2"`、
  `true` 一律 `422` 拒绝，不存在“非整数偏移”。
- 未知赛事 / 未知点位 → `404`；赛事已封存 → `409 race_sealed`。
- 每次成功设置/替换令赛事 `offset_version` +1；偏移变更与扫描提交、撤销、封存
  经同一赛事行锁串行，因此暂定成绩总是对应一个已提交的“扫描 + 偏移”版本。
  成绩本身按需重算、不落地：下一次 `GET .../results` 立即反映新偏移。
- 封存后 `GET .../offsets` 仍可读取（只读），但任何 `PUT` 均返回 `409`；
  封存时的偏移版本与成绩快照一起冻结，之后的请求无法改变快照。

### 2.4 赛中换芯

选手芯片损坏时，工作人员在**赛事未封存**期间登记一次换芯：

```jsonc
POST /races/{race_id}/chip-replacements
{
  "runner_id": "4f8c..-..-..",
  "new_chip": "CHIP-A2",
  "effective_at": "2026-10-01T08:22:00+00:00"
}
```

成功返回 `201` 与该选手的换芯时间线（见下）。**校验规则**（任一不满足则整笔拒绝，
不写入任何换芯记录）：

- 未知赛事 / 未知选手（含非法 UUID）→ `404`（`race_not_found` /
  `runner_not_found`）；赛事已封存 → `409 race_sealed`。
- `new_chip` 必须是**本赛事从未使用过**的芯片号——既不能是任何选手的初始绑定，
  也不能是此前任何换芯引入的芯片（跨赛事可以复用同号）→ 否则
  `409 chip_already_used`。
- `effective_at` 必须落在 `[start_time, cutoff_time]` 闭窗口内，且对同一选手
  **严格晚于上一次换芯生效时间**；否则 `422 invalid_effective_time`
  （恰等於起跑或截止时刻合法）。
- **整体冲突拒绝**：换芯生效后，旧（此前当前）芯片的有效期在该时刻终止。若该选手
  任何**已接收扫描（含已撤销记录）**以旧芯片记录、且其原始 `occurred_at`
  ≥ 新生效时间，这些扫描会落入无效时段；此时返回 `409 chip_replacement_conflict`，
  并在 `conflicts` 数组中逐条给出 `scan_no/point/chip/occurred_at/revoked/reason`。
  系统不会改写任何扫描的原始芯片号、扫描编号或归属，需要工作人员先核对再选择
  更晚的生效时间重新登记。

**芯片有效时段（半开区间，按设备原始时间匹配）**：

- 初始芯片：`[..., 第 1 次生效时间)`（左侧无界，起跑前的扫描也属它）；
- 第 k 次换芯芯片：`[第 k 次生效时间, 第 k+1 次生效时间)`；
- 最新（当前）芯片：`[最后一次生效时间, ...)`（右侧无界）。

边界瞬间归**新芯片**（旧 `... < t` 有效，新自 `t` 起有效）。扫描乱序/延迟补传时
仍以其自身的原始 `occurred_at` 匹配时段，与提交先后无关。时段外的新扫描以
`409 scan_outside_chip_period` 明确拒收（见 2.2）。

**查询时间线**（封存前后均可读，封存后为冻结内容）：

```jsonc
GET /races/{race_id}/runners/{runner_id}/chip-timeline
{
  "race_id": "...", "runner_id": "...", "bib": "A01", "name": "Alice",
  "original_chip": "CHIP-A", "current_chip": "CHIP-A2",
  "replacement_count": 1,
  "periods": [
    {"sequence": 0, "chip": "CHIP-A",  "is_initial": true,
     "effective_from": null, "effective_to": "2026-10-01T08:22:00+00:00",
     "is_current": false, "registered_at": null},
    {"sequence": 1, "chip": "CHIP-A2", "is_initial": false,
     "effective_from": "2026-10-01T08:22:00+00:00", "effective_to": null,
     "is_current": true, "registered_at": "2026-10-01T08:20:03.100000+00:00"}
  ]
}
```

换芯与扫描提交/撤销/偏移调整/封存共用赛事行锁**串行**；成功后下一次
`GET .../results` 即按原裁决规则重算暂定成绩，跨芯片的完赛路径合法，且每条
`used_scans` 都带提交时的原始 `chip`。赛事配置中的 `chip` 始终是初始绑定，另以
`current_chip` 表示当前有效芯片。

### 2.5 成绩

```jsonc
GET /races/{race_id}/results
{
  "race_id": "...", "name": "City 10K 2026",
  "sealed": false, "sealed_at": null,
  "offset_version": 2,
  "results": [
    {
      "runner_id": "...", "bib": "A01", "name": "Alice",
      "chip": "CHIP-A", "current_chip": "CHIP-A2",
      "status": "finished",
      "finish_time": "2026-10-01T08:30:00+00:00",
      "total_seconds": 1800.0,
      "rank": 1,
      "used_scans": [
        {"segment": 1, "point": "C1", "scan_no": "A-010", "chip": "CHIP-A",
         "occurred_at": "2026-10-01T08:10:00+00:00",
         "calibrated_at": "2026-10-01T08:10:03+00:00",
         "offset_seconds": 3, "segment_seconds": 603.0}
      ]
    },
    {"status": "did_not_finish", "finish_time": null, "total_seconds": null,
     "rank": null, "used_scans": []}
  ]
}
```

- 每条 `used_scans` 同时给出该扫描**提交时的原始 `chip`**、原始 `occurred_at`、
  校准后 `calibrated_at`（= 原始时间 + 该点偏移）与 `offset_seconds`；
  `segment_seconds`、`finish_time`、`total_seconds` 与排名全部基于校准时间。
  完赛路径允许跨芯片段（每个点用当时有效芯片记录的扫描）。
- 每名选手的 `chip` 始终是赛事配置中的初始绑定，`current_chip` 为当前有效芯片；
  未完赛者排在后面，`rank=null`。
- 完赛者按校准 `finish_time` 升序排名；相同完赛时间按所用扫描编号序列字典序排序。
- 裁决的选路是按点位顺序的动态规划：每个点保留“校准到达时间最早、同到达时间取
  扫描编号序列字典序最小”的前驱路径；最后一层再按（校准完赛时间，编号序列）取最优。

### 2.6 撤销与封存

- `DELETE /races/{race_id}/scans/{scan_no}`：封存前可用；撤销后 `GET /results`
  立即反映重算的暂定成绩。重复撤销返回 `409`，未知编号返回 `404`。
- `POST /races/{race_id}/seal`：对全部选手按当时偏移计算一次并写入快照
  （`result_snapshots`，含每条扫描的原始/校准时间与原始芯片号），同时置
  `sealed=true / sealed_at=...`；该操作对赛事行加 `FOR UPDATE` 行锁，
  与扫描提交/撤销/**偏移变更/换芯登记**互斥，保证快照与已提交数据一致。重复调用
  幂等并返回同一份快照（含相同 `offset_version`）。
- 封存后任何 `POST .../scans`、`.../scans/batch`、`DELETE .../scans/...`、
  `PUT .../points/{point}/offset`、`POST .../chip-replacements` 均返回 `409`；
  `GET .../results`、`GET .../offsets` 与 `GET .../chip-timeline` 仍可读取，
  但永远返回封存时的冻结内容，快照不随后续请求改变。

---

## 3. 调用示例

服务启动后另开终端：

```bash
# 端到端 Python 示例（仅用标准库）：覆盖建赛、拒收、乱序提交、幂等/冲突、
# 最早完赛 + 字典序决胜、撤销重算、点位时钟偏移（设置/替换/重算/原始时间保留/
# 非整数拒收/封存冻结）、赛中换芯（半开时段匹配/乱序/复用与非法时间/含撤销记录的
# 整体冲突拒绝/时间线/跨芯片重算/封存冻结）、封存后拒绝等全部场景
BASE_URL=http://127.0.0.1:8000 python3 examples/demo.py

# 或最小 curl 走查（含换芯）
bash examples/curl.sh
```

`examples/demo.py` 中内置了字典序决胜场景：选手 Dan 在三个点各有两条
发生时间相同的扫描，编号路径 `"DA01","DB01","DC01"` 与 `"DA02","DB02","DC02"`
完赛时间一致，服务选择扫描编号元组字典序更小的 `"DA01","DB01","DC01"`。

---

## 4. 运行测试

裁决算法（`app/adjudication.py`）不依赖任何第三方库，可直接用系统 Python 测试：

```bash
python3 tests/test_adjudication.py
# 或（容器/虚拟环境内）
pytest
```

覆盖：乱序提交、最早完赛路径、同成绩字典序决胜、相邻扫描等时刻拒收、
区间越界、闭区间边界（含起点 `min=0` 时恰在起跑瞬间）、缺终点、晚于截止、
恰好等于截止可完赛，点位偏移：校准时间用于区间/严格递增/截止/排名、原始时间
保留、偏移改变路径合法性等，以及**芯片换芯时段**：初始/换芯半开窗口的构建、
生效边界归属新芯片、多次换芯连续无重叠。

此外 `tests/integration_chip.py` 是换芯特性的端到端 HTTP 集成检查（含时间线、
复用/非法时间/封存错误、含撤销记录的整体冲突拒绝、乱序补传按原始时间匹配、
跨芯片重算与快照冻结）。它默认用 SQLite 内存文件库以脱离 Postgres 运行
（`tests/sqlite_shim.py` 仅做类型/时区兼容垫片，生产仍为 PostgreSQL）：

```bash
python3 tests/integration_chip.py
```

---

## 5. 项目结构

```
app/
  main.py          # FastAPI 路由与异常映射
  schemas.py       # Pydantic v2 请求/响应模型与入参校验
  database.py      # 引擎、会话、ORM 模型、建表（含连库重试与加列迁移）
  service.py       # 事务/行锁、扫描幂等与冲突、撤销、时钟偏移、换芯、成绩、封存快照
  adjudication.py  # 纯函数裁决（校准时间动态规划选路 + 芯片半开时段，零第三方依赖）
examples/
  demo.py          # 端到端调用示例（标准库 urllib）
  curl.sh          # curl 调用示例（含换芯）
tests/
  test_adjudication.py   # 纯函数裁决/芯片时段测试
  integration_chip.py    # 换芯端到端 HTTP 集成检查
  sqlite_shim.py         # 集成测试用 SQLite 方言兼容垫片（非生产代码）
Dockerfile
docker-compose.yml
requirements.txt
```

### 主要数据表

- `races`：起跑/截止时间、封存标志与封存时间、偏移版本 `offset_version`
  （配置仅在建赛时写入）；
- `race_points`：赛事内有序点位（`(race_id,position)`、`(race_id,code)` 唯一）；
- `clock_offsets`：赛事×点位唯一的有符号**整数**秒偏移（无行即 0），封存时冻结；
- `segments`：按序闭区间（秒，`NUMERIC(12,3)`）；
- `runners`：号码与初始芯片（赛事内各自唯一；`chip_code` 为固定初始绑定）；
- `chip_replacements`：选手的换芯记录（`sequence` 从 1 起、仅追加），含新芯片号、
  生效时间；`(race_id,chip_code)` 唯一保证新芯片本赛事从未使用；相邻生效时间构成
  各芯片的半开有效窗口；
- `scans`：`(race_id,scan_no)` 唯一，绑定选手与点位，保存**原始**发生时间与
  **提交时的原始芯片号 `chip_code`**（换芯绝不改写），含撤销标志；
- `result_snapshots`：封存时每名选手一份 JSONB 成绩快照（含原始/校准扫描时间与
  各扫描原始芯片号）。
