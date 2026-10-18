"""统一报告按模块保留各自评分含义，不平均不同模块通过率。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from .unified_adapters import save
from .metrics import calculate_metrics


def rate(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator, "value": numerator / denominator if denominator else None}


def shown(value):
    return f"{value['numerator']}/{value['denominator']}（{value['value'] * 100:.2f}%）" if value["value"] is not None else "N/A"


def summarize(state):
    modules = {}
    for module in ("agent", "backend", "ir", "rag"):
        records = [r for r in state["records"].values() if r["module"] == module]
        counts = Counter(r.get("final_result", "NOT_RUN") for r in records)
        decisive = counts["PASS"] + counts["FAIL"]
        origin = Counter(r.get("provenance", "NOT_RUN") for r in records)
        completed = sum(r.get("execution_status") == "COMPLETED_AWAITING_SCORING" for r in records)
        causes = Counter()
        for record in records:
            if record.get("final_result") == "PASS":
                continue
            score = record.get("score", {})
            cause = score.get("attribution", {}).get("primary") or (score.get("original_ir_result") or {}).get("root_cause")
            causes[cause or ("EXECUTION_OR_EVIDENCE" if record.get("final_result") not in {"FAIL", "REVIEW_REQUIRED"} else "SUCCESS_CRITERION")] += 1
        extra = {}
        if module == "ir":
            for name in ("intent", "route", "tool_selection", "confirmation_and_write_safety"):
                measured = [(r.get("score", {}).get("original_ir_result") or {}).get("checks", {}) for r in records]
                applicable = [c[name] for c in measured if c.get(name) is not None]
                extra[name] = rate(sum(v is True for v in applicable), len(applicable))
        if module == "rag":
            for name in ("retrieval", "correctness", "faithfulness", "insufficiency"):
                values = [r.get("score", {}).get("metrics", {}).get(name) for r in records]
                values = [v for v in values if v and v.get("status") != "NOT_APPLICABLE"]
                extra[name] = {"statuses": dict(Counter(v.get("status") for v in values)),
                               "pass_over_applicable": rate(sum(v.get("status") == "PASS" for v in values), len(values))}
        modules[module] = {"planned": len(records), "completed_execution": completed, "status_counts": dict(counts),
                           "provenance_counts": dict(origin), "pass_over_decisive": rate(counts["PASS"], decisive),
                           "pass_over_planned": rate(counts["PASS"], len(records)), "primary_attribution": dict(causes),
                           "module_specific_metrics": extra}
    pending = sum(r.get("final_result") not in {"PASS", "FAIL"} for r in state["records"].values())
    failed = sum(r.get("final_result") == "FAIL" for r in state["records"].values())
    native = calculate_metrics([r["score"] for r in state["records"].values() if r.get("score") and r["module"] in {"agent", "backend"}])
    return {"modules": modules, "native_metrics": native, "selection_count": len(state["records"]), "current_dataset_total": 120,
            "is_full_selection": len(state["records"]) == 120, "pending_count": pending, "failed_count": failed,
            "overall_score": None, "overall_score_reason": "四模块成功条件不同，不简单平均通过率",
            "conclusion": "完整纳入现行120条；仍存在未通过或待核实项" if len(state["records"]) == 120 and (failed or pending)
                          else "完整纳入现行120条，各模块已通过" if len(state["records"]) == 120 else "局部执行链验证，不是全量评测"}


def render(state, report_dir: Path):
    metrics = summarize(state)
    save(report_dir / "metrics.json", metrics)
    save(report_dir / "manifest.json", {"batch_id": state["batch_id"], "run_dir": state["run_dir"],
                                       "scope": state["scope"], "fingerprint": state["fingerprint"], "state_path": state["state_path"]})
    records = list(state["records"].values())
    with (report_dir / "case_results.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = ["# 采购 Agent 统一评测报告", "", f"批次：`{state['batch_id']}`；开始时间：{state['started_at']}。",
             f"结论：{metrics['conclusion']}。", "",
             "## 执行范围与版本", "", "本轮范围为Agent33 + Backend20 + IR21 + RAG46，共120条；旧RAG42及Legacy10排除。",
             "RAG采用rag-golden-1.2.1-accepted-candidate，按已验收的REQUIRED/OPTIONAL及知识只读规则评分。",
             "四模块分别保留：Agent任务Strict Pass、Backend业务规则、IR意图/路由/工具/安全、RAG答案/忠实性/不足处理/路由规则。",
             f"模型及配置：`{state['model_config']}`。冻结指纹：`{state['fingerprint']}`。",
             f"输入哈希与完整清单：`{state['run_dir']}/frozen_inputs.json`、`{state['run_dir']}/case_catalog.json`。", "",
             "## 各模块结果", "", "|模块|计划|完整执行证据|本批实际执行|历史复用|PASS|FAIL|待核实/未完成|PASS/确定结论|PASS/计划|",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for name, value in metrics["modules"].items():
        status = value["status_counts"]
        lines.append(f"|{name}|{value['planned']}|{value['completed_execution']}|{value['provenance_counts'].get('EXECUTED_THIS_BATCH',0)}|{value['provenance_counts'].get('HISTORICAL_VALIDATED',0)}|{status.get('PASS',0)}|{status.get('FAIL',0)}|{value['planned']-status.get('PASS',0)-status.get('FAIL',0)}|{shown(value['pass_over_decisive'])}|{shown(value['pass_over_planned'])}|")
    lines += ["", "REVIEW_REQUIRED、缺证、无效证据、环境未就绪及运行异常独立展示，排除于确定结论分母；PASS/计划同时列出，避免只看已判定样本造成误读。没有总体能力平均分。",
              "", "## 模块指标与异常归因", ""]
    for name, value in metrics["modules"].items():
        lines += [f"### {name}", "", f"状态分布：`{value['status_counts']}`。",
                  f"证据来源：`{value['provenance_counts']}`。主要归因：`{value['primary_attribution']}`。",
                  f"模块分项：`{value['module_specific_metrics']}`。", ""]
    lines += ["### 复用原Agent/Backend指标", "",
              f"Agent：`{metrics['native_metrics']['agent']}`。", "",
              f"Backend：`{metrics['native_metrics']['backend']}`。", "",
              "以上BCR/AVR沿用既有category代理指标，不代表逐条授权行为的精确发生率；IR安全检查和RAG无写入检查另行保留。", ""]
    lines += ["## 实际执行与证据复用", "",
              "EXECUTED_THIS_BATCH表示本次批次新运行；HISTORICAL_VALIDATED表示符合冻结配置/输入/源码/原始文件哈希的历史记录。",
              "恢复和再次调用会校验已保存产物并跳过完整Case；其原始运行出处不改变。每条记录保留cache_proof、result_path及完整artifacts。",
              "Agent/Backend/IR旧记录没有本统一入口的完整配置指纹时不自动复用。RAG独立基线经专门核验后可复用，其46条不能单独代表全量结果。", "",
              "## 安全与冻结验证", "", f"冻结校验：`{state.get('integrity',{})}`。",
              "Agent/IR只访问本轮独立SQLite与loopback模拟器；Backend角色写入只在新建隔离数据库执行。RAG阻断业务API，复制SQLite仅观测。",
              "未调整业务规则、Golden、知识库、索引、Prompt或模型参数以提高得分，未同步Airtable。服务端模型别名的实际权重版本不能由本地配置冻结保证。", "",
              "## 逐Case清单", "", "|模块:Case|执行来源|执行状态|评分|Evidence|", "|---|---|---|---|---|"]
    for record in records:
        path = str(record.get("evidence_path") or "").replace("\\", "/")
        link = f"[Evidence](<{path}>)" if path else "见异常记录"
        lines.append(f"|{record['key']}|{record.get('provenance','NOT_RUN')}|{record.get('execution_status','NOT_RUN')}|{record.get('final_result','NOT_RUN')}|{link}|")
    lines += ["", "## 未解决问题与使用限制", "",
              "- 人工复核、Judge引文/总分不一致及检索语义范围争议按原模块状态保留；统一调度不改变Golden或评分标准。",
              "- Backend B-09/B-10测试既有Harness确认生命周期；B-13的隔离粒度仍需按原规格审核，不能把Backend通过等同真实Agent门禁验收。",
              "- IR使用既有专项成功条件；RAG使用既有知识只读边界，API被阻断的路由偏离不是业务服务故障。",
              "- 本地固定模型配置及缓存字节可复现，真实模型仍有服务端变化和随机波动；RAG Judge与生成器同配置，存在同族偏差。",
              "- 单Case异常保留并继续其他Case；冻结输入变化则停止进一步执行并标记剩余项，避免混用不同基线。",
              "- 详细失败条件、实际观察、工具/业务状态和异常见case_results.jsonl、bad_cases.md及各Case原始证据。", ""]
    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    bad = ["# 统一评测未通过与待核实Case", ""]
    for record in records:
        if record.get("final_result") == "PASS":
            continue
        score = record.get("score", {})
        bad += [f"## {record['key']} · {record.get('final_result','NOT_RUN')}", "",
                f"来源：{record.get('provenance','NOT_RUN')}；Evidence：`{record.get('evidence_path','')}`。",
                f"评分/异常文件：`{record.get('result_path','')}`。", ""]
        for criterion in score.get("criteria", []):
            if criterion.get("status") != "PASS":
                bad.append(f"- {criterion.get('criterion_id')} [{criterion.get('status')}] {criterion.get('success_criterion')}：{criterion.get('reason') or criterion.get('observed')}；引用 `{criterion.get('evidence_refs',[])}`")
        bad.append(f"- 归因/失败：`{score.get('attribution') or score.get('failure_reasons') or record.get('error')}`")
        bad.append("")
    (report_dir / "bad_cases.md").write_text("\n".join(bad), encoding="utf-8")
    return metrics
