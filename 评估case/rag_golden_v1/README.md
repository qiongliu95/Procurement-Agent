# 独立 RAG Golden 候选集

版本：`rag-golden-1.0.0-candidate`，2026-10-14。

46条AUTO_VALIDATED用例；10条SKIPPED；0条NEEDS_DECISION。只完成数据构建及离线校验，未运行正式RAG评估、未替换旧42条用例、未同步Airtable。

## 文件

- `evaluation_cases.jsonl`：每行一个完整可用Golden Case。
- `evaluation_cases.csv`：相同信息的UTF-8 BOM版本，支持Excel查看；对象和数组单元格使用JSON，无删字段。
- `evaluation_schema.json`：JSON Schema Draft 2020-12。还须运行脚本检查跨字段与原文一致性。
- `skipped_cases.jsonl`：排除场景、原因、旧场景参考ID；未可靠形成Golden的场景不会伪造答案或证据。
- `provenance_manifest.json`：正式知识、旧数据、受保护业务源码及本版本产物的哈希基线。

总览位于`../../procurement_eval_v08/reports/rag_golden_v1/采购知识库_全量知识总览.md`。
覆盖报告、`needs_decision.md`、`validation_results.json`位于`../../procurement_eval_v08/evaluation/reports/rag_golden_v1/`。

## 原文证据契约

`golden_evidence`逐事实保存正式文档ID、标题、章节、原文、文件路径、SHA-256、行号和字符偏移。`role=fact`用于必要事实；`role=scope`用于保留适用边界，不能算作额外必要来源。

- 行号含front matter，从1开始，两端包含。
- 字符范围从0开始、右端不包含，基于原始UTF-8字节解码后的Unicode codepoint，保留CRLF/LF，不做空白正规化。不是字节偏移，也不是JavaScript UTF-16索引。
- `source_hash`是正式Markdown原始字节的SHA-256，与manifest的`reviewed_sha256`一致。
- `knowledge_unit_id`来自总览章节导航，不是RAG Chunk ID。
- A/B/MIXED表示来源类别，**不是强制性级别**。A类中的guide与B类中的专业建议仍须按原文适用范围理解。

Golden Answer采用完整原文段落和适用范围组成的参考答案；资料不足时附受限的不足说明。必要判断以`required_facts[].required_judgment`为准，不能要求模型逐字复述所有参考文本。专业建议允许同义表达和有据的不同方案，但必须保留必要条件、否定、例外和权威边界。

`AUTO_VALIDATED`不是人工核验，也不是RAG运行PASS。来源定位检查与抽取支持检查分别记录；没有用生成AI的自我评价充当正确性证明，也未声称进行独立语义模型复核。问题语义完备性及跨文档语义必要性仍有自动检查边界，详见覆盖报告。

## 离线复现

在`procurement_eval_v08`目录执行，使用Python 3.10及以上，仅需标准库：

```powershell
python -m evaluation.golden_dataset validate
python -m unittest evaluation.tests.test_golden_dataset -v

# 只有需要重新生成这个独立候选版本时才执行
python -m evaluation.golden_dataset build
```

`validate`验证交付数据、Schema、CSV往返、受保护文件、原文哈希和精确位置，并更新本版本校验结果及覆盖报告；不调用网络、Agent、Backend或RAG。
`build`从`evaluation/golden_blueprints.py`的证据先行输入重建本目录。若受保护来源相对基线已变化，会拒绝静默重置，不覆盖正式知识。

## 后续评测接入

现有`evaluation.case_loader.CaseLoader`严格接收五字段Agent Case及对应执行/Fixture文件，**不能直接载入本JSONL**。本轮未改Loader、Runner、Agent或Backend。

`checker_spec`给出各Case的原文覆盖、参数化Top-K必要证据召回、跨文档事实覆盖、正确性、忠实性、证据不足、只读路由与禁止写入要求，以及可复用的现有检查函数。其集成状态是`SPEC_ONLY_NOT_EXECUTED`。

后续执行前需完成：

1. 为实际Chunk建立经验证的固定原文哈希和区间映射。若无法映射，记录`EVIDENCE_MISSING`；命中同一文档不能当作命中全部事实。
2. 只把`question`发给被测系统，**Golden Answer、证据、禁止结论和检查规范不得注入被测提示词**。评分端单独保留Golden。
3. 固定语义评分rubric，允许合理同义答案；不把答案字符串相似度当正确性或忠实性证明。未确定的评分不自动算PASS。
4. Agent层复用只读Fixture及现有Trace/状态快照，检查本次新增事件，排除Fixture已有审计历史。RAG组件层不适用工具路由/写入检查。

本轮全部可用题均为静态知识咨询或明示的假设规则问题，没有请求真实业务记录。实时状态题在取得可靠、确定性的Fixture前保持SKIPPED。替换旧版和同步Airtable须后续确认，本目录不会自动执行。
