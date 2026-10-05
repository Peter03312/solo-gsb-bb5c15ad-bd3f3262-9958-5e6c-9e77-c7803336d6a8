# 电影节片源放映许可核验服务

基于 **Python + FastAPI + SQLite** 的放映许可核验服务：录入影片/场馆授权（有效时段、场次上限、撤销记录、限时暂停），对待核验场次进行**幂等、并发安全**的准入判定，返回准入结果、命中授权、剩余额度与拒绝原因。

## 功能特性

- **授权管理**：录入影片、场馆、有效时段、场次上限；查询实时已用/剩余额度
- **影片级共享额度池**：发行方可为影片登记**跨场馆总放映次数**池；授权录入时可选绑池（绑定后不可改），核准时授权与所绑池**各消耗一次**，池耗尽返回 `POOL_EXHAUSTED`
- **限时暂停**：运营可为单份授权登记带时区起止时刻的暂停区间（左闭右开，重叠按并集生效，登记后不可改）；暂停只影响**此后提交**且场次开始落入区间的新核验，既有流水与额度占用不变
- **已核准场次临时取消**：按原核验幂等键登记取消（开场前、非空原因，幂等）；取消后命中授权与所绑共享池的**实时已用次数各回退一次**，释放额度可供后续核验使用；原核验决定、扣减时余额快照与同键重放结果保持原样，核验查询可见取消状态
- **核验判定**：多份授权同时有效时只消耗一份额度，**跳过已撤销/已暂停授权**，优先最早到期且自身与所绑池均有余额的授权；覆盖时段且未撤销的授权全部暂停时返回 `LICENSE_SUSPENDED`
- **并发安全**：核验的"查状态 + 扣额度"、暂停登记与取消登记均在单个 `BEGIN IMMEDIATE` 事务内串行化，SQLite WAL 模式下写请求串行化，**每次核验/取消决定都对应完整的前态或后态，不超额、不重复释放**
- **幂等语义**：同一业务幂等键重试返回原结果（`replay=true`，不重复扣额度、不重复占池、不受此后暂停影响）；键相同但请求内容变化返回 **409 冲突**。取消同键同原因返回首次取消记录（不重复释放），同键不同原因返回 **409**
- **撤销语义**：撤销仅阻止后续核验；已核准场次记录及其池占用保留、不退还、可追溯（与取消不同：取消仅允许开场前并显式退还额度）
- **明确拒绝**：时间不在任何授权时段、授权已撤销、授权全部暂停、额度用尽、共享池耗尽、无授权时分别返回具体拒绝原因；取消被拒绝流水或已开场流水另有专门错误码

## 快速开始

### 方式一：Docker Compose 一键启动（推荐）

```bash
docker compose up --build -d
```

启动即自动完成建表初始化；`SEED_DEMO_DATA=1`（compose 默认开启）时，空库会写入两条演示授权，可直接体验核验流程。

- 服务地址：http://localhost:8000
- 交互式接口文档：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/health`

数据持久化在命名卷 `licensing-data`（容器内 `/data/filmfest.db`）。停止并清理：

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连同数据一起删除
```

### 方式二：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pytest -q        # 73 个用例，含 20 线程授权不超额 / 40 线程跨场馆不突破池上限 / 暂停登记与核验并发状态一致 / 取消与核验并发不超额不重复释放
```

### 调用示例脚本

服务启动后执行完整演示（录入 → 核验 → 幂等重放 → 冲突 → 撤销 → 追溯）：

```bash
bash examples/demo.sh
```

共享额度池完整演示（建池 → 绑池校验 → 跨场馆消耗 → 幂等 → 池耗尽 → 撤销不退池）：

```bash
bash examples/demo_pools.sh
```

授权限时暂停完整演示（登记暂停 → 左闭右开边界 → 跳过暂停授权 → 全部暂停 `LICENSE_SUSPENDED` → 历史重放 → 查询与不可改）：

```bash
bash examples/demo_suspensions.sh
```

已核准场次临时取消完整演示（开场前登记取消 → 授权与池各回退一次 → 释放额度再核验 → 同键重放/冲突 → 未知键/已拒绝/已开场错误 → 取消状态查询）：

```bash
bash examples/demo_cancellations.sh
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FESTIVAL_DB_PATH` | `./filmfest.db`（容器内 `/data/filmfest.db`） | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（容器内） |
| `SEED_DEMO_DATA` | 关闭 | 设为 `1` 时，空库启动写入两条演示授权 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/pools` | 登记影片级共享额度池（影片、正整数上限） |
| GET | `/pools` | 池列表（含已用/剩余次数、绑定授权数），可按 `film_id` 过滤 |
| GET | `/pools/{id}` | 单个池详情 |
| POST | `/licenses` | 录入授权（影片、场馆、有效时段、场次上限，可选 `pool_id` 绑池） |
| GET | `/licenses` | 授权列表（含已用/剩余额度、所绑池快照、撤销状态），可按 `film_id`/`venue_id`/`pool_id` 过滤 |
| GET | `/licenses/{id}` | 单条授权详情 |
| POST | `/licenses/{id}/revoke` | 撤销授权（幂等保护：重复撤销返回 409） |
| POST | `/licenses/{id}/suspensions` | 登记限时暂停（带时区起止、非空原因；授权不存在 404，时刻非法 422） |
| GET | `/licenses/{id}/suspensions` | 某份授权的全部暂停记录（授权不存在 404） |
| GET | `/suspensions` | 全部暂停记录，可按 `license_id` 过滤（只读、不可改） |
| GET | `/suspensions/{id}` | 单条暂停记录详情 |
| POST | `/verifications` | 核验场次（幂等） |
| GET | `/verifications` | 核验流水，可按 `film_id`/`venue_id`/`decision`/`license_id`/`pool_id`/`cancelled` 过滤 |
| GET | `/verifications/{idempotency_key}` | 按幂等键查询单条核验记录（含 `cancelled`/`cancellation` 取消状态） |
| POST | `/verifications/{idempotency_key}/cancellation` | 按原核验幂等键登记已核准场次的临时取消（开场前、非空原因；幂等） |
| GET | `/verifications/{idempotency_key}/cancellation` | 查询某次核验的取消登记（取消时间、原因、关联授权与共享池） |
| GET | `/cancellations` | 全部取消登记，可按 `license_id`/`pool_id`/`film_id`/`venue_id` 过滤 |
| GET | `/cancellations/{id}` | 单条取消登记详情 |

### 调用示例（curl）

录入授权：

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-10-31T23:59:59Z",
       "max_sessions":5,"note":"主竞赛单元"}'
```

核验场次：

```bash
curl -X POST http://localhost:8000/verifications \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "start_time":"2026-10-05T19:30:00Z",
       "idempotency_key":"screening-0001"}'
```

准入响应（命中授权 3，本次消耗后剩余 4 场；该授权未绑池，池字段为 null）：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "film_id": "film-autumn-river",
  "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00.000000Z",
  "decision": "approved",
  "license_id": 3,
  "pool_id": null,
  "reject_reason": null,
  "remaining_quota": 4,
  "pool_remaining_quota": null,
  "verified_at": "2026-10-04T08:00:00.000000Z",
  "replay": false
}
```

拒绝响应：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "QUOTA_EXHAUSTED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

撤销授权：

```bash
curl -X POST http://localhost:8000/licenses/3/revoke \
  -H 'Content-Type: application/json' -d '{"reason":"版权方临时撤片"}'
```

### 授权限时暂停

为授权 3 登记一个限时暂停（区间**左闭右开** `[suspend_from, suspend_to)`，时间必须带时区偏移，库内统一存 UTC；结束须严格晚于开始）：

```bash
curl -X POST http://localhost:8000/licenses/3/suspensions \
  -H 'Content-Type: application/json' \
  -d '{"suspend_from":"2026-10-05T00:00:00+08:00",
       "suspend_to":"2026-10-07T00:00:00+08:00",
       "reason":"版权方临时通知暂停两日"}'
```

响应返回**暂停编号、归一化到 UTC 的区间和原因**：

```json
{
  "id": 1,
  "license_id": 3,
  "suspend_from": "2026-10-04T16:00:00.000000Z",
  "suspend_to": "2026-10-06T16:00:00.000000Z",
  "reason": "版权方临时通知暂停两日",
  "created_at": "2026-10-05T01:20:00.000000Z"
}
```

- 授权不存在 → `404 {"code":"NOT_FOUND"}`；时刻不带时区 / 无法解析 / 结束不晚于开始 / 原因为空白 → `422`，均**不留任何记录**
- 同一授权可重复登记、区间允许重叠，核验时**重叠部分按并集生效**
- 记录登记后**不可修改、不可删除**（`PUT`/`PATCH`/`DELETE` 返回 405）

暂停影响下的核验（仅影响暂停登记**之后提交**、且场次开始时间落入区间的新核验）：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "LICENSE_SUSPENDED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

查询暂停记录：

```bash
curl http://localhost:8000/licenses/3/suspensions     # 按授权
curl 'http://localhost:8000/suspensions?license_id=3' # 全局列表过滤
curl http://localhost:8000/suspensions/1              # 按暂停编号
```

### 影片级共享额度池（跨场馆总放映次数）

登记池：

```bash
curl -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","max_sessions":3,"note":"跨场馆总放映次数"}'
```

录入授权时用 `pool_id` 绑定（绑定后不可修改）：

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-2",
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-10-31T23:59:59Z",
       "max_sessions":10,"pool_id":1}'
```

- 池不存在 → `422 {"code":"POOL_NOT_FOUND"}`；池的影片与授权影片不符 → `422 {"code":"POOL_FILM_MISMATCH"}`
- 不传 `pool_id` 的授权（含所有旧数据）保持原语义，核验不检查池
- 绑定池的授权核准时，响应额外给出 `pool_id` 与消耗后的 `pool_remaining_quota`：

```json
{
  "decision": "approved",
  "license_id": 4,
  "pool_id": 1,
  "reject_reason": null,
  "remaining_quota": 9,
  "pool_remaining_quota": 2,
  "replay": false
}
```

池耗尽时（授权自身仍有余额）拒绝：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "POOL_EXHAUSTED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

查询池与授权展示一致的已用/剩余次数（同一批 approved 流水同时计入两者）：

```bash
curl http://localhost:8000/pools/1
curl 'http://localhost:8000/licenses?pool_id=1'
```

### 已核准场次临时取消

仅可在**场次开始前**取消一条 **approved** 核验流水，按**原核验幂等键**登记并提供**非空原因**：

```bash
curl -X POST http://localhost:8000/verifications/screening-0001/cancellation \
  -H 'Content-Type: application/json' \
  -d '{"reason":"台风预警，场馆通知该场次临时取消"}'
```

响应返回**取消时间、原因及关联授权与共享池**（未绑池时 `pool_id` 为 null）：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "verification_id": 4,
  "license_id": 3,
  "pool_id": null,
  "reason": "台风预警，场馆通知该场次临时取消",
  "cancelled_at": "2026-10-05T10:00:00.000000Z"
}
```

取消成功后：

- 命中授权与其所绑共享池的**实时已用次数各回退一次**（`/licenses`、`/pools` 立即反映释放后的已用/剩余），**后续核验可使用释放的额度**；
- 原核验流水的 `decision`、扣减时的 `remaining_quota` / `pool_remaining_quota` **快照保持原样不回写**；同键重放仍返回首次决定（`replay=true`），仅额外附上 `cancelled=true` 与 `cancellation` 取消登记；
- 幂等：同键**同原因**重复提交返回**首次取消记录**（同一 `id`/`cancelled_at`，额度只释放一次）；同键**不同原因**返回 **409**，`existing` 字段附首次取消记录；
- 错误情形：

| 情形 | 状态码 | code |
|---|---|---|
| 幂等键对应的核验不存在 | 404 | `NOT_FOUND` |
| 流水是 rejected（从未占用额度） | 422 | `VERIFICATION_NOT_APPROVED` |
| 场次已开始（`now >= start_time`，边界即已开场不可取消） | 422 | `SCREENING_ALREADY_STARTED` |
| 原因为空白 | 422 | （参数校验） |
| 同键不同原因重复取消 | 409 | `CONFLICT` |

查询取消状态——核验详情中可见取消标记，也可单独取取消登记：

```bash
curl http://localhost:8000/verifications/screening-0001
curl http://localhost:8000/verifications/screening-0001/cancellation
curl 'http://localhost:8000/verifications?cancelled=true'
curl http://localhost:8000/cancellations
curl 'http://localhost:8000/cancellations?license_id=3'
```

核验详情中的取消字段（旧字段与旧客户端完全兼容，仅新增两个字段）：

```json
{
  "decision": "approved",
  "remaining_quota": 4,
  "pool_remaining_quota": null,
  "replay": true,
  "cancelled": true,
  "cancellation": {
    "id": 1,
    "reason": "台风预警，场馆通知该场次临时取消",
    "cancelled_at": "2026-10-05T10:00:00.000000Z",
    "license_id": 3,
    "pool_id": null
  }
}
```

> 与撤销的区别：**撤销**针对授权本身，永久阻止后续核验、不退还原占用；**取消**针对单条已核准且未开场的流水，幂等登记并显式把授权与池各回退一次。授权事后被撤销不影响其历史核准流水在开场前取消与退额；暂停/撤销仍按既有规则限制新核验（释放出的额度在授权暂停或撤销期间仍无法被新核验使用）。

## 核验规则说明

**授权选择**：取该 `影片+场馆` 的全部授权，按 `valid_to` 升序遍历，命中第一个"覆盖开始时间、未撤销、**未暂停**、自身剩余额度 > 0，且**未绑池或所绑池剩余 > 0**"的授权——即最早到期且自身与池均有余额者；一次核验只消耗一份额度，且授权与其所绑池**各消耗一次**（`remaining_quota` / `pool_remaining_quota` 为消耗后的剩余值）。

**限时暂停**：暂停是挂在**单份授权**上的左闭右开区间 `[suspend_from, suspend_to)`，按场次开始时间判定（`suspend_from <= start < suspend_to`）；同一授权多条暂停的重叠部分按并集生效。暂停只影响**暂停登记之后提交**的新核验：核验时暂停授权与撤销授权一样被跳过；暂停**之前**已存在的核准/拒绝流水、授权已用额度与池占用一律不变；同一幂等键重放始终返回首次结果（含首次的批准），不重新评估暂停。暂停不退还任何额度，区间结束后授权自动恢复可用；永久撤销仍然优先阻止该授权（已撤销+已暂停按撤销处理）。暂停记录一经登记不可修改、不可删除。

**共享额度池**：池为影片级，被同一影片下任意场馆的绑定授权共享。池的实时已用次数等于所有绑定该池的授权产生的、**未取消的** `approved` 流水数；取消一条绑池核准时同步回退。未绑定池的授权保持旧语义。池一经授权绑定不可更换、不可解绑；未提供删池接口。

**幂等**：`idempotency_key` 全库唯一。重试同一键且请求内容（影片、场馆、开始时间）一致 → 返回首次存储的结果并标记 `replay=true`，不重复扣额度、**不重复占用池**、也不受首次登记之后新增的暂停影响；内容不一致 → `409` 并在 `existing` 字段附上首次结果。

**临时取消**：取消按原核验幂等键登记在独立的 `cancellations` 表中（与核验一对一），**不改写** `verifications` 原行——首次决定与扣减时余额快照始终可读、可追溯。实时已用次数通过统计"不存在取消登记的 approved 流水"得到，因此插入取消即等于授权与所绑池各回退一次，且天然不可能重复释放。取消只允许：① 键存在（否则 404）；② 流水为 approved（rejected 返回 422 `VERIFICATION_NOT_APPROVED`）；③ 当前时间严格早于场次开始时间，即 `now < start_time`（边界相等即视为已开场，返回 422 `SCREENING_ALREADY_STARTED`）。取消原因必须非空；同键同原因重放返回首次记录，同键异因返回 409 并附首次记录。取消后原核验同键重放仍返回首次批准与首次快照，仅附加取消状态。释放出的额度立即可被后续新核验使用，但暂停与撤销仍按既有规则优先限制新核验。

**撤销**：撤销记录独立存表。被撤销的授权不再参与后续核验；撤销前已核准的场次记录及其池占用原样保留、**不予退还**，可通过 `/verifications` 与 `/verifications/{key}` 追溯。撤销不影响其历史核准流水在开场前走取消流程退还额度。

**并发**：所有核验、取消、暂停登记与撤销均在 `BEGIN IMMEDIATE` 事务内完成"查重/查取消 → 读状态 → 决定 → 落库"，SQLite 写锁将全部写请求统一串行化：取消与新核验并发时，每次决定都对应取消前（额度仍占）或取消后（额度已释）的完整状态，**既不会超额消耗，也不会重复释放**；暂停登记同样不会出现"只生效一半"。WAL 模式 + 30s busy_timeout 保证读写不互相阻塞。测试以 20 线程并发抢 3 场授权额度、40 线程跨 4 场馆抢容量 5 的池、20 路核验与 1 路暂停登记同时开跑，以及容量 1 的授权/池上 20 路新核验与 1 路取消同时开跑验证：批准数恰好等于上限、取消仅释放一次、额度占用严格一致。

**拒绝原因**（`reject_reason`）：

| 值 | 含义 |
|---|---|
| `NO_LICENSE` | 该影片+场馆没有任何授权 |
| `TIME_OUT_OF_WINDOW` | 有授权，但开始时间不在任何有效时段内 |
| `LICENSE_REVOKED` | 覆盖该时段的授权均已撤销 |
| `LICENSE_SUSPENDED` | 存在覆盖该时段且未撤销的授权，但它们在该时刻**全部处于暂停中** |
| `QUOTA_EXHAUSTED` | 授权有效但场次自身额度已用完 |
| `POOL_EXHAUSTED` | 时段内未撤销、未暂停的授权仍有自身余额，却都因所绑池耗尽而不可用 |

拒绝原因按上表顺序判定（旧原因优先级不变）：先判定无授权/时间外/全部撤销；随后若覆盖时段且未撤销的授权**全部暂停**即返回 `LICENSE_SUSPENDED`（即使它们自身额度也已用尽）；否则**跳过暂停授权**后沿用原有判定与次序——例如某授权自身余额尚在、但池耗尽，同时另一份授权自身额度也已用完时，只要仍存在"自身有余额但仅被池卡住"的未暂停授权，即返回 `POOL_EXHAUSTED`；未绑池的授权不受池影响。

**时间处理**：核验场次时间按 ISO-8601 解析，不带时区按 UTC 处理，无法解析返回 422；**暂停起止时刻必须显式带时区偏移**（如 `Z` 或 `+08:00`），不带时区、无法解析或结束不晚于开始均返回 422。库内统一存定长 UTC 字符串，保证比较即时间序。

## 项目结构

```
app/
  main.py       # FastAPI 入口与路由（create_app 工厂）
  service.py    # 业务逻辑：额度池/授权/撤销/限时暂停/幂等核验/授权选择/已核准场次取消
  db.py         # SQLite 连接与建表（WAL、外键、busy_timeout、旧库迁移）
  schemas.py    # Pydantic 请求/响应模型
  timeutil.py   # UTC 时间规范化
tests/
  test_api.py          # API 端到端测试 + 并发不超额测试
  test_pools.py        # 共享额度池：绑定校验/跨场馆/池耗尽/撤销不退/并发不突破上限
  test_suspensions.py  # 限时暂停：登记校验/左闭右开/并集/跳过选择/拒绝原因次序/幂等/并发状态一致
  test_cancellations.py # 临时取消：回退授权与池/释放再用/开场前校验/幂等与冲突/重放快照/查询/并发不超额不重复释放
examples/
  demo.sh               # 完整调用示例脚本
  demo_pools.sh         # 共享额度池调用示例脚本
  demo_suspensions.sh   # 授权限时暂停调用示例脚本
  demo_cancellations.sh # 已核准场次临时取消调用示例脚本
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
