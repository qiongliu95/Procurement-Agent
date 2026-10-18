# 采购 Agent 自动化评估框架

本目录是 `procurement_eval_v4` 的执行层。正式 Case、执行脚本和 Fixture 规格保持在项目同级的 `procurement_eval_v4/`，运行证据写入 `evaluation/runs/`。

## 当前范围

### 四模块统一入口（现行120条）

现行执行范围：Agent33、Backend20、IR21、RAG v1.2.1已验收候选46，共120条。
旧RAG42、其他Golden版本及Legacy10不重复计入；历史116条规划/Manifest不代表现行运行清单。
该入口是现有Runner、Checker、IR专项脚本和RAG Baseline的统一调度器，不替换各模块成功标准。

IR 独立脚本与统一入口均配置现有知识索引；IR03 的必需工具为当前模型使用的 `search_knowledge`。意图评分以原始模型提案为依据，缺少该证据时标记 `EVIDENCE_MISSING`，不归为模型误判，也不计入意图指标的确定结论分母；已观察到的路由或安全失败仍保留为 FAIL。历史报告和原始证据不自动重评或覆盖。

在项目目录配置 `.env` 的 `AGENT_MODEL_NAME`、`AGENT_MODEL_BASE_URL`、`AGENT_MODEL_API_KEY`，
安装 `requirements.txt`，保留现有正式Case、知识库及已构建索引和本地Embedding权重，然后执行一条命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evaluation full
```

默认全部重新运行（包括RAG生成和DeepEval Judge），可能产生模型费用。
使用已核验的RAG独立基线作为缓存、其他74条本批重新执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m evaluation full --reuse-evidence
```

缓存仅限当前已保存的 `rag_baseline_v1_2_1_20261014`；校验Golden/知识库/索引/Embedding权重、
模型配置、评分源码、完整历史原始文件、Evidence关联和确定性检查，任何不一致不复用。
报告逐条区分本批执行和历史复用，绝不把独立RAG报告冒充全量评测。

```powershell
# 只读完整执行清单；--case为局部验证，不是全量评测。
.\.venv\Scripts\python.exe -X utf8 -m evaluation full --list
.\.venv\Scripts\python.exe -X utf8 -m evaluation full --case backend:C01
# 断点恢复；批次ID见运行输出或evaluation/reports/latest_unified_manifest.json。
.\.venv\Scripts\python.exe -X utf8 -m evaluation full --resume <batch_id> --reuse-evidence
```

恢复时有效PASS、FAIL、REVIEW_REQUIRED均保留，不为提升得分重试FAIL；只重试异常、缺证或哈希校验失败项。
冻结文件/模型配置/依赖/执行源码变化则拒绝混用同批结果，需新建批次；旧尝试不会覆盖。
不能同时对同一批次运行多个进程。正常评分FAIL不导致批次异常退出；执行异常/未完成或冻结变化返回2。
外部服务故障需恢复后重试；不修改被测系统。无需Codex、GUI或Airtable。

产物：`evaluation/runs/<batch_id>/`保存state、冻结配置、完整清单、逐Case原始记录和评分；
`evaluation/reports/<batch_id>/`保存report.md、bad_cases.md、metrics.json、case_results.jsonl及模块报告。
RAG缓存保留原目录及哈希引用，不复制/改写旧证据。Agent/Backend/IR各Case均用独立SQLite；
Backend审批/PO只操作本批隔离测试数据；RAG阻断业务API并使用冻结只读索引。
各模块分开统计，同时列PASS/确定结论、PASS/计划及非二元状态，不计算四模块通过率平均分。


- 正式 v4：支持 33 条 Agent Case 与 20 条 Backend Case 的加载、Fixture 和执行脚本。
- Agent：复用真实 `AgentRuntime`、Tool Calling、确认状态机和模型适配器；同一 Case 保持 Session 连续。
- Backend：以真实 `backend.Store` 驱动角色权限、审批、准入、预算、版本冲突、幂等与故障注入。
- Evidence：保存步骤、路由、确认、工具轨迹、审计事件、SQLite 前后快照和状态差异。
- Checker：逐项 Success Criteria、Strict Pass、人工复核、Evidence 哈希与离线重复评分。
- Batch / Reporter：Agent、Backend 或全集批跑；计算 TSR、BCR、AVR、覆盖率、Backend 通过率和 Bad Case。

## 运行

在 `procurement_eval_v08` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m evaluation --case E01 --case E02B --case A02_BC03
```

未指定 `--case` 时保留 EVAL-01 三条兼容默认值。命令默认读取项目 `.env`，但不覆盖已存在的环境变量；也可以先设置模型环境变量并使用 `--no-env-file`。列出所有正式 v4 Agent Case：

```powershell
.\.venv\Scripts\python.exe -m evaluation --list
```

## 单次运行产物

每个 Run 使用独立目录 `evaluation/runs/<run_id>/`：

- `manifest.json`：Case、Run、Session、模型和执行状态摘要
- `evidence.json`：供 EVAL-02 Checker 使用的完整结构化证据
- `raw.jsonl`：步骤、Runtime Trace、后端审计和业务状态差异的追加式记录
- `trace.jsonl`、`traces/*.jsonl`：统一及原始 Runtime Trace
- `before_state.json`、`after_state.json`：SQLite 业务状态快照
- `fixture.sqlite`：该 Case 的最终隔离数据库
- `errors.json`：仅在出现异常时生成

`A02_BC03` 的 `cancel_latest_confirmation` 由 Harness 直接调用 Runtime 的确认 API；若 Agent 没有产生待确认操作，步骤会记录 `NO_PENDING_CONFIRMATION` 并继续后续用户消息，供 Checker 判断，而不会伪造取消事件。

## 离线 Strict Pass 评分

按已有 Run ID 重复评分，不会重新调用模型：

```powershell
.\.venv\Scripts\python.exe -m evaluation score `
  --run-id E01-20261013T234731-b81f0545 `
  --run-id E02B-20261013T234738-798b08c7 `
  --run-id A02_BC03-20261013T234802-36bf07d4
```

也可以直接指定一个或多个 Evidence 文件：

```powershell
.\.venv\Scripts\python.exe -m evaluation score --evidence .\evaluation\runs\<run-id>\evidence.json
```

评分会向 Run 目录的 `scored.jsonl` 追加一条带 Evidence SHA-256 的记录，并在 `evaluation/reports/<run-id>/score_report.md` 输出单次评分报告。合并评分报告默认输出到 `evaluation/reports/score_report.md`，可通过 `--report` 显式指定位置。需要语义复核时仍在 Run 目录生成 `manual_review.template.json`。复制并填写模板后，可通过 `--review-file <path>` 复评；人工结论必须绑定 Case、Run、Evidence 哈希和有效 JSON Pointer，且不能覆盖确定性 Checker 结果。既有历史报告保持原位。

Strict Pass 聚合顺序：硬性违规或任一必需条件 FAIL → `FAIL`；否则缺证 → `EVIDENCE_MISSING`；否则存在未处理人工项 → `REVIEW_REQUIRED`；全部条件 PASS 且无硬性违规 → `PASS`。Case 或 Success Criteria 与正式 v4 不一致时为 `INVALID_CASE`。

## 批量执行与指标报告

```powershell
# 真实模型 Agent Suite
.\.venv\Scripts\python.exe -m evaluation batch --suite agent

# 确定性 Backend Suite
.\.venv\Scripts\python.exe -m evaluation batch --suite backend

# 一次执行全集
.\.venv\Scripts\python.exe -m evaluation batch --suite all
```

已有 Evidence 可以不调用模型直接重评分，并合并两个历史批次：

```powershell
.\.venv\Scripts\python.exe -m evaluation batch `
  --batch-manifest .\evaluation\reports\<agent-batch>\manifest.json `
  --batch-manifest .\evaluation\reports\<backend-batch>\manifest.json
```

每个批次写入 `evaluation/reports/<batch-id>/`，包含 `manifest.json`、`scored.jsonl`、`metrics.json` 和 `report.md`。同一 Run 在一份报告中只保留最后一次评分；待复核和缺证状态不混入 PASS/FAIL 比率分母。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s evaluation\tests -v
```

自动化测试使用脚本模型，但调用真实 Agent Runtime、loopback 业务 API 和 SQLite。正式命令默认使用实际配置的 OpenAI-compatible 模型。

EVAL-03 的最新完整实跑结果见 `evaluation/reports/latest_report.md`，对应指标为 `latest_metrics.json`。

## v1.2.1 Golden RAG首次Baseline

独立入口复用真实Runtime、编码器/索引、EvidenceCollector与Trace，不经旧五字段CaseLoader，也不替换旧评测数据：

```powershell
.\.venv\Scripts\python.exe -m evaluation.rag_baseline freeze
.\.venv\Scripts\python.exe -m evaluation.rag_baseline pilot
.\.venv\Scripts\python.exe -m evaluation.rag_baseline run
.\.venv\Scripts\python.exe -m evaluation.rag_baseline_scoring pilot
.\.venv\Scripts\python.exe -m evaluation.rag_baseline_scoring all
.\.venv\Scripts\python.exe -m evaluation.rag_baseline_scoring format-repairs
.\.venv\Scripts\python.exe -m evaluation.rag_baseline_reporting
```

原始执行位于`evaluation/runs/rag_baseline_v1_2_1_20261014/`；报告、指标、逐Case CSV及Bad Case位于`evaluation/reports/rag_baseline_v1_2_1_20261014/`。首次基线的定向证据对齐、元数据及召回范围复核均已保存在该目录；汇总命令不会发起模型请求。已有执行和相同Judge Prompt读取缓存，不重复运行；禁止用同一目录覆盖不同配置。

业务API全部由评测护栏阻断，仅运行知识读取；索引缺失或指纹变化直接停止，不自动重建。当前工具允许模型选择K，默认4并可选1～8，指标必须报告实际K，不能标成统一Recall@4。规范证据按原文区间覆盖，另列同义事实召回估计；不能用文档命中代替事实召回。REQUIRED/OPTIONAL按v1.2.1合同评分，指标矛盾保留待核实；本轮不自动调参或修改Golden。
