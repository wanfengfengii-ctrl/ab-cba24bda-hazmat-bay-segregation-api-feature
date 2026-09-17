# 港口危险品配载预审 API

纯后端 Python 服务：接收一个舱位与 2–20 个货项，枚举全部无向货项对并依据
固定隔离规则给出同舱结论。裁决完全由本仓库的规则引擎（`app/rules.py`）
计算，不依赖任何外部服务；相同货项以任意次序提交，结论与依据字节级一致。

## 受理的类别子集

本服务只受理以下 6 个危险品类别（规则表对该子集闭合），其他类别一律整体拒绝：

| 类别   | 含义                   |
| ------ | ---------------------- |
| `FLAM` | 易燃品                 |
| `OXID` | 氧化剂                 |
| `TOX`  | 有毒品                 |
| `CORR` | 腐蚀品                 |
| `WET`  | 遇湿反应品             |
| `GAS`  | 压缩/液化气体          |

## 裁决规则（无向配对）

规则两端不区分顺序，同类之间允许：

| 配对                | 裁决        |
| ------------------- | ----------- |
| FLAM–OXID           | 禁止（FORBID） |
| FLAM–GAS            | 禁止（FORBID） |
| WET–CORR            | 禁止（FORBID） |
| TOX–FLAM            | 隔板（PARTITION） |
| TOX–OXID            | 隔板（PARTITION） |
| CORR–GAS            | 隔板（PARTITION） |
| 其余配对（含同类）  | 允许（ALLOW）  |

舱位级结论按优先级归并：任一配对命中禁止规则 → `FORBID`；否则任一配对命中
隔板规则 → `PARTITION`；否则 → `ALLOW`。

## 依据（evidence）排序

- 枚举所有不同货项构成的无向对，仅命中规则（FORBID / PARTITION）的对进入依据；
- 每对内部按货项编号先小后大排列（字符串按字典序）；
- 依据列表按（首编号, 次编号）升序；
- 因此录入顺序不影响输出，响应体字节级一致。

## 接口

### `POST /api/v1/stowage/assess`

请求体：

```json
{
  "hold": "HOLD-3",
  "items": [
    {"id": "C330", "category": "WET"},
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"}
  ]
}
```

- `hold`：非空字符串，空舱位（缺失、空串、纯空白）整体拒绝；
- `items`：2–20 个货项，`id` 为非空字符串且全舱唯一，`category` 须属于上述类别子集。

成功响应 `200`：

```json
{"hold":"HOLD-3","conclusion":"FORBID","evidence":[{"first":"C101","second":"C205","firstCategory":"FLAM","secondCategory":"OXID","rule":"FORBID"}]}
```

错误响应 `400`（代码稳定，可用于程序化处理）：

```json
{"error":{"code":"UNKNOWN_CATEGORY","message":"Item 'B2' has unknown category 'RADIO'; expected one of ['CORR', 'FLAM', 'GAS', 'OXID', 'TOX', 'WET']."}}
```

| 错误代码                  | 含义                                   |
| ------------------------- | -------------------------------------- |
| `INVALID_JSON`            | 请求体不是合法 JSON                    |
| `INVALID_REQUEST`         | 顶层结构或 `items` 结构非法            |
| `EMPTY_HOLD`              | 舱位缺失、为空或纯空白                 |
| `ITEM_COUNT_OUT_OF_RANGE` | 货项数量不在 2–20 之间                 |
| `INVALID_ITEM_ID`         | 货项编号缺失、为空或非字符串           |
| `DUPLICATE_ITEM_ID`       | 货项编号重复                           |
| `UNKNOWN_CATEGORY`        | 类别不在受理子集内                     |

### `POST /api/v1/stowage/removal-impact`

单件移除影响分析：当预审结论为禁止或需隔板时，快速判断卸下哪一件货最能
改善结果。请求体与 `assess` 完全相同（`hold` + `items`），校验规则与错误
代码也完全相同；任何非法输入整体拒绝，不产生部分分析。

成功响应 `200`：先给出原始裁决，再按货项编号升序列出卸下每件货后的结论
与依据，最后给出推荐编号：

```json
{
  "hold": "HOLD-3",
  "original": {
    "conclusion": "FORBID",
    "evidence": [
      {"first": "A1", "second": "B2", "firstCategory": "FLAM", "secondCategory": "GAS", "rule": "FORBID"},
      {"first": "B2", "second": "C3", "firstCategory": "GAS", "secondCategory": "CORR", "rule": "PARTITION"}
    ]
  },
  "removals": [
    {"removed": "A1", "conclusion": "PARTITION", "evidence": [
      {"first": "B2", "second": "C3", "firstCategory": "GAS", "secondCategory": "CORR", "rule": "PARTITION"}
    ]},
    {"removed": "B2", "conclusion": "ALLOW", "evidence": []},
    {"removed": "C3", "conclusion": "FORBID", "evidence": [
      {"first": "A1", "second": "B2", "firstCategory": "FLAM", "secondCategory": "GAS", "rule": "FORBID"}
    ]}
  ],
  "recommendations": ["A1", "B2"]
}
```

- `original`：未移除任何货项时的裁决，结构与 `assess` 响应一致；
- `removals`：按货项编号升序，每条记录卸下该货后的结论与依据；移除后
  只剩一件货时没有配对，按 `ALLOW` 处理；
- `recommendations`：能把严重程度降级的货项编号（禁止→隔板或允许、
  隔板→允许），按字典序排列；无改善时为空列表。

分析复用与预审相同的校验与裁决函数；相同货项以任意次序提交，完整响应
字节级一致。

### `POST /api/v1/stowage/reviews`

交接审核把危险品预审从草稿推进到已确认记录。请求体在 `assess` 的基础上
增加一个非空字符串 `commandId`（幂等键）：

```json
{
  "commandId": "cmd-0001",
  "hold": "HOLD-3",
  "items": [
    {"id": "C330", "category": "WET"},
    {"id": "C101", "category": "FLAM"},
    {"id": "C205", "category": "OXID"}
  ]
}
```

成功响应 `201`：建立 `revision=1` 的 `DRAFT` 草稿，`request` 保存规范化
请求（货项按编号升序，录入次序不影响结果），`decision` 复用规则引擎裁决：

```json
{
  "reviewId": "9f1c…",
  "revision": 1,
  "status": "DRAFT",
  "request": {"hold": "HOLD-3", "items": [{"id": "C101", "category": "FLAM"}, {"id": "C205", "category": "OXID"}, {"id": "C330", "category": "WET"}]},
  "decision": {"conclusion": "FORBID", "evidence": [{"first": "C101", "second": "C205", "firstCategory": "FLAM", "secondCategory": "OXID", "rule": "FORBID"}]}
}
```

- **commandId 判重**：请求先按 `commandId` 判重。同标识且规范化后内容
  相同（重试/并发重发，含货项次序不同）原样重放——同一个 `reviewId`、
  字节一致的 `201` 响应，不会重复建单；同标识但内容不同返回
  `409 COMMAND_ID_REUSED`。判重先于其他校验，因此重试即便是畸形请求也
  得到稳定的 409。
- 未见过的 `commandId` 才执行与 `assess` 完全相同的校验，错误代码与
  `assess` 一致（`EMPTY_HOLD`、`ITEM_COUNT_OUT_OF_RANGE` 等）。

### `POST /api/v1/stowage/reviews/{reviewId}/commands`

对草稿下达命令，把草稿推进版本或冻结。请求体：

```json
{"commandId": "cmd-0002", "expectedRevision": 1, "action": "REPLACE",
 "items": [{"id": "C101", "category": "WET"}, {"id": "C330", "category": "WET"}]}
```

- `commandId`：非空字符串，命令级幂等键（全局不可复用）；
- `expectedRevision`：正整数，调用方认为当前所处的版本，用于乐观并发；
- `action`：
  - `REPLACE`：必须带 `items`（2–20 个货项，校验规则同 `assess`）。复用
    既有校验与裁决整体重算，成功后 `revision` 加一，状态仍为 `DRAFT`，
    响应结构与建单相同（`request` + `decision`）。非法输入整体拒绝，
    返回 400，版本与状态保持不变，不留下部分状态；
  - `CONFIRM`：无 `items`，不递增版本；把当前版本的请求与裁决冻结为
    `snapshot`，状态变为 `FINALIZED`：

```json
{"reviewId": "9f1c…", "revision": 2, "status": "FINALIZED",
 "snapshot": {"revision": 2, "hold": "HOLD-3", "request": {...}, "decision": {...}}}
```

**判重与错误顺序**（每个请求固定按此处理）：

1. `commandId` 已落地：同评审、同命令类型、同 `expectedRevision` 且同内容
   （CONFIRM 无请求体，前三者相同即同内容）→ 原样重放首次的成功响应，
   字节一致、版本不重复推进；否则 `409 COMMAND_ID_REUSED`；
2. 新命令依次按 `REVIEW_NOT_FOUND`（404）→ `REVISION_CONFLICT`（409，
   带 `currentRevision`）→ `REVIEW_FINALIZED`（409）报错。

**并发语义**：争用同一 `expectedRevision` 的多个命令只有一个原子成功：

- 胜出者为 `REPLACE`：版本推进一次，其余命令得到 `REVISION_CONFLICT`，
  用新的 `commandId` 按响应中的 `currentRevision` 重试即可；
- 胜出者为 `CONFIRM`：评审冻结在当前版本（版本不递增），其余命令得到
  `REVISION_CONFLICT`（版本已过期时）或 `REVIEW_FINALIZED`（版本相同但
  已冻结时），之后再无任何命令可以改变快照。

| 错误代码              | 含义                                                 |
| --------------------- | ---------------------------------------------------- |
| `COMMAND_ID_REUSED`   | commandId 已落地但本次内容/类型/版本与其不一致（409） |
| `REVIEW_NOT_FOUND`    | reviewId 不存在（404）                                |
| `REVISION_CONFLICT`   | expectedRevision 与当前版本不一致（409，含 currentRevision） |
| `REVIEW_FINALIZED`    | 评审已确认冻结，不再接受命令（409）                   |
| `INVALID_REQUEST`     | commandId/action/expectedRevision 结构非法（400）     |

评审数据为进程内存储（服务重启即清空），单把锁保证“判重 → 版本比对 →
应用”整段原子，适配交接审核场景的去重与防覆盖。

### `GET /health`

健康检查，返回 `{"status": "ok"}`。

### 示例

```bash
curl -s -X POST http://localhost:8000/api/v1/stowage/assess \
  -H 'Content-Type: application/json' \
  -d '{"hold":"HOLD-3","items":[{"id":"C101","category":"FLAM"},{"id":"C205","category":"OXID"}]}'
# {"hold":"HOLD-3","conclusion":"FORBID","evidence":[{"first":"C101","second":"C205","firstCategory":"FLAM","secondCategory":"OXID","rule":"FORBID"}]}
```

## 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（覆盖全部 21 种类别配对、规则优先级、依据排序与排列不变性）：

```bash
pytest
```

## Docker Compose

Compose 只运行 API 一个常驻服务；宿主端口由环境变量 `API_PORT` 覆盖（默认 8000）：

```bash
API_PORT=9000 docker compose up --build api
# 服务监听在宿主的 9000 端口
```

一次性验收服务 `verify`：等待 API 健康后，先跑完整 pytest 判据，再对运行中的
API 做 HTTP 级验收（三种结论、排列字节级一致、各类错误代码），随后退出：

```bash
docker compose up --build --exit-code-from verify verify
# 或：docker compose run --rm verify
```

`verify` 以退出码报告结果：全部通过为 0，任一检查失败为 1。

## 项目结构

```
app/
  main.py        # FastAPI 入口与错误处理
  rules.py       # 规则引擎：配对裁决、舱位级归并与单件移除影响分析
  validation.py  # 请求校验、规范化与稳定错误代码
  store.py       # 评审存储：草稿版本、乐观并发、命令幂等与冻结快照
tests/
  test_rules.py  # 全部配对、优先级、依据排序、移除分析与排列不变性
  test_api.py    # assess/removal-impact 的 HTTP 行为、错误代码、字节级一致
  test_reviews.py # 评审建单、替换、确认、判重、错误顺序与并发原子性
verify/
  acceptance.py  # 对运行中实例的一次性 HTTP 验收（含并发争用）
Dockerfile
docker-compose.yml
requirements.txt
pytest.ini
```
