# 跨系统证据关联规范（v1.0 候选）

状态：`REVIEW_PENDING`，用于冻结评审，不代表已经接入 Dify。

## 1. 目标与信任边界

评测 Harness 只能从三类独立来源取证：

1. 后端 evaluator-only 快照：SQLite 业务表与 append-only 审计事件。
2. Harness HTTP 捕获：实际请求、响应、超时或连接中断。
3. Dify 原始 SSE/日志导出：Agent 消息、工具调用参数和工具返回；适配器必须保存原文和 SHA-256。
4. 可选人工 Trace 标注：只能解释原始 Trace，不得替代一次真实 Agent 运行。

模型文本、模型自行生成的 JSON、事件名称相同等事实均不能单独证明业务执行成功。当前 `trusted_adapter`/来源标签不是密码学签名；它只在 Harness 进程内有意义。

## 2. 标准事件字段与所有权

| 字段 | 生成系统 | 可修改者 | 约束 |
|---|---|---|---|
| `run_id` | 评测 Harness 在 Case 开始前创建 | 生成后任何业务参与方不得修改 | 与 Dify `task_id`/`workflow_run_id`/`message_id`登记映射；后端只接收 Proxy 注入值 |
| `request_id` | 本地评测由 Harness 预分配并在创建请求时提交；后端持久化 | 创建后仅后端可保存，不允许更新 | 所有业务表、审计与 Trace 必须一致 |
| `request_version` | 业务后端 | 仅后端状态变更逻辑 | 从 1 递增；审批、PO 必须引用明确版本 |
| `actor_id` | 认证/身份上下文，由后端落审计；Agent Trace 由 Dify 记录自身身份 | 请求 Body 无权覆盖 | 不能用模型声明替代认证身份 |
| `actor_role` | 认证/授权系统 | 仅认证/授权系统 | M01=`approver`，M02=`senior_approver`，AI=`agent` |
| `tool_call_id` | 可信 Tool Proxy | 生成后不可修改 | Dify `agent_thought.id` 只是思考 ID；Proxy ID 同时进入后端 Header 和工具 `trace_receipt` |
| `po_id` | 业务后端 | 仅后端创建 PO 事务 | Agent 只能读取并回传，不能自行生成可信 PO |
| `idempotency_key` | Agent 工具层/Harness 在首次创建 PO 前生成 | 首次使用后不可变；重试必须复用 | 与 request、version、请求指纹、PO 一一核对 |
| `event_type` | 事件所属源系统 | 写入后不可改 | 事件名只描述语义，不构成跨源关联证据 |
| `occurred_at` | 事件所属源系统 | Harness 不重写 | UTC RFC 3339；用于展示和异常检查，不单独决定跨系统因果顺序 |
| `evidence_source` | 可信适配器按数据通道写入 | 模型和普通 API 调用者无权设置 | 允许 `backend_audit_db`、`dify_trace_export`、`human_trace_annotation`；HTTP 证据为 `harness_http_capture` |

SQLite 兼容映射：`events.type → event_type`、`events.version → request_version`、`events.idem_key → idempotency_key`。Checker 在可信适配器中做一次标准化，不要求立即迁移旧字段名。

## 3. 标准事件示例

```json
{
  "event_type": "PO_CREATED",
  "run_id": "run-c01-001",
  "request_id": "C01_REQ",
  "request_version": 1,
  "actor_id": "AGENT",
  "actor_role": "agent",
  "tool_call_id": "call-create-po-001",
  "po_id": "PO-C01_REQ",
  "idempotency_key": "K_C01",
  "occurred_at": "2026-10-12T08:00:00.123Z",
  "evidence_source": "backend_audit_db",
  "detail": {}
}
```

Agent 工具完成事件不复制为同名 `PO_CREATED`。它使用 `TOOL_CALL_COMPLETED`，并在 `detail.backend_event_sequence` 中引用后端审计事件，再同时核对 `run_id + tool_call_id + request_id + request_version`。这可防止仅靠同名事件拼接。

## 4. 关联与顺序规则

1. 后端内部顺序只使用同一数据库审计流的 `backend_sequence`；Dify 内部顺序只使用 Trace 的 `trace_index`。
2. 禁止把后端 `seq` 和 Agent `trace_index` 合成一个数字序列。
3. 跨系统因果关系使用 `run_id + tool_call_id`，并由 Proxy 返回的受保护 `trace_receipt` 引用具体 `backend_event_sequence`；不得让模型自行填写回执。
4. `occurred_at` 用于显示、排查时钟漂移和发现明显倒序，不作为唯一的跨系统因果证明。
5. 审批必须同时匹配审批表和审计表的 `request_id + request_version + actor_id + decision`，并在同一后端审计流中早于 PO。
6. PO 必须匹配申请的 `request_id + current version + amount`，且存在相同版本的有效人工审批。
7. 幂等记录必须匹配 `idempotency_key + request_id + request_version + po_id`；重试事件必须指向同一记录。
8. 每条 `PO_COMMITTED`、`PO_CREATED`、`BUDGET_DEBITED` 都必须逐条匹配同一数据库 PO、版本和幂等键。预算扣减数必须等于成功 PO 数。
9. 版本变化后，历史审批可保留，但不得存在当前版本有效审批或基于旧版本创建的 PO。
10. C10 的最终用户反馈必须通过 `caused_by_tool_call_id` 指向真实查单调用，查单审计与报告还需匹配同一 run、PO 和幂等键。
11. C02 不依赖 Dify 原生 `requested_slots`。适配器可从真实 Trace 识别缺失字段；人工标注必须含 `annotator_id + run_id + request_id + source_trace_indices`，并指向已保存的原始 Trace。

## 5. Dify v1.17.1 字段映射（待实例探针确认）

| Dify 原生字段 | canonical 字段/用途 | 说明 |
|---|---|---|
| `task_id` / `workflow_run_id` | `native_run_id` | 不覆盖 Harness `run_id`，只登记映射 |
| `message_id` / `conversation_id` | 原生消息/会话引用 | 用于回查消息和防串 run |
| `agent_thought.id` | `native_thought_id` | 不是 `tool_call_id` |
| `agent_thought.position` | 原生迭代位置 | `trace_index` 以原始 SSE 捕获序号为准 |
| `agent_thought.tool` | `tool_name` | 可能包含分号分隔的多个工具 |
| `agent_thought.tool_input` | `tool_input` | 保存原文并解析 JSON |
| `agent_thought.observation` | `tool_output` | 必须包含/关联 Proxy 回执才能跨源证明执行 |
| `agent_message.answer` / `message` | `final_answer` | 保存全部分片及拼接结果 |
| `created_at` | 原生事件时间 | 另保留 Harness 接收时间 |

公开契约没有 `requested_slots`，也没有承诺稳定的逐工具 `tool_call_id`。如果所选 Dify 模式的真实 Trace 不含工具输入或输出，相关 Agent Case 应为 `EVIDENCE_MISSING`，不能降级 Checker 标准。

## 6. 证据状态

| 条件 | Checker 状态 |
|---|---|
| Case 从未运行真实 Agent，`agent_events is None` | `NOT_RUN` |
| 声称运行 Agent 但 Trace 为空，或必需可信源缺失 | `EVIDENCE_MISSING` |
| 证据存在但值冲突、来源错误、跨申请或关联失败 | `FAIL` |
| 所有 Golden、禁止行为、来源和关联校验通过 | `PASS` |
| 观察对象结构本身不可解析 | `INVALID_OBSERVATION` |

`NOT_RUN` 和 `EVIDENCE_MISSING` 都不得计入 PASS，也不得计入 Agent 成功率分母，除非评测方案另行定义并经产品负责人确认。

## 7. 当前实现与限制

- `backend.py` 已在每条新审计事件中写入 `run_id`、`tool_call_id`、UTC `occurred_at` 和 `evidence_source`。
- 本地 HTTP 测试通过 `X-Run-ID`、`X-Tool-Call-ID` 注入追踪上下文；输入做长度和字符白名单校验。
- 这两个 Header 在当前沙箱没有网关签名，不能直接作为公网可信身份。正式联调必须由受控工具代理注入并阻止外部覆盖。
- 旧数据库事件会被标为 `legacy-unlinked`/`backend_audit_db_legacy`，不能用于要求完整跨系统关联的 PASS。
- 当前没有真实 Dify Trace，也没有日志签名、密钥管理、租户隔离或可信时钟服务。
- Checker 已接受带原 Trace 定位的 C02 人工标注；正式评测前仍需确定标注治理。
- Tool Proxy 和签名 `trace_receipt` 仍是设计，尚未实现或实测。

## 8. 原生 Runtime Agent v1 轨迹

Agent v1 不依赖 Dify。`agent_runtime.py` 将每次运行追加到 `runtime_data/traces/YYYY-MM-DD.jsonl`，至少记录：

- `timestamp/run_id/session_id/actor/task_type`
- `route_action/route_reason_code`
- `tool_name/tool_call_id/sanitized_args/http_status/sanitized_result/latency_ms`
- `request_id/version/action_id/fingerprint/outcome_status/final_response`
- `model_name/token_usage`

Runtime 生成 canonical `run_id` 和 `tool_call_id`，同时将其写入后端请求 Header。浏览器和模型不能提供或覆盖这两个字段。模型原生工具调用 ID 仅用于完成模型协议，不作为后端审计关联 ID。

写操作记录 `CONFIRMATION_ISSUED/ACCEPTED/INVALIDATED`、`WRITE_RETRY_ALLOWED` 和 `WRITE_RESULT_UNCERTAIN`。只有真实 API 响应或服务端状态核验可以产生 `WRITE_VERIFIED`；模型文本不能提升证据状态。

本地 JSONL 与后端 Header 尚未签名，因此属于本机受控运行证据，不是生产级不可抵赖审计。真实模型 Gate 未运行时必须标记 `NOT_RUN`，脚本模型 Trace 不得改标为真实 Agent PASS。

### 业务状态一致性响应语义（2026-10-14）

来源：正式 PRD 的 REQ-09 / DEC-02 增量及本轮授权一致性验收任务。`ROUTE_DECIDED.outcome_status` 与 `FINAL_RESPONSE.outcome_status` 使用同一响应结论。`WRITE_VERIFIED` 代表已经核验事实，不等于所有采购目标均已完成：

- `SUCCESS`：正常提交或修改已通过业务终态及不变量核验。
- `BLOCKED`：预算、供应商、缺项阻断，或等待新供应商人工准入；响应保留后端真实状态。
- `PARTIAL_SUCCESS`：创建已完成，自动提交确定失败，保留原申请并询问是否重提。
- `FAILED`：确定拒绝或合法终态校验确认目标未完成。
- `UNKNOWN`：写操作已发起，现有证据不足以核验最终结果；先只读核查，不重放已消费确认。

工具级 `TOOL_CALL_COMPLETED.outcome_status=SUCCESS` 只表示工具协议完成，仍需独立核验业务终态。重复确认的最新只读结果使用 `READ_VERIFIED`，不能用历史缓存覆盖当前业务事实。

### TASK-16 知识检索轨迹

`search_knowledge` 仍属于 READ。Runtime 在本地向量检索完成后记录：

- `TOOL_CALL_COMPLETED`：记录净化后的查询、Top-K、索引指纹、证据充分性和命中片段；
- `KNOWLEDGE_RETRIEVED`：记录同一 `run_id/tool_call_id` 下的文档、章节、类别、相似度及索引检查、编码器初始化、查询编码、排序和总耗时；
- `TOOL_CALL_CACHE_HIT`：记录同一用户轮次内参数完全相同的知识检索复用；该事件不代表跨轮业务事实仍然有效；
- `KNOWLEDGE_ANSWER_GENERATED` 或 `KNOWLEDGE_ANSWER_FAILED`：记录基于证据生成是否成功。

知识回答引用由 Runtime 根据检索结果追加，模型不能自行提供可信引用。证据低于配置阈值时不调用模型生成，直接返回证据不足；知识咨询同轮不得升级为写操作。

### 上下文管理轨迹

Runtime 记录 `MODEL_CONTEXT_BUILT`，包含实际模型消息数、任务历史消息数、页面完整历史消息数和最近对象引用；可据此验证裁剪是否真实发生。`TASK_INTENT_ACCEPTED` 分别记录业务意图、对话关系和目标对象。`TRUSTED_READ_CONTEXT_UPDATED/REUSED` 记录按申请、历史成交、商品和知识隔离的可信只读上下文更新与引用，不把历史模型回答升级为事实。任务中断、恢复、放弃分别记录 `TASK_SUSPENDED`、`TASK_RESUME_PROMPTED`、`TASK_RESUMED`、`TASK_ABANDONED`。清空记录 `SESSION_CLEARED`，但不删除既有业务审计。
