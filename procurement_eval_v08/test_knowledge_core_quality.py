"""BC-32 核心规则补证回归；真实验收复用现有 HTTP/SQLite 夹具。"""
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import numpy as np

from knowledge_rag import KnowledgeIndex, default_knowledge_dir
import test_knowledge_rag as rag_helpers
import test_runtime_real_acceptance as http_helpers


CORE_CASES = [
    ("approval", "先暂停一下，公司采购超过一万元需要什么审批？", {"KB-003#002", "KB-003#004"}),
    ("approval", "顺便问一句，采购金额正好一万元要谁批，需要报价凭证吗？", {"KB-003#002"}),
    ("approval", "买东西不到一万元和超过一万元分别由谁审批，需不需要报价？", {"KB-003#002"}),
    ("approval", "公司采购审批的金额分界和报价要求是什么？", {"KB-003#002"}),
    ("quotation", "先暂停一下，买一万五的设备要准备什么报价材料？我说我有报价就算核验了吗？", {"KB-003#002", "KB-003#003"}),
    ("quotation", "高金额采购的可信报价凭证由谁核验？系统能识别附件真假吗？", {"KB-003#003"}),
    ("quotation", "超过万元的采购有报价单就能直接过审批吗？", {"KB-003#002", "KB-003#003", "KB-003#004"}),
    ("quotation", "聊个制度问题，提交的报价附件会自动做OCR和真实性鉴别吗？", {"KB-003#003"}),
    ("budget", "先暂停一下，采购预算什么时候扣减？提交、人工审批、创建PO、重复请求和创建失败分别怎么算？", {"KB-004#002"}),
    ("budget", "我想问下预算规则：提交申请和审批会扣钱吗，下单成功、失败、重复点又怎么算？", {"KB-004#002"}),
    ("budget", "采购预算是在提交申请时扣，还是审批通过时扣？", {"KB-004#002"}),
    ("budget", "帮我解释预算扣减规则，重复创建采购订单会再扣一次吗？失败呢？", {"KB-004#002"}),
    ("combined", "超过一万元由谁审批、要什么报价凭证，提交、审批还是生成PO时扣预算？",
     {"KB-003#002", "KB-003#003", "KB-003#004", "KB-004#002"}),
]


class LowScoreEncoder(rag_helpers.KeywordEncoder):
    def encode_query(self, query):
        self.query = query
        result = np.zeros(512, dtype=np.float32)
        result[511] = 1
        return result


class CoreRuleRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.encoder = LowScoreEncoder()
        self.index = KnowledgeIndex(default_knowledge_dir(), Path(self.tmp.name), encoder=self.encoder)
        self.index.ensure_index()

    def test_core_evidence_comes_from_approved_original_sections_even_at_low_vector_score(self):
        # 低向量分数下，明确的核心制度节仍可作为原文依据；不能伪造或抬高相似度。
        for _, question, required in CORE_CASES:
            with self.subTest(question=question):
                result = self.index.search(question, 1)
                self.assertEqual(self.encoder.query, question)
                self.assertEqual(result["query"], question)
                self.assertTrue(result["evidence_sufficient"])
                self.assertTrue(required <= {h["chunk_id"] for h in result["hits"]})
                originals = {c.chunk_id: c for c in self.index._chunks}
                for hit in result["hits"]:
                    self.assertEqual(hit["text"], originals[hit["chunk_id"]].text)
                    self.assertEqual(hit["score"], 0)

    def test_unrelated_and_live_fact_questions_keep_original_refusal(self):
        for question in ("今天天气如何", "不同供应商的报价单怎么比较才公平？", "如何比较总拥有成本？",
                         "我现在剩余预算是多少？", "显示器以前的成交价是多少？"):
            with self.subTest(question=question):
                result = self.index.search(question, 4)
                self.assertFalse(result["evidence_sufficient"])
                self.assertEqual(result["core_rule_chunk_ids"], [])
                self.assertEqual(len(result["hits"]), 4)

    def test_top_k_candidates_retained_deduplicated_and_final_evidence_bounded(self):
        q = CORE_CASES[-1][1]
        scores = self.index._vectors @ self.encoder.encode_query(q)
        originals = [self.index._chunks[int(i)].chunk_id for i in np.argsort(-scores, kind="stable")[:4]]
        result = self.index.search(q, 4)
        ids = [h["chunk_id"] for h in result["hits"]]
        self.assertEqual(ids[:4], originals)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(len(ids), 8)
        for top_k in (1, 4, 8):
            self.assertTrue(CORE_CASES[-1][2] <= {h["chunk_id"] for h in self.index.search(q, top_k)["hits"]})
            self.assertLessEqual(len(self.index.search(q, top_k)["hits"]), 8)

    def test_missing_or_non_authoritative_sections_cannot_bypass_vector_threshold(self):
        originals = list(self.index._chunks)
        for mode in ("missing", "unapproved_class"):
            with self.subTest(mode=mode):
                self.index._chunks = [replace(c, review_class="B") if c.doc_id == "KB-003" else c
                                      for c in originals if mode != "missing" or c.doc_id != "KB-003"]
                # 即使通用片段的向量分数满分，也不能替代缺失的原始制度条款。
                self.index._vectors = np.stack([self.encoder.encode_query("test") for c in self.index._chunks])
                self.index.ensure_index = lambda: {"corpus_fingerprint": "missing-core-controlled"}
                result = self.index.search(CORE_CASES[0][1])
                self.assertFalse(result["evidence_sufficient"])
                self.assertEqual(result["core_rule_chunk_ids"], [])


@unittest.skipUnless(os.getenv("RUN_KNOWLEDGE_CORE_REAL") == "1", "需要显式开启定向真实模型验收")
class CoreKnowledgeRealTests(unittest.TestCase):
    setUpClass = classmethod(http_helpers.RuntimeRealAcceptanceTests.setUpClass.__func__)
    tearDownClass = classmethod(http_helpers.RuntimeRealAcceptanceTests.tearDownClass.__func__)
    setUp = http_helpers.RuntimeRealAcceptanceTests.setUp
    tearDown = http_helpers.RuntimeRealAcceptanceTests.tearDown
    session = http_helpers.RuntimeRealAcceptanceTests.session
    state = http_helpers.RuntimeRealAcceptanceTests.state
    send = http_helpers.RuntimeRealAcceptanceTests.send
    assert_readonly = http_helpers.RuntimeRealAcceptanceTests.assert_readonly

    def test_core_first_answers_and_suspended_context(self):
        self.check_first_answers(CORE_CASES)

    def test_quotation_high_amount_dependency(self):
        self.check_first_answers(CORE_CASES[4:8])

    def check_first_answers(self, cases):
        for topic, question, required in cases:
            if self._outcome.result.shouldStop:
                return
            with self.subTest(question=question):
                client = self.session()
                paused = question.startswith("先暂停一下")
                if paused:
                    self.send(client, "帮我创建采购申请：键盘2个，单价120元，用途办公。")
                    original = dict(self.state(client).slots)
                response = self.send(client, question)
                self.assertIn(response["route_reason_code"], {"READ_VERIFIED", "INTERRUPTED_TASK_WAITING_RESUME"})
                self.assertIsNone(response["confirmation"])
                result = response.get("result") or {}
                self.assertTrue(required <= {h["chunk_id"] for h in result.get("knowledge", [])}, response)
                self.assertTrue(result.get("citations"))
                if topic in {"approval", "combined"}:
                    self.assertIn("M02", response["message"])
                    self.assertIn("报价", response["message"])
                if topic in {"quotation", "combined"}:
                    self.assertIn("报价", response["message"])
                if "一万五" in question:
                    self.assertIn("M02", response["message"])
                if topic in {"budget", "combined"}:
                    self.assertRegex(response["message"], r"PO|采购订单")
                    self.assertIn("扣", response["message"])
                if paused:
                    self.assertEqual(self.state(client).suspended_task.slots, original)
                    self.send(client, "继续刚才的采购申请")
                    self.assertEqual(self.state(client).slots, original)
                    self.send(client, "放弃当前任务")


if __name__ == "__main__":
    unittest.main(verbosity=2)
