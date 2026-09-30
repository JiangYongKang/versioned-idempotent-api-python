# 版本化幂等订单服务（Python / FastAPI）

在原有下单服务之上增加一层**对外请求的版本化与治理能力**：

- 同一资源两套接口版本（`v1` / `v2`）并存，旧版本字段语义与返回结构永不被破坏；
- 按**幂等键**去重与并发控制：同键重复提交副作用只生效一次，同键并发结论一致、不重复写入；
- **在途请求上限**：超过上限立即、明确地拒绝（不排队、不静默丢弃）；
- **超时 / 取消 / 外部依赖失败**全部归一化为可区分的错误类型，不泄露内部异常，不留中间状态；
- **优雅关停**：关停后拒绝新请求，在途请求处理完或明确失败，结论确定；
- HTTP 与进程内 / CLI 入口共享同一套核心逻辑，结论在任何入口下都一致、可解释。

外部依赖（“库存系统”）用**本地内存状态 + 线程锁**模拟，可注入延迟与强制失败，
无需任何真实第三方服务即可本地复现全部边界。

---

## 1. 目录结构

```
app/
  errors.py         # 对外错误分类（唯一基类 ServiceError）与错误码
  versioning.py     # 版本协商：缺失 / 不支持 可区分
  models.py         # 分版本严格校验 + 分版本响应构造
  external.py       # 本地状态模拟的“库存系统”（延迟/取消/强制失败/回滚）
  idempotency.py    # 幂等键状态机 + 全局在途闸门 + 关停信号
  service.py        # 核心编排：版本→键→字段→闸门→幂等→有界执行→归一化错误
  http.py           # FastAPI 入口（断连取消、统一错误体、lifespan 优雅关停）
  cli.py            # 进程内直调入口（不走 HTTP，结论一致）
  logging_setup.py  # 结构化判定日志（请求标识/幂等键/判定结论/依据）
main.py             # uvicorn 启动入口
tests/              # 覆盖全部边界的单测（含真实 uvicorn 断连测试）
```

---

## 2. 快速开始

```bash
# 安装依赖（需要 Python >= 3.14）
uv sync

# 启动 HTTP 服务
uv run python main.py
# 或：uv run uvicorn main:app --port 8000
```

下单接口：`POST /orders`，版本与幂等键通过请求头传递：

| 请求头 | 含义 | 必填 |
| --- | --- | --- |
| `X-Api-Version` | 接口版本：`v1` / `v2` | 是 |
| `Idempotency-Key` | 幂等键（非空字符串） | 是 |
| `X-Request-ID` | 请求标识，缺省自动生成；响应原样回显 | 否 |

```bash
curl -s -X POST http://127.0.0.1:8000/orders \
  -H 'X-Api-Version: v2' -H 'Idempotency-Key: order-001' \
  -H 'Content-Type: application/json' \
  -d '{"item":"book","quantity":2}'
```

---

## 3. 版本兼容规则

| | v1 | v2 |
| --- | --- | --- |
| 接受字段 | `item`、`quantity`（多传即拒） | `item`、`quantity`、可选 `priority` |
| `priority` | 不接受（出现即 `invalid_field`） | 可选，**缺省稳定为 `"normal"`**，取值 `normal`/`high` |
| 返回结构 | `{order_id, item, quantity}` | v1 的**超集**：追加 `priority`、`api_version` |

规则：

1. **旧版本不破坏**：v1 的字段集合、含义、类型、返回结构保持不变；v2 只做“向后兼容的超集式新增”。
2. **新字段默认确定**：v2 未提供 `priority` 时恒为 `"normal"`，不依赖调用方、不随时间变化。
3. **严格校验**：缺失/空值/类型不符（如 `"1"` 不会被静默当成整数）/未知字段，一律 `invalid_field`，错误信息指出具体字段。
4. **版本问题与字段问题分开拒绝**：
   - 没带版本头 → `version_missing`（400）
   - 带了但不支持（如 `v3`）→ `version_unsupported`（400）
   - 版本合法但字段非法 → `invalid_field`（422）

---

## 4. 幂等与并发语义

### 4.1 幂等键

- 每个写请求必须带非空 `Idempotency-Key`，否则 `idempotency_key_missing`（400）。
- 键的“请求指纹”为 `版本 + item + quantity + priority` 的规范化 JSON。
- **同键同指纹**：
  - 首个请求（leader）执行并产生**唯一一次**外部副作用；
  - 之后的重复提交（串行或并发，follower）阻塞等待同一结论并**原样重放**：
    成功返回同一 `order_id`（响应头 `Idempotent-Replay: true`，HTTP 200），
    失败重放同一错误码。结论一致、不重复写入。
- **同键不同指纹**：`idempotency_conflict`（409），立即拒绝，不产生额外副作用。
- **失败结论也被记录**：同一键会一致地拿到该失败（便于调用方区分“仍在处理”与“确定失败”）；
  调用方修正后应**使用新幂等键**重试（失败是 `retryable=true`）。

> 幂等去重在**单个服务实例的内存状态**内生效；这是 bootstrap 的本地模拟实现。
> 生产上应把幂等表替换为共享存储（如 Redis / DB 唯一键），语义不变。

### 4.2 在途并发上限

- 服务有显式上限（默认 `max_in_flight=4`，测试中为 1~2）。
- 达到上限后新请求**立即**得到 `concurrency_limit_exceeded`（HTTP 429，`retryable=true`），
  错误信息含上限与当前在途数——**不无限排队，也不静默丢弃**。
- 被限流的请求未占用任何资源、无副作用；名额释放后重试即可成功（名额不会泄漏）。

---

## 5. 错误分类

所有对外错误都是 `ServiceError` 的子类，响应体统一为：

```json
{ "error": { "code": "...", "message": "...", "retryable": false, "request_id": "..." } }
```

| code | HTTP | retryable | 含义 |
| --- | --- | --- | --- |
| `version_missing` | 400 | 否 | 未提供版本 |
| `version_unsupported` | 400 | 否 | 版本不在支持列表 |
| `invalid_field` | 422 | 否 | 字段缺失/类型错/非法取值/未知字段/Body 非 JSON 对象 |
| `idempotency_key_missing` | 400 | 否 | 未提供幂等键 |
| `idempotency_conflict` | 409 | 否 | 幂等键被不同请求体复用 |
| `concurrency_limit_exceeded` | 429 | 是 | 在途数超上限，立即拒绝 |
| `timeout` | 504 | 是 | 超过请求超时 |
| `cancelled` | 503 | 是 | 请求被调用方取消 / 客户端断连 |
| `external_dependency_failed` | 502 | 是 | 模拟外部依赖失败 |
| `shutting_down` | 503 | 否 | 服务关停中，拒绝新请求 |
| `internal_error` | 500 | 是 | 未预期内部异常（**不透出内部细节**） |

要点：

- **内部异常不直接透出**：任何非预期异常在边界归一化为 `internal_error`，
  对外信息不含内部堆栈/细节；细节只进服务端判定日志。
- **超时 vs 取消 vs 外部失败可区分**：三类错误码、HTTP 状态码各不相同。
- **无中间状态**：超时/取消会令外部写入中止；若与外部成功发生临界竞态，
  已写入的预留会被立即回滚（`release`），订单表与库存表保持一致。

### 超时 / 取消的确定性裁决

外部调用在工作线程执行，主线程在「成功完成 / 调用方取消 / 超时」三者间按
**先到事件**裁决：成功先到则提交；取消先到为 `cancelled`；超时窗口用尽为 `timeout`。
超时或取消后会等待工作线程完成中止与回滚收尾再返回，因此结果确定、无悬挂写入。

---

## 6. 优雅关停

- 收到关停信号（FastAPI lifespan 退出 / 直接调用 `service.shutdown()`）后：
  立即进入关停态，新请求得到 `shutting_down`（503）；
- 等待所有在途请求**得出确定结论**（成功提交，或失败/超时/取消），在途计数归零；
- 不会出现“请求结果未知”的情况。可观测：`GET /health` 返回 `status` 与 `in_flight`。

---

## 7. 判定日志

每个关键判定点打印一条结构化日志，包含**请求标识、幂等键、判定结论、判定依据**：

```
INFO [request_id=c3 idem_key=CONC] REPLAY | reason=key already completed order_id=ord-1
WARNING [request_id=overflow idem_key=overflow] REJECT | reason=concurrency_limit_exceeded in_flight=2 limit=2
WARNING [request_id=slow idem_key=slow] ABORT | reason=timeout
```

判定结论（`decision`）包括：`VERSION_RESOLVED / REJECT / ADMITTED / COMMIT /
REPLAY / REPLAY_FAILURE / ABORT / FAIL / SHUTDOWN`，单测通过 `caplog` 断言这些字段。

---

## 8. 本地验证

```bash
# 全量单测（31+ 用例，含真实 uvicorn 断连取消）
uv run pytest

# 只看某类边界
uv run pytest tests/test_idempotency.py    # 重复键 / 同键并发 / 指纹冲突
uv run pytest tests/test_concurrency.py   # 超限立即拒绝 / 限流后重试
uv run pytest tests/test_timeout_cancel.py# 超时 / 中途取消 / 外部失败 / 内部异常归一化
uv run pytest tests/test_shutdown.py      # 优雅关停
uv run pytest tests/test_http_entry.py    # HTTP 状态码/错误码/重放头
uv run pytest tests/test_http_cancel.py   # 真实网络客户端中途断开
```

进程内 / CLI 直调入口（与 HTTP 共享同一核心，结论一致；注意 CLI 每次是新进程、
内存状态不跨进程，重放需在同一实例内观察，HTTP 用例已覆盖跨连接重放）：

```bash
# 正常 v2（缺省 priority=normal）
uv run python -m app.cli --version v2 --key k-1 --item book --quantity 2
# 超时：外部延迟 3s，请求超时 1s
uv run python -m app.cli --version v1 --key slow --item a --quantity 1 \
  --inject-delay 3 --timeout 1
# 外部依赖失败
uv run python -m app.cli --version v1 --key boom --item a --quantity 1 --force-fail
# 非法版本
uv run python -m app.cli --version v9 --key x --item a --quantity 1
```

手动 HTTP 验证：

```bash
# 1) 缺版本 -> 400 version_missing
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/orders \
  -H 'Idempotency-Key: k' -d '{"item":"a","quantity":1}'
# 2) 同键重复 -> 第二次 200 且 Idempotent-Replay: true
curl -s -D - -X POST localhost:8000/orders \
  -H 'X-Api-Version: v1' -H 'Idempotency-Key: dup' \
  -d '{"item":"a","quantity":1}' -o /dev/null
curl -s -D - -X POST localhost:8000/orders \
  -H 'X-Api-Version: v1' -H 'Idempotency-Key: dup' \
  -d '{"item":"a","quantity":1}' -o /dev/null
```

---

## 9. 设计边界与可扩展点

- 幂等表与库存表为**单实例内存模拟**；替换为 Redis/DB 即可水平扩展，接口不变
  （`IdempotencyStore`、`FakeWarehouse` 都是可注入边界）。
- 超时为服务端请求级超时；如需分阶段超时（如排队超时 / 依赖超时）可在 `OrderService` 扩展。
- 版本协商目前基于 `X-Api-Version` 请求头；如需 URL 前缀 `/v1/...` 或内容协商，
  在 `versioning.resolve_version` 与路由层接入即可，错误分类不变。
