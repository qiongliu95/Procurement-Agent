"""REQ-10/11、BC-25/26/29：脚本模型及临时 HTTP/SQLite 定向回归。"""
import json
import gc
import os
import unittest
from datetime import datetime
from pathlib import Path

import test_context_management as helpers
from agent_runtime import AgentRuntime, ModelTurn, ModelToolCall
from evaluation.fixture import snapshot_database
from intent_routing_validation import intent_from_events
from test_knowledge_rag import FakeKnowledgeIndex


def proposal(**values):
    return ModelTurn(None, [helpers.tool('propose_task_state', values)])


class RuntimeMinimalRepairTests(unittest.TestCase):
    call = helpers.ContextManagementTests.call
    fixture = helpers.ContextManagementTests.fixture
    trace_events = helpers.ContextManagementTests.trace_events

    def setUp(self):
        helpers.ContextManagementTests.setUp(self)
        self.observations = []
        self.runtimes = []
        self.states = {}

    def tearDown(self):
        if os.getenv('RUNTIME_MINIMAL_EVIDENCE_DIR'):
            path = Path(os.environ['RUNTIME_MINIMAL_EVIDENCE_DIR']) / self._testMethodName
            path.mkdir(parents=True, exist_ok=False)
            events = [json.loads(line) for p in (Path(self.tmp.name) / 'traces').glob('*.jsonl')
                      for line in p.read_text(encoding='utf-8').splitlines()]
            data = {'test': self.id(), 'real_model': False, 'steps': self.observations,
                    'model_inputs': [m.received for _, m in self.runtimes], 'trace': events,
                    'final_business_state': snapshot_database(Path(self.tmp.name) / 'context.sqlite'),
                    'final_sessions': [runtime.public_session(state) for runtime, state in self.states.values()]}
            path.joinpath('evidence.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        # 旧 IR 计数函数的 SQLite 上下文不显式 close；仅回收测试引用，避免 Windows 清理被占用。
        gc.collect()
        helpers.ContextManagementTests.tearDown(self)

    def runtime(self, turns, *, sufficient=True):
        model = helpers.CapturingModel(turns)
        runtime = AgentRuntime(model, self.api, self.trace, knowledge_index=FakeKnowledgeIndex(sufficient))
        self.runtimes.append((runtime, model))
        return runtime, model

    def send(self, runtime, state, message):
        self.states[state.session_id] = (runtime, state)
        before = snapshot_database(Path(self.tmp.name) / 'context.sqlite')
        result = runtime.handle_message(state, message)
        self.observations.append({'input': message, 'output': result, 'before': before,
                                  'after': snapshot_database(Path(self.tmp.name) / 'context.sqlite'),
                                  'session': runtime.public_session(state)})
        events = self.trace_events(result['trace_id'])
        for event_type in ('ROUTE_DECIDED', 'FINAL_RESPONSE'):
            self.assertEqual(len([e for e in events if e['event_type'] == event_type]), 1)
        return result

    def assert_no_write(self, result):
        self.assertIsNone(result['confirmation'])
        self.assertFalse(any(e.get('tool_name') in {'create_request', 'update_request', 'submit_request'}
                             for e in self.trace_events(result['trace_id'])))
        before, after = (self.observations[-1][key]['tables'] for key in ('before', 'after'))
        # 业务 GET 可追加只读审计；业务表必须完全一致，审计也不能包含写操作。
        self.assertEqual({k: v for k, v in before.items() if k != 'events'},
                         {k: v for k, v in after.items() if k != 'events'})
        self.assertEqual(after['events'][:len(before['events'])], before['events'])
        self.assertTrue(all(e['type'] in {'REQUEST_STATUS_READ', 'PO_STATUS_READ'}
                            for e in after['events'][len(before['events']):]))

    def interrupted(self):
        self.fixture('A001')
        self.fixture('A002')
        runtime, model = self.runtime([
            proposal(intent='update', request_id='A001', quantity=4),
            *helpers.ContextManagementTests.query_turns('A002'),
        ])
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, '把申请 A001 的数量改成4台。')
        old_action = first['confirmation']['action_id']
        query = self.send(runtime, state, '中途先查询 A002 的状态。')
        self.assertEqual(query['route_reason_code'], 'INTERRUPTED_TASK_WAITING_RESUME')
        self.assertEqual(state.suspended_task.target_request_id, 'A001')
        self.assertEqual(state.suspended_task.slots['quantity'], 4)
        self.assertIsNone(state.pending_action)
        return runtime, model, state, old_action

    def test_explicit_create_has_only_write_confirmation(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool('match_supplier', {'name': '启明办公'})]),
            proposal(intent='create', item='显示器', quantity=2, unit_price='800',
                     supplier_name='启明办公', purpose='办公'),
        ])
        state = runtime.new_session(self.id())
        result = self.send(runtime, state, '帮我创建采购申请：2台显示器，单价800元，供应商启明办公，用于办公。')
        self.assertEqual(result['route_action'], 'CONFIRM')
        self.assertEqual(len(self.observations), 1)
        self.assertEqual(self.observations[-1]['before']['tables'], self.observations[-1]['after']['tables'])

    def test_explicit_update_has_only_write_confirmation(self):
        self.fixture('A001')
        runtime, _ = self.runtime([proposal(intent='update', request_id='A001', quantity=3)])
        state = runtime.new_session(self.id())
        result = self.send(runtime, state, '把申请 A001 的数量改成3台。')
        self.assertEqual(result['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.target_request_id, 'A001')
        self.assertEqual(state.pending_action.expected_version, 1)

    def test_interrupted_ambiguous_change_then_natural_resume_refreshes_version(self):
        runtime, model, state, old_action = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3))
        ambiguous = self.send(runtime, state, '那就改成3台。')
        self.assertEqual(ambiguous['route_action'], 'ASK')
        self.assertIn('A001', ambiguous['message'])
        self.assertIn('3', ambiguous['message'])
        self.assert_no_write(ambiguous)
        # 模拟暂停期间业务对象被其他合法操作更新，恢复不能使用快照版本。
        status, _ = self.call('POST', '/requests/A001/update', {'purpose': '最新用途', 'expected_version': 1})
        self.assertEqual(status, 200)
        model.turns.append(proposal(intent='update', request_id='A001', quantity=3))
        resumed = self.send(runtime, state, '好的，继续修改原来的 A001，数量改成3台。')
        self.assertEqual(resumed['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.target_request_id, 'A001')
        self.assertEqual(state.pending_action.expected_version, 2)
        self.assertEqual(state.pending_action.args, {'quantity': 3})
        self.assertIsNone(state.suspended_task)
        self.assertNotEqual(resumed['confirmation']['action_id'], old_action)
        self.assertEqual(self.call('GET', '/requests/A001')[1]['quantity'], 2)
        done = runtime.confirm(state, resumed['confirmation']['action_id'], 'confirm')
        self.observations.append({'confirmation_output': done, 'session_after_confirm': runtime.public_session(state),
                                  'after_confirm': snapshot_database(Path(self.tmp.name) / 'context.sqlite')})
        self.assertEqual(done['outcome_status'], 'SUCCESS')
        self.assertEqual(self.call('GET', '/requests/A001')[1]['quantity'], 3)
        self.assertEqual(self.call('GET', '/requests/A002')[1]['quantity'], 2)
        runtime.confirm(state, old_action, 'confirm')
        runtime.confirm(state, resumed['confirmation']['action_id'], 'confirm')
        self.assertEqual(self.call('GET', '/requests/A001')[1]['version'], 3)

    def test_plain_continue_rebuilds_update_not_only_restores_slots(self):
        runtime, _, state, _ = self.interrupted()
        resumed = self.send(runtime, state, '继续')
        self.assertEqual(resumed['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.args, {'quantity': 4})
        self.assertEqual(state.pending_action.target_request_id, 'A001')

    def test_resume_keeps_original_amount_conflict_validation(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A001', quantity=3))
        result = self.send(runtime, state, '继续修改 A001，数量改成3台，总金额100元')
        self.assertEqual(result['route_action'], 'ASK')
        self.assertIn('冲突', result['message'])
        self.assert_no_write(result)

    def test_natural_resume_without_id_retains_staged_changes_not_query_fields(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3,
                                    unit_price='999', purpose='A002错误用途'))
        self.send(runtime, state, '那就改成3台')
        self.assertEqual(state.resume_proposal, {'quantity': 3})
        model.turns.append(proposal(intent='update', request_id='A002', quantity=4, unit_price='999'))
        result = self.send(runtime, state, '好的，继续修改原来的申请')
        self.assertEqual(result['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.target_request_id, 'A001')
        self.assertEqual(state.pending_action.args, {'quantity': 3})

    def test_relative_resume_uses_latest_quantity_and_absolute_change_replaces_delta(self):
        for absolute in (False, True):
            with self.subTest(absolute=absolute):
                rid = 'REL-ABS' if absolute else 'REL-DELTA'
                self.fixture(rid)
                runtime, model = self.runtime([
                    proposal(intent='update', request_id=rid, relative_quantity_delta=2),
                    *helpers.ContextManagementTests.query_turns(rid),
                ])
                state = runtime.new_session()
                self.send(runtime, state, f'把 {rid} 的数量增加2台')
                self.send(runtime, state, f'先查询 {rid} 的状态')
                self.call('POST', f'/requests/{rid}/update', {'quantity': 5, 'expected_version': 1})
                if absolute:
                    model.turns.append(proposal(intent='update', request_id=rid, quantity=3))
                    result = self.send(runtime, state, f'继续修改 {rid}，数量改成3台')
                else:
                    result = self.send(runtime, state, '继续')
                self.assertEqual(result['route_action'], 'CONFIRM')
                self.assertEqual(state.pending_action.args, {'quantity': 3 if absolute else 7})
                self.assertEqual(state.pending_action.expected_version, 2)

    def test_affirmation_of_ownership_is_not_write_authorization(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3))
        self.send(runtime, state, '那就改成3台')
        resumed = self.send(runtime, state, '是')
        self.assertEqual(resumed['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.args, {'quantity': 3})
        self.assertEqual(self.call('GET', '/requests/A001')[1]['quantity'], 2)

    def test_natural_rejection_clears_suspended_without_writing(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3))
        self.send(runtime, state, '那就改成3台')
        self.assert_no_write(self.send(runtime, state, '不继续修改原来的任务了'))
        self.assertIsNone(state.suspended_task)
        self.assertEqual(state.slots, {})

    def test_explicit_other_object_does_not_require_abandoning_old_task(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A001', quantity=3))
        result = self.send(runtime, state, '把 A002 的数量改成3台')
        self.assertEqual(result['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.target_request_id, 'A002')
        self.assertEqual(state.suspended_task.target_request_id, 'A001')

    def test_confirming_explicit_other_object_does_not_resume_old_task(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3))
        pending = self.send(runtime, state, '把 A002 的数量改成3台')
        done = runtime.handle_message(state, '确认')
        self.assertEqual(done['outcome_status'], 'SUCCESS')
        self.assertEqual(self.call('GET', '/requests/A002')[1]['quantity'], 3)
        self.assertEqual(self.call('GET', '/requests/A001')[1]['quantity'], 2)
        self.assertEqual(state.suspended_task.target_request_id, 'A001')
        self.assertIsNone(state.resume_proposal)
        restored = self.send(runtime, state, '继续')
        self.assertEqual(restored['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.target_request_id, 'A001')

    def test_second_interruption_cannot_overwrite_existing_suspended_task(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A002', quantity=3))
        self.send(runtime, state, '把 A002 的数量改成3台')
        model.turns.extend(helpers.ContextManagementTests.query_turns('A001'))
        result = self.send(runtime, state, '先查询 A001')
        self.assertEqual(result['route_action'], 'ASK')
        self.assertEqual(state.suspended_task.target_request_id, 'A001')
        self.assertEqual(state.suspended_task.slots, {'quantity': 4})
        self.assert_no_write(result)

    def test_still_ambiguous_two_ids_cannot_restore_or_confirm(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.append(proposal(intent='update', request_id='A001', quantity=3))
        result = self.send(runtime, state, '继续修改 A001 或 A002，数量3台')
        self.assertEqual(result['route_action'], 'ASK')
        self.assert_no_write(result)

    def test_rules_with_write_words_remain_consultation(self):
        for message in ('创建PO需要什么条件？', '采购申请提交后预算就会扣掉吗？要是 PO 创建失败或者请求重复了呢？',
                        '假设供应商标记为new，未完成准入前能直接创建PO吗？', '申请 A001 还能修改吗？'):
            with self.subTest(message=message):
                runtime, _ = self.runtime([
                    ModelTurn(None, [helpers.tool('search_knowledge', {'query': message})]),
                    proposal(intent='consult'), ModelTurn('根据资料，只有实际创建 PO 才扣减预算。', []),
                ])
                state = runtime.new_session(message)
                result = self.send(runtime, state, message)
                self.assertEqual(result['route_reason_code'], 'READ_VERIFIED')
                self.assert_no_write(result)
                self.assertIsNone(state.active_task_type)

    def test_consultation_can_interrupt_active_create(self):
        q = '创建PO需要哪些审批材料？'
        runtime, _ = self.runtime([
            proposal(intent='create', item='显示器'),
            ModelTurn(None, [helpers.tool('search_knowledge', {'query': q})]),
            proposal(intent='consult'), ModelTurn('以已确认审批规则为准。', []),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, '帮我创建显示器采购申请')
        result = self.send(runtime, state, q)
        self.assertEqual(result['route_reason_code'], 'INTERRUPTED_TASK_WAITING_RESUME')
        self.assertEqual(state.suspended_task.slots, {'item': '显示器'})
        self.assert_no_write(result)

    def test_ir_ambiguous_objects_expose_raw_and_accepted_intent(self):
        self.fixture('A001')
        self.fixture('A002')
        for intent in ('query', 'query', 'update'):
            runtime, _ = self.runtime([ModelTurn(None, [helpers.tool('list_requests', {})]),
                                       proposal(intent=intent, quantity=3)])
            state = runtime.new_session()
            result = self.send(runtime, state, '我上次的采购申请' + ('数量改成3台' if intent == 'update' else '现在什么状态？'))
            events = self.trace_events(result['trace_id'])
            self.assertEqual(result['route_reason_code'], 'REQUEST_OBJECT_NOT_UNIQUE')
            self.assertEqual(intent_from_events(events), intent)
            self.assertEqual([e['accepted_intent'] for e in events if e['event_type'] == 'TASK_INTENT_ACCEPTED'], [intent])
            self.assertFalse(any(e['event_type'] == 'TASK_PROPOSAL_VALIDATED' for e in events))
            self.assert_no_write(result)

    def test_ir_original_three_cases_keep_original_scoring_conditions(self):
        import intent_routing_validation as ir

        ir.seed_requests(self.base)
        root = Path(__file__).parent / 'evaluation/runs/unified-20261014T144438-03a3c998/ir'
        for cid in ('IR06', 'IR13', 'IR18'):
            folder = next(root.glob(cid + '-*'))
            turns = []
            for file in sorted((folder / 'model_calls').glob('*.json')):
                turn = json.loads(file.read_text(encoding='utf-8'))['model_turn']
                turns.append(ModelTurn(turn['content'], [ModelToolCall(**call) for call in turn['tool_calls']]))
            runtime, _ = self.runtime(turns)
            case = next(c for c in ir.CASES if c['id'] == cid)
            result = ir.evaluate_case(case, runtime, Path(self.tmp.name) / 'traces' / (datetime.now().strftime('%Y-%m-%d') + '.jsonl'),
                                      Path(self.tmp.name) / 'context.sqlite')
            self.observations.append({'original_case_replay': result})
            self.assertTrue(all(result['checks'].values()), result)
            self.assertEqual(result['turns'][0]['object_validation'], 'AMBIGUOUS')
            self.assertEqual(result['turns'][0]['actual_intent'], result['turns'][0]['runtime_accepted_intent'])

    def test_ir_legacy_trace_still_reads_validated_intent(self):
        self.assertEqual(intent_from_events([{'event_type': 'TASK_PROPOSAL_VALIDATED', 'proposal': {'intent': 'query'}}]), 'query')
        self.assertIsNone(intent_from_events([]))

    def test_direct_answer_uses_evidence_finalizes_and_clears_read_task(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool('search_knowledge', {'query': '预算什么时候扣？'})]),
            ModelTurn('根据检索证据，只有实际创建 PO 才扣减预算。', []),
        ])
        state = runtime.new_session(self.id())
        state.active_task_type = 'consult'
        result = self.send(runtime, state, '预算什么时候扣？')
        self.assertEqual(result['route_reason_code'], 'READ_VERIFIED')
        self.assertIn('根据检索证据', result['message'])
        self.assertTrue(result['result']['citations'])
        self.assertIsNone(state.active_task_type)
        self.assertEqual(state.task_messages, [])
        self.assert_no_write(result)

    def test_insufficient_direct_answer_is_unresolved_not_success(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool('search_knowledge', {'query': '礼品采购制度？'})]),
            ModelTurn('资料不足，不能判断允许或禁止。', []),
        ], sufficient=False)
        result = self.send(runtime, runtime.new_session(), '礼品采购制度？')
        self.assertEqual(result['route_action'], 'ASK')
        self.assertNotEqual(result['outcome_status'], 'SUCCESS')
        self.assertIn('没有足够证据', result['message'])
        self.assert_no_write(result)

    def test_original_b17_u01_direct_answers_replay_with_real_evidence(self):
        from test_knowledge_answer_aggregation import ReplayIndex

        for cid in ('RGV1-B17', 'RGV1-U01'):
            folder = Path(__file__).parent / 'evaluation/runs/rag_baseline_v1_2_1_20261014/cases' / cid
            old = json.loads((folder / 'evidence.json').read_text(encoding='utf-8'))
            retrievals = [e['sanitized_result'] for e in old['trace']['events']
                          if e['event_type'] == 'TOOL_CALL_COMPLETED' and e.get('tool_name') == 'search_knowledge']
            turns = []
            for file in sorted((folder / 'model_calls').glob('*.json')):
                turn = json.loads(file.read_text(encoding='utf-8'))['model_turn']
                turns.append(ModelTurn(turn['content'], [ModelToolCall(**call) for call in turn['tool_calls']]))
            runtime, _ = self.runtime(turns)
            runtime.knowledge_index = ReplayIndex(retrievals)
            result = self.send(runtime, runtime.new_session(), old['steps'][0]['user_input'])
            if cid == 'RGV1-B17':
                self.assertEqual(result['route_reason_code'], 'READ_VERIFIED')
                self.assertIn('口头承诺', result['message'])
                self.assertIn('违约金', result['message'])
                self.assertTrue(result['result']['citations'])
            else:
                self.assertEqual(result['route_reason_code'], 'READ_GOAL_UNRESOLVED')
                self.assertNotEqual(result['outcome_status'], 'SUCCESS')
                self.assertIn('没有足够证据', result['message'])
            self.assert_no_write(result)

    def test_combined_knowledge_and_authorized_business_read_keeps_sources(self):
        self.fixture('A001')
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool('get_request', {'request_id': 'A001'}),
                             helpers.tool('search_knowledge', {'query': '查询 A001 并解释预算规则'})]),
            ModelTurn('A001 已提交，预算只在 PO 创建成功时扣减。', []),
        ])
        result = self.send(runtime, runtime.new_session(), '查询 A001 并解释预算规则')
        self.assertEqual(result['route_action'], 'STOP')
        self.assertIn('A001', result['message'])
        self.assertTrue(result['result']['citations'])
        self.assertTrue(any(e.get('tool_name') == 'get_request' for e in self.trace_events(result['trace_id'])))
        self.assert_no_write(result)

    def test_direct_text_without_evidence_remains_ask(self):
        runtime, _ = self.runtime([ModelTurn('请补充目标。', [])])
        self.assertEqual(self.send(runtime, runtime.new_session(), '你好')['route_action'], 'ASK')

    def test_direct_answer_does_not_finish_suspended_write(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.extend([ModelTurn(None, [helpers.tool('search_knowledge', {'query': '预算什么时候扣？'})]),
                            ModelTurn('仅 PO 扣减预算。', [])])
        result = self.send(runtime, state, '预算什么时候扣？')
        self.assertEqual(result['route_action'], 'ASK')
        self.assertIsNotNone(state.suspended_task)
        self.assert_no_write(result)

    def test_mixed_read_write_keeps_intent_and_slots_but_defers_card(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool('search_knowledge', {'query': '先查规则，再帮我创建采购申请'})]),
            proposal(intent='create', item='显示器', quantity=2), ModelTurn('依据检索资料解释规则。', []),
        ])
        state = runtime.new_session(self.id())
        result = self.send(runtime, state, '先查规则，再帮我创建2台显示器的采购申请')
        self.assertEqual(result['route_reason_code'], 'MIXED_WRITE_DEFERRED')
        self.assertEqual(state.active_task_type, 'create')
        self.assertEqual(state.slots, {'item': '显示器', 'quantity': 2})
        self.assertIn('未', result['message'])
        self.assert_no_write(result)

    def test_mixed_resume_keeps_staged_fields_without_same_turn_card(self):
        runtime, model, state, _ = self.interrupted()
        model.turns.extend([
            ModelTurn(None, [helpers.tool('search_knowledge', {'query': '规则'})]),
            proposal(intent='update', request_id='A001', quantity=3),
            ModelTurn('检索资料中的预算规则。', []),
        ])
        result = self.send(runtime, state, '先查询预算规则，再继续修改 A001，数量改为3台')
        self.assertEqual(result['route_reason_code'], 'MIXED_WRITE_DEFERRED')
        self.assertEqual(state.resume_proposal, {'quantity': 3})
        self.assert_no_write(result)
        result = self.send(runtime, state, '继续')
        self.assertEqual(result['route_action'], 'CONFIRM')
        self.assertEqual(state.pending_action.args, {'quantity': 3})

    def test_mixed_write_first_keeps_intent_regardless_of_request_order(self):
        self.fixture('A001')
        cases = [
            ('把 A001 的数量改成3台，同时查询预算规则', {'intent': 'update', 'request_id': 'A001', 'quantity': 3}),
            ('创建2台显示器采购申请，顺便查规则', {'intent': 'create', 'item': '显示器', 'quantity': 2}),
            ('采购2台显示器，单价800元，供应商启明办公，同时查预算规则',
             {'intent': 'create', 'item': '显示器', 'quantity': 2, 'unit_price': '800'}),
        ]
        for message, fields in cases:
            with self.subTest(message=message):
                runtime, _ = self.runtime([
                    ModelTurn(None, [helpers.tool('search_knowledge', {'query': '规则'})]),
                    proposal(**fields), ModelTurn('检索资料中的预算规则。', []),
                ])
                state = runtime.new_session()
                result = self.send(runtime, state, message)
                self.assertEqual(result['route_reason_code'], 'MIXED_WRITE_DEFERRED')
                self.assertEqual(state.active_task_type, fields['intent'])
                self.assertEqual(state.slots['quantity'], fields['quantity'])
                self.assert_no_write(result)


if __name__ == '__main__':
    unittest.main()
