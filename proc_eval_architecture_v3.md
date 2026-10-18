# 采购 Agent 评测架构 v3（结构收敛与数据获取方案）

## 1. 评什么与数据来源

保留 53 条候选 Case（N 正常14、E 异常15、B 边界15、P 权限9）。不把真实测试结果塞进静态用例文件。评测分开计算：**Agent 任务完成 / 业务后端规则 / 评分有效性**；同一能力的不同变体不能重复宣称覆盖了新能力。

```text
cases.json ──case_id, task/turns, fixture_ref, success_criteria──┐
fixtures.json ──部署前置数据与数据库快照──┬──────────────┤
                                            ↓              │
                                  Runner / Harness         │
                       ┌────────────────┴────────────┐     │
                       │Agent会话API                 │业务后端API
                       │对话、模型工具调用、确认动作 │供应商、审批、PO
                       └──────────┬─────────────────┘     │
                                  ↓                       │
                     原始 Trace + 环境前/后快照          │
                                  ↓                       ↓
                          证据适配器 (Evidence Adapter)
                                  ↓
                        统一 Evidence 对象（含 provenance）
                                  ↓
                Checker Registry → 每条 criterion 的 PASS/FAIL/缺证据
                                  ↓
                        raw.jsonl / scored.jsonl / 报告
```

## 2. 五类文件，各自只负责一件事

| 文件 | 职责 | 说明 |
|---|---|---|
| `proc_eval_cases_v3.json` | **题目** | 每条 `case_id, category, title, task, initial_state(引用), success_criteria, checker_refs`；无执行结果，无统一冗余证据清单 |
| `proc_eval_fixtures_v3.json` | **题目前的世界** | 各条已有条件迁移为 `requirements`，`provisioned=false`；这不是已经建好的 SQLite 快照 |
| `proc_eval_checkers_v3.json` | **评分器目录** | 保留 v2.2 的 92 个候选 Checker 名称；全部标记 `NOT_IMPLEMENTED`，不冒充已可调用函数；下一轮应收敛为通用模块 |
| `raw.jsonl`（运行产物） | **原始证据** | 每轮 prompt / response / route / tool trace / confirm 事件 / DB 变更；带 run_id、session_id、case_id、时间与环境版本 |
| `scored.jsonl`（运行产物） | **评分结果** | `raw_sha256`、checker_version、criterion_results、case_status、failure_attribution；一条 Case 可以含多个断言 |

## 3. 证据从哪里来（具体针对现有系统）

| 证据字段 | 权威来源 | 读取时机 | 注意 |
|---|---|---|---|
| `user_messages` | Runner 提交的用户指令 | 每个 turn | 用户文本是任务目标，不等于真实业务事实 |
| `agent_response`, `route_action`, `route_reason_code` | Agent API 响应；`FINAL_RESPONSE`/`ROUTE_DECIDED` 审计事件 | 每个 turn | 不凭回复宣称已写入数据库 |
| `model_tools` | `MODEL_TURN_COMPLETED` 和 `TOOL_CALL_STARTED/COMPLETED` | 每个 turn | 工具名和入参可追溯；当前日志**缺少** `propose_task_state` 提案参数 |
| `confirmation` | `CONFIRMATION_ISSUED/ACCEPTED/REJECTED/INVALIDATED` | 操作前后 | 必须用 action_id、fingerprint 关联；无明确执行步骤不得自动确认 |
| `request_before/after` | 采购后端受权接口 (`get_request`, `list_requests`) 或隔离 SQLite 查询 | Case 前后及关键转移时 | DB 为事实源；匿名/无权限不能跳过权限校验，仅测试环境可使用管理员检查器 |
| `supplier_status` | `match_supplier` 或后台供应商表 | 交互时 + 最终 | 同时比较用户指定供应商名称与实际写入名称，防止以正常供应商替换黑名单供应商 |
| `po_count`, `po_version`, `po_amount` | 后台 PO 数据/隔离 SQLite | 创建 PO 前后 | 按 request_id + version 关联，不只看全局 PO 数量 |
| `budget_delta` | 后台预算记录/隔离 SQLite | PO 前后 | 比较 `before - after`，不用硬编码“余额是4850” |
| `approval/evidence` | 后台申请详情和凭证状态 | 审批前后 | `required_approver` 不等于 `approved_by`；凭证状态为已核验不等于文件真实性已验证 |

**两个数据通道必须区分**：Agent 生产路径由 U01 权限执行；**Checker 的只读后台证据通道**仅在隔离测试环境可访问，并应记录来源及访问权限。绝不能让 Agent 直接拿 Checker 的管理员访问令牌。

## 4. Harness 的执行流程

1. 读取 Case，检查 `initial_state` 引用及前置条件是否可配置。
2. 隔离启动测试服务/临时 SQLite，加载 Fixture；不连接当前人工演示数据库，也不复用现有采购记录。
3. 每条独立 Case 使用独立会话及数据库快照；**同一 Case 的多轮消息必须复用一个 session_id**。
4. 运行 `task` 的 user turns 或业务动作。只有 Case 明确写了 `confirm` 才触发确认；业务端动作需显式身份。
5. 逐轮保存不可修改的事件 Trace；关键写入前后读业务状态，保存快照与关联 ID。
6. 证据适配器将事件和 DB 查询结果转换成统一证据字段。每项证据保留 source、run_id、timestamp、lookup key。
7. Checker 只读取证据对象，逐断言评分；缺字段返回 `EVIDENCE_MISSING`，不按文字猜测。
8. 原始证据先落盘，再评分；评分保留源 hash 和 checker 版本；支持离线重算。

### 业务动作接口的适配限制

当前 v2.2 中后台业务操作多为 `create_and_submit`、`approve_as_m01` 等**自然语言占位步骤**，尚不是可执行的接口调用。Agent 侧也需要确认本地 `agent_server.py` 的会话请求/确认 API 契约。必须在看过实际本地源码后再映射请求方法和认证方式；本版不捏造接口路径或未验证的权限配置。

## 5. 通用 Checker 应该只有少量，复杂逻辑按规则配置

建议先实现这些 **能力级 checker**，不为每条用例造一个 Python 函数：

- `route_check`：ASK / CONFIRM / STOP，依据结构化 route 字段。
- `slot_integrity_check`：用户本轮明确提供的字段与最终 confirmation / 写入一致；多轮要按任务边界追踪来源。
- `no_side_effect_check`：本不应写入时数据库/业务写入事件没有变化。
- `backend_state_check`：状态、审批、供应商、PO 与目标环境一致。
- `budget_delta_check`：预算前后精确金额差。
- `idempotency_check`：重复操作后同一业务对象/版本无多余 PO 或预算扣减。
- `auth_boundary_check`：角色、动作、HTTP 结果及状态不越权。
- `clarification_review`：语义澄清是否覆盖必要信息，初期用人工复核，LLM Judge 须与确定性证据分开。

旧的 92 个名称只是候选别名，需将它们映射到上述检查器再实现，**目前全部未实现**。

## 6. 三条代表 Case 如何评分

### E01：模糊需求

`task`: 办公设备需求描述；`initial_state`: 空白 U01 测试会话；判据是 ASK、澄清关键字段、无写入。`route_check` 从 `ROUTE_DECIDED` 取 ASK；`clarification_review` 审核询问内容；`no_side_effect_check` 从隔离数据库前后或写入事件判断。无必要保存 PO 全量详情。

### C04_BC06：供应商不应被替换

准备启明办公(active)、风险供应商(blacklisted)，用户明确采购风险供应商。Checker 同时读取**用户文本/解析字段、match_supplier 查询、确认 payload 与后端写入状态**。断言不能把风险供应商变成启明办公；不允许产生与用户意图冲突的合法申请。仅看到正确的 `match_supplier` 不足以判 PASS。现有日志缺 `propose_task_state` 提案参数，可能需要审计事件扩展。

### C01：审批与 PO

准备余额充足的市场部初始预算，提交低金额申请，M01 人工审批，U01 创建 PO，再重试创建；检查状态为 PO_CREATED、同申请版本恰好一张 PO、`budget_before - budget_after = PO.amount`、重复执行不额外扣钱。这个 Case 主要给后端业务规则分数，不能算成 Agent 独立完成审批。

## 7. 未完成的工程工作与风险

- 53 条是**迁移后的候选用例**，不是 53 条可运行/已验证用例。
- 53 个 Fixture 仅保留原 requirements，目前均未完成创建；部分条件（U02、工具超时、冻结时钟、并发版本）需特殊环境。
- 92 个 checker 名称尚未真正实现，也没有确定输出 schema；先设计统一 evidence 再逐个实现通用 checker。
- 部分原 Golden 使用 `contains_all_or_semantic_equivalent` / `evaluate_list_rule`，不可直接当确定性 Checker；需要评审后转为可量化规则或人工/LLM 复核。
- 当前迁移保留原用例中的自然语言步骤，Runner 尚不能直接执行；这避免把未实现的测试步骤伪装成可运行脚本。
- 源码审计应补 `propose_task_state` 的脱敏参数、当前任务 ID 与字段 provenance，以便定位 BC-03/BC-06。

## 8. 建议的实施先后

**最小纵切片**：E01（澄清）→ A02_BC03（同类型任务隔离）→ C04_BC06（供应商完整性）→ C01（后端审批/预算）。先打通：读取 Case → 部署 Fixture → Runner → Trace → Evidence Adapter → Checker → 结果。能验证后再覆盖 53 条，不以用例数量充当项目成果。
