"""BC-24：隔离回放原始检索/多段生成，验证最终可见回答，不冒充真实模型评测。"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_runtime import AgentRuntime, ModelToolCall, ModelTurn, OpenAICompatibleModel, TraceWriter
from agent_tools import ProcurementApiClient
from backend import Store
from evaluation.fixture import snapshot_database
from test_knowledge_rag import call


class ReplayIndex:
    def __init__(self, results):
        self.results = copy.deepcopy(results)

    def search(self, query, top_k=4):
        result = self.results.pop(0)
        assert result['query'] == query
        return result


class EvidenceModel:
    def __init__(self, turns, failure=False, generator=None):
        self.turns = list(turns)
        self.received = []
        self.failure = failure
        self.generator = generator

    def complete(self, messages, tools):
        self.received.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        if tools:
            return self.turns.pop(0)
        if self.generator:
            return self.generator.complete(messages, tools)
        if self.failure:
            raise RuntimeError('controlled generation failure')
        # 回放旧生成行为：局部证据缺扣减条款时拒答，完整证据才给出一致答案。
        body = messages[-1]['content']
        evidence = json.loads(body.split('\n证据：', 1)[1])
        text = '\n'.join(hit['text'] for hit in evidence)
        answer = '可保留草稿，但不等于已提交。可延期、缩量或申请追加预算，不得拆单规避。'
        answer += ('仅实际生成 PO 才扣减预算一次。' if '仅实际生成PO才扣减预算一次' in text
                   else '预算扣减时点知识资料不足。')
        return ModelTurn(answer, [])


class KnowledgeAnswerAggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'fixture.sqlite'
        Store(self.db)
        self.before = snapshot_database(self.db)
        self.model = None
        self.response = None
        self.model_info = {'real_model': False, 'mode': 'scripted_regression'}

    def tearDown(self):
        after = snapshot_database(self.db)
        self.assertEqual(self.before, after, '知识咨询不得改变 SQLite 业务状态')
        # 仅显式要求留存时保存本轮新证据，默认测试仍使用临时目录。
        if os.getenv('BC24_TEST_EVIDENCE_DIR'):
            self.root.joinpath('evidence.json').write_text(json.dumps({
                'test': self.id(), 'model': self.model_info, 'response': self.response,
                'model_calls': self.model.received if self.model else [],
                'business_state_before': self.before, 'business_state_after': after,
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            shutil.copytree(self.root, Path(os.environ['BC24_TEST_EVIDENCE_DIR']) / self._testMethodName)
        self.temp.cleanup()

    def execute(self, results, question, *, interrupted=False, failure=False, turns=None, generator=None):
        if turns is None:
            turns = [ModelTurn(None, [call('search_knowledge', {'query': question}, n)])
                     for n in range(len(results))]
            turns.append(ModelTurn(None, [call('propose_task_state', {'intent': 'consult'}, 9)]))
        self.model = EvidenceModel(turns, failure=failure, generator=generator)
        runtime = AgentRuntime(self.model, ProcurementApiClient('http://127.0.0.1:1', 'demo-agent', 1),
                               TraceWriter(self.root / 'traces'), knowledge_index=ReplayIndex(results))
        state = runtime.new_session('BC24-regression')
        if interrupted:
            state.active_task_type = 'create'
            state.slots = {'item': '显示器'}
        self.response = runtime.handle_message(state, question)
        self.assertIsNone(self.response['confirmation'])
        self.assertIsNone(state.pending_action)
        events = [json.loads(line) for p in (self.root / 'traces').glob('*.jsonl')
                  for line in p.read_text(encoding='utf-8').splitlines()]
        finals = [e for e in events if e['event_type'] == 'FINAL_RESPONSE']
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]['final_response'], self.response['message'])
        self.assertEqual(len([e for e in events if e['event_type'] == 'ROUTE_DECIDED']), 1)
        return runtime, state, events

    @staticmethod
    def result(question, texts, sufficient=True):
        return {'query': question, 'evidence_sufficient': sufficient, 'hits': [
            {'chunk_id': f'test#{n}', 'doc_id': 'test', 'title': '测试知识', 'heading': str(n),
             'class': 'B', 'source': '隔离夹具', 'text': text}
            for n, text in texts
        ]}

    @staticmethod
    def original_d02():
        folder = Path(__file__).parent / 'evaluation/runs/rag_baseline_v1_2_1_20261014/cases/RGV1-D02'
        original = folder / 'evidence.json'
        evidence = json.loads(original.read_text(encoding='utf-8'))
        results = [e['sanitized_result'] for e in evidence['trace']['events']
                   if e['event_type'] == 'TOOL_CALL_COMPLETED' and e.get('tool_name') == 'search_knowledge']
        question = evidence['steps'][0]['user_input']
        turns = []
        for number in (1, 2, 3):
            turn = json.loads((folder / f'model_calls/{number:02d}.json').read_text(encoding='utf-8'))['model_turn']
            turns.append(ModelTurn(turn['content'], [ModelToolCall(**c) for c in turn['tool_calls']]))
        return results, question, turns

    def test_d02_original_three_retrievals_one_consistent_answer(self):
        results, question, turns = self.original_d02()
        self.assertEqual([len(r['hits']) for r in results], [6, 6, 8])
        _, state, events = self.execute(results, question, turns=turns)
        self.assertEqual(len([c for c in self.model.received if not c['tools']]), 1)
        self.assertEqual(len([e for e in events if e['event_type'] == 'KNOWLEDGE_RETRIEVED']), 3)
        self.assertEqual(len([e for e in events if e['event_type'] == 'KNOWLEDGE_ANSWER_GENERATED']), 1)
        self.assertNotIn('知识资料不足', self.response['message'])
        for phrase in ('草稿', '不等于已提交', '追加预算', '不得拆单', '仅实际生成 PO'):
            self.assertIn(phrase, self.response['message'])
        citations = self.response['result']['citations']
        self.assertEqual(len(citations), 8)
        self.assertEqual(len({c['chunk_id'] for c in citations}), 8)
        self.assertEqual(self.response['message'].count('来源：'), 1)
        self.assertIsNone(state.active_task_type)

    def test_later_sufficient_result_replaces_global_insufficiency(self):
        q = '先咨询预算什么时候扣？'
        self.execute([self.result(q, [], False), self.result(q, [(1, '仅实际生成PO才扣减预算一次')])], q)
        self.assertNotIn('没有足够证据', self.response['message'])
        self.assertEqual(len([c for c in self.model.received if not c['tools']]), 1)

    def test_all_insufficient_results_refuse_once_without_generation(self):
        q = '先咨询未收录的制度？'
        self.execute([self.result(q, [], False), self.result(q, [], False)], q)
        self.assertEqual(self.response['message'].count('没有足够证据'), 1)
        self.assertEqual(len([c for c in self.model.received if not c['tools']]), 0)

    def test_union_retains_early_topic_not_only_last_retrieval(self):
        q = '先咨询草稿及预算什么时候扣？'
        self.execute([self.result(q, [(1, '草稿不能等同已提交')]),
                      self.result(q, [(2, '仅实际生成PO才扣减预算一次')])], q)
        generated = [c for c in self.model.received if not c['tools']]
        self.assertEqual(len(generated), 1)
        body = generated[0]['messages'][-1]['content']
        self.assertIn('草稿不能等同已提交', body)
        self.assertIn('仅实际生成PO才扣减预算一次', body)

    def test_interrupted_consultation_has_one_final_with_resume(self):
        q = '先咨询预算什么时候扣？'
        _, state, _ = self.execute([self.result(q, [(1, '仅实际生成PO才扣减预算一次')])] * 2,
                                  q, interrupted=True)
        self.assertEqual(self.response['route_reason_code'], 'INTERRUPTED_TASK_WAITING_RESUME')
        self.assertIn('是否继续刚才的采购任务', self.response['message'])
        self.assertEqual(state.suspended_task.slots, {'item': '显示器'})

    def test_generation_failure_has_single_fallback_without_partial_answers(self):
        q = '先咨询预算什么时候扣？'
        _, _, events = self.execute([self.result(q, [(1, '仅实际生成PO才扣减预算一次')])] * 2,
                                    q, failure=True)
        self.assertEqual(self.response['message'].count('模型未能完成'), 1)
        self.assertEqual(len([e for e in events if e['event_type'] == 'KNOWLEDGE_ANSWER_FAILED']), 1)

    @unittest.skipUnless(os.getenv('RUN_BC24_MODEL_TEST') == '1', '真实生成仅在定向验收时显式开启')
    def test_d02_real_generation_on_archived_retrieval_union(self):
        from evaluation.rag_baseline import RecordingModel
        from evaluation.runner import load_env_file, sanitized_base_url

        load_env_file(Path(__file__).parent / '.env')
        model = OpenAICompatibleModel.from_env()
        self.model_info = {
            'real_model': True, 'mode': 'archived_tool_replay_real_generation_not_full_e2e',
            'model_name': model.model,
            'base_url': sanitized_base_url(os.environ['AGENT_MODEL_BASE_URL']),
        }
        try:
            results, question, turns = self.original_d02()
            _, _, events = self.execute(results, question, turns=turns,
                                       generator=RecordingModel(model, self.root / 'real_model_calls'))
        finally:
            model.client.close()
        self.assertEqual(len([e for e in events if e['event_type'] == 'KNOWLEDGE_ANSWER_GENERATED']), 1)
        self.assertFalse(any(e['event_type'] == 'KNOWLEDGE_ANSWER_FAILED' for e in events))
        self.assertEqual(len([c for c in self.model.received if not c['tools']]), 1)
        for phrase in ('草稿', '预算', 'PO', '拆'):
            self.assertIn(phrase, self.response['message'])
        self.assertNotIn('知识资料不足', self.response['message'])
        self.assertNotIn('未明确说明扣减时点', self.response['message'])


if __name__ == '__main__':
    unittest.main()
