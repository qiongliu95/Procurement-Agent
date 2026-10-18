# 采购 Agent 评测集 v4：按职责分层

## 核心修订

- `agent_cases.json`：33 条 Agent 测试。每条严格仅含 `case_id`、`category`、`task`、`initial_state`、`success_criteria`。
- `backend_cases.json`：20 条后端业务规则测试。**不计入 Agent 任务完成率**，单独计算业务规则通过率；其中 `task` 描述业务目标而不是假造 Agent 用户消息。
- `execution_scenarios.json`：Harness 操作步骤、用户消息时序、确认取消、模拟器操作、故障注入等，与 Case 的任务定义分离。
- `fixture_specs.json`：每个初始环境引用的需求规格，不等于真实数据库快照；所有条目 `SPEC_ONLY_NOT_PROVISIONED`。
- `case_index.json`：原始名称、分类、历史 Checker 列表，**只用于溯源/审核**，不传给 Agent 和评分器作为答案。

## Agent 单条 Test Case

```json
{
  "case_id": "A02_BC03",
  "category": "E",
  "task": [
    "采购2台电脑，单价3500元，供应商启明办公，用于新员工办公",
    "我需要采购2台办公电脑"
  ],
  "initial_state": "fx_A02_BC03",
  "success_criteria": [
    "后一笔新的采购任务必须独立于先前任务",
    "后一笔未提供单价、供应商、用途时应补问",
    "不得使用前一笔数据填充缺失字段或发起写入确认"
  ]
}
```

`task` 只保存用户输入的有序序列；**中间的取消确认**等操作由 `execution_scenarios.json` 插在两句话之间执行。Runner 必须在一个 Session 内跑完整条用例。

## 执行路径与信息来源

1. **Fixture Builder**：为 Case 准备独立 SQLite 快照，校验供应商、申请、审批、预算、当前身份等前置条件。Fixture 需求在 `fixture_specs.json`；未完成准备的 Case 标记 `NOT_READY`，不能当作测试失败。
2. **Harness**：读取 Case ID 并使用 `execution_scenarios.json` 控制多轮 Agent 用户消息、确认/取消动作、后台管理员操作、超时注入。每条 Case 自己一个会话；同一 Case 的所有轮次必须共用 Session。Harness 不得给 Agent 注入成功标准或 Checker 的预期答案。
3. **Agent API + Audit Log**：采集 `session_id`、`run_id`、`USER_MESSAGE`、`ROUTE_DECIDED`、`CONFIRMATION_*`、`TOOL_CALL_*`、`FINAL_RESPONSE`。日志只有经过 Run/Session 匹配后才属于对应 Case。
4. **Business API / SQLite**：通过独立只读校验接口获取申请状态、供应商、审批、PO、预算前后值；后台数据优先于 Agent 自述。必须保证数据来自同一个隔离数据库实例。
5. **Evidence Adapter**：按 Case ID 输出原始 evidence，不篡改模型输出或工具结果。证据不足应给 `EVIDENCE_MISSING`，而不是自动判 FAIL 或 PASS。
6. **Checker**：将 `success_criteria` 映射为可核验断言。Route、金额、数据库终态、PO 数量、预算 delta、授权事件用确定性检查；“解释原因”这类语义标准先人工复核，后续可加 Judge。不得让 Judge 虚构未见过的工具事件。
7. **报告**：每条 Case 保存 `raw.jsonl` 与独立 `scored.jsonl`；统计 Agent 任务通过率和后端业务规则通过率两个分母，并展示 `PASS / FAIL / NOT_RUN / EVIDENCE_MISSING / INVALID_CASE`。

## 需要在本地实施前确认

- v4 只完成**规范和格式重构**；不是已经实现的 Fixture Builder、Harness 或 Checker。
- 原始用例存在 `complete_or_cancel_task` 不确定步骤；本版在 `A02_BC03` / `C04_BC06` 固定为 `cancel_latest_confirmation`，仍需复核该执行路径是否符合测试目的。
- 若按初始 PRD，零单价是否可接受需再核对后端 API：保留 B-06，但不能凭候选 Golden 强行认定系统实现与规格一致。
- 部分 Case 需要专门的版本冲突、网络故障、权限隔离 Fixture，不能直接复用当前业务演示数据库。
- 历史 92 个 Checker 名称未迁移进 Case，保留在索引供核查；后期应收敛为少量通用可复用的 Checker。
