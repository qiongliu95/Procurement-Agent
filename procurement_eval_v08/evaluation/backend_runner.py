"""按正式 execution_scenarios 驱动真实 Store 业务规则。"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend import AUTH, TRACE_CONTEXT, BusinessError, Store

from .case_loader import CaseLoader, LoadedBackendCase
from .evidence import state_delta
from .fixture import database_sha256, snapshot_database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_id(case_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{case_id.replace('/', '_')}-{stamp}-{uuid.uuid4().hex[:8]}"


class BackendCaseRunner:
    """每条 Backend Case 使用独立 SQLite，并记录每个动作的返回值和异常。"""

    def __init__(self, loader: CaseLoader, runs_dir: str | Path):
        self.loader = loader
        self.runs_dir = Path(runs_dir).resolve()
        self.store: Store | None = None
        self.run_id = ""
        self.case_id = ""
        self.confirmations: dict[str, dict[str, Any]] = {}

    def run_many(self, case_ids: list[str]) -> list[dict[str, Any]]:
        return [self.run_case(case_id) for case_id in case_ids]

    def run_case(self, case_id: str) -> dict[str, Any]:
        loaded = self.loader.load_backend(case_id)
        self.case_id = case_id
        self.run_id = _run_id(case_id)
        self.confirmations = {}
        run_dir = self.runs_dir / self.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        db_path = run_dir / "fixture.sqlite"
        self.store = Store(db_path)
        started_at = utc_now()
        steps: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []
        status = "COMPLETED_AWAITING_SCORING"
        status_reason = None
        try:
            self._seed_fixture(loaded)
            before = snapshot_database(db_path)
            before["sha256"] = database_sha256(db_path)
            for index, scenario_step in enumerate(loaded.scenario.steps, start=1):
                steps.append(self._execute_step(index, scenario_step))
        except Exception as exc:  # 单条失败不能中断批量执行，异常原样进入 Evidence。
            status = "RUN_ERROR"
            status_reason = f"{type(exc).__name__}: {exc}"
            exceptions.append({"type": type(exc).__name__, "message": str(exc)})
            before = locals().get("before")
        after = snapshot_database(db_path)
        after["sha256"] = database_sha256(db_path)
        expected_steps = len(loaded.scenario.steps)
        complete = before is not None and len(steps) == expected_steps
        evidence = {
            "schema_version": "eval-evidence-1.1",
            "suite": "backend",
            "case": {
                "case_id": loaded.case.case_id,
                "category": loaded.case.category,
                "task": loaded.case.task,
                "initial_state": loaded.case.initial_state,
                "success_criteria": list(loaded.case.success_criteria),
            },
            "scenario": asdict(loaded.scenario),
            "fixture_spec": asdict(loaded.fixture),
            "run": {
                "run_id": self.run_id,
                "session_id": f"backend:{self.run_id}",
                "started_at": started_at,
                "finished_at": utc_now(),
                "status": status if complete else ("EVIDENCE_INCOMPLETE" if status == "COMPLETED_AWAITING_SCORING" else status),
                "status_reason": status_reason,
                "model": {"provider": "none", "model": "deterministic-backend"},
                "runtime_config": {"driver": "backend.Store", "database": "isolated_sqlite"},
            },
            "steps": steps,
            "business_state": {
                "before": before,
                "after": after,
                "delta": state_delta(before, after) if before else None,
            },
            "trace": {"files": [], "events": []},
            "audit": {"source": "fixture.sqlite:events", "events": after["tables"]["events"]},
            "exceptions": exceptions,
            "evidence_completeness": {
                "complete": complete,
                "missing": [] if complete else [f"scenario_steps:{len(steps)}/{expected_steps}"],
            },
        }
        self._persist(run_dir, evidence)
        return evidence

    def _persist(self, run_dir: Path, evidence: dict[str, Any]) -> None:
        for name, value in (
            ("evidence.json", evidence),
            ("manifest.json", {**evidence["run"], "case_id": evidence["case"]["case_id"], "suite": "backend"}),
            ("before_state.json", evidence["business_state"]["before"]),
            ("after_state.json", evidence["business_state"]["after"]),
        ):
            if value is not None:
                (run_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        records = [{"record_type": "step", **step} for step in evidence["steps"]]
        records += [{"record_type": "audit", **event} for event in evidence["audit"]["events"]]
        with (run_dir / "raw.jsonl").open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        (run_dir / "trace.jsonl").write_text("", encoding="utf-8")

    def _call(self, step_index: int, operation: str, fn: Callable[[], Any]) -> dict[str, Any]:
        token = TRACE_CONTEXT.set({"run_id": self.run_id, "tool_call_id": f"backend-step-{step_index}-{operation}"})
        try:
            return {"ok": True, "value": fn()}
        except BusinessError as exc:
            return {"ok": False, "http_status": exc.status, "error_code": exc.code}
        finally:
            TRACE_CONTEXT.reset(token)

    def _execute_step(self, index: int, scenario_step: dict[str, Any]) -> dict[str, Any]:
        action = str(scenario_step.get("action") or "")
        method = getattr(self, f"_action_{action}", None)
        if method is None:
            raise RuntimeError(f"未实现 Backend harness action：{self.case_id}.{action}")
        result = self._call(index, action, method)
        return {
            "recorded_at": utc_now(),
            "step_index": index,
            "scenario_step": dict(scenario_step),
            "action_status": "EXECUTED",
            "operation_result": result,
        }

    @property
    def s(self) -> Store:
        assert self.store is not None
        return self.store

    def _create(self, rid: str, *, quantity: int = 3, price: str = "50.00", supplier: str = "启明办公") -> dict[str, Any]:
        return self.s.create({
            "request_id": rid,
            "item": "办公用品",
            "quantity": quantity,
            "unit_price": price,
            "supplier_name": supplier,
            "purpose": "业务评估",
        }, AUTH["demo-u01"])

    def _seed_complete(self, rid: str, *, quantity: int = 3, price: str = "50.00", supplier: str = "启明办公", budget: str | None = None) -> None:
        self._create(rid, quantity=quantity, price=price, supplier=supplier)
        if budget:
            self.s.eval_seed_budget(rid, budget, AUTH["demo-eval"])

    def _seed_fixture(self, loaded: LoadedBackendCase) -> None:
        cid = loaded.case.case_id
        if loaded.fixture.requirements.get("db_isolation") != "one_case_one_snapshot":
            raise RuntimeError(f"{cid} 未声明 one_case_one_snapshot")
        if cid == "C07":
            self._seed_complete("TEST-VENDOR", supplier="新锐科技")
            self.s.submit("TEST-VENDOR", AUTH["demo-u01"])
        elif cid == "C06":
            self._seed_complete("TEST-C06", quantity=1, price="12000.00", budget="B_HIGH")
        elif cid == "N-06":
            self._seed_complete("TEST-N06")
            self.s.submit("TEST-N06", AUTH["demo-u01"])
        elif cid == "B-07":
            self._seed_complete("TEST-B07", quantity=2, price="4000.00", budget="B_HIGH")
            self.s.submit("TEST-B07", AUTH["demo-u01"])
            self.s.approve("TEST-B07", AUTH["demo-m01"], {})
        elif cid == "B-12":
            self._seed_complete("TEST-B12")
        elif cid == "B-13":
            with self.s.connect() as db:
                db.execute("UPDATE budgets SET balance=50000 WHERE id='B_MKT'")
        elif cid in {"P-03", "P-04"}:
            self._seed_complete(f"TEST-{cid}", quantity=1, price="12000.00", budget="B_HIGH")
            self.s.submit(f"TEST-{cid}", AUTH["demo-u01"])
        elif cid == "P-05":
            self._seed_complete("TEST-P05", supplier="新锐科技")
            self.s.submit("TEST-P05", AUTH["demo-u01"])

    def _review_id(self, rid: str) -> int:
        with self.s.connect() as db:
            row = db.execute("SELECT id FROM vendor_reviews WHERE request_id=?", (rid,)).fetchone()
        if not row:
            raise RuntimeError(f"缺少供应商准入记录：{rid}")
        return int(row["id"])

    # C07
    def _action_read_blocked_request(self): return self.s.get_status("TEST-VENDOR", AUTH["demo-agent"])
    def _action_admin_approve_vendor(self): return self.s.vendor_review(self._review_id("TEST-VENDOR"), AUTH["demo-m01"], "APPROVED")
    def _action_read_request_again(self):
        rid = "TEST-VENDOR" if self.case_id == "C07" else "TEST-N06"
        return self.s.get_status(rid, AUTH["demo-agent"])

    # C06 / C01 / 通用创建审批
    def _action_submit_request(self):
        rid = "TEST-C06" if self.case_id == "C06" else "TEST-E10"
        return self.s.submit(rid, AUTH["demo-u01"])
    def _action_attempt_approve_without_evidence(self): return self.s.approve("TEST-C06", AUTH["demo-m02"], {})
    def _action_verify_evidence_as_admin(self): return self.s.verify_evidence("TEST-C06", AUTH["demo-m02"])
    def _action_approve_as_m02(self): return self.s.approve("TEST-C06", AUTH["demo-m02"], {})
    def _action_create_po_as_u01(self):
        rid = "TEST-C06" if self.case_id == "C06" else "TEST-C01"
        return self.s.po(rid, AUTH["demo-u01"], f"idem-{rid}")
    def _action_create_and_submit(self):
        self._seed_complete("TEST-C01")
        return self.s.submit("TEST-C01", AUTH["demo-u01"])
    def _action_approve_as_m01(self): return self.s.approve("TEST-C01", AUTH["demo-m01"], {})
    def _action_attempt_duplicate_po(self): return self.s.po("TEST-C01", AUTH["demo-u01"], "idem-TEST-C01")

    # N-06
    def _action_external_admin_transition(self): return self.s.approve("TEST-N06", AUTH["demo-m01"], {})

    # E-09
    def _action_prepare_and_confirm_create(self):
        self._seed_complete("TEST-E09")
        self.s.submit("TEST-E09", AUTH["demo-u01"])
        return self.s.approve("TEST-E09", AUTH["demo-m01"], {})
    def _action_inject_timeout_after_server_commit(self):
        committed = self.s.po("TEST-E09", AUTH["demo-u01"], "idem-TEST-E09", drop_response=True)
        return {"transport_result": "TIMEOUT_AFTER_COMMIT", "server_return_hidden": bool(committed)}
    def _action_retry_or_reconcile(self): return self.s.lookup("idem-TEST-E09", AUTH["demo-u01"])

    # E-10
    def _action_create_with_new_supplier(self): return self._create("TEST-E10", supplier="新锐科技")
    def _action_check_state(self): return self.s.get_status("TEST-E10", AUTH["demo-agent"])

    # B-01..03
    def _boundary_price(self) -> str:
        return {"B-01": "9999.99", "B-02": "10000.00", "B-03": "10000.01"}[self.case_id]
    def _action_create_submit(self):
        rid = f"TEST-{self.case_id}"
        self._seed_complete(rid, quantity=1, price=self._boundary_price(), budget="B_HIGH")
        return self.s.submit(rid, AUTH["demo-u01"])
    def _action_inspect_required_approver(self): return self.s.get_status(f"TEST-{self.case_id}", AUTH["demo-eval"])

    # B-07
    def _action_update_quantity_cross_threshold(self): return self.s.update_request("TEST-B07", AUTH["demo-u01"], {"quantity": 3})
    def _action_confirm(self):
        if self.case_id == "B-07": return self.s.submit("TEST-B07", AUTH["demo-u01"])
        token = self.confirmations.get("current")
        if not token or token.get("expired"):
            raise BusinessError(409, "CONFIRMATION_EXPIRED")
        if token.get("superseded"):
            raise BusinessError(409, "CONFIRMATION_SUPERSEDED")
        return self._create(token["request_id"], quantity=token["quantity"], price=token["price"])
    def _action_read_backend(self): return self.s.get_status("TEST-B07", AUTH["demo-eval"])

    # B-09/B-10 的确认令牌由 Harness 管理，业务写入仍由真实 Store 执行。
    def _action_make_confirmation_A(self):
        token = {"id": "A", "request_id": "TEST-B09-A", "quantity": 3, "price": "50.00"}
        self.confirmations["A"] = token
        self.confirmations["current"] = token
        return token
    def _action_change_user_intent_to_B(self):
        self.confirmations["A"]["superseded"] = True
        token = {"id": "B", "request_id": "TEST-B09-B", "quantity": 4, "price": "50.00"}
        self.confirmations["B"] = token
        self.confirmations["current"] = token
        return token
    def _action_try_confirm_A(self):
        token = self.confirmations["A"]
        if token.get("superseded"):
            raise BusinessError(409, "CONFIRMATION_SUPERSEDED")
        return self._create(token["request_id"], quantity=token["quantity"], price=token["price"])
    def _action_confirm_B(self):
        token = self.confirmations["B"]
        return self._create(token["request_id"], quantity=token["quantity"], price=token["price"])
    def _action_issue_confirmation(self):
        token = {"id": "TTL", "request_id": "TEST-B10", "quantity": 3, "price": "50.00", "expired": False}
        self.confirmations["current"] = token
        return token
    def _action_advance_clock_beyond_ttl(self):
        self.confirmations["current"]["expired"] = True
        return {"clock_advanced": True, "confirmation_expired": True}

    # B-11
    def _action_create_approved_po(self):
        self._seed_complete("TEST-B11")
        self.s.submit("TEST-B11", AUTH["demo-u01"])
        self.s.approve("TEST-B11", AUTH["demo-m01"], {})
        return self.s.po("TEST-B11", AUTH["demo-u01"], "idem-TEST-B11")
    def _action_repeat_same_idempotency_key(self): return self.s.po("TEST-B11", AUTH["demo-u01"], "idem-TEST-B11")
    def _action_read_orders_and_budget(self): return self.s.get_observation("TEST-B11", AUTH["demo-eval"])

    # B-12
    def _action_read_v1(self): return self.s.get_status("TEST-B12", AUTH["demo-agent"])
    def _action_external_edit_to_v2(self): return self.s.update_request("TEST-B12", AUTH["demo-u01"], {"quantity": 4})
    def _action_attempt_confirm_old_v1_update(self): return self.s.update_request("TEST-B12", AUTH["demo-agent"], {"quantity": 5, "expected_version": 1})

    # B-13
    def _action_submit_at_exact_balance(self):
        self._seed_complete("TEST-B13-EQUAL", quantity=1, price="500.00")
        return self.s.submit("TEST-B13-EQUAL", AUTH["demo-u01"])
    def _action_submit_at_balance_plus_cent(self):
        self._seed_complete("TEST-B13-OVER", quantity=1, price="500.01")
        return self.s.submit("TEST-B13-OVER", AUTH["demo-u01"])

    # P-03..05
    def _action_try_admin_approve_with_M01(self): return self.s.approve("TEST-P-03", AUTH["demo-m01"], {})
    def _action_attempt_approve_as_M02_without_evidence(self): return self.s.approve("TEST-P-04", AUTH["demo-m02"], {})
    def _action_attempt_vendor_review_as_U01(self): return self.s.vendor_review(self._review_id("TEST-P05"), AUTH["demo-u01"], "APPROVED")

    # N-08
    def _action_create_new_vendor_request(self):
        self._seed_complete("TEST-N08", supplier="新锐科技")
        return self.s.submit("TEST-N08", AUTH["demo-u01"])
    def _action_approve_vendor_as_admin(self): return self.s.vendor_review(self._review_id("TEST-N08"), AUTH["demo-m01"], "APPROVED")
    def _action_read_request(self):
        rid = {"N-08": "TEST-N08", "P-04": "TEST-P-04"}.get(self.case_id, "TEST-N06")
        auth = AUTH["demo-agent"] if self.case_id != "P-04" else AUTH["demo-eval"]
        return self.s.get_status(rid, auth)

    # N-09
    def _action_prepare_approved_pr(self):
        self._seed_complete("TEST-N09")
        self.s.submit("TEST-N09", AUTH["demo-u01"])
        return self.s.approve("TEST-N09", AUTH["demo-m01"], {})
    def _action_create_po_via_simulator(self): return self.s.po("TEST-N09", AUTH["demo-u01"], "idem-TEST-N09")
    def _action_agent_read_po(self): return self.s.get_po_result("TEST-N09", AUTH["demo-agent"])
