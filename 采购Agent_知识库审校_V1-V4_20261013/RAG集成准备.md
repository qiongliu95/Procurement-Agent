# 中文 Embedding 与 RAG 集成准备

## 1. 当前真实实现

- `procurement_eval_v08/agent_tools.py` 已有 `get_procurement_rules`，但它只读取 `procurement_rules_v1.json` 并做本地关键词匹配。
- `search_purchase_history` 调用 `/history/prices`；`search_products` 调用 `/products`。历史价格、商品价格/状态、申请状态、供应商状态必须继续由 API 提供，不进知识索引。
- `requirements.txt` 当前只有 `openai>=1.51,<2`。代码中未找到中文 Embedding 模型、切块、向量存储、索引 manifest、Retriever 或 RAG 引用组装。
- 因此，“现有中文 Embedding/RAG”的真实结论是：**尚未实现**。本轮不得把拟议方案记为已具备能力。

## 2. 索引构建的最小改动（后续轮次）

1. 以 `采购Agent_正式知识库_v1/manifest.json` 为唯一输入清单；只读 `index_allowed=true` 的 A/B 文档，不扫描候选集和 C 类。
2. 新增一个独立的索引构建脚本，按 Markdown 标题切块；每块保留 `doc_id/title/heading/class/source_url/version/reviewed_sha256`。不切断条款和表格，超长段落才按可配置长度再切。
3. 中文本地基线可先验证 `BAAI/bge-small-zh-v1.5`：其官方模型卡标注 Chinese、MIT，并提供 Sentence Transformers 和短查询检索指令用法。该选型只是待实测基线，未安装、未下载、未验收。模型卡：https://huggingface.co/BAAI/bge-small-zh-v1.5
4. 52 篇规模下先用 `sentence-transformers + numpy` 的归一化向量和余弦排序，不引入向量数据库或 RAG 框架。索引输出用一个 `.npz` 向量文件加一个 JSON 元数据文件，并记录模型名、模型修订、语料 manifest 哈希、切块参数和构建时间。
5. 构建时硬校验：正式文档数为 52、A=11、B=41，每篇哈希与 manifest 一致，不包含 `review_class=C`。任一失败则中止构建。

## 3. Agent 接入的最小改动（后续轮次）

1. 在 `agent_tools.py` 新增一个只读 `search_knowledge` 适配器与 Tool Schema，参数只包含用户查询和可选的 A/B 范围；模型不能提供索引路径、模型名或任意过滤表达式。
2. Retriever 只返回结构化的 `doc_id/title/heading/class/source_url/snippet/score`；A 类和 B 类在模型上下文中显式区分，防止通用建议被表述为企业制度。
3. Runtime 仍保留路由与安全边界：知识工具只能 READ，不能创建确认、触发 ACT、填充实时价格或推断结构化状态。
4. 规则问题的优先级：正式 A 类文档/现有 `procurement_rules_v1.json` 高于 B 类通用建议；若两者不一致，不合并答案，而是返回冲突并停止。
5. 回答展示至少包含文档标题、章节、分类和来源；检索分数仅用于排序，不得当作“事实置信度”。无有效片段时明确说资料不足。

## 4. 定向验收建议

- A 类边界：9999.99/10000、预算扣减时点、黑名单/新供应商、Agent 不审批/不创建 PO。
- B 类边界：选型、TCO、SLA、合同和风险回答必须标明“通用建议”和外部来源。
- 反例：当前 PR 状态、商品价格、历史成交价、供应商状态必须调 API；禁止从知识片段回答。
- 证据：每次检索的 query、top-k、命中 chunk ID、语料/模型版本和最终引用记入 Trace。

## 5. 本轮决策

**先不重建索引。** 原因是当前不存在可重建的向量索引，且本轮需先固化 52 篇放行语料和 13 篇 C 类隔离结果。索引构建应在用户确认本次审校结果后单独执行。

