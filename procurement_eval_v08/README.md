# 采购业务模拟器与 AI 采购 Agent v1

## 2026-10-17 交付入口

当前版本按已知限制完成演示型 MVP 归档，不宣称生产交付或全量测试全绿。以 [最终交付说明](reports/final_delivery/PROJECT_HANDOVER.md) 查看功能状态、运行版本、人工验收清单和原始测试索引；正式需求主稿为 [当前 Markdown PRD](../采购Agent_PRD_v1.2_Agent首版开发定稿.md)。根目录 `PRD v1.2.docx` 是10月12日历史基线，未同步后续增量。

REQ-16 独立可编辑表单按已确认 DEC-10 延期；当前使用自然语言录入、修改与最终确认卡，不是“表单已实现”。M7基础性能与Token成本基线已完成，费用N/A，详 [性能报告](reports/agent_performance_cost_baseline.md)。最新页面的完整人工彩排仍待验收。

## 2026-10-16 最新创建规则 DEC-09 / REQ-17

员工只需提供商品、数量、用途，例如“创建2台显示器采购申请，用于市场部办公”。供应商和采购价由现有商品目录自动补齐，确认卡展示完整字段，员工确认后才创建并提交。商品名称先精确匹配，无精确项时允许唯一包含匹配；多候选必须由员工选择。

商品目录只保留一个“采购价”，现有参考价升级为该唯一采购价；未配置供应商的商品默认使用新锐科技。M02 可维护商品、供应商和预算，M01 对三类目录只读，U01 不可查看商品目录，Agent 保留商品只读权限。目录维护不会自动改写已落库申请；已有申请仍按原确认、权限、版本和审批规则修改。

升级须同时重载 Backend 和 Agent。已有会话不应跨代码升级继续确认；只有用户授权后才停服并先备份数据库。启动迁移只为缺失供应商的商品补新锐科技并补齐兼容价格字段，不覆盖已有人工供应商选择。归档核对发现现有服务已在2026-10-16 21:12重启，晚于最近Runtime源码修改；本轮未再次重启。进程时间、页面哈希及只读核对见交付证据。REQ-16表单未实现但已获准延期，采购用途和写前确认并未延期。

以下保留既有部署说明和历史五字段评估示例；其中自由填价/目录外商品的创建示例不再是新版验收命令，新建规则以本节为准。旧 Case、Golden 和历史 Evidence 未改写。

> 自动化评估框架位于 `evaluation/`；统一执行、EVAL-01 Evidence、EVAL-02 Strict Pass 评分及报告见 `evaluation/README.md`。

**性质：本地采购业务沙箱与单 Agent 演示型 MVP；不是 ERP 或生产服务。**

## 当前可验证内容

- HTTP API + SQLite 持久化：员工提交采购申请、确定性预算/供应商检查、人工角色审批、状态版本、唯一模拟 PO、预算成功扣减、幂等重试及可查询日志。
- 单页本地模拟器：查看申请、预算、供应商、PO 和商品目录，按 M01/M02 处理审批、报价凭证核验、新供应商准入及商品维护。
- 目录管理：M02 可维护商品、供应商和预算；M01 只读。U01 不可查看商品目录，Agent 可只读查询；商品采用受审计软删除，供应商和预算删除受引用保护。
- Agent 业务工具：草稿、已有 DRAFT 续办提交、有权查询、字段修改、供应商匹配、PO 真实结果查询，以及带来源的历史成交价和中文 RAG 采购知识咨询；不向 Agent 开放人工操作或 `/__eval__`。
- 中文 RAG：只从 `采购Agent_正式知识库_v1/manifest.json` 读取 52 篇 A/B 类审校文档，使用 `BAAI/bge-small-zh-v1.5` 的 512 维归一化向量和 NumPy 余弦排序；C 类不入索引。回答附文档/章节引用，证据不足时拒绝推测。
- 初始模拟供应商：启明办公→`S_ACTIVE`、风险供应商→`S_BLACK`、新锐科技→`S_NEW`；名称中的NEW不是当前状态，实际准入状态以Backend为准。初始预算种子为B_MKT 5000元、B_LOW 1000元、B_HIGH 20000元，不是当前余额；不要用README替代实际预算查询。Agent提交名称，Backend解析可信ID。
- F01–F08：预算默认从可信身份与部门映射，不接受普通请求覆盖预算字段；用途非空文本即可；预算只在首次 PO 创建成功时扣减；黑名单禁止；新供应商生成独立的准入待办。金额不超过一万元由 M01 审批，超过一万元先 M01、后 M02，M02 最终批准前须核验可信凭证；两级记录绑定申请当前版本，PO 放行复核完整审批链。`SUBMITTED` 为待 M01，`PENDING_HIGH_APPROVAL` 为 M01 已批、待 M02；`approval.current_approver` 为当前节点，`required_approver` 保留最终审批级别。

旧审批路由修复：`python scripts/repair_approval_routes.py --db demo_test.sqlite` 默认只预览。先停止旧 Backend，再加 `--apply`；脚本先在 `runtime_data/backups/` 创建 SQLite 备份，只将零审批、无最终批准标记、无 PO/幂等/扣款痕迹的旧高金额待审批申请回退到 M01，并写入修复审计。任何版本已有审批记录的申请不自动改动，旧单级批准不能绕过新的 PO 完整链门禁；需人工复核。重启 Agent 会清除内存会话，不删除采购业务记录。
- Checker 冻结候选：校验审计轨迹、申请 ID、版本、操作角色、审批与 PO、幂等键、预算扣减及 Agent Tool Trace 的跨系统关联。Agent Trace 缺失时返回 `NOT_RUN`/`EVIDENCE_MISSING`。

## 运行（Windows PowerShell，推荐 Python 3.11）

模拟器本身不需要第三方依赖或 Docker；Agent 需要安装 `requirements.txt` 中的模型 SDK。

```powershell
cd .\procurement_eval_v08
py -3.11 backend.py .\demo_test.sqlite
```

浏览器访问 `http://127.0.0.1:8765/`。页面预设身份为普通用户U01、首级审批M01、二级审批及主数据管理M02，服务只监听loopback。已有数据演示不运行 `reset_demo.py`；该脚本会重建演示库，只能另经明确授权用于数据重置。

## 启动 AI 采购 Agent v1

### Windows 一键启动（推荐）

首次使用：

1. 安装 Python 3.11，并确保安装时勾选了 Python Launcher（`py.exe`）。
2. 本机演示复用已有验收环境。新机安装入口为 `py -3.11 -m pip install -r requirements.txt`，但当前依赖声明与已验收安装版本不一致，新机安装尚未验收，不保证直接复现；本轮不安装、升级或改依赖清单。
3. 双击 `配置采购Agent模型.bat`，填写模型 Base URL、模型名称和 API Key。
4. 双击 `启动采购Agent.bat`。脚本会等待两个服务都就绪，再自动打开 Agent（`http://127.0.0.1:8780/`）和采购模拟器（`http://127.0.0.1:8765/`）。

千问可使用 Base URL `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型名例如 `qwen-plus`；DeepSeek 可使用 Base URL `https://api.deepseek.com`，模型名例如 `deepseek-chat`。应选择支持 OpenAI-compatible Chat Completions Tool Calling 的具体模型。

模型配置保存在当前 Windows 用户的 `%LOCALAPPDATA%\ProcurementAgent\model-config.json`。其中 API Key 由 Windows DPAPI 加密，只能由当前 Windows 用户解密；配置不写入 bat、源码或项目目录。换模型或更新 Key 时重新双击 `配置采购Agent模型.bat` 即可。

日常使用只需双击 `启动采购Agent.bat`。重复启动会复用已经就绪的服务，不会再创建一套进程。双击 `关闭采购Agent.bat` 可关闭由该启动器创建的后台服务；为防止误杀，它不会按端口强制关闭来源不明的进程。运行日志位于 `runtime_data\logs`。

Agent 启动前会预热检索模型，冷启动可能超过一分钟；启动器默认等待 180 秒，每 10 秒提示进度。`agent.out.log` 实时记录初始化阶段，`agent.err.log` 记录异常，`launcher.log` 追加保留启动器诊断。进程提前退出时会立即报错，不会空等到超时。如本机确实需要更长时间，可运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\procurement_agent_control.ps1 -Action Start -AgentStartupTimeoutSeconds 300`（上限 600 秒）。

启动器始终复用现有 `demo_test.sqlite`，不会调用 `reset_demo.py` 或清空数据库。若 Python 版本、依赖、模型配置、必要文件或端口存在问题，窗口会保留中文错误提示。

### 手动启动（排查时使用）

先在一个 PowerShell 窗口启动模拟器：

```powershell
cd .\procurement_eval_v08
py -3.11 backend.py .\demo_test.sqlite
```

再在第二个 PowerShell 窗口临时设置一个支持 OpenAI-compatible Tool Calling 的 Qwen 或 DeepSeek 模型，并启动 Agent：

```powershell
cd .\procurement_eval_v08
# 本机演示沿用已有依赖；新机安装须先核对交付说明中的版本差异。
$env:AGENT_MODEL_API_KEY = '<your-api-key>'
$env:AGENT_MODEL_BASE_URL = '<provider-compatible-base-url>'
$env:AGENT_MODEL_NAME = '<model-name>'
py -3.11 agent_server.py
```

浏览器访问 `http://127.0.0.1:8780/`。Agent 对话入口固定绑定 U01；M01/M02 仍只在模拟器页面完成审批、凭证和供应商准入操作。API Key 不写入 `.env.example` 或源码。

可配置默认值：

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `PROCUREMENT_API_BASE_URL` | `http://127.0.0.1:8765` | 模拟器地址 |
| `PROCUREMENT_AGENT_TOKEN` | `demo-agent` | 本地 Agent 服务凭据，不发送给浏览器或模型 |
| `AGENT_PORT` | `8780` | Agent 页面端口 |
| `AGENT_CONFIRM_TTL_SECONDS` | `600` | 确认有效期 |
| `AGENT_MAX_MODEL_STEPS` | `6` | 单轮模型/工具循环上限 |
| `AGENT_HTTP_TIMEOUT_SECONDS` | `10` | 模拟器调用超时 |
| `AGENT_WRITE_RETRY_LIMIT` | `1` | 具备可靠服务端排他证据时的最大安全重试次数 |
| `AGENT_CONVERSATION_TURNS` | `12` | 兼容旧配置；仅作为模型任务消息上限的外层约束 |
| `AGENT_MODEL_CONTEXT_MESSAGES` | `6` | 发给模型的任务相关历史消息上限；结构化任务状态不受此上限影响 |
| `AGENT_SESSION_HISTORY_MESSAGES` | `200` | 当前内存会话用于页面恢复的消息上限；服务重启后允许丢失 |
| `AGENT_KNOWLEDGE_DIR` | 项目同级的 `采购Agent_正式知识库_v1` | 已审校知识目录 |
| `AGENT_KNOWLEDGE_INDEX_DIR` | `runtime_data/knowledge_index` | NumPy 向量和切块元数据持久化目录 |
| `AGENT_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 必须输出 512 维向量 |
| `AGENT_RAG_CHUNK_CHARS` | `700` | 单块最大字符数 |
| `AGENT_RAG_CHUNK_OVERLAP` | `80` | 超长内容的重叠字符数 |
| `AGENT_RAG_MIN_SCORE` | `0.62` | 普通知识语义检索的默认证据门槛；明确核心制度按完整 A 类原文节判定依据 |
| `AGENT_RAG_WARMUP` | `1` | Agent 服务启动时预热中文编码器；就绪后首轮检索不再承担模型初始化耗时 |

### Agent 权限边界

模型可选择的只读工具：`match_supplier`、`list_requests`、`get_request`、`get_request_po`、`get_request_budget`、`search_purchase_history`、`search_products`、`search_knowledge`、兼容的 `get_procurement_rules`。模型另可使用无业务写入的 `propose_task_state` 提交意图、参数和澄清问题。

创建、已有草稿提交和修改只由Runtime在有效确认后执行。Agent只读目录采购价与供应商，采购价不是实时市场报价；历史成交价不能替代当前采购价。历史记录只来自已创建PO的可见申请，并标注申请、PO、成交日期，空物品条件最多返回最近20条。预算读取只沿有权申请返回当前余额/金额/缺口/时点，不提供总额或已用明细；不能据用户说“两万元”推算已用金额。制度、操作指南和通用知识通过 `search_knowledge` 检索审校语料，A类解释不能替代后端实时校验。后台维护、审批、凭证核验、供应商准入、PO、评测接口及身份参数不向模型开放。

### 会话与上下文

- 当前浏览器 Cookie 绑定单个内存会话；刷新或重新打开页面会从 `/api/session` 恢复聊天记录、有效任务和待确认卡。服务重启后允许丢失会话。
- 写任务被临时查询打断时只保留一个确定性任务快照。查询完成后必须选择“继续”或“不继续”，不会把查询结果写回采购参数。
- 模型输入由当前结构化任务、暂停任务、按对象类型隔离的最近可信只读结果、最近申请引用、当前候选、最多 6 条任务相关消息和当前输入组成；完整页面历史不会持续发送给模型。
- 模型分别提出“业务意图”和“对话关系”（新任务、追问、补充、修正、确认、取消、切换、暂停、恢复或无法判断），Runtime 校验后才更新任务状态。
- “列出我的全部采购申请和进度”使用 `query_scope=collection` 返回当前身份可见的申请列表；集合读取不要求唯一编号，也不选定可写对象。修改、提交仍须唯一申请及原有确认。
- 同类创建任务切换为新商品时清除上一笔价格、供应商和用途；只有新任务标签而没有新商品时保留原补参。临时咨询可暂停原采购，恢复后仍重新核验并确认。
- `correction` 可复用相同对象类型的可信证据；已完成的申请集合查询保留原查询范围和原话，纠错时重新读取最新列表。明确编号和显式新范围优先。
- “它/刚才那个/第二个”只可依据上述可信引用解析；历史成交、商品、知识和申请候选互不混用。修改或提交前仍重新读取业务对象，候选不唯一时必须澄清。
- 同一用户轮次内参数一致的知识检索只执行一次，缓存命中和分段耗时记录在 Trace；跨轮只复用查询条件与证据摘要，不把旧模型回答当作业务事实。
- 明确涉及审批金额、报价凭证核验、预算扣减的咨询，会从同一已审校索引补齐 KB-003/KB-004 的对应 A 类原文节；报价凭证同时补齐定义“高金额”的金额分界。`top_k` 指向量候选数，最终含补证最多 8 个片段，必要时裁剪末尾非核心候选。核心条款不齐时不以高向量分数替代原文依据；其他知识沿用原语义门槛。Trace 的 `core_rule_chunk_ids` 记录补证依据，不改变问题原话、规则内容或写入权限。
- 页面“清空会话”只重置内存聊天、任务、引用和确认，不删除 SQLite 采购记录或审计日志。
- 最新公共边界修复允许从已保存澄清对象绑定只读参数，参数省略不自动扩大为全目录；将创建所需目录读取与独立咨询区分，辅助读取后继续创建并重算缺失字段。最终回答按当前目标/对象/范围筛证据，不能仅因工具返回就标记成功。商品搜索召回与最终候选分开，回复和结果卡统一使用采购价。商品切换选择不是业务写入确认。
- 上述是当前生产路径的局部共享机制，不代表隔离TaskContract自动提交原型已接入，也不保证自由文本任意误判都能被确定性识别。

手工构建/强制更新索引：

```powershell
.\.venv\Scripts\python.exe .\build_knowledge_index.py --force
```

启动 Agent 时会校验正式 manifest、文档数量和 SHA-256；语料指纹变化时自动更新索引，哈希不一致则停止，不带病启动。

运行测试：

```powershell
cd .\procurement_eval_v08
py -3.11 -m unittest -v test_simulator_v1.py
py -3.11 -m unittest -v test_http_contract.py
py -3.11 -m unittest -v test_checker_evidence.py
py -3.11 -m unittest -v test_agent_runtime.py test_agent_http.py
py -3.11 -m unittest -v test_knowledge_rag.py
py -3.11 -m unittest -v test_multiturn_dialogue.py
py -3.11 -m unittest -v test_windows_launcher.py
```

当前专项沿用同一unittest入口，不新增测试框架：

```powershell
py -3.11 -m unittest -v test_runtime_boundaries
py -3.11 -m unittest -v test_catalog_delivery test_item_switch_confirmation test_request_budget
py -3.11 -m unittest -v test_two_stage_approval
```

`test_runtime_boundaries.load_tests`收录26条离线Case和3条显式开关真实Case；默认真实Case跳过。需要另行获准真实模型测试时使用原有 `RUN_BOUNDARY_REAL=1`、`BOUNDARY_REAL_ONLY=1`，并配置 `CATALOG_EVIDENCE_DIR` 留存原始模型输入与Trace。当前收尾不再次执行。目录兼容回归通过不表示旧裸入口全绿：最近组合164 PASS/9 SKIP；旧89项入口48 PASS/37 FAIL/1 ERROR/3 SKIP；知识聚合3 PASS/3 FAIL/1 SKIP。历史失败和跳过的适用边界详交付说明，勿为了得到PASS运行重置演示库或放宽断言。

真实模型 Gate 包含申请查询和历史成交连续追问，必须显式开启；没有密钥时测试结果为 `NOT_RUN`。需要留存 JSONL 证据时可额外设置 `REAL_MODEL_TRACE_DIR`：

```powershell
$env:RUN_REAL_MODEL_TEST = '1'
$env:REAL_MODEL_TRACE_DIR = 'runtime_data/traces/real-model-validation'
py -3.11 -m unittest -v test_agent_real_model.py
```

`reset_demo.py` 只会重建同目录下固定命名的 `demo_test.sqlite`，不会接受任意数据库路径。测试脚本使用临时隔离 SQLite，不修改演示库。用 Ctrl+C 退出服务。

## 运行性质与历史证据

本节下列20/20、12/12及Dify NOT_RUN均是早期接口/评测候选记录，不是当前Agent完整验收状态，也不是新两级审批的当前通过率。当前交付以顶部交付说明及各专项原始证据为入口，保留此处历史结果以免混淆统计口径。

普通开发、分析、审计及开发回归结果存放在 `reports/`；HTTP 契约测试结果输出为 `reports/backend_contract_results.json`。Agent 自动化评估报告、评分汇总和指标存放在 `evaluation/reports/`，原始 Trace、Evidence、运行状态和复评记录保留在 `evaluation/runs/`。一次性开发总结默认只在最终回复中呈现；历史报告与证据不因输出路径调整而重新生成或迁移。

`test_http_contract.py` 在本地启动真实 HTTP server、发送真实 POST/GET、查询 SQLite；因此**接口和确定性业务规则有运行证据**。但测试脚本代替了 AI 客户端发请求，故**不得把业务流程的 PASS 当作 Dify/LLM Agent 成功率**。

- 本地 HTTP/SQLite 测试：20/20 通过。
- Checker 合成正反例单元测试：12/12 通过，覆盖 PO ID、审批角色/版本、幂等键、事件顺序、缺 DB 证据、虚假成功声明、Agent↔后端强关联和 C02 可信人工标注。
- API-only Cases：C03/C04/C05/C08/C09 的真实本地 HTTP/SQLite Golden 判定通过。
- Agent Cases：C01/C02/C06/C07/C10 的后端路径按各自范围验证，但完整 Checker 均为 `NOT_RUN`，因为没有真实 Dify Trace。
- `cases_v1.0_candidate.json` 是统一冻结候选，**不是正式冻结 v1.0**；没有评估模型质量与准确率。

## API 契约（本地演示）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/requests` | 申请草稿；接收 `supplier_name`，可信主数据解析供应商 ID，身份带出部门和预算 |
| GET | `/requests` | 查询当前身份有权查看的申请列表 |
| GET | `/requests/{id}` | 面向有权主体的最小只读业务状态；不返回评测快照、预算余额或审计全量 |
| POST | `/requests/{id}/submit` | 必填、供应商和预算校验、审批路由；Agent 必须提交 `expected_version` |
| PATCH/POST | `/requests/{id}/update` | 修改 `supplier_name/item/quantity/unit_price/purpose`；Agent 必须提交 `expected_version`，实际变化产生新版本并使旧审批失效 |
| GET | `/suppliers/match?name=...` | 按名称匹配可信供应商主数据 |
| GET | `/products?query=...&status=active|inactive|all` | Agent、M01、M02 查询商品目录；U01 禁止访问 |
| GET | `/products/{code}` | 查询单个商品详情 |
| POST | `/admin/products` | 仅 M02 新增商品；默认供应商为新锐科技 |
| PATCH/DELETE | `/admin/products/{code}` | 仅 M02 编辑、启停或软删除商品；商品编码不可修改 |
| POST | `/admin/suppliers` | 仅 M02 新增供应商；初始状态为 `new` |
| PATCH/DELETE | `/admin/suppliers/{id}` | 仅 M02 修改或受保护删除供应商 |
| POST | `/admin/budgets` | 仅 M02 新增预算 |
| PATCH/DELETE | `/admin/budgets/{id}` | 仅 M02 修改或受保护删除预算；`B_MKT` 不可删除 |
| GET | `/requests/{id}/po` | 查询该申请是否已创建 PO 及真实结果 |
| GET | `/requests/{id}/budget` | 沿有权申请读取关联部门当前预算余额、申请金额、缺口和时点；不返回预算总额/已用明细 |
| GET | `/history/prices?item=...` | 查询当前身份可见、已创建 PO 的历史成交记录和可追溯价格来源 |
| POST | `/requests/{id}/approve` | 真实演示角色 M01/M02 审批，忽略请求体伪造 actor |
| POST | `/requests/{id}/evidence` | 人工登记报价凭证可信标记，Agent 不可调用 |
| POST | `/requests/{id}/change_amount` | 修改金额触发版本失效 |
| POST | `/requests/{id}/po` | 仅已有效审批时创建 PO；幂等键在 body 中 |
| GET | `/lookup/po?key=...` | 按幂等键查 PO |
| GET | `/admin/overview` | 管理员读取页面所需的真实预算、供应商、申请、准入待办和 PO 状态 |
| POST | `/admin/vendor-reviews/{id}/decision` | 管理员处理供应商准入；`decision` 为 `APPROVED` 或 `REJECTED` |
| GET | `/__eval__/{id}` | 仅本机评测角色可读的数据库状态与审计快照 |
| POST | `/__eval__/seed_budget` | 仅本机评测身份，设置合成 Fixture 的预算科目 |

测试凭据仅用于本机：`demo-u01`、`demo-m01`、`demo-m02`、`demo-agent`、`demo-eval`，通过 `Authorization: Bearer ...` 传入。

⚠️ **安全声明**：角色令牌硬编码且公开，接口仅绑定 loopback 地址，未实现企业级认证、租户隔离、访问控制、日志签名或并发高压保障。请勿通过端口转发、内网穿透或公网部署暴露此原型，也不要接入真实公司数据。

## 证据可信度与剩余工作

- 评分从 evaluator-only DB 快照、实际 HTTP 捕获和真实 Dify Trace 构建，不接受 Agent 自行组织的“已经成功”JSON。
- 后端与 Checker 已实现 `run_id`、`tool_call_id` 等契约字段及关联校验；真实 Dify Trace 尚未产生，因此跨系统链路仍未实测。
- 详细字段所有权、关联算法和缺证状态见 `trace_contract.md`。各系统本地序号不会被直接拼成统一时间线。
- D01–D05 已映射到代码/候选规格；仍需在锁定版本 Dify 上验证真实 Trace 和安全 Proxy 关联。冻结的正确时间是**实际模型测试之前**，不是等成功后再修改 Golden。
- 旧 Dify 冻结材料继续作为历史证据保留，但本版 Agent 不依赖 Dify；正式模型验收以 `test_agent_real_model.py` 和 Runtime JSONL Trace 为准。

## Agent v1 当前验收状态

- 原模拟器、HTTP 契约、Checker、Agent Runtime 与页面边界合并回归通过；详见 `AGENT_V1_TEST_REPORT.md`。
- 脚本模型测试只证明 Runtime、真实 loopback API、确认门禁和异常策略，不代表真实 LLM 成功率。
- 真实模型 Gate 默认不读取或解密本机启动器配置，未显式注入模型环境变量时为 `NOT_RUN`；只有实际执行并通过 `test_agent_real_model.py` 的运行才可作为该次 Agent E2E 证据。
- Agent Trace 默认写入 `runtime_data/traces/YYYY-MM-DD.jsonl`，该目录已排除版本控制。
