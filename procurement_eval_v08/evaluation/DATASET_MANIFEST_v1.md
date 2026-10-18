# 采购 Agent Dataset Manifest v1.0

- 清单编号：`DATASET_MANIFEST_v1`
- 扫描日期：2026-10-14（Asia/Shanghai）
- 对照方案：[EVALUATION_PLAN_v1.md](EVALUATION_PLAN_v1.md)
- 机器可读清单：[DATASET_MANIFEST_v1.json](DATASET_MANIFEST_v1.json)
- 总体状态：`REVIEW_PENDING_NOT_FROZEN`

本清单记录现有 Dataset 的来源、对应关系与完整性。完整性状态不是 Agent PASS/FAIL，不构成数据集冻结批准。本次只读取本地文件、已有证据及 Airtable「测试 Case」；所有标记仅写入清单，未修改原 Case、评分逻辑、运行记录或 Airtable。

## 1. 数量与来源核对

| 模块 | Plan数量 | 本地条目 | Airtable对应 | 完整性状态 |
|---|---:|---:|---:|---|
| Intent | 21 | 21 | 21 | PARTIAL_METADATA_REVIEW_PENDING |
| Agent | 33 | 33 | 33 | STRUCTURE_COMPLETE_REVIEW_PENDING |
| Backend | 20 | 20 | 20 | STRUCTURE_COMPLETE_REVIEW_PENDING |
| RAG | 42 | 42 | 42 | 36 GOLD_REVIEW_PENDING；6 ATOMIC_GOLD_REVIEW_PENDING |
| 规划合计 | 116 | 116 | 116 | 待审核冻结 |
| 历史条目（不计入116） | — | 10条历史Case的v0.8/v1.0两种版本 | 10 | LEGACY_EXCLUDED |

- Airtable实际读取126条：116条规划Case + 10条LEGACY；两页100+26条，已读到分页结束，Case ID无重复，12个现有字段均非空。
- 本地四模块ID唯一，规划Case无缺号、无额外并入项；v4 Scenario/Fixture/Index关联无缺失、无孤立项。
- v4 53条：五字段结构、用户消息与Scenario关联、成功条件、Fixture关联均完整；Airtable成功条件、步骤与Fixture JSON精确一致，任务文字均对应。
- RAG42：JSONL/CSV的ID、分类、问题、路由、答案要点、候选文档与Gold状态一致；包内6个文件SHA-256与package_manifest一致。
- Airtable RAG42的问题、路由、答案摘要、候选/必需文档ID与本地一致；它是管理摘要，未宣称包含JSONL的全部grader字段。
- Intent21：Airtable逐轮用户输入与本地一致；预期语义描述与脚本枚举/允许路径的映射仍需审核。

### 扫描定位

| 来源 | 定位 | 版本/用途 |
|---|---|---|
| Plan | `evaluation/EVALUATION_PLAN_v1.md` | v1.0；本次完整性依据 |
| Agent/Backend | `../评估case/procurement_eval_v4/` | 4.0；6个正式规格文件 |
| Intent | `intent_routing_validation.py` 的 `CASES` | IR01–IR21；无独立Dataset版本声明，按源文件哈希定位 |
| RAG | `../评估case/下载采购 Agent RAG 42 Case 统一评估适配版 v2/` | package 2.0 / schema 2.0-adapter |
| 历史本地Case | `cases_v0.8_review.json`、`cases_v1.0_candidate.json` | 10条C01–C10的旧版本系列，不合并进正式v4 |
| Airtable | [采购 Agent 项目管理 / 测试 Case](https://airtable.com/appevjN2bokd3HHLb/tblGDjcm4yi85uBf8) | Base `appevjN2bokd3HHLb`，Table `tblGDjcm4yi85uBf8` |
| 正式知识库 | `../采购Agent_正式知识库_v1/manifest.json` | 52篇；仅核验文档ID、放行标志、文件存在及reviewed_sha256 |
| 运行索引 | `runtime_data/knowledge_index/index_meta.json` | 52篇、364 chunks、BAAI/bge-small-zh-v1.5；未运行检索或重新构建索引 |

JSON中保存来源文件绝对路径及完整SHA-256、逐Case定位、Airtable record ID、原规格状态、原执行状态、检查状态与问题编号。对于Airtable，保留本次读取时的关键状态快照；createdTime不能作为最后修改时间或正式版本。

## 2. 完整性状态与检查边界

| 状态 | 条目数 | 含义 |
|---|---:|---|
| STRUCTURE_COMPLETE_REVIEW_PENDING | 53 | v4结构及关联完整，有现存运行证据；仍需冻结审批、标准与Fixture语义审核 |
| PARTIAL_METADATA_REVIEW_PENDING | 21 | Intent输入及预期检查存在，但版本、分类、Fixture与契约尚未规范化 |
| GOLD_REVIEW_PENDING | 36 | RAG结构与候选来源存在，标准答案对正式知识的语义核定未完成 |
| ATOMIC_GOLD_REVIEW_PENDING | 6 | RAG跨文档合并要点尚需细分与审核 |
| LEGACY_EXCLUDED | 10 | 历史规格留存，不进入116分母 |

未核实到满足本方案全部冻结要求的批准记录，因此本清单未把任何Case标记为FREEZE_READY。此结论表示本次审核依据尚未齐备，不推断外部不存在审批。

检查维度对应Plan §3–§6：身份/ID、模块及分类、版本、用户输入、上下文、初始环境、权限、知识环境、成功条件、禁止行为、Evidence契约、Checker映射、Airtable一致性与冻结审核。

- `PRESENT`：非空资料或明确引用存在，不等于规则正确或语义已验收。
- `PARTIAL`：资料存在，但缺少Plan要求的元数据、实例绑定或评分映射。
- `NOT_VERIFIED`：本次未核实，不能推断通过。
- `MATCH`：限定比较字段一致；不同批次的执行结果不要求相同。
- `NOT_APPLICABLE`：按Suite范围不适用，须有边界说明。
- 现存运行包的完整性通过文件、Case定义和评分哈希链接核对；没有重新运行Fixture、Agent、Backend或Checker，也没有进行PRD逐条规则审计。

## 3. 对照Plan的维度结果

| 维度 | Intent21 | Agent33 / Backend20 | RAG42 |
|---|---|---|---|
| ID/模块/分类 | ID齐全；模块由来源判断，显式能力分类待补 | ID、category、suite及Index齐全 | ID、scenario_type、evaluation_layers齐全 |
| 版本 | 无独立Dataset版本，源文件SHA定位；PRD v1.2非Dataset版本 | 容器version=4.0，沿用五字段，不额外插入Case字段 | package=2.0 / schema=2.0-adapter；正式采用待确认 |
| 输入/上下文 | turns与允许intents/routes齐全；Session策略在脚本 | task与Scenario关联；同Case Session/独立DB约定存在 | user_input、conversation_history存在 |
| 初始业务环境 | 共享seed_requests规格，未逐Case规范绑定 | 53个Fixture规格齐全，最新运行包有前后状态；特殊实现边界待审 | use_existing_read_only_fixture泛引用，未实例化执行 |
| 身份/权限 | 可信U01在脚本及Airtable描述 | Agent为U01；Backend主体按动作执行，部分规格只写as_defined_per_step | demo-u01；API/确认场景对象及版本待绑定 |
| 知识环境 | 如使用规则/知识，版本引用需核对 | 业务Suite不等同于RAG能力评估 | 引用30个不同文档ID均有效；52篇正文哈希匹配，Gold语义待审 |
| 成功条件 | 输入有允许集合/必需工具，语义别名与条件路径待审 | success_criteria齐全，最新逐Case评分项数对应；2条Agent有人工复核项 | required_points等字段齐全，36待知识核定、6待细化 |
| 禁止行为 | no-write/确认约束来自脚本与Airtable | 条件及全局业务约束有引用，硬性违规逐Case覆盖需冻结审核 | process.forbidden_behaviors / forbidden_claims / hard_fail_flags存在 |
| Evidence | 历史专项证据存在；不是统一eval-evidence契约 | 53条现存包完整且定义匹配，评分链接Evidence哈希 | required_trace_fields已声明，Case源仍NOT_RUN/NOT_SCORED |
| Checker/指标 | 专项评分脚本存在；标准化适用映射待审 | 现存评分映射存在；CCR/TCA/SVR映射需确认 | grader_plan存在，当前五字段Loader适配与Strict Pass映射未验证 |
| 冻结审核 | 未核实统一批准 | 文件标称正式4.0，116整体审核仍未核实 | 草案Gold待审，不具备直接冻结依据 |

## 4. 待核对事项

| 编号 | 模块/Case | 状态 | 观察与需要确认的内容 |
|---|---|---|---|
| M01 | All | NOT_VERIFIED | 116条规划Case数量与来源齐全，但本次范围内没有可验证的四模块统一冻结记录、审核结论与生效日期。v4标称正式4.0不等于116条统一冻结。 |
| M02 | Agent/Backend | PARTIAL | 53条Fixture仍声明SPEC_ONLY_NOT_PROVISIONED，Scenario仍声明SPEC_ONLY_NOT_RUNNABLE；53条最新已有Evidence却有完整运行包且Case定义一致。保留两种原状态，以关联证据说明可用性，不修改规格标记。 |
| M03 | Agent/Backend | CONFIGURATION_GAP | evaluation/__main__.py的默认目录为项目同级procurement_eval_v4；实际文件位于项目父目录/评估case/procurement_eval_v4。当前默认目录不存在。显式--suite-dir可选实际路径；本次未执行模型也未修改路径配置。 |
| M04 | Agent / C04_BC06 | SPEC_REVIEW_REQUIRED | v4 README称A02_BC03/C04_BC06固定cancel_latest_confirmation；C04_BC06实际Scenario只有两条user_message，没有取消动作。Airtable与实际Scenario一致。需确认文档与测试目的，未改Case。 |
| M05 | Agent / A02_BC03、E-02 | SC_REVIEW_REQUIRED | 最新已有评分仍分别保留SC-02人工复核：A02_BC03前置意图澄清是否满足字段补问；E-02合理补问的语义质量。Manifest只记录既有状态。 |
| M06 | Backend / B-09、B-10 | FIXTURE_SEMANTICS_REVIEW | backend_runner.py在自身字典中标记superseded/expired，并在Harness拒绝；不是直接验证AgentRuntime的确认/TTL服务。需确认Backend Case的被测边界与Fixture目的。已有PASS不能作为真实Runtime门禁验证。 |
| M07 | Backend / B-13 | FIXTURE_SEMANTICS_REVIEW | Fixture声明isolated_instances=true；Runner在同一Store内创建等额/超额两张申请。提交不扣余额，因此既有结果不自动失效，但是否满足声明的独立实例要求需审核。 |
| M08 | Intent | PARTIAL_METADATA | IR21嵌入Python CASES，包含id/turns/intents/routes/tools；没有独立Dataset版本或显式能力分类。Airtable的PRD v1.2同步标签不是Dataset冻结版本；可用源文件SHA256固定扫描时内容。 |
| M09 | Intent / IR01、IR02、IR04、IR13、IR16 | EXPECTATION_MAPPING_REVIEW | Airtable描述READ可用才执行、无工具说明限制或不唯一则ASK；脚本以tools集合的issubset作为必需调用检查。需冻结允许路径及TOOL_LIMITATION是否适用，并确认语义标签到枚举的映射。 |
| M10 | Intent | PARTIAL_EVIDENCE_CONTRACT | 脚本共用seed_requests和database_count，历史证据保存intent/route/tools/确认及申请数量。未发现按Case声明的Fixture ID与独立证据契约；不把数量检查当作完整版本/状态一致性检查。 |
| M11 | RAG | GOLD_REVIEW_PENDING | 36条needs_formal_kb_confirmation，6条needs_atomic_gold_review。所有引用的30个不同候选/必需文档ID均在正式52篇manifest中且文件哈希匹配；存在性与版本核验不等于答案要点已审核。 |
| M12 | RAG / RAG-022、RAG-025、RAG-026、RAG-027、RAG-028、RAG-029 | ATOMIC_GOLD_REVIEW_PENDING | 这6条required_points只有一个合并句，Gold标记needs_atomic_gold_review。必须审核每个文档应证明的事实与完整性，不能用文档命中数量替代答案评分。 |
| M13 | RAG | ADAPTER_NOT_VERIFIED | JSONL/CSV/Mappings齐全且包哈希匹配；当前CaseLoader仅加载v4 Agent/Backend五字段。包README及run_contract实际适配标记仍待核对，评分聚合描述为分项无权重，Strict Pass映射与指标适用分母尚需确认。 |
| M14 | RAG / RAG-037、RAG-038、RAG-039、RAG-040、RAG-042 | FIXTURE_SPEC_PARTIAL | 37-40为API_READ，不计检索命中率；42为READ_CONFIRM_ACT。initial_state使用use_existing_read_only_fixture，未绑定已核实对象与完整Harness动作。PR-001、未命名供应商和历史产品的前置环境需明确；查询不存在可按原Case行为处理，修改必须明确合法对象和版本。 |
| M15 | Agent/Backend | HISTORICAL_STATUS_REFERENCE | Airtable53条v4执行状态明确是EVAL-03修复前基线。7条原FAIL在较新本地Evidence的既有评分为PASS；属于批次差异，不是Case内容冲突，本次不覆盖历史状态。 |
| M16 | Intent / IR12、IR20 | EVIDENCE_REFERENCE_REVIEW | Airtable声明PASS，但历史证据统一指向20261013-132704-optimized，其IR12/IR20为FAIL。20261013-132948-optimized的2条定向重跑为PASS且预期映射与当前脚本一致。应建立旧失败→新回归的引用关系，不能凭旧文件支持最终PASS。 |
| M17 | Legacy | LEGACY_EXCLUDED | AirtableLEGACY-C01–C10对应本地v0.8-review/v1.0-candidate的C01–C10，属于历史版本系列；不能按ID相似性替代当前v4。同名C01/C06/C07与v4目标不同。 |
| M18 | All | METRIC_MAPPING_REVIEW | 当前统一报告统计TSR/BCR/AVR及工具诊断；Plan的CCR/TCA/SVR与RAG RA/EGR/HR需明确Case适用性、断言和缺证排除规则。RAG包已有检索分母排除，但不能从现有通过率换算其他指标。 |

### 执行证据与静态规格的区别

v4原始SPEC_ONLY标签不被本次改写。53条最新现存Run的before_state、after_state、最终SQLite、Trace/raw、manifest、Evidence均存在，Evidence中的任务和成功条件与当前Case一致，逐次评分能够以SHA-256关联原Evidence。这证明已有运行包可追溯，不代表所有Fixture实现语义和Success Criteria已通过正式冻结审核。

Intent的旧全量文件 `runtime_data/task_13_14/20261013-132704-optimized/TASK_14_OPTIMIZED_EVIDENCE.json` 包含21条；IR12/IR20旧结果为FAIL，较新的 `20261013-132948-optimized/TASK_14_OPTIMIZED_EVIDENCE.json` 仅含这2条且为PASS。清单分别保留旧证据与定向回归引用，不把后者描述为重新全量运行21条。

## 5. 逐Case完整性清单

下表为扫描时原始资料的完整性标记。逐条检查维度、来源定位、证据引用和原执行状态见JSON；不将任何完整性标记写回Case或Airtable。

### 5.1 Intent（21）

| Case | 轮数 | 本地允许意图（逐轮） | 完整性状态 | 待核对编号 |
|---|---:|---|---|---|
| IR01 | 1 | consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M09 |
| IR02 | 1 | consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M09 |
| IR03 | 1 | consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR04 | 1 | consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M09 |
| IR05 | 1 | query | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR06 | 1 | query | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR07 | 1 | create | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR08 | 1 | create | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR09 | 1 | create | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR10 | 1 | create | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR11 | 1 | update | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR12 | 1 | ambiguous/consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M16 |
| IR13 | 1 | consult/query | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M09 |
| IR14 | 1 | query | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR15 | 1 | ambiguous | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR16 | 1 | consult | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M09 |
| IR17 | 1 | unsupported | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR18 | 1 | update | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR19 | 1 | ambiguous | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |
| IR20 | 2 | consult → create | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18、M16 |
| IR21 | 2 | query → update | PARTIAL_METADATA_REVIEW_PENDING | M01、M08、M10、M18 |

### 5.2 Agent（33）

| Case | 类别 | 成功条件数 | Scenario步骤数 | 完整性状态 | 待核对编号 |
|---|---|---:|---:|---|---|
| E01 | N | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E02A | E | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E02B | N | 3 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| A02_BC03 | E | 3 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M05 |
| C04_BC06 | E | 3 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M04 |
| E03 | N | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| A06_R04 | B | 3 | 6 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| A07 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-01 | N | 3 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-02 | N | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-03 | N | 3 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-04 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-05 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-07 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-01 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-02 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M05 |
| E-03 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-04 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-05 | E | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-06 | E | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-07 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-08 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-11 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-12 | E | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-04 | B | 1 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-05 | B | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-06 | B | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-08 | B | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-01 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-02 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-06 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-07 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-08 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |

### 5.3 Backend（20）

| Case | 类别 | 成功条件数 | Scenario步骤数 | 完整性状态 | 待核对编号 |
|---|---|---:|---:|---|---|
| C07 | N | 3 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| C06 | B | 3 | 5 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| C01 | N | 3 | 4 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-06 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-09 | E | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| E-10 | E | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-01 | B | 1 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-02 | B | 1 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-03 | B | 1 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-07 | B | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-09 | B | 2 | 4 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M06 |
| B-10 | B | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M06 |
| B-11 | B | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-12 | B | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| B-13 | B | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18、M07 |
| P-03 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-04 | P | 2 | 2 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| P-05 | P | 2 | 1 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-08 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |
| N-09 | N | 2 | 3 | STRUCTURE_COMPLETE_REVIEW_PENDING | M01、M02、M03、M15、M18 |

### 5.4 RAG（42）

分类数量：单条件10、复合条件10、跨文档10、证据不足6、路由6。

| Case | 分类 | 预期路由 | 完整性状态 | 待核对编号 |
|---|---|---|---|
| RAG-001 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-002 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-003 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-004 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-005 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-006 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-007 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-008 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-009 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-010 | single_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-011 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-012 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-013 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-014 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-015 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-016 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-017 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-018 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-019 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-020 | multi_condition | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-021 | cross_document | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-022 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-023 | cross_document | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-024 | cross_document | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-025 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-026 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-027 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-028 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-029 | cross_document | RAG_READ_ONLY | ATOMIC_GOLD_REVIEW_PENDING | M01、M11、M13、M18、M12 |
| RAG-030 | cross_document | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-031 | insufficient_evidence | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-032 | insufficient_evidence | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-033 | insufficient_evidence | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-034 | insufficient_evidence | API_IF_SUPPORTED_ELSE_LIMITATION | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-035 | insufficient_evidence | OUT_OF_SCOPE_EXPLAIN | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-036 | insufficient_evidence | OUT_OF_SCOPE_EXPLAIN | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-037 | routing | BUSINESS_API_READ | GOLD_REVIEW_PENDING | M01、M11、M13、M18、M14 |
| RAG-038 | routing | BUSINESS_API_READ | GOLD_REVIEW_PENDING | M01、M11、M13、M18、M14 |
| RAG-039 | routing | BUSINESS_API_READ | GOLD_REVIEW_PENDING | M01、M11、M13、M18、M14 |
| RAG-040 | routing | BUSINESS_API_READ | GOLD_REVIEW_PENDING | M01、M11、M13、M18、M14 |
| RAG-041 | routing | RAG_READ_ONLY | GOLD_REVIEW_PENDING | M01、M11、M13、M18 |
| RAG-042 | routing | READ_CONFIRM_ACT | GOLD_REVIEW_PENDING | M01、M11、M13、M18、M14 |

RAG-037–040的retrieval.evaluate=not_applicable，按原规格不进入检索命中分母；RAG-042声明读→确认→授权写序列，仍需绑定真实对象和Harness。不能把42条全部作为同一RAG检索指标分母。

### 5.5 历史Case（排除10）

| Airtable Case | 原Case ID | 完整性状态 | Airtable Record |
|---|---|---|---|
| LEGACY-C01 | C01 | LEGACY_EXCLUDED | recAtNiS4y3PpyXCe |
| LEGACY-C02 | C02 | LEGACY_EXCLUDED | recL9AId9rhV09JK2 |
| LEGACY-C03 | C03 | LEGACY_EXCLUDED | recbdJQW5jftFKeUR |
| LEGACY-C04 | C04 | LEGACY_EXCLUDED | recP53Y483BBbC49i |
| LEGACY-C05 | C05 | LEGACY_EXCLUDED | recgcjKW8HCbVJXlA |
| LEGACY-C06 | C06 | LEGACY_EXCLUDED | recUj7JEuxcwiKkut |
| LEGACY-C07 | C07 | LEGACY_EXCLUDED | recNIUkwX05K8EqKj |
| LEGACY-C08 | C08 | LEGACY_EXCLUDED | recCnhhRUrooecG0z |
| LEGACY-C09 | C09 | LEGACY_EXCLUDED | reczxgYxnWLe7suQL |
| LEGACY-C10 | C10 | LEGACY_EXCLUDED | recg5akTEnTaw3xvD |

本地v0.8与v1.0-candidate为这10条历史目标的不同规格版本，不额外计成20条，也不自动按同名ID与v4合并。

## 6. Airtable对齐与历史执行状态

| 核对项 | 结果 |
|---|---|
| 规划ID对应 | 116/116；无缺失或重复 |
| 历史条目 | 10条明确LEGACY前缀，排除出116 |
| v4成功条件 | 53/53精确匹配原SC列表 |
| v4执行步骤 | 53/53解析Harness JSON后精确匹配 |
| v4 Fixture规格 | 53/53解析requirements后精确匹配 |
| v4任务输入 | 53/53与对应用户消息/业务目标一致 |
| Intent逐轮输入 | 21/21匹配；语义/允许路径映射待审核 |
| RAG输入、路由、摘要及文档ID | 42/42匹配；Airtable摘要非完整机器契约 |

Airtable v4的执行状态明确记录「EVAL-03修复前基线」，本地较新Run属于另一批次。E02B、E03、A06_R04、N-04、N-05、E-03、E-12在Airtable保留旧FAIL，本地现存较新评分记录为PASS；这不是Case内容冲突。本次既不同步这些状态，也不将Manifest作为新评分报告。

## 7. 冻结前的完整性门槛

按Plan §3记录待办，只有审核完成后才可建立冻结记录：

1. 确认四模块采用版本及源文件哈希，补齐Intent Dataset版本和能力分类映射。
2. 确认Fixture语义与被测主体，重点核对B-09、B-10、B-13及RAG业务对象场景。
3. 核定Success Criteria与禁止行为，处理A02_BC03/E-02语义复核、IR允许路径和RAG Gold。
4. 核对RAG薄适配、Strict Pass状态映射、逐Case指标适用性和Evidence契约。
5. 关联审核人、需求/规则/知识版本、结论与生效日期，记录统一冻结批准。

本次没有开展上述修复、Gold改写、外部台账更新、模型调用、评分或业务回归。

## 8. 来源完整性与留存

- 全部扫描来源文件的完整SHA-256保存在JSON的local_sources；Plan也单独绑定哈希。
- 原始Case、Plan、选定Evidence和evaluation Python文件在扫描前记录哈希，产物创建后再次核对：101个受保护文件哈希均未变化。
- 新增文件仅为 `evaluation/DATASET_MANIFEST_v1.md` 与 `evaluation/DATASET_MANIFEST_v1.json`；它们属于评估集清单，不替代Evaluation Report。
- `evaluation/runs/`、`evaluation/reports/`、`runtime_data/`及Airtable Case原文保留原位。
