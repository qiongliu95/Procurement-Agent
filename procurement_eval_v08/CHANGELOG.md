# 变更记录

## 2026-10-16｜两级审批定向修复

- 用户确认规则覆盖旧 F05：金额 ≤10000 元由 M01 审批；金额 >10000 元先 M01、后 M02。正常提交及供应商准入共用 M01 起点；M01 初审不写入最终批准状态。
- M02 前置检查当前申请版本的 M01 批准；高额最终批准保留可信凭证要求。PO 复核本版本按顺序发生的完整批准记录，预算及幂等扣减规则不变，Agent 仍无审批权限。
- 列表、详情、Agent 结果卡和状态回复区分当前节点与最终级别；人工批准请求附带页面读取的申请版本。
- 新增隔离两级审批回归及默认只预览的路由维护脚本。正式 PRD、规则 JSON、两篇审批知识及索引指纹同步；不修改冻结 Golden/Checker 或历史评测证据。
- 经授权停服备份后，仅修复 AGT-EF7E89A3493B4914：PENDING_HIGH_APPROVAL → SUBMITTED（待 M01），保留 v1、零批准和无 PO，新增修复事件154。预算、审批、PO、幂等及其他申请未改动。DEMO-HIGH 旧 M02 单级批准记录保留，需人工复核，不能创建新 PO。
- 验收区分：隔离 HTTP/SQLite 状态流转及页面按钮渲染逻辑已验证；未执行真实采购批准、真实模型调用或真人浏览器审批全流程，不能将离线通过等同于人工验收。
- 最终定向合并回归164项：159通过、5项真实模型测试跳过；规则来源版本更新后单项复测通过。审批专项13项（包含并发与旧版本反例）全部通过，页面两种节点×两种角色按钮逻辑及JS语法检查通过。旧目录夹具裸跑存在与现行目录规则不兼容的失败，采用项目既有Catalog兼容夹具复用安全断言，未为本次修复降低断言或调整冻结评分。

## 2026-10-12｜AI 采购 Agent v1

- 新增原生 OpenAI-compatible Tool Calling Runtime、固定 U01 会话、四个授权只读工具和最小聊天页面。
- 新增 ASK/READ/CONFIRM/ACT/STOP 路由、服务端确认绑定、创建/提交/修改受控写入和写后核验。
- `submit/update` 对 Agent 强制执行原子 `expected_version` 校验；版本冲突不会使用旧确认继续写入。
- 单价按 PRD 支持零元，数量、精度和其他业务规则保持不变。
- 未知执行结果只在唯一 request ID 或原子版本/状态条件提供可靠排他证据时最多重试一次，否则返回待核实。
- 新增 Runtime JSONL Trace、Agent 定向测试和显式真实模型 Gate；当前无模型密钥，真实 Gate 为 `NOT_RUN`。

## 2026-10-12｜P2 Dify 联调前置验证

### 最小业务 API

- `POST /requests` 改为接收 `supplier_name`，由可信供应商主数据解析并落库 `supplier_id`；拒绝 Agent 直接提供 ID，记录 `SUPPLIER_SOURCE_RESOLVED`。
- 新增 `GET /requests/{id}`，只返回授权主体需要的申请、审批、供应商和 PO 摘要；不返回预算余额、审计全量、幂等表或 evaluator 快照。
- 保留 D03 行为：高额申请可在凭证未齐时提交并路由 M02，但批准返回 `EVIDENCE_REQUIRED`，PO 不能创建。

### 证据适配

- C02 不再依赖 Dify 原生 `requested_slots`；Checker 接受 Trace 中识别的字段，或与 run/request/trace index 绑定的可信人工标注。
- 明确 Dify `agent_thought.id` 不等于稳定工具调用 ID；canonical `tool_call_id` 由未来可信 Tool Proxy 生成。
- 候选 Case 只调整 D01–D05 字段映射和证据来源，未改变 Golden Outcome。

### 验证与文档

- HTTP/API 回归由 14 条增至 19 条并全部通过；Checker 单元测试由 11 条增至 12 条并全部通过；合并回归 31/31。
- 新增 `DIFY_TRACE_VALIDATION_REPORT.md`、`SAFE_CONNECTION_PLAN.md`、`FREEZE_BLOCKERS.md`。
- 本机没有 Dify/Docker，真实 Agent 工具调用仍为 `NOT_RUN`；未部署服务、未开放公网、未调用付费模型。

## 2026-10-12｜v1.0 冻结候选准备（未冻结）

### 后端审计

- 为新审计事件增加 `run_id`、`tool_call_id`、`occurred_at`、`evidence_source`。
- 从受控 HTTP Header 接收 run/tool 关联标识；缺失时为本地调用生成唯一值，并校验长度和字符集。
- 使用线程安全的请求上下文，避免并发 HTTP 请求串用 Trace 标识。
- evaluator-only 快照增加幂等记录，支持 Checker 交叉核对 PO 与幂等表。
- 为已有 SQLite 表提供最小增量迁移；历史事件明确标记为 legacy，避免伪装成完整新证据。

原因：原事件表无法把真实 Agent Tool Trace 与后端执行证据可靠关联，也没有可信时间和来源字段。

### Checker

- 默认加载 `cases_v1.0_candidate.json`，保留 `cases_v0.8_review.json` 作为历史基线。
- 将后端审计和 Agent Trace 分通道校验，不再使用一个全局 `seq` 混排。
- 增加 DB 快照摘要、来源权限、逐事件契约字段与跨申请污染检查。
- 增加审批表↔审计、PO↔申请/版本/审批、PO↔幂等表、预算扣减↔PO 的关系检查。
- Agent 工具结果使用 `run_id + tool_call_id + backend_event_sequence` 关联，避免同名事件拼接。
- Agent Case 没有真实 Trace 时返回 `NOT_RUN`；声明已运行但空 Trace 时返回 `EVIDENCE_MISSING`。
- 修复反例测试发现的漏洞：每条 `PO_COMMITTED`、`PO_CREATED`、`BUDGET_DEBITED` 都必须独立匹配真实 PO，不能被其他正确事件掩盖。

原因：评分必须依赖真实业务状态和可信执行证据，不能依赖模型自述。

### 测试

- 保留 14 条真实 loopback HTTP + SQLite 回归测试，并显式分离 `http_backend_contract`、`full_case_checker`、`agent_e2e_result`。
- 新增 11 条 Checker 单元测试，包含用户要求的 7 类反例、正例/空 Trace 状态及跨系统强关联测试。
- HTTP 测试为每次调用注入同一 run 下唯一 tool call 标识，并核对审计字段。

原因：Checker 单元测试、业务 API 测试和 Agent E2E 评测不得混算。

### 规格与文档

- 新增 `cases_v1.0_candidate.json`，统一 C01–C10 结构并保留 v0.8 变更记录和待确认问题。
- 新增 `TECH_AUDIT.md`、`trace_contract.md`、`TEST_REPORT.md`、`DIFY_INTEGRATION_PLAN.md`。
- 更新 README 的测试命令、证据状态和冻结声明。

### 明确未做

- 未宣布 v1.0 正式冻结。
- 未连接或运行 Dify/LLM，未计算 Agent 成功率。
- 未部署云服务、未开放公网、未安装新依赖、未调用付费 API。
- 未修改 F01–F08、Golden Outcome 或评测集数量。
- 未开发 ERP、OCR、自动比价、合同分析或大型前端。
