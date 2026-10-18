"""将已完成的框架评分和证据汇总为审计交付，不执行新的模型调用。"""
from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path

from . import audit_golden_v11 as audit


def short(text, n=260):
    return text if len(text) <= n else text[:n] + "……（完整理由见JSONL及原始记录）"


def make_rows(cases, sources, probes, bindings):
    rows = []
    probe_failures = {p["case_id"]: p for p in probes if p["false_rejection_risk"] or p["false_acceptance"]}
    failed_bindings = {b["case_id"]: b for b in bindings if b["score"] == 0}
    for case in cases:
        values = []
        for dimension, primary in audit.primary_scores(case["case_id"]).items():
            review_path = audit.RUN / "metrics" / f"review__{case['case_id']}__{dimension}.json"
            review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None
            chosen = review if review and review["score"] < primary["score"] else primary
            verification = audit.quote_validation(chosen, case, sources)
            disagree = bool(review and review["score"] != primary["score"])
            result = {**chosen, "supporting_evidence": verification["references"],
                "framework_primary_score": primary["score"], "framework_review_score": review["score"] if review else None,
                "framework_primary_normalized_score": primary["framework_normalized_score"],
                "framework_primary_confidence": primary["confidence"],
                "confidence_origin": "confidence_from_the_framework_judgment_used_for_this_score",
                "score_origin": "minimum_of_actual_framework_primary_and_selective_review" if review else "actual_framework_primary",
                "review_disagreement": disagree, "uncertainty": verification["uncertainty"] or disagree,
                "citation_validation": verification,
                "primary_raw_record": str(audit.RUN / "calls" / f"primary__{case['case_id']}__{dimension}.json"),
                "review_raw_record": str(audit.RUN / "calls" / f"review__{case['case_id']}__{dimension}.json") if review else None}
            if disagree:
                result["reason"] += f" 复核与主审不一致（主审{primary['score']}，复核{review['score']}）；本审计保守保留较低分并标注不确定性，不用平均或挑选高分消除分歧。"
                result["review_reason"] = review["reason"]
            if case["case_id"] == "RGV1-A01" and dimension == "D6" and case["case_id"] in probe_failures:
                probe = probe_failures[case["case_id"]]
                result["score"] = min(1, result["score"])
                result["score_origin"] = "audit_gate_from_actual_GEval_counterfactual_probe_not_a_new_D6_model_score"
                result["reason"] += " 独立逐答案探针进一步发现：正确简答“一万元整走M02，还要提供可信报价材料”因未重复‘低于10000元才归M01’被原评分点P3判不完整。P3不是回答该笔申请必需的额外制度事实，因此D6审计分按已冻结的‘存在过严风险’标准降为1；框架原D6=2保留，未伪称G-Eval本来给1。"
                result["issue_type"] = list(set(result["issue_type"]) | {"OBSERVED_FALSE_REJECTION_OF_VALID_MINIMAL_ANSWER"})
                result["supporting_evidence"].append(dict(source="point_probe_results.json:RGV1-A01/V2",
                    quote=probe["answer"], explanation=probe["reason"], quote_found_unannotated_source=True))
                result["probe_evidence"] = probe
                result.update(confidence=None, confidence_origin="Counterfactual probe schema did not report numeric confidence; primary framework confidence retained separately.")
            if dimension == "D6" and case["case_id"] in failed_bindings:
                binding = failed_bindings[case["case_id"]]
                result.update(score=0, framework_normalized_score=binding["framework_normalized_score"],
                    framework_binding_probe_score=binding["score"],
                    score_origin="actual_GEval_targeted_point_fact_binding_probe_applied_to_D6_gate",
                    reason="当前评分点的fact_ids存在明确语义错绑，不能以文档别处有该知识替当前引用背书。" + binding["reason"],
                    uncertainty=False, suggested_action="SKIP_CURRENT_VERSION：先修正评分点到事实的绑定，再验证；本轮不改原Case。",
                    issue_type=["CONFIRMED_KEY_POINT_FACT_BINDING_ERROR"], binding_probe=binding)
                result.update(confidence=None, confidence_origin="Targeted binding probe schema did not report numeric confidence; source-linked defect confirmed, primary confidence retained separately.")
                by_f = {f["fact_id"]: f for f in case["required_facts"]}
                for point in case["answer_key_points"]:
                    for fid in point["fact_ids"]:
                        result["supporting_evidence"].append(dict(source=f"{point['point_id']}→{fid}", quote=by_f[fid]["text"],
                            explanation=f"原criterion：{point['criterion']}；上述为实际绑定原文，语义不支持该评分点。", quote_found_unannotated_source=True))
            result["audit_normalized_score"] = result["score"] / 2
            values.append(result)
        by_d = {r["dimension"]: r for r in values}
        quality_gate = all(by_d[d]["score"] == 2 and not by_d[d]["uncertainty"] for d in ["D1", "D2", "D6"])
        fatal = [r for r in values if r["dimension"] in ["D1", "D2", "D6"] and r["score"] == 0 and not r["uncertainty"]]
        uncertain = [r["dimension"] for r in values if r["uncertainty"]]
        classification = "SKIPPED" if fatal else ("PASS" if all(r["score"] == 2 for r in values) and not uncertain else "IMPROVEMENT_RECOMMENDED")
        current_label_usable = quality_gate and by_d["D5"]["score"] == 2 and not by_d["D5"]["uncertainty"] and all(by_d[d]["score"] >= 1 for d in ["D3", "D4"])
        d4_tags = by_d["D4"]["analysis_tags"]
        rows.append(dict(case_id=case["case_id"], classification=classification,
            quality_gate_pass=quality_gate, usable_unmodified_with_current_scenario_label=current_label_usable,
            scenario_type=case["scenario_type"], knowledge_type=case["knowledge_type"], answerability=case["answerability"],
            question=case["question"], dimension_scores=values,
            uncertain_dimensions=uncertain, difficulty_estimate=d4_tags.get("difficulty_estimate", "NOT_ESTIMATED"),
            difficulty_is_empirical=False, judge_answer_leakage_signal=d4_tags.get("answer_leakage", "NOT_ESTIMATED"),
            declared_attributes=case["attributes"], judge_effective_attributes=d4_tags.get("effective_attributes", []),
            scenario_audit=by_d["D5"]["analysis_tags"],
            notes=["判定对象为测试Case本身；PASS不是RAG评测成绩。", "支持引文可回放不意味着Judge的所有推理都正确。"] +
                (["C04早期把Required Facts角色本身说成错置不准确；后续针对性引用审计确认answer_key_points中的F1/F2指向确实对调，两者不能混为一谈。"] if case["case_id"] == "RGV1-C04" else [])))
    return rows


def generate(cases, sources):
    report, run = audit.REPORT, audit.RUN
    probes = json.loads((report / "point_probe_results.json").read_text(encoding="utf-8"))
    bindings = json.loads((report / "binding_probe_results.json").read_text(encoding="utf-8"))
    rows = make_rows(cases, sources, probes, bindings)
    if len(rows) != 46 or sum(len(r["dimension_scores"]) for r in rows) != 276:
        raise ValueError("46条六维评分未齐全，不生成完成报告")
    if audit.protected_snapshot() != json.loads((run / "protected_baseline.json").read_text(encoding="utf-8")):
        raise ValueError("审计输入或受保护源发生变化")
    audit.golden.write_jsonl(report / "case_quality_scores.jsonl", rows)
    flat = []
    for row in rows:
        for dim in row["dimension_scores"]:
            flat.append(dict(case_id=row["case_id"], classification=row["classification"],
                quality_gate_pass=row["quality_gate_pass"], usable_unmodified=row["usable_unmodified_with_current_scenario_label"],
                scenario_type=row["scenario_type"], knowledge_type=row["knowledge_type"], answerability=row["answerability"],
                question=row["question"], difficulty_estimate=row["difficulty_estimate"],
                **{k: dim.get(k) for k in ["dimension", "score", "framework_primary_score", "framework_review_score", "score_origin", "reason",
                    "supporting_evidence", "issue_type", "confidence", "confidence_origin", "suggested_action", "uncertainty", "review_reason", "primary_raw_record", "review_raw_record"]}))
    with (report / "case_quality_scores.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(flat[0]))
        writer.writeheader()
        for row in flat: writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    stats = audit.lexical_stats(cases)
    classifications = Counter(r["classification"] for r in rows)
    dimensions = {d: Counter(next(x["score"] for x in r["dimension_scores"] if x["dimension"] == d) for r in rows) for d in audit.RUBRICS}
    primary_distribution = {d: Counter(audit.primary_scores(r["case_id"])[d]["score"] for r in rows) for d in audit.RUBRICS}
    scenario = Counter(c["scenario_type"] for c in cases)
    docs = {e["doc_id"] for c in cases for e in c["golden_evidence"] if e["role"] == "fact"}
    doc_classes = Counter(sources[d]["class"] for d in docs)
    declared_attrs = Counter(a for c in cases for a in c["attributes"])
    effective_attrs = Counter(a for row in rows for a in row["judge_effective_attributes"] if isinstance(a, str))
    d4_signals = Counter(r["judge_answer_leakage_signal"] for r in rows)
    difficulty = Counter(r["difficulty_estimate"] for r in rows)
    uncertain_rows = [r["case_id"] for r in rows if r["uncertain_dimensions"]]
    disagreements = [dict(case_id=r["case_id"], dimension=d["dimension"], primary=d["framework_primary_score"], review=d["framework_review_score"])
                     for r in rows for d in r["dimension_scores"] if d["review_disagreement"]]
    d2_risks = [r["case_id"] for r in rows if next(d for d in r["dimension_scores"] if d["dimension"] == "D2")["score"] < 2]
    d6_risks = [r["case_id"] for r in rows if next(d for d in r["dimension_scores"] if d["dimension"] == "D6")["score"] < 2]
    cross_risks = [r["case_id"] for r in rows if r["scenario_type"] in ["C", "D"] and next(d for d in r["dimension_scores"] if d["dimension"] == "D5")["score"] < 2]
    failed_probes = [p for p in probes if p["false_rejection_risk"] or p["false_acceptance"]]
    confirmed_bad_binding = [b["case_id"] for b in bindings if b["score"] == 0]
    d2_omission_signals = [r["case_id"] for r in rows if any(any(t in str(it).upper() for t in ["OMISSION", "OMITTED", "MISSING"])
        for d in r["dimension_scores"] if d["dimension"] == "D2" for it in d["issue_type"])]
    d6_overstrict_signals = [r["case_id"] for r in rows if any(any(t in str(it).upper() for t in ["STRICT", "FALSE_REJECTION"])
        for d in r["dimension_scores"] if d["dimension"] == "D6" for it in d["issue_type"])]
    probe_correct = [p for p in probes if p["independently_designed_expected"].startswith("VALID")]
    probe_wrong = [p for p in probes if not p["independently_designed_expected"].startswith("VALID")]
    topic_probes = {topic: [d for d, s in sources.items() if topic in s["text"]] for topic in ["礼品", "积分"]}
    stats.update(classifications=dict(classifications), dimensions={d: dict(v) for d, v in dimensions.items()},
        primary_dimension_distributions={d: dict(v) for d, v in primary_distribution.items()},
        scenario_distribution=dict(scenario), knowledge_case_distribution=dict(Counter(c["knowledge_type"] for c in cases)),
        answerability_distribution=dict(Counter(c["answerability"] for c in cases)),
        formal_docs_evidenced=len(docs), formal_doc_class_distribution=dict(doc_classes),
        required_key_points=sum(len(c["answer_key_points"]) for c in cases),
        quality_gate_eligible=sum(r["quality_gate_pass"] for r in rows),
        usable_current_labels=sum(r["usable_unmodified_with_current_scenario_label"] for r in rows),
        uncertain_cases=uncertain_rows, review_disagreements=disagreements,
        declared_attributes=dict(declared_attrs), judge_effective_attributes=dict(effective_attrs),
        difficulty_estimates=dict(difficulty), answer_leakage_judge_signals=dict(d4_signals),
        direct_full_correct_answer_leakage_confirmed=0,
        question_golden_alignment_risk_cases=d2_risks, scoring_executability_risk_cases=d6_risks,
        cross_document_scenario_risk_cases=cross_risks, unanswerable_literal_topic_probes=topic_probes,
        probe_count=len(probes), correct_probe_count=len(probe_correct), incorrect_probe_count=len(probe_wrong),
        false_rejection_probes=len([p for p in probes if p["false_rejection_risk"]]),
        false_acceptance_probes=len([p for p in probes if p["false_acceptance"]]),
        confirmed_wrong_binding_cases=confirmed_bad_binding, confirmed_wrong_point_fact_links=5,
        golden_core_factual_errors_confirmed=0, critical_question_golden_nonresponse_confirmed=0,
        golden_omission_judge_signal_cases=d2_omission_signals, overstrict_judge_signal_cases=d6_overstrict_signals,
        static_attribute_findings=dict(strict_amount_boundary_cases=["RGV1-A01", "RGV1-A02", "RGV1-D01"],
            non_threshold_numeric_examples=["RGV1-A12", "RGV1-D06"],
            distracting_context_declared_cases=[c["case_id"] for c in cases if "distracting_context" in c["attributes"]],
            genuinely_irrelevant_background_confirmed=0, explicit_similar_wrong_evidence_control_fixtures=0),
        provenance=dict(zip_sha256=audit.sha(audit.ZIP.read_bytes()), rubric_sha256=audit.sha((report/"rubric.json").read_bytes()), protected_inputs_unchanged=True))
    audit.save(report / "dataset_statistics.json", stats)
    cross_distribution = {}
    for typ in "ABCD":
        selected = [r for r in rows if r["scenario_type"] == typ]
        cross_distribution[typ] = dict(total=len(selected), scenario_score_2=sum(next(d for d in r["dimension_scores"] if d["dimension"] == "D5")["score"] == 2 for r in selected),
            quality_gate_pass=sum(r["quality_gate_pass"] for r in selected), current_label_usable=sum(r["usable_unmodified_with_current_scenario_label"] for r in selected))
    audit.save(report / "audit_completion.json", dict(status="COMPLETE", cases=46, dimension_results=276,
        score_scale_valid=all(d["score"] in [0, 1, 2] for r in rows for d in r["dimension_scores"]),
        unique_cases=len({r["case_id"] for r in rows}), csv_rows=len(flat),
        required_outputs=["quality_audit_report.md", "case_quality_scores.jsonl", "case_quality_scores.csv", "quality_issues.md", "needs_decision.md", "judge_config.json", "skill_workflow_recommendations.md"],
        formal_source_and_original_datasets_unchanged=True, rag_evaluation="NOT_RUN", airtable_sync="NOT_RUN"))

    lines = ["# 采购RAG Golden v1.1独立质量审计", "", "审计日期：2026-10-14。对象是ZIP中的46条测试Case本身。", "",
        "## 整体结论", "",
        "**正式原文来源可靠，语义答案大体有据，问题自然性较好；评分合同和跨文档标签仍需要处理，不能把46条原样当成已经通过严格质量门槛的正式测试集。**", "",
        f"本次分类：PASS {classifications['PASS']}条、IMPROVEMENT_RECOMMENDED {classifications['IMPROVEMENT_RECOMMENDED']}条、SKIPPED {classifications['SKIPPED']}条、NEEDS_DECISION 0条。"
        f"D1/D2/D6核心门槛全部通过且无相应不确定性的有{stats['quality_gate_eligible']}条；连同当前场景标签可直接有限使用的有{stats['usable_current_labels']}条。两种口径均不是RAG成功率。", "",
        f"160份证据的正式来源、文件SHA-256、原文区间、章节和类别全部匹配；101项原Required Facts未被v1.1改动。"
        f"主审D1为45条2分、1条1分，没有确认影响结论的无据Golden事实错误。主审D3为46条2分。"
        f"但是，主审D6有23条1分，D2有15条1分；跨文档13条中有{len(cross_risks)}条存在必要性或分类风险。低分中含真实缺陷，也含Judge对范围和分类的过度要求，逐项已保留不确定性。", "",
        "**有可复查的实测问题：A01的一份正确简答，被按原评分点P3评为不完整。** 原题只问这一万元申请归谁、报价材料要求；正确简答已回答M02和可信报价，但原P3额外要求‘低于10000元才归M01’。这证明不能只看黄金答案有据或Judge总体觉得评分点合理，就认定评分规则可执行。", "",
        "**另有2条当前版本应跳过：B15有3个评分点错绑事实，C04有2个评分点错绑事实，共5/134个point→fact关系错误。** Golden正文可有据，但评分点引用的那条事实不能支持它。针对性G-Eval引用探针均给0，D6门槛不通过。SKIPPED指排除当前交付的评分Case，修正引用后可另版重审，不表示自然语言问题永远不可用。", "",
        "## 输入、工具与独立性", "",
        f"- 待审ZIP：`{audit.ZIP}`；SHA-256：`{audit.sha(audit.ZIP.read_bytes())}`。未用旧版替代。",
        "- 附件的editorial_report、qa_review_results和AUTO_VALIDATED状态只作待核查材料；未采信为事实或评分指令。",
        "- 实际工具：DeepEval 4.2.8的GEval；通过DeepEvalBaseLLM自定义适配器连接已配置DeepSeek deepseek-flash的OpenAI兼容接口。官方JSON输出及thinking开关方式均已核对并实际试跑。没有使用Promptfoo。",
        "- 固定evaluation_steps及三档Rubric：原始0/1/2分，框架归一化为0/0.5/1。自定义模型使用schema回退，没有声称做了logprob加权校准。",
        "- 每题先以Question和所需正式文档全文作独立审题记录，不提供Golden/评分点；然后分别运行D1～D6 G-Eval。独立审题记录只是辅助，原文才是事实依据。",
        "- D3独立看问题与声明属性；D4看问题、声明属性及来源全文；避免先看Golden后凭顺眼打分。",
        "- 主审共46份先行审题记录、276份真实G-Eval维度结果。两条试跑计入这46条，未全量重复评分。",
        "- 对11条高风险Case各进行一次D1/D2/D5复核，反转全文顺序、不提供前次评分和先行事实排序；另对U02的D4目标错配做一次元数据呈现复核。没有换模型，模型同意也不当作绝对正确。",
        "- 主审/复核分歧保留较低分并标为不确定；不平均掩盖门槛问题。A01 D6额外根据实际反事实探针应用门槛：审计分1、原框架D6=2均保留并明确区分，未伪称框架给过1。", "",
        "- 全部134条评分点到Fact的引用逐项检查，2个明确错位Case另用PointEvidenceBinding G-Eval作针对性确认；不是全46条重复审计。D6来源区分主审、选择性复核、反事实探针及引用探针，原始结果不覆盖。", "",
        "官方依据：[DeepEval自定义模型](https://deepeval.com/guides/guides-using-custom-llms)、[G-Eval固定步骤与Rubric](https://deepeval.com/docs/metrics-llm-evals)、[DeepSeek JSON](https://api-docs.deepseek.com/guides/json_mode/)、[思考开关](https://api-docs.deepseek.com/guides/thinking_mode/)。", "",
        "## 六维分布", "", "| 维度 | 主审0/1/2 | 最终审计0/1/2 | 解释 |", "|---|---|---|---|"]
    for d, rubric in audit.RUBRICS.items():
        fmt = lambda counter: "/".join(str(counter.get(i, 0)) for i in [0, 1, 2])
        lines.append(f"| {d} {rubric['title']} | {fmt(primary_distribution[d])} | {fmt(dimensions[d])} | {'质量门槛' if d in ['D1','D2','D6'] else '质量与覆盖属性'} |")
    lines += ["", "最终分布保留复核分歧的保守值，因此不能把其低分数量全部解释为已确认Case错误。逐题JSONL包含原始分数、复核分数、来源、理由、confidence、证据核对及不确定性。", "",
        "### D1：事实支持", "",
        "文件/区间恒等通过后才进行语义评分，未用抽取恒等替代问答审计。D01的‘新引入’与业务系统new状态关系得到主审1/复核2，说明系统状态是否已知的语用边界可产生分歧；应明确假设条件，不能因此声称查到了真实供应商。未发现B类建议被直接提升成普遍强制制度的确证。", "",
        "### D2：问答关系", "",
        f"存在范围、必要性或answerability风险的Case为：{', '.join(d2_risks)}。这些是风险集合，未全部证明为Golden遗漏。"
        "例如B11、B15的先行审题曾把具体标准、实时供应商状态等用户未索取信息当作必要前提，复核回到题目后给2；不能据此断言原Golden有致命缺漏。U01的实体制度不可答与缺证行为可答也容易被模型混淆。", "",
        "### D3/D4：表达与难度", "",
        f"自然性主审46条均为2分。难度预估为{dict(difficulty)}，只表示Judge估计，没有实际RAG对照实验，未验证区分度。"
        "表述已比机械照抄原文更接近真实咨询，但仍偏重规则解释和方法建议，不能代表真实用户问题频率、噪声、长对话或分布。", "",
        f"Judge答案泄露信号：{dict(d4_signals)}。其中3条leading_conclusion风险为B01、C03、D07，不能直接当作3条已证实泄露。"
        "C03提供的是待纠正的错误假设，D07给出服务采购的合法题设；本次未确认把正确完整答案直接写在题目中的Case（0条），但B01的原文示例复用及D06的100/84数量例子值得降同构性。普通术语和选项提示另行保留，不与答案泄露合计。", "",
        "### D5：必要场景", "",
        "重要区别：两份来源有据，不等于回答这道题一定需要两份来源。C04、D03、D04的单篇全文充分性风险较具体；C01/C02两份来源有用，但C/D分类存在分析口径风险；D08的核心节约率判断可由065单篇支撑，024只对被问到的数据清理步骤有增益。已逐条保留理由，不以doc_id数量作证明。", "",
        "### D6：评分执行", "",
        f"有评分执行风险的Case为：{', '.join(d6_risks)}。其中很多只是‘criterion把多项列举绑在一起’的潜在过严风险，不应宣称都已发生错判。"
        "A01则有独立逐答案探针观察，须先调整评分合同再严格使用。不同专业建议的合理替代方案仍没有做大规模覆盖验证。", "",
        "明确错绑：B15 P1‘单一来源不自动违规’绑定F1（成因识别），应由F3支持；P2‘识别依赖成因’绑定F2（替代控制），对应F1；P3‘备货替代等控制’绑定F3（制度范围），对应F2。C04 P1‘先用途性能后价格’错误绑定部署F1，对应F2；P2部署约束错误绑定选型优先级F2，对应F1。当前版本不应通过严格评分或必要证据召回门禁。", "",
        f"数量口径：已确认Golden正文关键事实错误0、关键答非所问0；Judge疑似遗漏信号{len(d2_omission_signals)}条（未证明都是实际遗漏）；问答范围/answerability风险15条；Judge疑似过严信号{len(d6_overstrict_signals)}条；实际正确简答被原规则拒绝完整通过1份；语义错绑2条Case/5条关系。禁止结论的系统性错误未获确证。", "",
        "## 场景与知识覆盖", "", "| 声明场景 | 总数 | D5=2 | 核心门槛通过 | 当前标签下可用 |", "|---|---:|---:|---:|---:|"]
    for typ, values in cross_distribution.items(): lines.append(f"| {typ} | {values['total']} | {values['scenario_score_2']} | {values['quality_gate_pass']} | {values['current_label_usable']} |")
    lines += ["", f"单文档33条（71.74%）；跨文档13条（28.26%）。来源类别的Case分布：{dict(Counter(c['knowledge_type'] for c in cases))}。"
        f"可回答性分布：{dict(Counter(c['answerability'] for c in cases))}。", "",
        f"必要事实引用47/52篇：A类{doc_classes['A']}/11、B类{doc_classes['B']}/41。共有101次事实引用，去重后95个完整段落区间、134个answer_key_points。"
        "这既不等于47篇内容全部测完，更不把总览的52篇覆盖当成测试覆盖。未选的KB-001、KB-011、PKB-V2-025、PKB-V4-058、PKB-V4-059仍有潜在未测细节。", "",
        "| 附加属性 | 声明数量 | Judge明确认可数量 |", "|---|---:|---:|"]
    for attr in audit.golden.ATTRIBUTES: lines.append(f"| {attr} | {declared_attrs[attr]} | {effective_attrs[attr]} |")
    lines += ["", "属性认可数量是D4模型推断，不是客观实验。未认可可能是遗漏，不能一概视作不存在。numeric_boundary中仅以100/84演示差异签收，不必真有阈值等号推理；distracting_context中‘季度复盘/分批收货’通常是相关条件，并非真正无关噪声，残留标签需要精化。", "",
        "独立看题面，声明distracting_context的B01、B05、B12、C04、D06五题均只剩相关采购条件，未确认真正无关背景（0/5）；Judge却沿用了全部5个标签，显示属性自报锚定。5个numeric_boundary标签中，严格金额等号/相邻边界为A01/A02/D01三条，另外两条为延期次数或数量差异。15条similar_knowledge是概念混淆方向，没有配套受控相似错误证据Fixture（0），不能据此宣称抗干扰能力已验证。", "",
        "## 重复、同构与覆盖缺口", "",
        f"归一化完全重复问题为0；101次事实引用使用95个不同区间，重复引用6次。字面二元字符余弦最高为{stats['top_lexically_similar_pairs'][0]['character_bigram_cosine']}，没有达到预设0.5候选阈值的题对；这不证明没有语义近义题。", "",
        "A01/A02/D01集中覆盖同一万元审批边界；A03/D02重复预算时点；B01/C03重复预算非单价；A11/B11重复建议与强制标准的区分。部分是有价值的单点/组合对照，部分有边际收益下降；不应按题数直接等同新增能力。", "",
        "明显能力缺口：缺少真实用户分布验证、同意图成对同义改写一致性、强相似错误证据的显式对照、真正无关背景噪声对照、足量不同规则的数值边界、长对话追问与指代、引用充分性对照。业务实时状态/写入安全题没有确定性Fixture，当前集合不能测试真实API结果或工具执行成功率。", "",
        "## 评分规则探针", "",
        f"3个Case、7份独立设计的探针答案（正确{len(probe_correct)}份、典型错误{len(probe_wrong)}份），分别通过真实G-Eval按原评分点逐答案评分。"
        f"正确答案未获完整满足判定{len([p for p in probes if p['false_rejection_risk']])}份，错误答案获完整满足{len([p for p in probes if p['false_acceptance']])}份。样本很小，不能推广为总体评分准确率。", "",
        "| Case/探针 | 预期语义 | 原规则评分0/1/2 | 观察 |", "|---|---|---:|---|"]
    for p in probes: lines.append(f"| {p['case_id']}/{p['answer_id']} | {p['independently_designed_expected']} | {p['score']} | {short(p['reason'], 220)} |")
    lines += ["", "此前3次批量规则质量探针也全部保留于scoring_rule_probes.json；A01/C02只给整体正反评价而缺逐点数组，故没有把它们伪装成完成了7份逐点判定。随后上述7份单答案探针补足了可检查的框架结果，未改六维Rubric。", "",
        "## Judge偏差与不确定性", "",
        f"有主审/复核分数分歧的维度记录{len(disagreements)}项，涉及不确定性Case：{', '.join(uncertain_rows) or '无'}。", "",
        "- U02主审D4=0的理由是‘知识库没该规则所以无评测价值’，忽略了UNANSWERABLE的缺证识别目标。复核展示原有answerability/expected_behavior后给2；保留0/2分歧，不声称这是Case已确认无效，也不强行改成PASS。",
        "- C04早期理由把Required Facts角色说成错置并不准确；独立审题排序无需与F1/F2对齐。但后续逐评分点检查确认answer_key_points的fact_ids确实对调，不能因早期理由不精确就忽略真实引用错误。完整错误链另有正式原文和绑定探针支持。",
        "- D08部分理由把当前D类误说成C类，又建议改为D。此内部矛盾保留，不能凭该句话证明标签错误；真正要核的是所问数据清理是否是必需子任务。",
        "- 先行审题可能把文档全部内容当成用户必要信息，从而错误要求Golden覆盖无关事实。评分要回到Question，不能把模型生成的needed_facts当新黄金标准。",
        "- 引文匹配最初受到展示行号和简化JSON视图影响，已用未加行号的原文/真实审计输入重新核对，原字段和警告仍保存。文本匹配仅能证明短引出现，不能证明理由中的推理。", "",
        "## 异常、成本与复查", "",
        "原始审题、G-Eval响应、全部Prompt、模型返回名、Token、原异常及恢复记录均保存在evaluation/runs/rag_golden_v1_1_quality_audit。格式错误的5份盲审只去除明确的response_format回显或读取唯一完整JSON对象，未更改事实值；8次连接/超时只补跑缺失项一次，未重新评分成功项。", "",
        f"用量：{audit.usage_summary()}。费用按官方峰时未缓存价估算，并以8元/美元作保守预算假设，不是实时汇率或实际账单；没有Usage的异常另列保留额。实际扣费无法从API确认。", "",
        "## 是否可用于正式评测及Skill", "",
        f"可以把{stats['usable_current_labels']}条已通过本次核心门槛和当前场景门槛的Case作为有明确版本边界的候选输入；其余应先处理对应评分点/分类或审计不确定性。"
        "不建议把全部46条原样当成正式Strict Pass标准。使用前仍需完成现有Runner适配、最小原文证据到实际Chunk的映射及语义Checker校准；这些属于后续评测接入，本轮没有运行。", "",
        "该方法适合作为可复用Skill工作流的候选，但应固化‘证据先行、独立审题、范围对齐、语义必要性、反事实评分门禁’，不能固化‘所有AI生成Case自动正确’。具体最小流程见skill_workflow_recommendations.md。", "",
        "本次没有修改正式知识、原46条、v1.1 ZIP、Agent、Backend、索引、Embedding、Top-K或Airtable；没有生成新的Golden Dataset或根据Judge自动重写问题。"]
    lines += ["", "## 交付索引与离线复查", "", "| 文件 | 内容 |", "|---|---|"]
    for name, purpose in [("case_quality_scores.jsonl", "46条Case，内含276项维度结果、主审/复核及分类"),
                          ("case_quality_scores.csv", "276行，便于按Case或维度排序"), ("quality_issues.md", "明确缺陷与风险，含证据及建议"),
                          ("needs_decision.md", "仅真正业务决策，本次为0"), ("judge_config.json", "实际工具版本、模型、固定Rubric和用量"),
                          ("skill_workflow_recommendations.md", "最小复用质量门禁及系统性偏差"),
                          ("binding_probe_results.json", "B15/C04评分点错绑的实际G-Eval确认"),
                          ("point_probe_results.json", "7份正反答案逐个评分与A01误拒观察")]:
        lines.append(f"| [{name}]({(report/name).as_posix()}) | {purpose} |")
    lines += ["", f"原始调用索引：`{run / 'raw_output_manifest.json'}`；异常：`{run / 'exceptions.jsonl'}`。",
        "审计脚本为evaluation/audit_golden_v11.py及audit_golden_v11_reporting.py。6项离线门禁回归通过，结果在reports/rag_golden_v1_1_quality_audit/validator_test_results.json。",
        "以下命令不发起新模型调用：", "", "```powershell", "# 在procurement_eval_v08目录执行",
        "python -m evaluation.audit_golden_v11 preflight", "python -m evaluation.audit_golden_v11 report",
        "python -m unittest evaluation.tests.test_golden_quality_audit -v", "```", "",
        "report依赖本次保存的模型结果；不得删除或替换原始失败记录后声称复现通过。"]
    (report / "quality_audit_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    issues = ["# 质量缺陷与优化风险", "", "本表区分已观察到的评分失败、稳定风险和Judge不确定性。完整理由、原文短引、confidence及主审/复核均见case_quality_scores.jsonl；原输入未改。", "",
        "## 已观察到的评分执行缺陷", "",
        "- **RGV1-A01 / D6**：原P3要求低于10000归M01；题目只问当前一万元申请及凭证。反事实正确简答已给M02及凭证，却因未表达P3获1而非2。证据：point_probe_results.json中的A01/V2，正式KB-003:L21。建议P3改为可选解释或评分时明确不要求题目未问的完整金额分段。当前Case保留为待优化，未自动改写。", "",
        "- **RGV1-B15 / D6 / SKIPPED**：P1→F1实际引用成因，P2→F2实际引用替代控制，P3→F3实际引用不当然违规；三者与对应criterion错位。建议映射分别核对F3/F1/F2。正式原文PKB-V4-057:L28/L32/L40；绑定探针0分。",
        "- **RGV1-C04 / D6 / SKIPPED**：P1（用途性能优先级）→F1（部署条件）、P2（部署）→F2（优先级），两个引用对调。建议分别核对F2/F1。正式原文PKB-V3-029:L32及PKB-V2-026:L24；绑定探针0分。", "",
        "## 全部低分或不稳定项", "", "| Case | 维度/分 | 类型 | 证据与风险说明 | 建议 |", "|---|---|---|---|---|"]
    for row in rows:
        for d in row["dimension_scores"]:
            if d["score"] < 2 or d["uncertainty"]:
                kind = "复核不一致，未裁为确证缺陷" if d["review_disagreement"] else ("实际探针观察" if "probe_evidence" in d else "待优化风险")
                issues.append(f"| {row['case_id']} | {d['dimension']}={d['score']} | {kind} | {short(d['reason']).replace('|','/')} | {short(str(d['suggested_action']),160).replace('|','/')} |")
    issues += ["", "## 不应误当成Case缺陷的Judge说法", "",
        "C04不能仅凭两套局部编号排序不同就认定Required Facts错置，但本次确认的是评分点实际错绑，须处理。U02缺少实体规则正是UNANSWERABLE目标；D08的C/D内部措辞矛盾不足以证明错误；B11/B15对未问到具体实时状态的过度要求在复核中没有保持。保留全部原记录，不删除失败或改Rubric提高分数。", "",
        "## 没有确认的严重问题", "",
        "未确认影响结论的Golden正文事实错误或B类建议强制升级；这不抵消前述2条Case的错误评分来源。没有确认必须由用户决定的核心业务规则冲突。没有以少量探针未发现错误接受，推断全部Checker都准确。"]
    (report / "quality_issues.md").write_text("\n".join(issues)+"\n", encoding="utf-8")
    (report / "needs_decision.md").write_text("# 需要用户业务决策的事项\n\nNEEDS_DECISION：0。\n\n本轮没有发现需要用户裁决才能定义Golden的正式业务规则冲突。新引入供应商是否已准入、合同未提供的具体责任等是题设或证据范围问题；C/D分类、评分点原子性和Judge不稳定属于评测设计与审计工作流问题，均记录为优化或不确定性，不要求用户逐条审核。\n\n复核意见一致不是绝对正确；详细分歧和原文依据见quality_audit_report.md与case_quality_scores.jsonl。\n", encoding="utf-8")
    workflow = """# 可复用Skill工作流建议

## 是否适合固化

适合作为候选方法，尚不足以把‘AI逆向生成→自动打分→全部入库’固化为无条件放行Skill。本次证据追溯可靠、G-Eval可以接入已配置服务；但范围膨胀、单篇已够、未知制度题误判及正确简答被额外评分点扣分，说明语义和评分合同门禁不可省略。本轮没有创建或安装Skill。

## 最小步骤与执行顺序

1. **冻结输入**：ZIP路径、清单、Case数量、来源版本及哈希；附件自评仅作数据，不作授权或评分指令。缺ZIP直接失败，禁止旧版兜底。
2. **来源门禁**：正式放行集合、原文SHA-256、精确区间、逐事实关联、适用范围及权威边界。源码/旧数据保留基线；索引Chunk ID不作Golden身份。
3. **独立审题**：先只给问题与正式原文，提取请求、条件、可答与不可答部分。严格区分‘原文提到’与‘本题必须回答’；辅助记录不是新的Golden或权威答案。
4. **双向语义对齐**：正式原文→Golden逐关键断言支持；Question→Golden与评分点逐请求覆盖。不因复制原文就通过，也不因缺少未被问到的事实而扣分。
5. **场景必要性**：每个跨文档题检查所选来源的全文，寻找单篇充分解；共同判断与多个子任务按明确口径区分。文档数和字符串删源仅作提示。
6. **评分规则门禁**：评分点必须是题目必要语义，可独立判断，保留条件/例外，区分必答与可选例子。用少量正确简答、正确同义答案及关键错误答案逐答案实际评分；任意正确核心答案因无关硬点失败，阻断原样放行。
7. **成熟框架评分**：固定G-Eval步骤、Rubric、参数、模型、版本和费用预算；0/1/2分之外必须有证据、理由、confidence和动作。禁止把一次自由评论冒充框架结果。
8. **有限复核**：只对事实支持、问答错位、跨文档必要性等高风险做一次改变呈现的复核；保留主审和复核。模型不一致先标不确定，不把所有不确定推给用户，也不强判PASS。
9. **数据集汇总**：分开报告核心门槛、场景有效性、难度估计、表达属性、近义/重复与缺口。自动放行状态与正式RAG分数完全分开。
10. **留痕与交付**：保存原Prompt、输出、Token、异常、解析修复、重试和受保护输入检查。只把真正有原文冲突且影响核心定义的业务问题列入needs_decision。

## 最有价值的确定性检查

- 原文文件哈希与行号/字符偏移回放，及事实→证据→评分点引用完整性。
- JSONL/CSV往返和唯一ID，保证审计的就是用户交付版本。
- 题设数值与规则数值分开追踪，不把用户给定的五年周期当成必须出现在原文的制度数字。
- 支持引文用未加展示行号的正式原文核对；局部事实编号不可跨审题清单强行对齐。
- 阶段输出结构使用完整Schema校验；ReasonScore之外的证据、置信度、动作、探针逐点结果也必须校验，避免模型省略后仍被视为完成。
- 正确简答与错误边界答案的反事实评分，A01显示其能发现整体质量评价未发现的过严问题。

## 容易系统性偏差的环节

- 完整原文给Judge后，它可能要求回答文档所有内容。独立审题也会过度抽取，必须再次与Question限定范围。
- 对UNANSWERABLE套用‘有实体知识才有测试价值’会误删有效缺证题；缺证行为支持与缺失的实体政策应分开建模。
- ‘新引入’不能自动等于实时数据库new；规则咨询也不能被强行改成实际业务状态查询。
- 反向生成会过多复用标准例子和已命名知识术语；需要真实用户样本与同意图不同表达的对照，才有分布代表性证据。
- 把多项列举全部硬绑定，会拒绝正确简答；但机械拆成过多评分点也可能扩大无关要求，应围绕本题必要判断而非原文所有词项。
- 两份来源都正确不代表都不可替代；C/D不一致可能来自分类口径，而非业务规则冲突。
- 同一模型改顺序后的意见一致只减少部分锚定，不能消除模型家族偏差。不能拿两个意见一致当真值。

## 最小放行条件

来源确定性检查通过；D1/D2/D6满足门槛且无相应不确定性；必要场景真实成立；正确同义及简答不因无关要求失败；典型错误不被接受；来源与回答权威边界完整。任一关键项失败，不用总分平均补偿。普通可修正设计问题留优化，不自动升级为需要用户决策。

## 后续仍需实际RAG才能验证

不同Chunk/Embedding/Top-K下的必要证据召回、生成忠实性、困难题真实区分度、参数间排名稳定性及Agent工具只读行为。Dataset审计无法替代这些实验，也不能证明生产用户分布代表性。
"""
    (report / "skill_workflow_recommendations.md").write_text(workflow, encoding="utf-8")
    cfg = json.loads((report / "judge_config.json").read_text(encoding="utf-8"))
    cfg.update(audit_status="COMPLETE", usage=audit.usage_summary(),
        primary_results=276, blind_requirements_records=46, selective_review_cases=11, selective_review_dimension_results=33,
        additional_objective_mismatch_review=1, scoring_rule_quality_probes=3, individual_answer_probes=7,
        targeted_point_evidence_binding_probes=2,
        repaired_blind_json_records=len(list((run/"repaired").glob("*.json"))),
        transport_retries=len(list((run/"calls").glob("*__retry1.json"))),
        raw_exceptions_retained=str(run/"exceptions.jsonl"),
        aggregation="No averages: critical gates plus conservative review handling; original and final score origins explicit.",
        citation_validation_correction="Remove display-only line annotations from matching inputs; preserve original verification and warnings, no rescoring.",
        secondary_probe_rubric=str(report/"scoring_probe_rubric.json"),
        audit_script_sha256=audit.sha(Path(audit.__file__).read_bytes()),
        reporting_script_sha256=audit.sha(Path(__file__).read_bytes()), protected_inputs_unchanged=True)
    actual_models = {json.loads(p.read_text(encoding="utf-8")).get("actual_model") for p in (run/"calls").glob("*.json")}
    cfg["server_returned_model_names"] = sorted(x for x in actual_models if x)
    audit.save(report / "judge_config.json", cfg)
    print(json.dumps(dict(classifications=dict(classifications), quality_gate=stats["quality_gate_eligible"],
        usable_current_labels=stats["usable_current_labels"], dimensions={d:dict(v) for d,v in dimensions.items()},
        probe_false_rejections=len(failed_probes), usage=audit.usage_summary()), ensure_ascii=False))
    return rows
