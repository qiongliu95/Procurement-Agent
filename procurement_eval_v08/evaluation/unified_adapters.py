"""复用四个既有评测模块；这里只负责证据包装、缓存核验和调用衔接。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import gc
import json
from pathlib import Path
import shutil
import threading
import uuid

from agent_runtime import AgentRuntime, TraceWriter
from agent_tools import ProcurementApiClient
from backend import App, Store
from knowledge_rag import build_default_index
import intent_routing_validation as ir

from .backend_runner import BackendCaseRunner
from .case_loader import AgentCase, CaseLoader, ExecutionScenario, FixtureSpec, LoadedCase
from .checker.scorer import OfflineScorer
from .evidence import EvidenceCollector, redact_secrets
from .fixture import snapshot_database
from .runner import EvaluationRunner, default_real_model_factory, runtime_state


PROJECT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def artifacts(paths: list[Path]) -> list[dict]:
    return [{"path": str(p.resolve()), "sha256": digest(p)} for p in sorted(set(paths)) if p.is_file()]


def verify_artifacts(records: list[dict]) -> bool:
    return bool(records) and all(Path(r["path"]).is_file() and digest(Path(r["path"])) == r["sha256"] for r in records)


class NativeAdapters:
    def __init__(self, loader: CaseLoader, run_dir: Path, report_dir: Path):
        self.loader, self.run_dir, self.report_dir = loader, run_dir, report_dir

    def execute(self, module: str, case_id: str) -> dict:
        if module == "ir":
            return self._ir(case_id)
        if module == "agent":
            models = []

            def factory(case):
                model, info = default_real_model_factory(case)
                models.append(model)
                return model, info

            try:
                evidence = EvaluationRunner(self.loader, self.run_dir / "agent", model_factory=factory).run_case(case_id)
            finally:
                for model in models:
                    model.client.close()
        else:
            evidence = BackendCaseRunner(self.loader, self.run_dir / "backend").run_case(case_id)
        path = self.run_dir / module / evidence["run"]["run_id"] / "evidence.json"
        score = OfflineScorer(self.loader).score_file(path, expected_context={
            "case_id": case_id, "run_id": evidence["run"]["run_id"],
            "session_id": evidence["run"]["session_id"],
        })
        OfflineScorer.persist(score, path.parent, report_dir=self.report_dir / "cases" / module / case_id)
        return {"score": score, "execution_status": evidence["run"]["status"],
                "evidence_path": str(path), "artifacts": artifacts(list(path.parent.rglob("*")))}

    def _ir(self, case_id: str) -> dict:
        from .rag_baseline import RecordingModel

        case = next(c for c in ir.CASES if c["id"] == case_id)
        run_id = f"{case_id}-unified-{uuid.uuid4().hex[:12]}"
        path = self.run_dir / "ir" / run_id
        labels = ("intent", "route", "tool_selection", "confirmation_and_write_safety")
        loaded = LoadedCase(
            AgentCase(case_id, "intent", tuple(case["turns"]), "ir_seed_requests", labels),
            ExecutionScenario(case_id, "ir", "specialized_ir", tuple({"type": "user_message", "text": s} for s in case["turns"]), "READY", ()),
            FixtureSpec("ir_seed_requests", case_id, "READY", {"actor": "U01", "isolation": "per_case_sqlite", "source": "intent_routing_validation.seed_requests"}),
        )
        collector = EvidenceCollector(path, run_id, loaded, {}, {"driver": "intent_routing_validation.evaluate_case"})
        app = App(("127.0.0.1", 0), Store(path / "fixture.sqlite"))
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{app.server_port}"
        before = None
        result = None
        model = None
        state = None
        status = "COMPLETED_AWAITING_SCORING"
        reason = None
        try:
            ir.seed_requests(base)
            before = snapshot_database(path / "fixture.sqlite")
            model, info = default_real_model_factory(loaded)
            collector.model_info = info
            runtime = AgentRuntime(RecordingModel(model, path / "model_calls"), ProcurementApiClient(base, "demo-agent", 30),
                                   TraceWriter(path / "traces"), model_name=info["model_name"],
                                   knowledge_index=build_default_index()[0])

            class RecordingRuntime:
                def new_session(self, session_id):
                    nonlocal state
                    state = runtime.new_session(session_id)
                    return state

                def handle_message(self, session, text):
                    initial = runtime_state(session)
                    try:
                        response = runtime.handle_message(session, text)
                    except Exception as exc:
                        collector.add_step({"step_index": len(collector.steps) + 1, "step_type": "user_message", "user_input": text,
                                            "runtime_state_before": initial, "runtime_state_after": runtime_state(session),
                                            "error": redact_secrets(str(exc))})
                        raise
                    collector.add_step({"step_index": len(collector.steps) + 1, "step_type": "user_message", "user_input": text,
                                        "agent_output": response, "runtime_state_before": initial, "runtime_state_after": runtime_state(session)})
                    return response

            trace_path = path / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
            # 预期意图/路由/必需工具及原有安全判定完全复用IR脚本，不改其通过条件。
            result = ir.evaluate_case(case, RecordingRuntime(), trace_path, path / "fixture.sqlite")
        except Exception as exc:
            status = "RUN_ERROR"
            reason = redact_secrets(f"{type(exc).__name__}: {exc}")
            collector.add_exception(exc, phase="ir_execution")
        finally:
            if model is not None:
                model.client.close()
            after = snapshot_database(path / "fixture.sqlite")
            app.shutdown()
            app.server_close()
            thread.join(timeout=5)
            # 旧IR计数函数使用SQLite事务上下文但未显式close，释放其已不可达句柄。
            gc.collect()
        evidence = collector.finish(status=status, status_reason=reason, session_id=state.session_id if state else None,
                                    initial_state=before, final_state=after)
        save(path / "ir_result.json", result)
        score = {
            "schema_version": "scored-result-1.0", "checker_version": "ir-specialized-existing", "suite": "ir",
            "case_id": case_id, "run_id": run_id, "evidence": {"path": str(path / "evidence.json"), "sha256": digest(path / "evidence.json")},
            "original_ir_result": result,
            "criteria": [{"criterion_id": name, "success_criterion": name,
                          "status": "EVIDENCE_MISSING" if passed is None else "PASS" if passed else "FAIL",
                          "observed": result, "evidence_refs": [{"pointer": "/steps", "description": "实际逐轮路由、输出与Session"},
                                                                  {"pointer": "/trace/events", "description": "实际工具调用"},
                                                                  {"pointer": "/business_state", "description": "SQLite前后状态"}]}
                         for name, passed in (result or {}).get("checks", {}).items()],
            "final_result": result["status"] if result and evidence["evidence_completeness"]["complete"] else "EVIDENCE_MISSING",
            "failure_reasons": (result or {}).get("failed_checks", [reason] if reason else [])
                               + [name + ":EVIDENCE_MISSING" for name in (result or {}).get("missing_checks", [])],
        }
        save(path / "score.json", score)
        with (path / "scored.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(score, ensure_ascii=False) + "\n")
        return {"score": score, "execution_status": evidence["run"]["status"], "evidence_path": str(path / "evidence.json"),
                "artifacts": artifacts(list(path.rglob("*")))}


class RagAdapter:
    """绑定新输出目录使用原Baseline函数；旧基线仅经配置和原始字节核验后复用。"""
    def __init__(self, run_dir: Path, report_dir: Path):
        from . import rag_baseline as run
        self.run, self.run_dir, self.report_dir = run, run_dir, report_dir
        self.historical_run = PROJECT / "evaluation/runs/rag_baseline_v1_2_1_20261014"
        self.historical_report = PROJECT / "evaluation/reports/rag_baseline_v1_2_1_20261014"
        self.prepared = False
        self.cache_audit = None

    @contextmanager
    def binding(self, run_dir: Path, report_dir: Path):
        old = self.run.RUN, self.run.REPORT
        self.run.RUN, self.run.REPORT = run_dir, report_dir
        try:
            yield
        finally:
            self.run.RUN, self.run.REPORT = old

    def prepare(self):
        if self.prepared:
            return
        with self.binding(self.run_dir / "rag_setup", self.report_dir / "rag_setup"):
            self.values, self.sources, self.index, self.mapping, self.config = self.run.freeze()
        self.prepared = True

    def audit_historical_cache(self) -> dict:
        if self.cache_audit is not None:
            return self.cache_audit
        self.prepare()
        errors = []
        try:
            if read(self.historical_run / "frozen_config.json") != self.config:
                errors.append("FROZEN_CONFIG_MISMATCH")
            protected = read(self.historical_run / "protected_inputs.json")
            if protected != self.run.protected():
                errors.append("PROTECTED_INPUT_MISMATCH")
            if read(self.historical_run / "embedding_artifacts.json") != read(self.run_dir / "rag_setup/embedding_artifacts.json"):
                errors.append("EMBEDDING_ARTIFACT_MISMATCH")
            for item in read(self.historical_report / "modified_files.json"):
                if Path(item["path"]).name in {"rag_baseline.py", "rag_baseline_scoring.py", "rag_baseline_reporting.py"}:
                    if digest(Path(item["path"])) != item["sha256"]:
                        errors.append("RAG_CHECKER_SOURCE_CHANGED:" + item["path"])
            archive = read(self.historical_run / "artifact_manifest.json")
            for item in archive["files"]:
                path = (self.historical_run / item["path"]).resolve()
                if not path.is_relative_to(self.historical_run.resolve()) or not path.is_file() or digest(path) != item["sha256"]:
                    errors.append("ARCHIVE_HASH_MISMATCH:" + item["path"])
            self.cache_audit = {"eligible": not errors, "errors": errors, "raw_artifact_count": len(archive["files"]),
                                "policy": "same_frozen_config_protected_inputs_checker_source_and_all_archived_raw_bytes",
                                "archive_path": str(self.historical_run / "artifact_manifest.json"),
                                "archive_sha256": digest(self.historical_run / "artifact_manifest.json")}
        except Exception as exc:
            self.cache_audit = {"eligible": False, "errors": [f"{type(exc).__name__}: {exc}"]}
        save(self.run_dir / "rag_cache_audit.json", self.cache_audit)
        return self.cache_audit

    def cached(self, case_id: str) -> dict | None:
        if not self.audit_historical_cache()["eligible"]:
            return None
        from .checker.context import EvidenceContext
        from .rag_baseline_scoring import deterministic
        case = next(c for c in self.values if c["case_id"] == case_id)
        path = self.historical_run / "cases" / case_id
        execution = read(path / "execution.json")
        score = read(path / "scored_result.json")
        context = EvidenceContext.load(execution["evidence_path"])
        context.validate_association()
        if execution["question"] != case["question"] or score["case_id"] != case_id or score["run_id"] != execution["run_id"]:
            return None
        if score["evidence"]["sha256"] != context.sha256 or context.evidence["run"]["status"] != "COMPLETED_AWAITING_SCORING":
            return None
        if score["deterministic"] != deterministic(execution):
            return None
        # 缓存依赖包含本Case的Judge调用/复核/逐项评分，而不只哈希最终答案文件。
        dependencies = [self.historical_run / item["path"] for item in read(self.historical_run / "artifact_manifest.json")["files"]
                        if case_id in item["path"]]
        return {"score": score, "execution_status": context.evidence["run"]["status"], "evidence_path": execution["evidence_path"],
                "artifacts": artifacts(list(path.rglob("*")) + dependencies + [self.historical_run / "artifact_manifest.json",
                    self.historical_run / "frozen_config.json", self.historical_run / "protected_inputs.json",
                    self.historical_run / "embedding_artifacts.json", self.historical_run / "chunk_source_mapping.json"]),
                "cache_proof": self.cache_audit}

    def execute(self, case_id: str) -> dict:
        import os
        from . import rag_baseline_scoring as scoring
        from . import rag_baseline_reporting as reporting
        self.prepare()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        _ = self.index.encoder
        case = next(c for c in self.values if c["case_id"] == case_id)
        attempt = self.run_dir / "rag" / (case_id + "-" + uuid.uuid4().hex[:12])
        attempt.mkdir(parents=True)
        shutil.copy2(self.run_dir / "rag_setup/observation_fixture.sqlite", attempt / "observation_fixture.sqlite")
        with self.binding(attempt, self.report_dir / "rag" / attempt.name):
            save(attempt / "frozen_config.json", self.config)
            save(attempt / "chunk_source_mapping.json", self.mapping)
            execution = self.run.run_one(case, self.index, self.mapping, self.sources, self.config)
            evidence = read(Path(execution["evidence_path"]))
            det = scoring.deterministic(execution)
            metrics = {}
            if execution["status"] == "COMPLETED_AWAITING_SCORING" and not det["environment_errors"]:
                judge = scoring.load_judge()
                for dimension in ["retrieval", "correctness", "faithfulness"]:
                    if dimension == "faithfulness" and not scoring.effective_hits(execution, evidence):
                        metrics[dimension] = {"status": "NOT_APPLICABLE", "reason": "未生成实质知识答案"}
                    else:
                        metrics[dimension] = scoring.metric(case, execution, dimension, judge, evidence)
                if case["answerability"] != "FULL" or metrics["retrieval"].get("score") != 2:
                    metrics["insufficiency"] = scoring.metric(case, execution, "insufficiency", judge, evidence)
            # 与原Baseline Reporter相同的聚合顺序，保留引文/细节不一致的复核状态。
            consistency = bool(metrics) and case["answerability"] != "UNANSWERABLE" and metrics["retrieval"].get("score") != 2 and metrics["correctness"]["status"] == "PASS" and metrics["faithfulness"]["status"] == "PASS"
            required = [metrics[d] for d in ["correctness", "faithfulness", "insufficiency"] if metrics.get(d) and metrics[d]["status"] != "NOT_APPLICABLE"]
            fail = det["route"]["status"] == "FAIL" or det["no_business_write"]["status"] == "FAIL" or any(m["status"] == "FAIL" for m in required)
            uncertain = consistency or any(m["status"] == "REVIEW_REQUIRED" for m in required)
            final = "EVIDENCE_MISSING" if not metrics or det["environment_errors"] else "FAIL" if fail else "REVIEW_REQUIRED" if uncertain else "PASS"
            attribution = reporting.failure_attribution(case, execution, metrics, det, consistency) if metrics else {"primary": "ENVIRONMENT", "tags": ["ENVIRONMENT"]}
            score = {"schema_version": "scored-result-1.0", "checker_version": "rag-baseline-1.0", "suite": "rag_baseline",
                     "case_id": case_id, "run_id": execution["run_id"], "category": case["scenario_type"], "answerability": case["answerability"],
                     "evidence": {"path": execution["evidence_path"], "sha256": digest(Path(execution["evidence_path"]))},
                     "criteria": [{"criterion_id": d, "success_criterion": scoring.RUBRICS[d]["title"], "status": m["status"], "observed": m,
                                   "evidence_refs": [{"pointer": "/steps/0", "description": "真实用户问题和知识回复"},
                                                     {"pointer": "/trace/events", "description": "实际检索与路由"}]}
                                  for d, m in metrics.items() if m["status"] != "NOT_APPLICABLE"],
                     "metrics": metrics, "deterministic": det, "retrieval_consistency_pending": consistency,
                     "final_result": final, "strict_pass": final == "PASS", "attribution": attribution,
                     "canonical_union": execution["canonical_recall"], "answer": (execution["response"] or {}).get("message")}
            save(Path(execution["evidence_path"]).parent / "scored_result.json", score)
            return {"score": score, "execution_status": execution["status"], "evidence_path": execution["evidence_path"],
                    "artifacts": artifacts(list(attempt.rglob("*")))}
