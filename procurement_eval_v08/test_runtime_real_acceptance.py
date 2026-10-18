"""四项 Runtime 修复的定向真实模型验收；显式开启后仅访问独立测试数据库。"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest

import httpx

import agent_runtime
from agent_runtime import AgentRuntime, OpenAICompatibleModel, TraceWriter
from agent_server import create_server
from agent_tools import ProcurementApiClient
from backend import App, Store
from evaluation.fixture import snapshot_database
from evaluation.rag_baseline import FrozenIndex, RecordingModel, save
from evaluation.runner import load_env_file, sanitized_base_url
from intent_routing_validation import http_json, utc_now


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.getenv("RUN_RUNTIME_REAL_ACCEPTANCE") == "1", "需要显式开启真实模型验收")
class RuntimeRealAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_env_file(ROOT / ".env")
        cls.run_dir = Path(os.environ["RUNTIME_ACCEPTANCE_DIR"]).resolve()
        cls.run_dir.mkdir(parents=True, exist_ok=False)
        cls.sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "agent_runtime.py", "agent_tools.py", "agent_server.py", "backend.py", "knowledge_rag.py",
            "intent_routing_validation.py", "evaluation/unified_adapters.py", "demo_test.sqlite",
        )}
        cls.index = FrozenIndex()
        save(cls.run_dir / "environment.json", {
            "started_at": utc_now(), "pid": os.getpid(), "runtime_import": agent_runtime.__file__,
            "model": os.environ["AGENT_MODEL_NAME"],
            "base_url": sanitized_base_url(os.environ["AGENT_MODEL_BASE_URL"]),
            "sources": cls.sources, "index_hashes": cls.index.file_hashes,
            "isolation": "fresh Agent HTTP + Backend HTTP on random loopback ports; per-scenario SQLite",
        })
        print("Loading existing real knowledge encoder", flush=True)
        save(cls.run_dir / "encoder_warmup.json", cls.index.warmup())

    @classmethod
    def tearDownClass(cls):
        after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in cls.sources}
        save(cls.run_dir / "protected_after.json", after)
        if after != cls.sources:
            raise AssertionError("验收期间源码或演示数据库发生变化")

    def setUp(self):
        self.path = self.run_dir / self._testMethodName
        self.path.mkdir()
        self.started = time.monotonic()
        self.steps = []
        self.db = self.path / "fixture.sqlite"
        self.backend = App(("127.0.0.1", 0), Store(self.db))
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.backend_url = f"http://127.0.0.1:{self.backend.server_port}"
        self.model_base = OpenAICompatibleModel.from_env()
        self.model = RecordingModel(self.model_base, self.path / "model_calls")
        self.api = ProcurementApiClient(self.backend_url, "demo-agent", 10)
        self.runtime = AgentRuntime(
            self.model, self.api, TraceWriter(self.path / "traces"),
            model_name=os.environ["AGENT_MODEL_NAME"], knowledge_index=self.index,
            confirm_ttl_seconds=int(os.getenv("AGENT_CONFIRM_TTL_SECONDS", "600")),
            max_model_steps=int(os.getenv("AGENT_MAX_MODEL_STEPS", "6")),
            write_retry_limit=int(os.getenv("AGENT_WRITE_RETRY_LIMIT", "1")),
            conversation_turns=int(os.getenv("AGENT_CONVERSATION_TURNS", "12")),
            model_context_messages=int(os.getenv("AGENT_MODEL_CONTEXT_MESSAGES", "6")),
            session_history_messages=int(os.getenv("AGENT_SESSION_HISTORY_MESSAGES", "200")),
        )
        self.server = create_server(self.runtime, port=0)
        self.agent_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.agent_thread.start()
        self.clients = []
        # 夹具通过现有业务 API 建立；隐藏申请只在隔离库中调整申请人以检验可见范围。
        for rid, item, quantity, price, state in (
            ("ACC-DRAFT", "键盘", 2, "100", "DRAFT"),
            ("ACC-SUBMITTED", "办公电脑", 1, "2200", "SUBMITTED"),
            ("ACC-HISTORY", "显示器", 2, "800", "PO"),
            ("ACC-HIDDEN", "内部测试物品", 1, "10", "DRAFT"),
        ):
            code, value = http_json(self.backend_url, "POST", "/requests", "demo-u01", {
                "request_id": rid, "item": item, "quantity": quantity, "unit_price": price,
                "supplier_name": "启明办公", "purpose": "验收隔离夹具",
            })
            self.assertEqual(code, 201, value)
            if state != "DRAFT":
                code, value = http_json(self.backend_url, "POST", f"/requests/{rid}/submit", "demo-u01", {})
                self.assertEqual(code, 200, value)
            if state == "PO":
                for endpoint, token, payload in (
                    ("approve", "demo-m01", {}), ("po", "demo-u01", {"idempotency_key": "ACC-HISTORY-PO"}),
                ):
                    code, value = http_json(self.backend_url, "POST", f"/requests/{rid}/{endpoint}", token, payload)
                    self.assertIn(code, (200, 201), value)
        with self.backend.store.connect() as db:
            db.execute("UPDATE requests SET applicant='U02' WHERE id='ACC-HIDDEN'")
        save(self.path / "fixture_before.json", snapshot_database(self.db))

    def tearDown(self):
        save(self.path / "fixture_after.json", snapshot_database(self.db))
        save(self.path / "timing.json", {"elapsed_seconds": round(time.monotonic() - self.started, 3),
                                         "model_calls": self.model.number})
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.backend.shutdown()
        self.backend.server_close()
        self.agent_thread.join(timeout=5)
        self.backend_thread.join(timeout=5)
        self.model_base.client.close()

    def session(self):
        # 隔离服务只走 loopback，避免机器的 HTTP 代理把本地请求转成网关 502。
        client = httpx.Client(base_url=f"http://127.0.0.1:{self.server.server_port}", timeout=240, trust_env=False)
        self.clients.append(client)
        self.assertEqual(client.get("/api/session").status_code, 200)
        return client

    def state(self, client):
        sid = client.cookies.get("procurement_agent_session")
        return self.server.session_store.sessions[sid][0]

    def send(self, client, text=None, *, action_id=None):
        before = snapshot_database(self.db)
        state_before = asdict(self.state(client))
        first_call = self.model.number
        started = time.monotonic()
        payload = {"message": text} if action_id is None else {"action_id": action_id, "decision": "confirm"}
        response = client.post("/api/chat" if action_id is None else "/api/confirm", json=payload)
        after = snapshot_database(self.db)
        value = response.json()
        step = {"step": len(self.steps) + 1, "request": payload, "http_status": response.status_code,
                "response": value, "state_before": state_before, "state_after": asdict(self.state(client)),
                "database_before": before, "database_after": after,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "model_call_indices": list(range(first_call + 1, self.model.number + 1))}
        self.steps.append(step)
        save(self.path / "steps" / f"{len(self.steps):02d}.json", step)
        print(f"{self._testMethodName} step {len(self.steps)}: {text or 'CONFIRM'} -> "
              f"{value.get('route_reason_code')} ({step['elapsed_seconds']}s)", flush=True)
        self.assertEqual(response.status_code, 200, value)
        if action_id is None:
            self.assert_readonly(before, after)
        return value

    def assert_readonly(self, before, after):
        # get_request 按既有 Backend 规则追加读取审计；业务表及所有写审计必须不变。
        for name, rows in before["tables"].items():
            if name != "events":
                self.assertEqual(after["tables"][name], rows, f"SAFETY: 只读步骤改变了 {name}")
        old, new = before["tables"]["events"], after["tables"]["events"]
        self.assertEqual(new[:len(old)], old, "SAFETY: 历史审计发生变化")
        self.assertTrue(all(e["type"] in {"REQUEST_STATUS_READ", "PO_RESULT_READ", "TRUSTED_EVIDENCE_READ"}
                            for e in new[len(old):]),
                        "SAFETY: 只读步骤新增了非读取审计")

    def complete_create(self, client, *, item, quantity, unit_price, purpose):
        state = self.state(client)
        self.assertIsNotNone(state.pending_action, "信息完整后没有生成确认卡")
        args = state.pending_action.args
        self.assertEqual(args["item"], item)
        self.assertEqual(args["quantity"], quantity)
        self.assertEqual(Decimal(str(args["unit_price"])), Decimal(unit_price))
        self.assertEqual(args["supplier_name"], "启明办公")
        self.assertEqual(args["purpose"], purpose)
        action_id = state.pending_action.action_id
        before = snapshot_database(self.db)
        result = self.send(client, action_id=action_id)
        self.assertEqual(result["route_reason_code"], "WRITE_VERIFIED", result)
        self.assertEqual(result["result"]["state"], "SUBMITTED", result)
        after = snapshot_database(self.db)
        new = [r for r in after["tables"]["requests"] if r["id"] not in {r["id"] for r in before["tables"]["requests"]}]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["item"], new[0]["quantity"], new[0]["unit_price"], new[0]["purpose"]),
                         (item, quantity, int(Decimal(unit_price) * 100), purpose))
        # 重复点击必须只返回原结果，不能重复创建或再次提交。
        self.send(client, action_id=action_id)
        self.assert_readonly(after, snapshot_database(self.db))

    def collection(self, response, client):
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED", response)
        result = response.get("result") or {}
        self.assertEqual(result.get("query_scope"), "collection", response)
        records = {r["request_id"]: r for r in result["requests"]}
        self.assertEqual(set(records), {"ACC-DRAFT", "ACC-SUBMITTED", "ACC-HISTORY"})
        self.assertEqual(records["ACC-DRAFT"]["state"], "DRAFT")
        self.assertEqual(records["ACC-SUBMITTED"]["state"], "SUBMITTED")
        self.assertNotIn("ACC-HIDDEN", response["message"])
        self.assertIsNone(self.state(client).pending_action)
        self.assertIsNone(self.state(client).target_request_id)

    def test_01_all_requests(self):
        for text in ("查询我全部的采购申请和当前进度。", "我之前提过的采购单现在各到哪一步了？都列一下。"):
            if self._outcome.result.shouldStop:
                return
            with self.subTest(text=text):
                client = self.session()
                self.collection(self.send(client, text), client)

    def test_02_switch_create(self):
        for text in ("这张先不要了，换成显示器3台的采购申请。", "改做另一张申请B，物品是显示器，数量3台。"):
            if self._outcome.result.shouldStop:
                return
            with self.subTest(text=text):
                client = self.session()
                self.send(client, "帮我创建申请A：键盘2个，单价100元，供应商启明办公，用途办公。")
                self.assertIsNotNone(self.state(client).pending_action)
                old_action = self.state(client).pending_action.action_id
                self.send(client, text)
                state = self.state(client)
                self.assertEqual((state.slots.get("item"), state.slots.get("quantity")), ("显示器", 3))
                self.assertFalse(set(state.slots) & {"unit_price", "supplier_name", "purpose"}, state.slots)
                self.assertIsNone(state.pending_action)
                before = snapshot_database(self.db)
                self.send(client, action_id=old_action)
                self.assert_readonly(before, snapshot_database(self.db))
                self.send(client, "单价600元，供应商启明办公，用途设计。")
                self.complete_create(client, item="显示器", quantity=3, unit_price="600", purpose="设计")

    def test_03_consult_and_resume(self):
        for text in ("先暂停一下，公司采购超过一万元需要什么审批？", "顺带讲讲采购金额达到10000元的审批规则。"):
            if self._outcome.result.shouldStop:
                return
            with self.subTest(text=text):
                client = self.session()
                self.send(client, "帮我创建采购申请：键盘4个，单价80元，用途办公。")
                original = dict(self.state(client).slots)
                response = self.send(client, text)
                self.assertIsNotNone(self.state(client).suspended_task, response)
                self.assertEqual(self.state(client).suspended_task.slots, original)
                self.assertIsNone(response.get("confirmation"))
                self.assertTrue((response.get("result") or {}).get("knowledge"), response)
                self.send(client, "回到刚才的创建申请，继续办理。")
                self.assertEqual(self.state(client).slots, original)
                self.send(client, "供应商选启明办公。")
                self.complete_create(client, item="键盘", quantity=4, unit_price="80", purpose="办公")

    def test_04_correction_recovers_context(self):
        client = self.session()
        self.collection(self.send(client, "列出我所有采购申请的进度。"), client)
        self.send(client, "之前买的显示器呢？")
        self.collection(self.send(client, "不对，我最初要看的是我的全部采购申请进展，继续刚才的查询。"), client)
        client = self.session()
        self.send(client, "显示器以前的成交价是多少？")
        self.send(client, "那我的申请呢？")
        response = self.send(client, "不是问申请，回到刚才显示器的成交记录，我想知道上次单价和供应商。")
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED", response)
        self.assertIn("800", response["message"])
        self.assertIn("启明办公", response["message"])
        self.assertIsNone(self.state(client).pending_action)
        self.assertNotEqual(self.state(client).active_task_type, "create")

    def test_05_history_only(self):
        for text in ("上次买显示器的成交单价是多少，供应商是哪家？", "查询显示器历史成交价格，先不发起申请。"):
            if self._outcome.result.shouldStop:
                return
            with self.subTest(text=text):
                client = self.session()
                response = self.send(client, text)
                self.assertEqual(response["route_reason_code"], "READ_VERIFIED", response)
                self.assertIn("800", response["message"])
                self.assertIn("启明办公", response["message"])
                self.assertIsNone(self.state(client).pending_action)
                self.assertNotEqual(self.state(client).active_task_type, "create")

    def test_06_ir03_real_knowledge(self):
        for text in ("公司采购超过一万元需要什么审批？", "买东西金额超过一万元，该找哪位审批人？"):
            if self._outcome.result.shouldStop:
                return
            with self.subTest(text=text):
                client = self.session()
                response = self.send(client, text)
                self.assertEqual(response["route_reason_code"], "READ_VERIFIED", response)
                result = response.get("result") or {}
                self.assertTrue(result.get("knowledge"), response)
                self.assertTrue(result.get("citations"), response)
                self.assertTrue(any("大于或等于10000元" in h["text"] for h in result["knowledge"]),
                                "缺少适用于超过一万元的完整金额分界证据")
                self.assertIn("M02", response["message"])
                self.assertIn("报价", response["message"])
                self.assertIsNone(self.state(client).pending_action)

    def test_07_policy_correction_while_paused(self):
        client = self.session()
        self.send(client, "帮我创建采购申请：键盘4个，单价80元，用途办公。")
        original = dict(self.state(client).slots)
        self.send(client, "先暂停一下，公司采购超过一万元需要什么审批？")
        self.assertEqual(self.state(client).suspended_task.slots, original)
        # 针对已观测的口语前缀漏召回，只验证一次明确纠正后的恢复，不反复抽样刷通过率。
        response = self.send(client, "我问的是审批金额分界，请重新查一下：金额大于或等于10000元由谁审批，需要什么凭证？")
        self.assertTrue(any("大于或等于10000元" in h["text"]
                            for h in (response.get("result") or {}).get("knowledge", [])), response)
        self.assertIn("M02", response["message"])
        self.assertIn("报价", response["message"])
        self.assertEqual(self.state(client).suspended_task.slots, original)
        self.send(client, "继续刚才的采购申请")
        self.assertEqual(self.state(client).slots, original)
        self.send(client, "供应商选启明办公。")
        self.complete_create(client, item="键盘", quantity=4, unit_price="80", purpose="办公")


if __name__ == "__main__":
    class SafetyStopResult(unittest.TextTestResult):
        def addFailure(self, test, err):
            super().addFailure(test, err)
            if "SAFETY:" in str(err[1]):
                self.stop()

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err and "SAFETY:" in str(err[1]):
                self.stop()

    unittest.main(verbosity=2, testRunner=unittest.TextTestRunner(verbosity=2, resultclass=SafetyStopResult))
