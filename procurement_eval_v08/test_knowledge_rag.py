"""TASK-16：中文知识索引、检索、引用和 Agent 只读路由测试。"""
from __future__ import annotations

import json
import os
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from agent_runtime import AgentRuntime, ModelToolCall, ModelTurn, TraceWriter
from agent_tools import ProcurementApiClient
from knowledge_rag import KnowledgeIndex, KnowledgeIndexError, build_default_index, default_knowledge_dir


class KeywordEncoder:
    dimension = 512
    terms = {"预算": 1, "扣减": 2, "审批": 3, "供应商": 4, "总拥有成本": 5}

    def vector(self, text):
        vector = np.zeros(512, dtype=np.float32)
        for word, index in self.terms.items():
            if word in text:
                vector[index] = 1.0
        if not vector.any():
            vector[0] = 1.0
        return vector / np.linalg.norm(vector)

    def encode_documents(self, texts):
        return np.stack([self.vector(text) for text in texts])

    def encode_query(self, query):
        if query == "今天天气如何":
            vector = np.zeros(512, dtype=np.float32)
            vector[511] = 1.0
            return vector
        return self.vector(query)


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, messages, tools):
        if not self.turns:
            raise AssertionError("Scripted model exhausted")
        return self.turns.pop(0)


class FakeKnowledgeIndex:
    def __init__(self, sufficient=True):
        self.sufficient = sufficient
        self.search_calls = 0

    def search(self, query, top_k=4):
        self.search_calls += 1
        return {
            "query": query,
            "model_id": "fake-512",
            "index_fingerprint": "test-fingerprint",
            "evidence_sufficient": self.sufficient,
            "timings_ms": {"total": 1.0},
            "hits": [{
                "chunk_id": "KB-004#002", "doc_id": "KB-004",
                "title": "预算校验与扣减", "heading": "预算在什么阶段扣减",
                "class": "A", "source": "正式 PRD / backend.py", "source_url": None,
                "text": "仅首次成功创建 PO 时原子扣减预算一次。", "score": 0.88,
            }],
        }


def call(name, arguments, number):
    return ModelToolCall(f"call-{number}", name, arguments)


class KnowledgeIndexTests(unittest.TestCase):
    def test_manifest_chunk_index_persistence_and_sorting(self):
        with tempfile.TemporaryDirectory() as temp:
            index = KnowledgeIndex(
                default_knowledge_dir(), Path(temp), encoder=KeywordEncoder(), min_score=0.5
            )
            built = index.ensure_index()
            self.assertTrue(built["rebuilt"])
            self.assertEqual(built["document_count"], 52)
            self.assertEqual(built["class_counts"], {"A": 11, "B": 41})
            self.assertEqual(np.load(Path(temp) / "vectors.npy").shape[1], 512)
            result = index.search("预算什么时候扣减", top_k=4)
            self.assertTrue(result["evidence_sufficient"])
            self.assertEqual(result["hits"][0]["doc_id"], "KB-004")
            self.assertGreaterEqual(
                result["hits"][0]["score"], result["hits"][1]["score"]
            )
            self.assertFalse(index.ensure_index()["rebuilt"])
            chunks = json.loads((Path(temp) / "chunks.json").read_text(encoding="utf-8"))
            self.assertFalse(any(item["review_class"] == "C" for item in chunks))

    def test_unrelated_question_is_insufficient(self):
        with tempfile.TemporaryDirectory() as temp:
            index = KnowledgeIndex(
                default_knowledge_dir(), Path(temp), encoder=KeywordEncoder(), min_score=0.5
            )
            self.assertFalse(index.search("今天天气如何", top_k=3)["evidence_sufficient"])

    def test_search_exposes_stage_timings(self):
        with tempfile.TemporaryDirectory() as temp:
            index = KnowledgeIndex(
                default_knowledge_dir(), Path(temp), encoder=KeywordEncoder(), min_score=0.5
            )
            self.assertIn("encoder_warmup_ms", index.warmup())
            result = index.search("预算什么时候扣减", top_k=3)
            self.assertEqual(
                set(result["timings_ms"]),
                {"index_check", "encoder_init", "query_encode", "similarity", "total"},
            )
            self.assertGreaterEqual(result["timings_ms"]["total"], 0)

    def test_approved_document_change_rebuilds_and_bad_hash_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            knowledge = root / "knowledge"
            shutil.copytree(default_knowledge_dir(), knowledge)
            index = KnowledgeIndex(
                knowledge, root / "index", encoder=KeywordEncoder(), min_score=0.5
            )
            first = index.ensure_index()
            target = knowledge / "knowledge" / "A" / "V1_04_预算校验与扣减.md"
            target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(KnowledgeIndexError):
                index.ensure_index()
            manifest_path = knowledge / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            new_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            next(item for item in manifest["documents"] if item["id"] == "KB-004")[
                "reviewed_sha256"
            ] = new_hash
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            second = index.ensure_index()
            self.assertTrue(second["rebuilt"])
            self.assertNotEqual(first["corpus_fingerprint"], second["corpus_fingerprint"])


class KnowledgeAgentTests(unittest.TestCase):
    def runtime(self, turns, temp, sufficient=True, knowledge_index=None):
        return AgentRuntime(
            ScriptedModel(turns), ProcurementApiClient("http://127.0.0.1:1", "demo-agent", 1),
            TraceWriter(Path(temp) / "traces"),
            knowledge_index=knowledge_index or FakeKnowledgeIndex(sufficient),
        )

    def test_rag_consultation_uses_llm_and_appends_runtime_citations(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.runtime([
                ModelTurn(None, [call("search_knowledge", {"query": "预算什么时候扣减"}, 1)]),
                ModelTurn(None, [call("propose_task_state", {"intent": "consult"}, 2)]),
                ModelTurn("只有首次成功创建 PO 时才扣减预算。", []),
            ], temp)
            response = runtime.handle_message(runtime.new_session("RAG-CONSULT"), "预算什么时候扣减？")
            self.assertEqual(response["route_action"], "STOP")
            self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
            self.assertIsNone(response["confirmation"])
            self.assertIn("KB-004｜预算校验与扣减｜预算在什么阶段扣减", response["message"])
            self.assertIn("以后端现行规则和真实业务返回为准", response["message"])
            self.assertEqual(response["result"]["citations"][0]["chunk_id"], "KB-004#002")

    def test_rag_insufficient_evidence_refuses_without_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = self.runtime([
                ModelTurn(None, [call("search_knowledge", {"query": "今天天气如何"}, 1)]),
                ModelTurn(None, [call("propose_task_state", {"intent": "consult"}, 2)]),
            ], temp, sufficient=False)
            response = runtime.handle_message(runtime.new_session("RAG-REFUSE"), "今天天气如何？")
            self.assertIn("没有足够证据", response["message"])
            self.assertIsNone(response["confirmation"])
            self.assertEqual(response["result"]["citations"], [])

    def test_duplicate_knowledge_search_in_one_turn_reuses_result(self):
        with tempfile.TemporaryDirectory() as temp:
            knowledge_index = FakeKnowledgeIndex()
            runtime = self.runtime([
                ModelTurn(None, [
                    call("search_knowledge", {"query": "预算什么时候扣减", "top_k": 4}, 1),
                    call("search_knowledge", {"query": "预算什么时候扣减", "top_k": 4}, 2),
                ]),
                ModelTurn(None, [call("propose_task_state", {"intent": "consult"}, 3)]),
                ModelTurn("仅首次成功创建 PO 时扣减预算。", []),
            ], temp, knowledge_index=knowledge_index)
            response = runtime.handle_message(runtime.new_session("RAG-CACHE"), "预算什么时候扣减？")
            self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
            self.assertEqual(knowledge_index.search_calls, 1)
            trace = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(temp, "traces").glob("*.jsonl")
            )
            self.assertIn('"event_type": "TOOL_CALL_CACHE_HIT"', trace)


@unittest.skipUnless(os.getenv("RUN_RAG_MODEL_TEST") == "1", "需要显式开启真实中文 Embedding 测试")
class RealEmbeddingRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index, cls.meta = build_default_index()

    def test_real_embedding_dimension_ranking_and_refusal(self):
        self.assertEqual(self.meta["embedding_dimension"], 512)
        approval = self.index.search("采购金额正好10000元由谁审批？", 4)
        self.assertEqual(approval["hits"][0]["doc_id"], "KB-003")
        self.assertTrue(approval["evidence_sufficient"])
        tco = self.index.search("如何比较总拥有成本？", 4)
        self.assertEqual(tco["hits"][0]["doc_id"], "PKB-V2-015")
        self.assertFalse(self.index.search("今天天气怎么样？", 4)["evidence_sufficient"])


if __name__ == "__main__":
    unittest.main()
