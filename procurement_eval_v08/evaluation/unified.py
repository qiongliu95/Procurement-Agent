"""四模块统一调度：冻结输入、逐Case进度、异常隔离与证据恢复。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import traceback
import uuid

import intent_routing_validation as ir
from knowledge_rag import default_index_dir, default_knowledge_dir

from .case_loader import CaseLoader
from .evidence import redact_secrets
from .runner import EvaluationRunner, load_env_file, sanitized_base_url
from .unified_adapters import NativeAdapters, RagAdapter, artifacts, digest, read, save, verify_artifacts, PROJECT
from .unified_reporting import render


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(v) for v in value]
    return value


def object_hash(value):
    return hashlib.sha256(json.dumps(canonical(value), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def catalog(loader):
    from . import rag_baseline
    entries = []
    # Backend先验证本地执行链，后续模型Case彼此隔离。
    for module, ids in (("backend", loader.list_backend_case_ids()), ("agent", loader.list_case_ids())):
        for case_id in ids:
            item = loader.load_backend(case_id) if module == "backend" else loader.load(case_id)
            definition = {"case": vars(item.case), "scenario": vars(item.scenario), "fixture": vars(item.fixture)}
            entries.append({"key": module + ":" + case_id, "module": module, "case_id": case_id,
                            "version": "procurement_eval_v4-4.0", "source": str(loader.suite_dir),
                            "case_fingerprint": object_hash(definition), "definition": canonical(definition)})
    for case in ir.CASES:
        entries.append({"key": "ir:" + case["id"], "module": "ir", "case_id": case["id"], "version": "IR21-source-snapshot",
                        "source": str(PROJECT / "intent_routing_validation.py"), "case_fingerprint": object_hash(case), "definition": canonical(case)})
    for case in rag_baseline.cases():
        entries.append({"key": "rag:" + case["case_id"], "module": "rag", "case_id": case["case_id"], "version": case["case_version"],
                        "source": str(rag_baseline.DATA / "evaluation_cases.jsonl"), "case_fingerprint": object_hash(case), "definition": case})
    if dict(Counter(e["module"] for e in entries)) != {"agent": 33, "backend": 20, "ir": 21, "rag": 46}:
        raise ValueError("现行模块数量不等于33/20/21/46，拒绝误报全量")
    if len({e["key"] for e in entries}) != 120:
        raise ValueError("统一清单存在重复模块:Case")
    return entries


def frozen_inputs(loader, entries):
    from . import rag_baseline
    protected = rag_baseline.protected()
    files = [PROJECT / "intent_routing_validation.py", PROJECT / "requirements.txt"]
    files += list(loader.suite_dir.glob("*"))
    files += list((PROJECT / "evaluation").glob("*.py"))
    files += list((PROJECT / "evaluation/checker").glob("*.py"))
    for path in files:
        if path.is_file():
            protected[str(path.resolve())] = digest(path)
    versions = {}
    for name in ("openai", "numpy", "sentence-transformers", "torch", "transformers", "deepeval"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    # 只留凭据指纹，绝不把API Key明文写入运行配置。
    model = {"name": os.getenv("AGENT_MODEL_NAME"), "base_url": sanitized_base_url(os.getenv("AGENT_MODEL_BASE_URL", "")),
             "credential_sha256": hashlib.sha256(os.getenv("AGENT_MODEL_API_KEY", "").encode()).hexdigest(), "temperature": 0}
    config = {k: v for k, v in os.environ.items() if k.startswith("AGENT_") and k != "AGENT_MODEL_API_KEY"}
    config["AGENT_MODEL_BASE_URL"] = model["base_url"]
    return {"schema_version": "unified-freeze-1.0", "protected_files": protected, "model": model,
            "effective_agent_environment": config, "versions": versions, "catalog_hash": object_hash(entries),
            "native_runtime_config": EvaluationRunner(loader, PROJECT / "evaluation/runs")._runtime_config(),
            "knowledge_directory": str(default_knowledge_dir()), "index_directory": str(default_index_dir()),
            "excluded": {"rag42": "superseded_in_this_execution_by_accepted_rag46_v1.2.1_not_same_ids_or_gold",
                         "legacy10": "historical_v0.8_v1.0_not_current_v4", "older_rag46": "same_family_other_versions_not_double_counted"}}


def changed_inputs(snapshot):
    return [path for path, sha in snapshot["protected_files"].items() if not Path(path).is_file() or digest(Path(path)) != sha]


def resumable(record, fingerprint):
    return (record.get("fingerprint") == fingerprint and record.get("final_result") in {"PASS", "FAIL", "REVIEW_REQUIRED"}
            and record.get("execution_status") == "COMPLETED_AWAITING_SCORING"
            and verify_artifacts(record.get("artifacts", []))
            and Path(record.get("result_path", "")).is_file()
            and digest(Path(record["result_path"])) == record.get("result_sha256"))


class UnifiedEvaluator:
    def __init__(self, state, snapshot, loader, *, reuse_evidence=False):
        self.state, self.snapshot, self.loader = state, snapshot, loader
        self.run_dir, self.report_dir = Path(state["run_dir"]), Path(state["report_dir"])
        self.reuse_evidence = reuse_evidence
        self.native = NativeAdapters(loader, self.run_dir, self.report_dir)
        self.rag = RagAdapter(self.run_dir, self.report_dir)

    def persist(self):
        self.state["updated_at"] = now()
        save(Path(self.state["state_path"]), self.state)

    def execute(self):
        for key, previous in list(self.state["records"].items()):
            if resumable(previous, self.state["fingerprint"]):
                print("RESUME_VALID", key, previous["final_result"], flush=True)
                continue
            changed = changed_inputs(self.snapshot)
            if changed:
                self.state["integrity"] = {"unchanged": False, "changed_paths": changed}
                self.state["fatal_error"] = "冻结输入变化，剩余Case保持未执行"
                self.persist()
                break
            record = {k: previous[k] for k in ("key", "module", "case_id", "case_fingerprint")}
            record.update(fingerprint=self.state["fingerprint"], execution_status="RUNNING", final_result="NOT_RUN",
                          started_at=now(), provenance="EXECUTED_THIS_BATCH", attempt_count=previous.get("attempt_count", 0) + 1,
                          previous_attempts=previous.get("previous_attempts", []) + ([previous["result_path"]] if previous.get("result_path") else []))
            self.state["records"][key] = record
            self.persist()
            print("START", key, "attempt", record["attempt_count"], flush=True)
            try:
                result = None
                if record["module"] == "rag" and self.reuse_evidence:
                    result = self.rag.cached(record["case_id"])
                    if result:
                        record["provenance"] = "HISTORICAL_VALIDATED"
                if result is None:
                    result = self.rag.execute(record["case_id"]) if record["module"] == "rag" else self.native.execute(record["module"], record["case_id"])
                record.update(result)
                record["final_result"] = result["score"]["final_result"]
            except Exception as exc:
                # 单Case异常保留完整原因；后面的模块/Case仍继续执行。
                record.update(execution_status="RUN_ERROR", final_result="EVIDENCE_MISSING",
                              error={"type": type(exc).__name__, "message": redact_secrets(str(exc)),
                                     "traceback": redact_secrets(traceback.format_exc())})
                partial = [p for directory in (self.run_dir / record['module']).glob(record['case_id'] + '-*')
                           if directory.is_dir() for p in directory.rglob('*')]
                record['artifacts'] = artifacts(partial)
            record["finished_at"] = now()
            path = self.run_dir / "results" / record["module"] / record["case_id"] / f"attempt-{record['attempt_count']}-{uuid.uuid4().hex[:8]}.json"
            save(path, record)
            record["result_path"], record["result_sha256"] = str(path), digest(path)
            self.persist()
            render(self.state, self.report_dir)
            print("RESULT", key, record["final_result"], record["provenance"], flush=True)
        changed = changed_inputs(self.snapshot)
        self.state["integrity"] = {"unchanged": not changed, "changed_paths": changed}
        self.state["finished_at"] = now()
        self.persist()
        return render(self.state, self.report_dir)


def full_main(argv):
    parser = argparse.ArgumentParser(description="采购Agent现行120条统一评测，无需Codex或GUI")
    parser.add_argument("--case-dir", type=Path)
    parser.add_argument("--env-file", type=Path, default=PROJECT / ".env")
    parser.add_argument("--runs-dir", type=Path, default=PROJECT / "evaluation/runs")
    parser.add_argument("--reports-dir", type=Path, default=PROJECT / "evaluation/reports")
    parser.add_argument("--case", action="append", help="module:case，例如backend:C01；仅用于局部验证")
    parser.add_argument("--list", action="store_true", help="只列出现行清单，不执行模型")
    parser.add_argument("--reuse-evidence", action="store_true", help="复用通过冻结输入/配置/源码/原始字节核验的历史RAG证据")
    parser.add_argument("--resume", help="恢复本入口的批次ID或state.json；校验有效结果，只重试异常/缺证项")
    args = parser.parse_args(argv)
    load_env_file(args.env_file)
    loader = CaseLoader(args.case_dir)
    entries = catalog(loader)
    if args.list:
        print(json.dumps({"counts": dict(Counter(e["module"] for e in entries)), "total": 120,
                          "case_ids": [e["key"] for e in entries], "excluded": ["旧RAG42", "Legacy10", "其他RAG46旧版本"]}, ensure_ascii=False, indent=2))
        return 0
    snapshot = frozen_inputs(loader, entries)
    fingerprint = object_hash(snapshot)
    if args.resume:
        state_path = Path(args.resume) if args.resume.endswith(".json") else args.runs_dir / args.resume / "state.json"
        state = read(state_path)
        if state["fingerprint"] != fingerprint:
            raise ValueError("冻结输入/模型/依赖/执行源码已变化，拒绝混用旧批次；请创建新批次")
        if args.case and set(args.case) != set(state["records"]):
            raise ValueError("恢复时不能改变批次Case范围")
    else:
        requested = set(args.case or [e["key"] for e in entries])
        if requested - {e["key"] for e in entries}:
            raise ValueError("未知module:Case：" + ",".join(sorted(requested - {e["key"] for e in entries})))
        batch_id = "unified-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run_dir, report_dir = (args.runs_dir / batch_id).resolve(), (args.reports_dir / batch_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        report_dir.mkdir(parents=True, exist_ok=False)
        save(run_dir / "frozen_inputs.json", snapshot)
        save(run_dir / "case_catalog.json", {"current_counts": dict(Counter(e["module"] for e in entries)), "total": 120,
                                            "selection": sorted(requested), "entries": entries, "excluded": snapshot["excluded"]})
        state = {"schema_version": "unified-progress-1.0", "batch_id": batch_id, "started_at": now(), "fingerprint": fingerprint,
                 "run_dir": str(run_dir), "report_dir": str(report_dir), "state_path": str(run_dir / "state.json"),
                 "scope": "FULL_120" if len(requested) == 120 else "PARTIAL_VALIDATION", "model_config": snapshot["model"],
                 "records": {e["key"]: {"key": e["key"], "module": e["module"], "case_id": e["case_id"],
                                        "case_fingerprint": e["case_fingerprint"], "execution_status": "NOT_RUN", "final_result": "NOT_RUN"}
                             for e in entries if e["key"] in requested}}
    evaluator = UnifiedEvaluator(state, snapshot, loader, reuse_evidence=args.reuse_evidence)
    evaluator.persist()
    metrics = evaluator.execute()
    save(args.reports_dir / "latest_unified_manifest.json", {"batch_id": state["batch_id"], "state_path": state["state_path"],
                                                          "report_path": str(Path(state["report_dir"]) / "report.md")})
    print(json.dumps({"batch_id": state["batch_id"], "state_path": state["state_path"], "report_path": str(Path(state["report_dir"]) / "report.md"),
                      "modules": metrics["modules"], "scope": state["scope"], "integrity": state["integrity"]}, ensure_ascii=False, indent=2))
    return 2 if not state["integrity"]["unchanged"] or any(r.get("execution_status") in {"RUN_ERROR", "NOT_RUN"} for r in state["records"].values()) else 0
