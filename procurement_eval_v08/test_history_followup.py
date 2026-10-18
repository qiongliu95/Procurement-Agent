"""历史成交 R01–R10：可信字段投影、实际前端函数，以及显式开启的真实模型连续验证。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from agent_runtime import AgentRuntime, ModelTurn, OpenAICompatibleModel
from evaluation.fixture import snapshot_database
from test_multiturn_dialogue import MultiturnDialogueTests as Helpers, ScriptedModel, proposal, tool


ROOT = Path(__file__).resolve().parent
REQUEST_ID = "AGT-C98B0B31CBF54924"
QUESTIONS = [
    "查询办公鼠标的历史成交价。", "那上一次是哪个供应商？", "那次买了多少个？",
    "对应的采购申请编号是什么？", "把刚才的完整成交记录给我。",
]


def follow(fields=None, **values):
    if fields is not None:
        values["history_fields"] = fields
    return ModelTurn(None, [proposal("consult", "follow_up", "purchase_history", **values)])


def initial_turns():
    return [
        ModelTurn(None, [tool("search_purchase_history", {"item": "办公鼠标"})]),
        ModelTurn(None, [proposal("consult", "new_task", "purchase_history")]),
    ]


def frontend_observation(responses):
    """直接执行页面内原函数，非仅断言 HTML 包含某段字符串。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("需要 Node 执行前端离线展示测试")
    page = (ROOT / "agent.html").read_text(encoding="utf-8")
    functions = page[page.index("function answerText"):page.index("function addConfirmation")]
    render = page[page.index("function render"):page.index("async function confirmAction")]
    restore = page[page.index("async function restoreSession"):page.index("clearSession.onclick")]
    script = (
        "const fs=require('fs'); const responses=JSON.parse(fs.readFileSync(0,'utf8')); "
        "let cards=[],messages=[]; "
        "function addPanel(kind,title,content){cards.push({kind,title,content})}; "
        "function addMessage(role,text){messages.push({role,text})}; function addConfirmation(){}; "
        + functions + render + restore
        + "responses.forEach(render); const live={cards:[...cards],messages:[...messages]}; "
        "cards=[];messages=[]; global.fetch=async()=>({json:async()=>({messages:responses.map(x=>"
        "({role:'assistant',content:x.message,result:x.result}))})}); "
        "restoreSession().then(()=>console.log(JSON.stringify({live,restored:{cards,messages}})));"
    )
    result = subprocess.run([node, "-e", script], input=json.dumps(responses, ensure_ascii=False),
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class HistoryFollowupTests(unittest.TestCase):
    real_model = False
    request_id = REQUEST_ID
    call = Helpers.call
    fixture = Helpers.fixture
    events = Helpers.events

    def setUp(self):
        Helpers.setUp(self)
        self.observations = []
        self.frontend_observations = []
        self.models = []
        self.states = []
        self.fixture(self.request_id, item="办公鼠标", quantity=3, price="50.00", po=True)

    def tearDown(self):
        try:
            if os.getenv("HISTORY_FOLLOWUP_EVIDENCE_DIR"):
                path = Path(os.environ["HISTORY_FOLLOWUP_EVIDENCE_DIR"]) / self._testMethodName
                path.mkdir(parents=True, exist_ok=False)
                events = [json.loads(line) for file in (Path(self.tmp.name) / "traces").glob("*.jsonl")
                          for line in file.read_text(encoding="utf-8").splitlines()]
                evidence = {
                    "test": self.id(), "real_model": self.real_model, "steps": self.observations,
                    "frontend": self.frontend_observations,
                    "model_inputs": [model.received for model in self.models], "trace": events,
                    "final_business_state": snapshot_database(Path(self.tmp.name) / "multi.sqlite"),
                }
                path.joinpath("evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            Helpers.tearDown(self)

    def runtime(self, turns):
        model = ScriptedModel(turns)
        self.models.append(model)
        return AgentRuntime(model, self.api, self.trace, model_name="offline-script"), model

    def send(self, runtime, state, message):
        before = snapshot_database(Path(self.tmp.name) / "multi.sqlite")
        response = runtime.handle_message(state, message)
        after = snapshot_database(Path(self.tmp.name) / "multi.sqlite")
        self.observations.append({"input": message, "response": response, "before": before,
                                  "after": after, "session": runtime.public_session(state)})
        self.assertEqual(before["tables"], after["tables"], "只读追问不得修改业务或审计记录")
        self.assertIsNone(response["confirmation"])
        self.assertIsNone(state.pending_action)
        events = self.events(response["trace_id"])
        for event_type in ("FINAL_RESPONSE", "ROUTE_DECIDED"):
            self.assertEqual(sum(e["event_type"] == event_type for e in events), 1)
        self.assertEqual(next(e["final_response"] for e in events if e["event_type"] == "FINAL_RESPONSE"),
                         response["message"])
        return response

    def field_answer(self, fields, question):
        runtime, model = self.runtime([*initial_turns(), follow(fields)])
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, QUESTIONS[0])
        second = self.send(runtime, state, question)
        self.assertEqual(second["route_reason_code"], "READ_VERIFIED")
        self.assertEqual(second["result"]["history"], first["result"]["history"])
        self.assertEqual(second["result"]["display_mode"], "fields")
        self.assertEqual(second["result"]["requested_fields"], fields)
        self.assertEqual(len(model.received), 3, "字段选择不能增加模型调用")
        return second

    def frontend(self, responses):
        observation = frontend_observation(responses)
        self.frontend_observations.append(observation)
        return observation

    def test_r01_initial_history_query_returns_verified_record(self):
        runtime, _ = self.runtime(initial_turns())
        response = self.send(runtime, runtime.new_session(self.id()), QUESTIONS[0])
        self.assertEqual(response["result"]["result_type"], "history")
        self.assertEqual(response["result"]["display_mode"], "full")
        self.assertIn(f"申请 {REQUEST_ID} / PO ", response["message"])
        self.assertEqual(response["result"]["history"][0]["quantity"], 3)

    def test_r02_supplier_followup_only_answers_supplier(self):
        response = self.field_answer(["supplier_name"], QUESTIONS[1])
        self.assertEqual(response["message"], "供应商：启明办公。")
        self.assertNotIn(REQUEST_ID, response["message"])

    def test_r03_quantity_followup_only_answers_quantity(self):
        response = self.field_answer(["quantity"], QUESTIONS[2])
        self.assertEqual(response["message"], "采购数量：3 件（办公鼠标）。")
        self.assertNotIn("50.00", response["message"])
        self.assertNotIn("启明办公", response["message"])

    def test_r04_request_id_followup_only_answers_id(self):
        response = self.field_answer(["request_id"], QUESTIONS[3])
        self.assertEqual(response["message"], f"采购申请编号：{REQUEST_ID}。")

    def dialogue(self, model=None):
        if model is None:
            runtime, model = self.runtime([
                *initial_turns(), follow(["supplier_name"]), follow(["quantity"]),
                follow(["request_id"]), follow([]),
            ])
        else:
            runtime = AgentRuntime(model, self.api, self.trace, model_name=os.environ["AGENT_MODEL_NAME"])
        state = runtime.new_session(self.id())
        responses = [self.send(runtime, state, question) for question in QUESTIONS]
        return runtime, model, state, responses

    def test_r05_full_record_request_restores_full_scope(self):
        _, _, state, responses = self.dialogue()
        full = responses[-1]
        self.assertEqual(full["result"]["display_mode"], "full")
        self.assertEqual(full["result"]["requested_fields"], [])
        for fact in ("3 件", "50.00", "启明办公", REQUEST_ID):
            self.assertIn(fact, full["message"])
        self.assertEqual(state.slots, {})
        self.assertIsNone(state.active_task_type)

    def test_r06_multiple_records_require_selection_before_field_answer(self):
        self.fixture("HISTORY-SECOND", item="办公鼠标", quantity=5, price="70.00", po=True)
        runtime, _ = self.runtime([*initial_turns(), follow(["quantity"]),
                                   follow(["quantity"], reference_index=2)])
        state = runtime.new_session(self.id())
        initial = self.send(runtime, state, QUESTIONS[0])
        ambiguous = self.send(runtime, state, "那笔的数量呢？")
        self.assertEqual(ambiguous["route_action"], "ASK")
        self.assertEqual(ambiguous["route_reason_code"], "READ_REFERENCE_NOT_UNIQUE")
        self.assertIsNone(ambiguous["result"])
        selected = self.send(runtime, state, "第二笔的数量")
        record = initial["result"]["history"][1]
        self.assertIn(f"{record['quantity']} 件", selected["message"])
        self.assertEqual(selected["result"]["history"], [record])

    def test_r07_consecutive_followups_do_not_repeat_search_or_model_call(self):
        with patch.object(self.api, "search_purchase_history", wraps=self.api.search_purchase_history) as search:
            _, model, _, responses = self.dialogue()
        self.assertEqual(search.call_count, 1)
        self.assertEqual(len(model.received), 6)
        for response in responses[1:]:
            self.assertTrue(any(e["event_type"] == "TRUSTED_READ_CONTEXT_REUSED"
                                for e in self.events(response["trace_id"])))

    def test_r08_short_followup_has_no_card_in_live_or_restored_page(self):
        response = self.field_answer(["supplier_name"], "刚才这笔是哪家公司供货的？")
        observation = self.frontend([response])
        for mode in ("live", "restored"):
            self.assertEqual(observation[mode]["cards"], [])
            self.assertEqual(observation[mode]["messages"][0]["text"], response["message"])

    def test_r09_full_query_keeps_cards_without_duplicate_details(self):
        _, _, _, responses = self.dialogue()
        observation = self.frontend(responses)
        for mode in ("live", "restored"):
            self.assertEqual(len(observation[mode]["cards"]), 2)
            self.assertEqual(observation[mode]["messages"][0]["text"], responses[0]["result"]["summary"])
            card = observation[mode]["cards"][-1]["content"]
            for fact in ("办公鼠标", "3", "50.00", "150.00", "CNY", "启明办公", REQUEST_ID, "PO-"):
                self.assertIn(fact, card)
        self.assertEqual(observation["live"], observation["restored"])

    def test_r10_other_readonly_results_keep_existing_behavior(self):
        from test_multiturn_dialogue import FakeKnowledgeIndex
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("search_products", {"query": "显示器"})]),
            ModelTurn(None, [proposal("consult", "new_task", "product")]),
            ModelTurn(None, [tool("search_knowledge", {"query": "审批规则"})]),
            ModelTurn(None, [proposal("consult", "switch_task", "knowledge")]),
            ModelTurn("达到一万元由 M02 审批。", []),
        ])
        runtime.knowledge_index = FakeKnowledgeIndex()
        state = runtime.new_session(self.id())
        product = self.send(runtime, state, "查询显示器商品目录")
        knowledge = self.send(runtime, state, "换个话题，审批规则是什么？")
        self.assertEqual(product["result"]["result_type"], "products")
        self.assertEqual(knowledge["result"]["result_type"], "knowledge")
        self.assertTrue(knowledge["result"]["citations"])
        observation = self.frontend([product, knowledge])
        self.assertEqual(len(observation["live"]["cards"]), 1)
        self.assertEqual(observation["live"]["messages"][-1]["text"], knowledge["message"])

    def test_multiple_fields_and_alternative_wording_use_only_evidence(self):
        response = self.field_answer(["supplier_name", "quantity"], "说说这笔的供货方和采购件数")
        self.assertEqual(response["message"], "供应商：启明办公；采购数量：3 件（办公鼠标）。")
        self.assertNotIn(REQUEST_ID, response["message"])

    def test_missing_or_invalid_fields_are_not_invented(self):
        runtime, _ = self.runtime([*initial_turns(), follow(["supplier_name"]), follow(["unknown_column"])])
        state = runtime.new_session(self.id())
        self.send(runtime, state, QUESTIONS[0])
        state.trusted_read_contexts["purchase_history"]["data"]["records"][0]["supplier_name"] = None
        response = self.send(runtime, state, QUESTIONS[1])
        self.assertIn("未提供", response["message"])
        self.assertNotIn("启明办公", response["message"])
        invalid = self.send(runtime, state, "这笔的其他字段呢？")
        self.assertEqual(invalid["route_action"], "ASK")
        self.assertIsNone(invalid["result"])

    def test_free_text_model_answer_cannot_override_history_facts(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("search_purchase_history", {"item": "办公鼠标"})]),
            ModelTurn("供应商是虚构公司，买了999件。", []),
        ])
        response = self.send(runtime, runtime.new_session(self.id()), QUESTIONS[0])
        self.assertNotIn("虚构公司", response["message"])
        self.assertNotIn("999", response["message"])
        self.assertIn("启明办公", response["message"])


@unittest.skipUnless(os.getenv("RUN_HISTORY_FOLLOWUP_REAL_MODEL") == "1", "NOT_RUN: 需显式开启真实模型验证")
class RealHistoryFollowupTests(HistoryFollowupTests):
    real_model = True
    # 外部模型仅接触临时库合成记录，不使用用户复现中的真实编号或演示数据库。
    request_id = "SYNTHETIC-HISTORY-ONLY-01"
    # 不继承离线测试作为真实验证数量；独立加载器仅选择下面这一条。
    def test_real_model_consecutive_history_followups(self):
        from evaluation.rag_baseline import RecordingModel
        from evaluation.runner import sanitized_base_url

        output = Path(os.environ["HISTORY_FOLLOWUP_EVIDENCE_DIR"]) / "real_model"
        output.mkdir(parents=True, exist_ok=False)
        model = OpenAICompatibleModel.from_env()
        recorded = RecordingModel(model, output / "model_calls")
        try:
            _, _, state, responses = self.dialogue(recorded)
            output.joinpath("conversation.json").write_text(json.dumps({
                "real_model": True, "model": os.environ["AGENT_MODEL_NAME"],
                "base_url": sanitized_base_url(os.environ["AGENT_MODEL_BASE_URL"]),
                "steps": self.observations, "frontend": self.frontend(responses),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            for response in responses:
                self.assertEqual(response["route_reason_code"], "READ_VERIFIED", response)
            self.assertEqual(responses[1]["message"], "供应商：启明办公。")
            self.assertEqual(responses[2]["message"], "采购数量：3 件（办公鼠标）。")
            self.assertEqual(responses[3]["message"], f"采购申请编号：{self.request_id}。")
            self.assertEqual(responses[-1]["result"]["display_mode"], "full")
            self.assertIsNone(state.active_task_type)
            calls = [e for response in responses for e in self.events(response["trace_id"])
                     if e["event_type"] == "TOOL_CALL_STARTED"]
            self.assertEqual([e["tool_name"] for e in calls], ["search_purchase_history"])
            self.assertEqual(recorded.number, 6, "五轮对话仅首次检索后多一步，不增加回答生成调用")
        finally:
            model.client.close()


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(HistoryFollowupTests)
    suite.addTest(RealHistoryFollowupTests("test_real_model_consecutive_history_followups"))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
