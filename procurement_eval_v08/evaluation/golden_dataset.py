"""独立 Golden 数据构建与离线验证；不导入 Agent、Backend 或 RAG。

运行：python -m evaluation.golden_dataset build|validate
只写新候选目录及其专属报告目录，不更新旧用例、索引、latest 报告或 Airtable。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path

from .golden_blueprints import BLUEPRINTS, SKIP_BLUEPRINTS

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
KB = ROOT / "采购Agent_正式知识库_v1"
DATA = ROOT / "评估case" / "rag_golden_v1"
REPORTS = PROJECT / "evaluation" / "reports" / "rag_golden_v1"
OVERVIEW = PROJECT / "reports" / "rag_golden_v1" / "采购知识库_全量知识总览.md"
LEGACY = ROOT / "评估case" / "下载采购 Agent RAG 42 Case 统一评估适配版 v2"
VERSION = "rag-golden-1.0.0-candidate"
ATTRIBUTES = ["direct", "colloquial", "implicit_condition", "numeric_boundary",
              "distracting_context", "conditions_exceptions", "partial_evidence",
              "unanswerable", "similar_knowledge"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def load_sources(root=KB):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    docs = manifest["documents"]
    if (manifest.get("total"), manifest.get("A"), manifest.get("B")) != (52, 11, 41):
        raise ValueError("正式manifest声明数量不符")
    if len(docs) != 52 or Counter(d["class"] for d in docs) != {"A": 11, "B": 41}:
        raise ValueError("正式文档数量/分类不符")
    if len({d["id"] for d in docs}) != 52 or len({d["path"] for d in docs}) != 52:
        raise ValueError("正式文档ID/路径重复")
    listed = {d["path"] for d in docs}
    actual = {p.relative_to(root).as_posix() for p in (root / "knowledge").rglob("*.md")}
    if actual != listed:
        raise ValueError("正式Markdown集合与manifest不一致")
    sources = {}
    for d in docs:
        path = (root / d["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("正式来源路径越界")
        raw = path.read_bytes()
        text = raw.decode("utf-8")  # 不做换行正规化，字符偏移可精确回放。
        if not d.get("index_allowed") or sha(raw) != d["reviewed_sha256"]:
            raise ValueError(f"{d['id']} 未放行或哈希不一致")
        header = text.split("---", 2)[1]
        identity = re.search(r"^(?:doc_id|id):\s*['\"]?([^'\"\r\n]+)", header, re.M)
        if (not identity or identity.group(1) != d["id"] or "review_status: approved_for_index" not in header
                or f"review_class: {d['class']}" not in header or "index_allowed: true" not in header):
            raise ValueError(f"{d['id']} front matter不符")
        sources[d["id"]] = {**d, "text": text, "hash": sha(raw), "lines": text.splitlines(keepends=True)}
    return sources


def locate(doc, start, end):
    lines = doc["lines"]
    if not (1 <= start <= end <= len(lines)):
        raise ValueError("证据行号越界")
    a = sum(map(len, lines[:start - 1]))
    body = "".join(lines[start - 1:end]).rstrip("\r\n")
    b = a + len(body)
    section = "适用范围"
    unit = 0
    for line in lines[:start]:
        if line.startswith("## "):
            section = line[3:].strip()
            unit += 1
    return dict(
        doc_id=doc["id"], title=doc["title"], section=section,
        knowledge_unit_id=f"{doc['id']}:S{unit}", source_path=doc["path"],
        source_class=doc["class"], evidence_text=body,
        source_span=dict(char_start=a, char_end=b, line_start=start, line_end=end,
                         convention="utf8_decoded_unicode_codepoints_zero_based_half_open_preserve_newlines"),
        source_hash=doc["hash"], evidence_hash=sha(body.encode("utf-8")),
    )


def render_answer(case):
    parts = []
    if case["missing_information"]:
        parts.append("当前知识资料不足以确认：" + "；".join(case["missing_information"]) +
                     "。这不等于相关规则不存在，也不能据此作出确定业务结论。")
    # 原文适用范围一并呈现，不能凭A/B标签把建议升级为强制规则。
    for e in case["golden_evidence"]:
        if e["role"] == "scope":
            parts.append(f"适用边界〔{e['doc_id']}〕：{e['evidence_text']}")
    for fact in case["required_facts"]:
        e = next(e for e in case["golden_evidence"] if e["evidence_id"] == fact["evidence_ids"][0])
        parts.append(f"{fact['text']}〔{e['title']} / {e['section']}〕")
    return "\n\n".join(parts)


def checker_spec(case):
    facts = [f["fact_id"] for f in case["required_facts"]]
    spec = {
        "integration_status": "SPEC_ONLY_NOT_EXECUTED",
        "source_evidence_coverage": {"required": True, "unit": "fact_source_span", "fact_ids": facts,
            "rule": "按同一source_hash和原文区间映射核验事实证据；文档ID命中不计事实覆盖。"},
        "top_k_necessary_evidence_recall": {"required": case["answerability"] != "UNANSWERABLE",
            "k": "runtime_parameter", "denominator": "required_fact_count",
            "rule": "每个事实的完整证据区间被前K条经验证映射的区间并集覆盖才算召回；作用域单列，不伪造必要多文档。",
            "unmapped_status": "EVIDENCE_MISSING"},
        "cross_document_fact_coverage": {"required": case["scenario_type"] in ["C", "D"],
            "rule": "分别检查每份必要来源中的指定事实，不能只数distinct doc_id。"},
        "answer_correctness": {"required": True, "fact_ids": facts,
            "rule": "按required_judgment评价必要判断，保持原文条件、否定、数值及范围。接受同义表述和有据的不同专业方案；不要求复述段落所有无关细节。",
            "forbidden_claims": case["forbidden_claims"], "uncertain_status": "REVIEW_REQUIRED"},
        "answer_faithfulness": {"required": True,
            "rule": "回答新增的每个关键事实也须有可靠证据；不得用Golden字符串相似度代替事实核验。"},
        "insufficient_evidence": {"required": case["answerability"] != "FULL",
            "missing_information": case["missing_information"],
            "rule": "回答有据部分，明确不能确定的请求及可补充来源；不得把未提及当作不存在。"},
        "agent_tool_route": {"required": True, "route_family": "knowledge_read_only",
            "expected_action": "READ", "expected_tool": "search_knowledge",
            "reuse": "evaluation.checker.checks.check_route",
            "evidence": "steps/N/agent_output与TOOL_CALL_COMPLETED.sanitized_result.hits或KNOWLEDGE_RETRIEVED；不得只查STARTED事件。",
            "requirement": "仅Agent层适用；实际route_reason须由后续适配器按当前版本绑定，不硬编码未知reason。"},
        "no_business_write": {"required": True, "allow_write": False,
            "reuse": ["evaluation.checker.checks.no_write_observation", "evaluation.checker.checks.suppliers_unchanged", "evaluation.checker.checks.common_no_write_violation"],
            "precondition": "未来Agent评估使用隔离只读fixture；只核对本次新增审计事件，排除fixture既有历史事件。",
            "rule": "前后业务状态/供应商表无变化、无写事件、无确认写卡；不能只相信模型说未写入。"},
        "result_contract": {"evidence": "eval-evidence-1.0", "scored": "scored-result-1.0",
            "criterion_statuses": ["PASS", "FAIL", "EVIDENCE_MISSING", "REVIEW_REQUIRED"],
            "note": "AUTO_VALIDATED是数据质量状态，不是模型执行PASS；无证据不得算通过。"},
    }
    return spec


def build_case(bp, sources):
    # 依次落实原文证据、事实、Golden和约束，最后绑定问题；禁止从检索结果造证据。
    evidence, facts, conditions = [], [], []
    for i, item in enumerate(bp["facts"], 1):
        doc = sources[item["doc_id"]]
        e = locate(doc, item["line_start"], item["line_end"])
        if item["line_start"] != item["line_end"] or not e["evidence_text"].strip():
            raise ValueError("本MVP只采用完整非空原文段落行，不使用截断句子")
        if e["evidence_text"].startswith(("#", "---", ">", "- 外部", "- 参考")):
            raise ValueError("必要事实不能是标题、来源URL或作用域声明")
        e.update(evidence_id=f"E{i}", fact_id=f"F{i}", role="fact")
        evidence.append(e)
        facts.append(dict(fact_id=f"F{i}", text=e["evidence_text"], evidence_ids=[f"E{i}"],
                          required_judgment=item["role"], source_class=doc["class"],
                          authority="按原文适用范围及措辞，不把B类专业建议升级为公司制度"))
        conditions.append(dict(question_text=item["question_anchor"], fact_ids=[f"F{i}"],
                               treatment="source_supported_judgment"))
    for doc_id in dict.fromkeys(e["doc_id"] for e in evidence):
        doc = sources[doc_id]
        line = next(i for i, s in enumerate(doc["lines"], 1) if s.startswith("> 适用"))
        e = locate(doc, line, line)
        e.update(evidence_id=f"S{len([x for x in evidence if x['role'] == 'scope']) + 1}",
                 fact_id=None, role="scope")
        evidence.append(e)
    classes = {e["source_class"] for e in evidence}
    case = dict(case_id=bp["id"], scenario_type=bp["scenario"],
                knowledge_type="MIXED" if len(classes) > 1 else next(iter(classes)),
                theme=bp["theme"], attributes=bp["attributes"],
                required_facts=facts, golden_evidence=evidence,
                forbidden_claims=bp["forbidden_claims"], answerability=bp["answerability"],
                expected_behavior=bp["expected_behavior"], question_conditions=conditions,
                missing_information=bp["missing_information"],
                cross_document_reason=bp["cross_document_reason"],
                fixture=None, real_time_dependency=bp["real_time_dependency"],
                gold_status="AUTO_VALIDATED", case_version=VERSION,
                provenance=dict(blueprint_sha256=sha(canonical(bp)),
                    source="formal_manifest_and_markdown_only", overview_path=str(OVERVIEW.relative_to(ROOT)).replace("\\", "/"),
                    overview_sha256=sha(OVERVIEW.read_bytes()),
                    construction="source_evidence -> required_facts -> extractive_golden -> constraints -> question",
                    semantic_review="No independent semantic model or human review performed"))
    case["golden_answer"] = render_answer(case)
    case["checker_spec"] = checker_spec(case)
    case["question"] = bp["question"]
    return case


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": required or list(properties), "additionalProperties": False}


def arr(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


def shape_schema(value):
    if isinstance(value, dict): return obj({k: shape_schema(v) for k, v in value.items()})
    if isinstance(value, list): return arr(shape_schema(value[0]) if value else {})
    return {"type": {str: "string", bool: "boolean", int: "integer", type(None): "null"}[type(value)]}


def schema():
    s = {"type": "string", "minLength": 1}
    strings = arr(s)
    evidence = obj({**{k: s for k in ["evidence_id", "doc_id", "title", "section", "knowledge_unit_id", "source_path", "evidence_text", "source_hash", "evidence_hash"]},
        "fact_id": {"type": ["string", "null"]}, "role": {"enum": ["fact", "scope"]},
        "source_class": {"enum": ["A", "B"]},
        "source_span": obj({**{k: {"type": "integer", "minimum": 0} for k in ["char_start", "char_end"]},
            **{k: {"type": "integer", "minimum": 1} for k in ["line_start", "line_end"]}, "convention": s})})
    fact = obj(dict(fact_id=s, text=s, evidence_ids=arr(s, 1), required_judgment=s,
                    source_class={"enum": ["A", "B"]}, authority=s))
    p = dict(case_id=s, scenario_type={"enum": ["A", "B", "C", "D"]},
        knowledge_type={"enum": ["A", "B", "MIXED"]}, theme=s,
        attributes=arr({"enum": ATTRIBUTES}, 1), question=s, golden_answer=s,
        required_facts=arr(fact, 1), golden_evidence=arr(evidence, 2),
        forbidden_claims=arr(s, 1), answerability={"enum": ["FULL", "PARTIAL", "UNANSWERABLE"]},
        expected_behavior=arr(s, 1),
        question_conditions=arr(obj(dict(question_text=s, fact_ids=arr(s, 1), treatment={"const": "source_supported_judgment"})), 1),
        missing_information=strings, cross_document_reason={"type": "string"},
        fixture={"type": "null"}, real_time_dependency={"const": False},
        gold_status={"enum": ["AUTO_VALIDATED", "SKIPPED", "NEEDS_DECISION"]},
        case_version={"const": VERSION},
        provenance=obj({k: s for k in ["blueprint_sha256", "source", "overview_path", "overview_sha256", "construction", "semantic_review"]}),
        checker_spec=shape_schema(checker_spec(dict(required_facts=[{"fact_id": "F1"}],
            answerability="FULL", scenario_type="A", forbidden_claims=["example"], missing_information=["example"])) ))
    # Checker是版本化确定性模板：校验器逐字段与checker_spec(case)比较。
    result = obj(p)
    result.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Independent procurement RAG Golden candidate", "$comment": "Schema用于Golden Case；skipped_cases为审计排除记录，可无golden。额外语义及跨字段检查由golden_dataset.py执行。"})
    return result


def check_schema(value, spec, path="$", errors=None):
    """执行本交付schema使用的JSON Schema子集，避免为离线校验新增依赖。"""
    errors = [] if errors is None else errors
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    names = spec.get("type", [])
    names = [names] if isinstance(names, str) else names
    if names and not any(type(value) is types[n] for n in names):
        errors.append(f"{path}: type"); return errors
    if "enum" in spec and value not in spec["enum"]: errors.append(f"{path}: enum")
    if "const" in spec and value != spec["const"]: errors.append(f"{path}: const")
    if isinstance(value, str) and len(value) < spec.get("minLength", 0): errors.append(f"{path}: minLength")
    if type(value) is int and value < spec.get("minimum", value): errors.append(f"{path}: minimum")
    if isinstance(value, dict):
        for key in spec.get("required", []):
            if key not in value: errors.append(f"{path}.{key}: missing")
        props = spec.get("properties", {})
        if spec.get("additionalProperties") is False:
            for key in value.keys() - props.keys(): errors.append(f"{path}.{key}: unexpected")
        for key in value.keys() & props.keys(): check_schema(value[key], props[key], f"{path}.{key}", errors)
    if isinstance(value, list):
        if len(value) < spec.get("minItems", 0): errors.append(f"{path}: minItems")
        for i, item in enumerate(value): check_schema(item, spec.get("items", {}), f"{path}[{i}]", errors)
    return errors


def validate_case(case, sources, bp=None):
    location, semantic = [], []
    errors = check_schema(case, schema())
    if errors:
        return dict(case_id=case.get("case_id"), status="FAIL", errors=errors,
                    source_location_checks=location, semantic_support_checks=semantic)
    by_e = {e["evidence_id"]: e for e in case["golden_evidence"]}
    if len(by_e) != len(case["golden_evidence"]): errors.append("duplicate_evidence_id")
    for e in case["golden_evidence"]:
        doc = sources.get(e["doc_id"])
        ok = False
        try:
            span = e["source_span"]
            expected = locate(doc, span["line_start"], span["line_end"])
            ok = all(e[k] == v for k, v in expected.items())
            ok = ok and doc["text"][span["char_start"]:span["char_end"]] == e["evidence_text"]
        except (TypeError, KeyError, ValueError):
            pass
        location.append(dict(evidence_id=e["evidence_id"], status="PASS" if ok else "FAIL",
                             checks=["released_source", "file_sha256", "span_and_line_roundtrip", "exact_quote", "section_and_class", "evidence_sha256"]))
        if not ok: errors.append(f"source_mismatch:{e['evidence_id']}")
    for fact in case["required_facts"]:
        refs = [by_e.get(i) for i in fact["evidence_ids"]]
        ok = len(refs) == 1 and refs[0] is not None
        ok = ok and refs[0]["role"] == "fact" and refs[0]["fact_id"] == fact["fact_id"]
        ok = ok and fact["text"] == refs[0]["evidence_text"] and fact["source_class"] == refs[0]["source_class"]
        semantic.append(dict(fact_id=fact["fact_id"], status="PASS_EXTRACTIVE_SUPPORT" if ok else "FAIL",
            method="full_source_paragraph_identity", required_judgment=fact["required_judgment"],
            limitation="逐字完整段落保证未改写数值/否定/例外；不独立证明问题相关性或专业方案完备性。"))
        if not ok: errors.append(f"unsupported_fact:{fact['fact_id']}")
    fact_ids = {f["fact_id"] for f in case["required_facts"]}
    if len(fact_ids) != len(case["required_facts"]): errors.append("duplicate_fact_id")
    if {e["fact_id"] for e in by_e.values() if e["role"] == "fact"} != fact_ids:
        errors.append("orphan_fact_evidence")
    covered = set()
    for condition in case["question_conditions"]:
        if condition["question_text"] not in case["question"] or not set(condition["fact_ids"]) <= fact_ids:
            errors.append("question_condition_unbound")
        covered.update(condition["fact_ids"])
    if covered != fact_ids: errors.append("question_fact_coverage_incomplete")
    if any(x not in case["question"] for x in case["missing_information"]): errors.append("missing_request_not_in_question")
    if (case["answerability"] == "FULL") != (not case["missing_information"]): errors.append("answerability_mismatch")
    if case["golden_answer"] != render_answer(case): errors.append("golden_contains_unmapped_or_changed_claim")
    if case["checker_spec"] != checker_spec(case): errors.append("checker_contract_changed")
    if case["real_time_dependency"] or case["fixture"] is not None: errors.append("realtime_fixture_not_supported_in_static_mvp")
    factual = [e for e in by_e.values() if e["role"] == "fact"]
    doc_ids = {e["doc_id"] for e in factual}
    scopes = [e for e in by_e.values() if e["role"] == "scope"]
    if {e["doc_id"] for e in scopes} != doc_ids or len(scopes) != len(doc_ids): errors.append("scope_coverage_incomplete")
    if any(e["fact_id"] is not None for e in scopes): errors.append("scope_counted_as_fact")
    classes = {e["source_class"] for e in factual}
    if case["knowledge_type"] != ("MIXED" if len(classes) > 1 else next(iter(classes))): errors.append("knowledge_authority_mismatch")
    n, count, scenario = len(factual), len(doc_ids), case["scenario_type"]
    if (scenario == "A" and (n != 1 or count != 1) or scenario == "B" and (n < 2 or count != 1)
            or scenario in ["C", "D"] and (n < 2 or count < 2 or not case["cross_document_reason"])):
        errors.append("scenario_cardinality_or_necessity_mismatch")
    if len({e["evidence_hash"] for e in factual}) != n: errors.append("duplicate_source_fact")
    ablations = []
    if scenario in ["C", "D"]:
        for doc_id in sorted(doc_ids):
            lost = [e["fact_id"] for e in factual if e["doc_id"] == doc_id and
                    not any(e["evidence_text"] in sources[other]["text"] for other in doc_ids - {doc_id})]
            ablations.append(dict(removed_doc=doc_id, lost_unique_exact_facts=lost))
            if not lost: errors.append("redundant_cross_document_source:" + doc_id)
    # 数字必须来自证据或明确的背景。当前题库没有数值背景，故直接拒绝无据数字。
    question_numbers = set(re.findall(r"\d+(?:\.\d+)?", case["question"]))
    source_numbers = set(re.findall(r"\d+(?:\.\d+)?", "\n".join(e["evidence_text"] for e in factual)))
    if question_numbers - source_numbers: errors.append("question_numeric_condition_not_in_evidence")
    if bp is not None:
        if case["answerability"] == "UNANSWERABLE":
            terms = bp.get("unknown_topic_terms", [])
            matches = {term: [doc_id for doc_id, doc in sources.items() if term in doc["text"]] for term in terms}
            if not terms or any(matches.values()): errors.append("unanswerable_topic_requires_reselection")
            semantic.append(dict(method="frozen_full_corpus_topic_probe", searched_documents=len(sources), matches=matches,
                status="PASS_BOUNDED_INSUFFICIENCY" if terms and not any(matches.values()) else "FAIL",
                limitation="关键词无命中只是固定资料不足的保守辅助，绝不是证明现实企业不存在制度。"))
        if case != build_case(bp, sources): errors.append("case_differs_from_versioned_blueprint")
    semantic.append(dict(status="PASS_CONSTRAINED_CHECKS" if not errors else "FAIL",
        checks=["golden_only_mapped_source_paragraphs_and_scope", "question_anchor_bindings", "numeric_tokens_preserved",
                "scenario_cardinality", "per_document_exact_fact_ablation", "authority_scope_preserved"],
        cross_document_ablation=ablations,
        independent_semantic_review="NOT_PERFORMED",
        limitations="没有将生成模型自评当作正确性证明。结构化删源检查不等于语义不可替代性证明；自然语言漏条件/近义冗余仍可能存在。"))
    return dict(case_id=case["case_id"], status="PASS" if not errors else "FAIL", errors=errors,
                source_location_checks=location, semantic_support_checks=semantic)


def snapshot_protected():
    files = [KB / "manifest.json", KB / "manifest.csv", KB / "README.md"]
    files += list((KB / "knowledge").rglob("*.md"))
    files += [p for p in LEGACY.iterdir() if p.is_file()]
    files += [PROJECT / p for p in ["agent_runtime.py", "agent_tools.py", "agent_server.py", "backend.py", "knowledge_rag.py", "build_knowledge_index.py"]]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p.read_bytes()) for p in files}


def csv_text(cases):
    buffer = io.StringIO(newline="")
    fieldnames = list(cases[0]) if cases else []
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in cases:
        writer.writerow({k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in row.items()})
    return buffer.getvalue()


def validate_csv(cases, path):
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
    if len(rows) != len(cases): return False
    for original, row in zip(cases, rows):
        if set(original) != set(row): return False
        restored = {k: row[k] if isinstance(v, str) else json.loads(row[k]) for k, v in original.items()}
        if restored != original: return False
    return True


def overview_check(sources):
    overview = OVERVIEW.read_text(encoding="utf-8")
    missing = []
    for d in sources.values():
        if d["hash"] not in overview or f"### {d['id']}｜" not in overview:
            missing.append(d["id"])
        # 每个正式正文段落都须出现在完整总览中，防止只列文档名声称全量覆盖。
        end_front = d["text"].find("---", 3)
        body = d["text"][end_front + 3:]
        for line in body.splitlines():
            if line.strip() and line.strip() not in overview: missing.append(d["id"] + ":body")
    return sorted(set(missing))


def write_reports(result, sources, cases, skipped):
    """只更新本候选版本报告，不触碰既有评估报告或latest指针。"""
    old = read_jsonl(LEGACY / "cases_v2.jsonl")
    old_docs = {d for c in old for d in c["retrieval"]["candidate_evidence_doc_ids"]}
    new_docs = set(result["case_evidence_documents"])
    count = result["counts"]
    unique_spans = {(e["doc_id"], e["source_span"]["char_start"], e["source_span"]["char_end"])
                    for c in cases for e in c["golden_evidence"] if e["role"] == "fact"}
    lines = ["# RAG Golden Evaluation Dataset 覆盖与自动校验报告", "",
        f"日期：2026-10-14；版本：`{VERSION}`；交付状态：独立候选版本。", "",
        "## 结论与范围", "",
        f"正式知识库52篇全部放行且文件哈希匹配（A类11篇、B类41篇），全量总览覆盖52/52篇及其全部正文非空行。"
        f"本轮共考虑{count['candidates']}个场景候选：AUTO_VALIDATED {len(cases)}，SKIPPED {len(skipped)}，NEEDS_DECISION 0。", "",
        f"必要事实{result['fact_count']}项，跨用例去重原文事实区间{len(unique_spans)}个；含适用范围的证据记录{result['evidence_count']}项。"
        f"可用Case实际引用{len(new_docs)}/52篇正式原文（{len(new_docs)/52:.2%}）。未强制为每篇文档生成题目。", "",
        "AUTO_VALIDATED仅表示通过本版本的确定性来源、抽取支持和结构约束。未进行人工逐题核验，也未进行独立语义模型复核；不能解释为100%语义正确。没有运行被测RAG、正式模型评测或Airtable同步，不能据此报告RAG成功率。", "",
        "## 环境与复用", "",
        f"- 正式来源：`{KB}`；唯一清单：`manifest.json`，核对`reviewed_sha256`，未使用original_sha256充当放行版本。",
        f"- 新Case及Schema：`{DATA}`；与旧42条用例目录并列，未覆盖旧文件。",
        f"- 总览：`{OVERVIEW}`；普通报告按项目约定留在`reports/`。",
        f"- 校验结果及本报告：`{REPORTS}`。",
        "- 当前正式Agent/Backend数据仍为`评估case/procurement_eval_v4/`。现有CaseLoader严格使用五字段Case并依赖execution_scenarios、fixture_specs，新Golden JSONL不能直接喂入现有Runner。",
        "- 复用已有eval-evidence-1.0、scored-result-1.0结果语义，checker_spec列明check_route/no_write_observation/suppliers_unchanged/common_no_write_violation。仅声明后续适配要求，没有重新开发Runner或改旧Loader。",
        "- 当前切块器不保留精确原文字符区间。后续须将实际检索文本映射回固定source_hash的原文区间；未映射时为EVIDENCE_MISSING，不能以doc_id命中代替事实召回。", "",
        "## 场景矩阵", "", "| 场景 | 候选 | AUTO_VALIDATED | SKIPPED | NEEDS_DECISION |", "|---|---:|---:|---:|---:|"]
    labels = {"A": "单文档、单知识单元", "B": "单文档、多知识点", "C": "跨文档、共同判断", "D": "跨文档、多知识点"}
    for typ in "ABCD":
        cand, valid = count["scenario_candidates"].get(typ, 0), count["scenario_valid"].get(typ, 0)
        lines.append(f"| {typ} {labels[typ]} | {cand} | {valid} | {cand-valid} | 0 |")
    lines += ["", "A类场景指一个完整知识单元，可含该规则不可分割的条件/例外；不是把每个词拆成一条事实。场景A/B与来源A/B是两个不同维度。", "",
              "| 附加属性 | 可用Case数（可重叠） |", "|---|---:|"]
    for key, val in result["attributes"].items(): lines.append(f"| {key} | {val} |")
    lines += ["", "| 可回答性 | 数量 |", "|---|---:|"]
    for key, val in result["answerability"].items(): lines.append(f"| {key} | {val} |")
    lines += ["", "partial_evidence属性也包括‘怎样处理缺失报价项’这类完全可回答的方法题；因此其数量不必等于PARTIAL。UNANSWERABLE表示请求的公司具体制度不可由固定知识包确认，仍有可靠的缺证处理规则；不表示该制度现实中不存在。", "",
        "## 逐项自动校验与边界", "",
        "| 检查 | 实施 | 本轮结果 |", "|---|---|---|",
        "| 正式来源 | 清单唯一性、52文件集合、11/41分类、front matter放行、原始字节SHA-256 | 52/52 |",
        f"| 原文证据 | 文件哈希、证据哈希、精确行号/字符偏移、原文逐字回放、章节及类别 | {result['evidence_count']}/{result['evidence_count']}；来源完整率100% |",
        f"| Required Facts支持 | 每项采用完整原文段落，逐字恒等；不截掉否定、数字、例外 | {result['fact_count']}/{result['fact_count']} |",
        "| Golden忠实性 | 只允许事实段落、原文适用范围及固定缺证说明组成；额外关键断言拒绝 | 通过 |",
        "| 问题条件 | 每个必要判断绑定问题原句；数值在事实中存在；缺失请求与answerability对应 | 通过结构检查，不证明语言理解绝对完备 |",
        "| 跨文档必要性 | 每份来源贡献独立事实、删源损失精确事实、作用域不计必要来源；另附业务理由 | C/D共13条通过；近义冗余仍需警惕 |",
        "| 权威性 | A/B分类来自manifest，随证据保留原文适用范围，全文原句保留情态 | 不把B类普遍升级为强制规则 |",
        "| 未提及规则 | 未知制度用固定不足说明及支持该行为的原文，不作存在性否定 | 2条UNANSWERABLE |",
        "| 实时状态 | 静态MVP不纳入real_time_dependency=true，无Fixture则拒绝 | 8个旧场景的实时/业务依赖排除 |",
        "| 输出一致性 | JSON Schema所用子集、跨字段引用、唯一ID、CSV嵌套字段/多行往返、构建输入及产物哈希 | 通过 |", "",
        "原文定位与支持性检查分别记录在validation_results.json中的source_location_checks、semantic_support_checks。支持性方法是受约束的抽取恒等与条件绑定，并非用生成AI自评证明正确。必要判断的自然语言相关性、暗含条件是否穷尽、跨文档的语义不可替代性仍存在自动校验边界。", "",
        "## 跨文档Case的业务必要性", "", "| Case | 类型 | 必要性说明 |", "|---|---|---|"]
    for c in cases:
        if c["scenario_type"] in ["C", "D"]: lines.append(f"| {c['case_id']} | {c['scenario_type']} | {c['cross_document_reason']} |")
    lines += ["", "## 与旧42条覆盖的差异", "",
        "旧版分布为single_condition 10、multi_condition 10、cross_document 10、insufficient_evidence 6、routing 6。旧multi_condition实际关联多个候选文档，不能直接映射成新版B场景。旧42条均没有本轮要求的逐事实原文定位Golden；其30篇是候选文档ID覆盖，不是证据召回证明。", "",
        f"新版有{len(cases)}条独立原文Golden，来源覆盖从旧候选30篇变为{len(new_docs)}篇已定位来源；新增来源：{'、'.join(sorted(new_docs-old_docs))}。旧来源中本轮未直接选入的有：{'、'.join(sorted(old_docs-new_docs)) or '无'}。", "",
        "保留与扩展：金额等号、预算扣减、准入、修改、RFQ、TCO、SLA、履约、报价缺项、服务验收等覆盖保留；增加正式目录权限、可信字段来源、笔记本/工作站/打印耗材/家具、绿色建议权威性、重复采购、未知制度和无关背景。单文档多知识点有19条，跨文档按共同判断与多子任务分别计数。", "",
        "不逐字迁移旧问题：旧42条仅作场景参考，Golden全部从正式原文重新取证。部分旧复合问题拆为可验证子场景；未把恰好包含相似主题当成完全等价题目。", "",
        "| 旧Case | 新覆盖/处理 |", "|---|---|"]
    mapping = [
        ("001–003", "A01/A02/A03：金额边界与扣减"), ("004–005", "A07/D01、D03：准入和修改；D03覆盖已建PO限制"),
        ("006–010", "A09/B06/B05/C02/A12/D05：缺项、TCO、RFQ、SLA、履约；不保留原问题中无来源的具体承诺数值"),
        ("011–012", "B02/D01/D02：可信凭证、审批和预算替代；不构造真实状态"),
        ("013–015", "A09/B19/D05/B10：可比报价、风险、终端选型"),
        ("016–020", "B17/A07/A06/D03/D06/D07：延期、准入、版本、分批和服务；原复合条件按子场景覆盖"),
        ("021–024", "D01/D03/B16/A07/B12：高金额准入、变更调价、黑名单及紧急；不将重复审批条文凑成跨文档"),
        ("025–027", "C01/C02/D06/B13/D08：打印长期成本、售后、数量质量差异和节约口径"),
        ("028–030", "B15/B07/D09/B12/D01：单一来源、费用未知、软件条款、紧急及准入；未保留所有旧条件的单题合并"),
        ("031–033", "U01/U02保留缺证不推断不存在的能力，换用明确未覆盖的制度问题；不声称原三题均有一一等价题"),
        ("034–040、042", "S01–S08排除：无确定性业务Fixture或依赖实时外部数据"),
        ("041", "B01保留纯咨询不建单，选型部分由A08/B10覆盖；全部Case另配置Agent只读与禁止写入检查")]
    for oldid, content in mapping: lines.append(f"| RAG-{oldid} | {content} |")
    lines += ["", "## 52篇文档覆盖明细", "", "| 文档 | 类别 | 标题 | 总览 | 可用Case必要事实 |", "|---|---|---|---|---|"]
    for doc in sources.values():
        ids = [c["case_id"] for c in cases if any(e["doc_id"] == doc["id"] and e["role"] == "fact" for e in c["golden_evidence"])]
        lines.append(f"| {doc['id']} | {doc['class']} | {doc['title']} | 已覆盖 | {', '.join(ids) or '未选题（保留全文，不为数量凑题）'} |")
    lines += ["", "未独立选题的KB-001、KB-011为流程/FAQ复述，PKB-V2-025为通用风险导航，PKB-V4-058/059为黑名单/准入扩展；相邻业务规则已有覆盖，但不能宣称这些文档所有细节均已评测。", "",
        "## 自动校验不能保证的事项", "",
        "1. 原文可能自身有错误、历史时点限制或不完整；哈希只证明与冻结放行版一致，不证明现实世界事实。",
        "2. 逐字支持不等于问题所有隐含条件均已理解。自然语言相关性、专业方案充分性及跨文档语义必要性没有独立语义审查证明。",
        "3. 字符区间为解码UTF-8后的Unicode codepoint零基半开区间，保留原始换行；不是字节偏移或JavaScript UTF-16偏移。下游必须转换并校验source_hash。",
        "4. Top-K需要完整必要事实证据区间覆盖；当前索引无精确位置，尚未实现/执行实际Chunk映射。来源重叠可按已验证区间并集合并，不因同文档其他段被检索而算召回。",
        "5. Checker规范已给出，但语义评分器、Runner适配、实际工具Trace/写入检查与不同索引参数的正式评测未执行；没有成功率或召回率成绩。",
        "6. 旧数据、Agent、Backend、知识切块源码和正式知识哈希均受保留基线检查；没有以重建或修订历史报告来制造通过结果。", "",
        "## 交付与复现", "",
        "数据目录含evaluation_cases.jsonl、evaluation_cases.csv、evaluation_schema.json、skipped_cases.jsonl、provenance_manifest.json及README.md。",
        "报告目录含evaluation_coverage_report.md、needs_decision.md、validation_results.json；总览位于reports/rag_golden_v1。",
        "源输入evaluation/golden_blueprints.py保留证据先行的选题、必要判断、错误结论及问题；evaluation/golden_dataset.py执行离线构建和校验，未引入新运行框架。", "",
        "校验器负向回归结果保存于reports/rag_golden_v1/validator_test_results.json；该测试结果与本次RAG执行成绩无关。", "",
        "```powershell", "# 在 procurement_eval_v08 目录；标准库实现，无需网络或模型配置",
        "python -m evaluation.golden_dataset validate",
        "python -m unittest evaluation.tests.test_golden_dataset -v",
        "# 仅在明确要重建本独立候选版本时使用；不修改旧版本",
        "python -m evaluation.golden_dataset build", "```", "",
        "确认后的旧版替换与Airtable同步属于后续任务，本轮未执行。"]
    (REPORTS / "evaluation_coverage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    decision = """# 需要业务决策的事项

NEEDS_DECISION：0。

没有发现必须暂停构建、由用户裁决才能定义核心Golden的业务冲突。AUTO_VALIDATED不要求用户逐条审核。

已自动处理的边界：

- B类‘小范围试单/样品验证’仍以适用制度和正式授权为前提，不能覆盖A类新供应商未准入不得创建PO。
- 预算不足或黑名单相关草稿/证据留存，不等于提交成功或正常审批，不与阻断规则构成直接冲突。
- A类guide/FAQ与B类专业建议保留原文明示的权威边界；不把建议统一改成公司硬性制度。
- KB-010中‘尚未接入’是历史时点表述；当前系统接入状态题跳过，未修改原文。
- 实时价格、库存、供应商状态、真实PR状态与写操作题缺确定性Fixture，已跳过；不要求用户逐条补数据。
- FAQ与原条款同义重复不作为跨文档必要证据；相关候选已跳过。

详见同目录覆盖报告与数据目录skipped_cases.jsonl。此结论仅针对本轮固定知识包和候选定义，不是对现实企业制度完整性的担保。
"""
    (REPORTS / "needs_decision.md").write_text(decision, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "validate"])
    args = parser.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    try:
        protected = snapshot_protected()
        sources = load_sources()
        missing = overview_check(sources)
        if missing: raise ValueError("总览正文覆盖缺失:" + repr(missing))
        checks, accepted, skipped = [], [], []
        for bp in BLUEPRINTS:
            try:
                case = build_case(bp, sources)
                check = validate_case(case, sources, bp)
                checks.append(check)
                if check["status"] == "PASS": accepted.append(case)
                else: skipped.append(dict(case_id=bp["id"], scenario_type=bp["scenario"], question=bp["question"],
                    gold_status="SKIPPED", case_version=VERSION, reasons=check["errors"], candidate=case))
            except (ValueError, KeyError, StopIteration) as exc:
                skipped.append(dict(case_id=bp["id"], scenario_type=bp["scenario"], question=bp["question"],
                    gold_status="SKIPPED", case_version=VERSION, reasons=[str(exc)]))
        skipped += [dict(case_id=b["id"], scenario_type=b["scenario"], question=b["question"], gold_status="SKIPPED",
            case_version=VERSION, reasons=[b["reason"]], legacy_case_id=b.get("legacy_case_id"),
            source_location_status="NOT_APPLICABLE_NO_RELIABLE_GOLDEN", semantic_support_status="EXCLUDED_BEFORE_GOLDEN") for b in SKIP_BLUEPRINTS]
        ids = [x["case_id"] for x in accepted + skipped]
        if len(ids) != len(set(ids)): raise ValueError("case_id重复")
        if len({x["question"] for x in accepted}) != len(accepted): raise ValueError("重复问题")
        artifacts = ["evaluation_cases.jsonl", "evaluation_cases.csv", "evaluation_schema.json", "skipped_cases.jsonl"]
        if args.command == "build":
            baseline_path = DATA / "provenance_manifest.json"
            if baseline_path.exists():
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                if baseline["protected_files"] != protected:
                    raise ValueError("受保护输入已变化；不得以重建候选集静默重置基线")
            write_jsonl(DATA / artifacts[0], accepted)
            (DATA / artifacts[1]).write_text(csv_text(accepted), encoding="utf-8-sig", newline="")
            write_json(DATA / artifacts[2], schema())
            write_jsonl(DATA / artifacts[3], skipped)
            write_json(DATA / "provenance_manifest.json", dict(case_version=VERSION, protected_files=protected,
                overview_sha256=sha(OVERVIEW.read_bytes()), manifest_sha256=sha((KB / "manifest.json").read_bytes()),
                blueprint_sha256=sha(canonical(BLUEPRINTS)),
                artifact_hashes={name: sha((DATA / name).read_bytes()) for name in artifacts}))
        delivered = read_jsonl(DATA / "evaluation_cases.jsonl")
        manifest = json.loads((DATA / "provenance_manifest.json").read_text(encoding="utf-8"))
        delivery_errors = []
        if delivered != accepted: delivery_errors.append("delivered_cases_differ_from_validated_candidates")
        if read_jsonl(DATA / "skipped_cases.jsonl") != skipped: delivery_errors.append("skip_audit_changed")
        if json.loads((DATA / "evaluation_schema.json").read_text(encoding="utf-8")) != schema(): delivery_errors.append("schema_changed")
        if not validate_csv(delivered, DATA / "evaluation_cases.csv"): delivery_errors.append("csv_roundtrip_failed")
        for name in artifacts:
            if manifest["artifact_hashes"].get(name) != sha((DATA / name).read_bytes()): delivery_errors.append("artifact_hash_changed:" + name)
        if manifest["protected_files"] != protected or protected != snapshot_protected(): delivery_errors.append("protected_sources_or_code_changed")
        if manifest["overview_sha256"] != sha(OVERVIEW.read_bytes()): delivery_errors.append("overview_changed")
        if manifest["blueprint_sha256"] != sha(canonical(BLUEPRINTS)): delivery_errors.append("blueprint_changed")
        for case in delivered:
            result = validate_case(case, sources)
            if result["status"] != "PASS": delivery_errors += [case["case_id"] + ":" + x for x in result["errors"]]
        counts = Counter(c["scenario_type"] for c in accepted)
        candidate_counts = Counter(b["scenario"] for b in BLUEPRINTS + SKIP_BLUEPRINTS)
        valid_docs = sorted({e["doc_id"] for c in accepted for e in c["golden_evidence"] if e["role"] == "fact"})
        result = dict(schema_version="golden-validation-1.0", command=args.command,
            validation_status="PASS" if not delivery_errors else "FAIL", errors=delivery_errors,
            knowledge_base=dict(documents=52, A=11, B=41, released_and_hash_verified=52, overview_covered=52),
            counts=dict(candidates=len(BLUEPRINTS) + len(SKIP_BLUEPRINTS), AUTO_VALIDATED=len(accepted),
                SKIPPED=len(skipped), NEEDS_DECISION=0, scenario_candidates=dict(candidate_counts), scenario_valid=dict(counts)),
            fact_count=sum(len(c["required_facts"]) for c in accepted),
            evidence_count=sum(len(c["golden_evidence"]) for c in accepted),
            evidence_source_completeness=1.0 if accepted and not delivery_errors else None,
            case_evidence_documents=valid_docs, overview_missing=[],
            attributes=dict(Counter(a for c in accepted for a in c["attributes"])),
            answerability=dict(Counter(c["answerability"] for c in accepted)),
            independent_semantic_review="NOT_PERFORMED", model_evaluation="NOT_RUN", airtable_sync="NOT_RUN",
            protected_files_unchanged=not any("protected" in x for x in delivery_errors),
            csv_roundtrip=not any("csv" in x for x in delivery_errors), case_checks=checks,
            excluded_case_checks=[dict(case_id=s["case_id"], status="SKIPPED", reasons=s["reasons"],
                source_location_checks=s.get("source_location_status", "FAILED_OR_NOT_AVAILABLE"),
                semantic_support_checks=s.get("semantic_support_status", "FAILED_OR_NOT_AVAILABLE")) for s in skipped])
        write_json(REPORTS / "validation_results.json", result)
        if not delivery_errors:
            write_reports(result, sources, accepted, skipped)
        print(json.dumps({k: result[k] for k in ["validation_status", "counts", "fact_count", "evidence_count", "errors"]}, ensure_ascii=False))
        return 0 if not delivery_errors else 1
    except (ValueError, KeyError, OSError, StopIteration, json.JSONDecodeError) as exc:
        write_json(REPORTS / "validation_results.json", dict(validation_status="FAIL", error=str(exc), model_evaluation="NOT_RUN"))
        print("VALIDATION FAILED: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
