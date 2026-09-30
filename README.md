# Versioned & Idempotent Orders API（Python / FastAPI）

在一个最小订单服务之上，加一层**对外请求的版本化与治理能力**。外部依赖（支付
网关、库存服务）用本地内存状态模拟，支持故障注入。目标是让：

- 不同版本的请求；
- 同一幂等键的重复 / 并发提交；
- 超时、中途取消、依赖失败、服务关停；

在**任何入口**（HTTP 路径版本、HTTP 头版本、服务直调、函数式入口）下都得到
**一致且可解释**的结论：成功数据一致，失败时错误码 / HTTP 状态 / 原因可区分，
内部异常不外泄，且不留下结果不确定的中间状态。

## 目录结构

```
src/versioned_orders/
  errors.py         # 归一化错误码与对外错误体（不含内部堆栈）
  logging_setup.py  # 结构化判定日志：request_id / idempotency_key / decision / reason
  models.py         # v1/v2 请求模型、默认值、按版本裁剪的返回视图
  versioning.py     # 版本解析协商、请求体解析与字段校验
  idempotency.py    # 幂等键登记、在途等待、结论缓存与重放
  concurrency.py    # 全局在途上限（立即拒绝）与关停闸门（优雅排空）
  dependencies.py   # 本地模拟外部依赖（支付/库存），支持延迟/失败注入
  service.py        # 核心编排（与 HTTP 无关，多入口复用同一结论）
  api.py            # FastAPI 入口与统一异常处理
  __main__.py       # CLI：python -m versioned_orders
tests/              # 边界测试（版本/幂等/并发/超时/取消/关停/多入口）
main.py             # 兼容原始启动方式：uvicorn main:app
```

## 1. 版本兼容规则

支持 `v1`、`v2` 两套版本并存，通过以下任一方式指定（同时给出时必须一致）：

- 路径前缀：`POST /v1/orders`、`POST /v2/orders`
- 请求头：`POST /orders` + `X-API-Version: v1|v2`

| 情况 | 结论 | code | HTTP |
|---|---|---|---|
| 路径与头都未指定版本 | 拒绝 | `VERSION_MISSING` | 400 |
| 版本不在 {v1,v2}（如 `/v3/orders`） | 拒绝 | `VERSION_UNSUPPORTED` | 400 |
| 路径版本与头版本不一致 | 拒绝 | `VERSION_CONFLICT` | 400 |
| 字段非法 / 缺必填 / 取值越界 | 拒绝 | `FIELD_INVALID`（details 带字段名与入参值） | 422 |

字段语义：

- **v1 是冻结契约**：字段集合、必填性、返回结构永不改变；v1 的响应里不会
  出现 v2 的新字段。
- **v2 只做增量**：在 v1 全部字段之上新增
  - `priority`：`normal` | `high`，**缺省固定为 `normal`**；
  - `coupon_code`：字符串或 null，**缺省固定为 `null`**。

  新字段缺省值是确定且稳定的（不随环境、不随机）。读接口
  `GET /v1/orders/{id}` 与 `GET /v2/orders/{id}` 也按版本裁剪返回结构。

## 2. 幂等与并发语义

所有变更请求必须携带 `Idempotency-Key` 头（缺失 →
`IDEMPOTENCY_KEY_MISSING` / 400）。

幂等结论以“键 + 请求指纹（版本 + 规范化请求体的哈希）”为键：

- **首次提交**：键进入 `inflight`，副作用（支付扣款、库存预留、订单写入）
  只在这一次执行；
- **同键同体重复提交（在途或已完成）**：不执行任何副作用，等待 / 重放首个
  请求的**完全相同结论**（成功返回同一 `order_id`，失败返回同一错误码）。
  响应里 `replayed=true`；
- **同键不同体**：明确拒绝，`IDEMPOTENCY_REPLAY_CONFLICT` / 409，避免把旧
  结论错误重放到新请求上；
- **失败也是结论**：超时 / 取消 / 依赖失败都会被缓存并重放；要换一种结果
  重试，必须使用**新的幂等键**。

由此保证：副作用恰好一次（exactly-once effect），同键并发结果一致、无重复
写入（`order_id`/`charge_id`/`reservation_id` 都由幂等键确定性派生）。

全局在途治理：

- 服务有全局在途上限 `max_inflight`（默认 8）。**超过上限立即拒绝**
  （`CONCURRENCY_LIMIT` / 429，details 带 `max_inflight`），**不排队、不悄悄
  丢弃**；在途槽位释放后服务自动恢复。
- 注意：同键的并发等待者不额外重复执行副作用，但它们同样占用在途名额
  （它们是真实的在途请求）。

## 3. 超时、取消、依赖失败与关停

| 场景 | code | HTTP | 说明 |
|---|---|---|---|
| 超过本次超时预算 | `TIMEOUT` | 504 | details 带 `stage` 与 `timeout_seconds`；未提交订单 |
| 请求被调用方 / 关停中途取消 | `CANCELLED` | 499 | details 带 `stage`；等待者取消与首请求取消都归一化 |
| 模拟依赖返回失败 | `UPSTREAM_ERROR` | 502 | details 只带受控的 `dependency`，不透出内部异常文本 |
| 关停后到达的新请求 | `SHUTDOWN` | 503 | 收到关停信号后立即拒绝新工作 |
| 资源不存在 | `NOT_FOUND` | 404 | |
| 未预期内部错误 | `INTERNAL_ERROR` | 500 | 固定文案，内部细节仅写日志 |

关键保证：

- **内部异常不外泄**：`UpstreamError`、栈信息等不会出现在响应体里；对外只有
  稳定的 `code` + `reason`。
- **无中间状态**：先调用两个外部依赖，**全部成功后**才做一次**纯同步**提交
  （提交路径上没有 `await` 挂起点，不会再被取消/超时打断）。依赖在任何一步
  失败，都不会写入订单。
- **优雅关停**：收到关停信号后，关停闸门翻转 → 新请求得到 `SHUTDOWN`；
  在途请求继续执行，由 `wait_drained` 等待其结束；超过宽限期
  （`shutdown_grace`，默认 5s）仍未排空则明确报告未排空（不留“结果不确定”）。

每个响应信封都带 `request_id`（可由 `X-Request-Id` 指定，否则自动生成）与
`idempotency_key`。所有关键判定都打结构化日志，含请求标识、幂等键、判定类型
与判定依据，例如：

```json
{"request_id":"req-c0","idempotency_key":"concurrent-key",
 "decision":"idem_inflight_wait",
 "reason":"identical idempotency key in flight; waiting for the original outcome"}
```

## 4. 错误响应格式

```json
{
  "ok": false,
  "error": {
    "code": "CONCURRENCY_LIMIT",
    "message": "server is handling too many in-flight requests",
    "reason": "global in-flight limit of 8 reached; the request is rejected immediately rather than queued",
    "details": {"max_inflight": 8}
  },
  "request_id": "req_...",
  "idempotency_key": "k-123",
  "replayed": false
}
```

成功响应：`{"ok": true, "data": {...}, "request_id": ..., "idempotency_key": ..., "replayed": false}`。

## 5. 本地验证方法

环境要求 Python 3.14，使用 [uv](https://docs.astral.sh/uv/) 管理依赖。

```bash
uv sync                      # 安装依赖
uv run pytest                # 运行全部测试（-s 已默认开启，可看到判定日志）
```

启动服务（三种等价方式）：

```bash
uv run python -m versioned_orders --port 8000
uv run uvicorn main:app --port 8000          # 兼容原始入口
uv run uvicorn versioned_orders.api:create_app --factory --port 8000
```

手工验证：

```bash
# v2 创建（缺省 priority=normal, coupon_code=null）
curl -s -X POST http://127.0.0.1:8000/v2/orders \
  -H 'Idempotency-Key: k-1' -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","sku":"sku-1","quantity":2,"amount_cents":1500}'

# 同键重复提交 -> replayed=true，副作用不重复
curl -s -X POST http://127.0.0.1:8000/v2/orders \
  -H 'Idempotency-Key: k-1' -H 'Content-Type: application/json' \
  -d '{"customer_id":"c1","sku":"sku-1","quantity":2,"amount_cents":1500}'

# 头版本入口；缺版本 / 非法版本 / 冲突
curl -s -X POST http://127.0.0.1:8000/orders -H 'X-API-Version: v2' \
  -H 'Idempotency-Key: k-2' -H 'Content-Type: application/json' -d '{...}'
```

### 仅用于本地验证的测试钩子

- `X-Debug-Inject`：JSON，注入依赖延迟/失败：
  `{"payment":{"delay":0.4}}`、`{"payment":{"fail":true}}`、
  `{"inventory":{"fail":true,"fail_category":"out_of_stock"}}`；
- `X-Request-Timeout`：覆盖本次请求超时预算（秒）。

```bash
# 超时：预算 0.05s，支付依赖延迟 0.4s -> TIMEOUT/504；同键重试仍重放同一结论
curl -s -X POST http://127.0.0.1:8000/v1/orders \
  -H 'Idempotency-Key: slow-1' -H 'Content-Type: application/json' \
  -H 'X-Request-Timeout: 0.05' \
  -H 'X-Debug-Inject: {"payment":{"delay":0.4}}' \
  -d '{"customer_id":"c1","sku":"sku-1","quantity":1,"amount_cents":100}'
```

## 6. 测试覆盖一览（tests/）

- `test_versioning.py`：版本矩阵（缺失/不支持/冲突）、v1 冻结与 v2 稳定默认、
  字段非法可区分；
- `test_idempotency.py`：重复提交重放、8 路同键并发单一写入且结果一致、
  同键不同体 409、失败结论重放、判定日志含 request_id/幂等键/依据；
- `test_concurrency.py`：在途超限立即 429 且不排队、关停拒绝新请求并排空、
  宽限期超时给出确定结论；
- `test_failures.py`：超时、首请求中途取消、同键等待者取消、依赖失败归一化、
  各类错误码两两可区分；
- `test_http_api.py`：三类 HTTP 版本入口等价、错误矩阵、网络重放、同键并发、
  按版本读回、409 冲突；
- `test_entrypoints.py`：HTTP / 服务直调 / 函数式入口共享状态时同键结论一致。
