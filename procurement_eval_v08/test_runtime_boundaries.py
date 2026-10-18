"""公共边界 Case 库：C/D/B/E 及反向保护，复用目录验收的隔离 HTTP/SQLite 入口。

运行：py -3.11 -m unittest test_runtime_boundaries
原始模型输入、Trace、Session 和业务快照随 CATALOG_EVIDENCE_DIR 留存。
"""
import json
import os
from pathlib import Path
import unittest
from dataclasses import asdict

from agent_runtime import AgentRuntime, ModelTurn, OpenAICompatibleModel
from evaluation.fixture import snapshot_database
from evaluation.rag_baseline import RecordingModel
from test_catalog_delivery import CatalogDeliveryTests, proposal
from test_context_management import tool
from test_request_budget import BudgetReadTests, RID


def read(name, **args):
    return ModelTurn(None, [tool(name, args)])


class RuntimeBoundaryTests(CatalogDeliveryTests):
    def setUp(self):
        super().setUp()
        self.maintain('P-IT-002', purchase_price='5499', reference_price='5499')
        self.models = []

    def runtime(self, turns):
        runtime = super().runtime(turns)
        self.models.append(runtime.model)
        return runtime

    def tearDown(self):
        self.steps.append({'model_inputs': [m.received for m in self.models]})
        super().tearDown()

    def events(self, run=None):
        return [e for f in (Path(self.tmp.name)/'traces').glob('*.jsonl')
                for e in map(json.loads, f.read_text(encoding='utf-8').splitlines())
                if run is None or e['run_id'] == run]

    def direction(self):
        return proposal('ambiguous', item='电脑', quantity=2, purpose='工作使用',
                        question='您想申请采购还是了解价格？')

    def add_desk(self):
        code, data = self.call('POST', '/admin/products', {
            'code':'TEST-DESK', 'name':'电脑桌', 'category':'办公家具', 'brand_model':'测试桌',
            'specification':'合成测试规格', 'unit':'张', 'reference_price':'100',
            'purchase_price':'100', 'supplier_name':'启明办公', 'status':'active'})
        self.assertEqual(code, 201, data)

    def test_c01_saved_item_is_admitted_for_price_read(self):
        runtime = self.runtime([self.direction(), read('search_products', query='电脑'),
                                proposal('consult', target_object='product')])
        state = runtime.new_session()
        self.send(runtime, state, '电脑，2台，工作使用')
        result = self.send(runtime, state, '了解价格')
        calls = [e for e in self.events(result['trace_id']) if e['event_type']=='TOOL_CALL_STARTED']
        self.assertEqual([e['sanitized_args']['query'] for e in calls], ['电脑'])
        self.assertEqual(result['result']['products'][0]['code'], 'P-IT-002')
        self.assertIsNone(result['confirmation'])
        self.assertIn('"quantity": 2', runtime.model.received[1][0]['content'])

    def test_c02_omitted_query_uses_pending_item_not_entire_catalog(self):
        runtime = self.runtime([self.direction(), read('search_products'),
                                proposal('consult', target_object='product')])
        state=runtime.new_session()
        self.send(runtime,state,'电脑，2台，工作使用')
        result=self.send(runtime,state,'了解价格')
        self.assertEqual([p['code'] for p in result['result']['products']], ['P-IT-002'])
        self.assertEqual(state.trusted_read_contexts['product']['query']['query'], '电脑')

    def test_c03_no_query_is_not_explicit_unrestricted_scope(self):
        runtime=self.runtime([]); state=runtime.new_session()
        with self.assertRaisesRegex(ValueError, 'PRODUCT_QUERY_REQUIRED'):
            runtime._execute_read_tool(state,'search_products',{},'了解价格')
        self.assertFalse(any(e.get('tool_name')=='search_products' for e in self.events()))

    def test_c04_explicit_unrestricted_query_remains_available(self):
        runtime=self.runtime([read('search_products'),proposal('consult',target_object='product')])
        result=self.send(runtime,runtime.new_session(),'查询全部商品目录')
        self.assertGreater(len(result['result']['products']),1)

    def test_c05_pending_item_does_not_ground_arbitrary_model_item(self):
        runtime=self.runtime([self.direction()]);state=runtime.new_session()
        self.send(runtime,state,'电脑，2台，工作使用')
        with self.assertRaisesRegex(ValueError,'UNGROUNDED_PRODUCT_QUERY'):
            runtime._execute_read_tool(state,'search_products',{'query':'鼠标'},'了解价格')

    def test_c06_previous_flag_can_use_pending_item_without_prior_read(self):
        runtime=self.runtime([self.direction(),read('search_products',use_previous_query=True),
                                proposal('consult',target_object='product')])
        state=runtime.new_session();self.send(runtime,state,'电脑，2台，工作使用')
        result=self.send(runtime,state,'了解价格')
        self.assertIsNotNone(result['result'],result)
        self.assertEqual(result['result']['products'][0]['code'],'P-IT-002')

    def test_c07_conflicting_model_intent_and_product_target_keep_unaccepted_fields(self):
        # 回放 real-01 的实际矛盾提案：create + product，并非 current_task。
        runtime=self.runtime([proposal('create','new_task',target_object='product',item='电脑',quantity=2,purpose='工作使用'),
            read('search_products',query='电脑'),proposal('consult',target_object='product')])
        state=runtime.new_session()
        first=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(first['route_action'],'ASK',first)
        self.assertIsNone(first['confirmation']);self.assertIsNone(state.active_task_type)
        self.assertEqual(state.clarification['known_slots']['quantity'],2)
        result=self.send(runtime,state,'了解价格')
        self.assertIn('5499',result['message']);self.assertIsNone(result['confirmation'])

    def test_c08_explicit_create_is_not_asked_for_intent_again(self):
        runtime=self.runtime([proposal('create','new_task',target_object='product',item='电脑',quantity=2,purpose='工作使用')])
        result=self.send(runtime,runtime.new_session(),'帮我创建电脑采购申请，数量2台，用于工作使用')
        self.assertEqual(result['route_action'],'CONFIRM',result)

    def test_c09_model_omits_fields_in_ask_but_employee_triple_is_retained(self):
        runtime=self.runtime([proposal('ambiguous','new_task',target_object='unknown',question='申请还是咨询？'),
            read('search_products',query='电脑'),proposal('consult',target_object='product')])
        state=runtime.new_session();self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(state.clarification['known_slots'],{'item':'电脑','quantity':2,'purpose':'工作使用'})
        result=self.send(runtime,state,'了解价格')
        self.assertIn('5499',result['message']);self.assertIsNone(result['confirmation'])
        self.assertIn('"quantity": 2',runtime.model.received[1][0]['content'])

    def test_c10_direct_ask_saves_only_well_formed_candidate_fields(self):
        runtime=self.runtime([ModelTurn('您要办理申请还是咨询？',[])])
        state=runtime.new_session();result=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(state.clarification['known_slots']['quantity'],2)
        self.assertIsNone(state.active_task_type);self.assertIsNone(result['confirmation'])

    def test_d01_catalog_auxiliary_read_continues_original_create(self):
        runtime=self.runtime([proposal('create'), read('search_products',query='电脑'),
                                proposal(item='电脑',quantity=2,purpose='工作使用')])
        state=runtime.new_session()
        self.send(runtime,state,'申请采购单')
        result=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(result['route_action'],'CONFIRM',result)
        self.assertEqual(state.pending_action.args['product_code'],'P-IT-002')
        self.assertEqual(state.pending_action.args['quantity'],2)
        self.assertEqual(state.pending_action.args['purpose'],'工作使用')
        self.assertEqual(state.missing_slots,[])
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_d02_explicit_create_can_read_catalog_without_consultation_gate(self):
        runtime=self.runtime([read('search_products',query='电脑'),
                                proposal(item='电脑',quantity=2,purpose='工作使用')])
        state=runtime.new_session()
        result=self.send(runtime,state,'请创建电脑采购申请，数量2台，用于工作使用')
        self.assertFalse(any(e['event_type']=='READ_TOOL_REJECTED' for e in self.events()))
        self.assertEqual(result['route_action'],'CONFIRM',result)

    def test_d03_partial_fields_recalculate_remaining_missing_slots(self):
        runtime=self.runtime([proposal('create'),read('search_products',query='电脑'),
                                proposal(item='电脑',quantity=2)])
        state=runtime.new_session();self.send(runtime,state,'申请采购单')
        result=self.send(runtime,state,'电脑，2台')
        self.assertEqual(state.missing_slots,['purpose'])
        self.assertIn('用途',result['message']);self.assertNotIn('物品、',result['message'])
        self.assertIsNone(result['confirmation'])

    def test_d04_catalog_ambiguity_selection_is_not_a_product_switch(self):
        old=self.call('GET','/products/P-IT-002')[1]
        fields={k:old[k] for k in ('name','category','brand_model','specification','unit','reference_price','status')}
        fields.update(code='SECOND-PC',brand_model='合成另一型号',supplier_name='启明办公')
        self.assertEqual(self.call('POST','/admin/products',fields)[0],201)
        runtime=self.runtime([proposal(item='电脑',quantity=2,purpose='工作使用'),
                                proposal(item='SECOND-PC')])
        state=runtime.new_session()
        first=self.send(runtime,state,'申请电脑，数量2台，用于工作使用')
        self.assertEqual(first['route_reason_code'],'CATALOG_PRODUCT_AMBIGUOUS')
        result=self.send(runtime,state,'选择SECOND-PC')
        self.assertEqual(result['route_action'],'CONFIRM',result)
        self.assertEqual(state.pending_action.args['product_code'],'SECOND-PC')
        self.assertEqual(state.pending_action.args['quantity'],2)
        self.assertEqual(state.pending_action.args['purpose'],'工作使用')

    def test_d05_unrequested_history_does_not_turn_auxiliary_catalog_into_consultation(self):
        runtime=self.runtime([proposal('create'),ModelTurn(None,[tool('search_products',{'query':'电脑'}),
            tool('search_purchase_history',{'item':'电脑'},2)]),
            proposal(item='办公笔记本电脑',quantity=2,purpose='工作使用')])
        state=runtime.new_session();self.send(runtime,state,'申请采购单')
        result=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(result['route_action'],'CONFIRM',result)
        self.assertNotIn('历史成交',result['message'])
        self.assertEqual(state.pending_action.args['product_code'],'P-IT-002')

    def test_reverse_independent_history_suspends_and_resumes_create(self):
        runtime=self.runtime([proposal(item='电脑',quantity=2),
            read('search_purchase_history',item='鼠标'),
            proposal('consult','new_task',target_object='purchase_history'),
            proposal(item='电脑',quantity=2,purpose='工作使用')])
        state=runtime.new_session();self.send(runtime,state,'申请电脑，2台')
        answer=self.send(runtime,state,'先独立查询鼠标历史成交价')
        self.assertIsNotNone(state.suspended_task)
        self.assertEqual(state.suspended_task.slots['item'],'电脑')
        self.assertIsNone(answer['confirmation'])
        self.send(runtime,state,'继续原来的采购任务')
        result=self.send(runtime,state,'用途是工作使用')
        self.assertEqual(result['route_action'],'CONFIRM',result)
        self.assertEqual(state.pending_action.args['item'],'办公笔记本电脑')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_e01_search_recall_does_not_make_desk_a_computer_candidate(self):
        self.add_desk()
        runtime=self.runtime([read('search_products',query='电脑'),proposal('consult',target_object='product')])
        result=self.send(runtime,runtime.new_session(),'查询电脑采购价')
        self.assertEqual([p['code'] for p in result['result']['products']],['P-IT-002'])
        self.assertNotIn('电脑桌',result['message'])

    def test_e02_substring_only_cannot_auto_select_purchase_target(self):
        self.add_desk()
        self.call('PATCH','/admin/products/P-IT-002',{'status':'inactive'})
        # 只保留前缀召回；数据库仍有名字包含“电脑”的桌子，不代表对象已唯一确定。
        self.call('DELETE','/admin/products/P-IT-002')
        runtime=self.runtime([proposal(item='电脑',quantity=2,purpose='工作使用')])
        result=self.send(runtime,runtime.new_session(),'创建电脑申请，数量2台，用于工作使用')
        self.assertIsNone(result['confirmation'],result)
        self.assertEqual(result['route_action'],'ASK')

    def test_e03_price_labels_in_answer_and_page_use_purchase_price(self):
        runtime=self.runtime([read('search_products',query='电脑'),proposal('consult',target_object='product')])
        result=self.send(runtime,runtime.new_session(),'查询电脑价格')
        self.assertIn('采购价',result['message']);self.assertNotIn('旧参考价',result['message'])
        page=Path(__file__).with_name('agent.html').read_text(encoding='utf-8')
        self.assertNotIn('参考单价',page)
        self.assertIn('采购价',page)

    def test_goal01_unrelated_history_does_not_complete_product_question(self):
        runtime=self.runtime([read('search_purchase_history',item='鼠标'),
                                proposal('consult',target_object='product',question='请指定商品')])
        result=self.send(runtime,runtime.new_session(),'查询电脑价格，不要鼠标历史成交价')
        self.assertNotEqual(result['outcome_status'],'SUCCESS',result)
        self.assertEqual(result['route_action'],'ASK')
        self.assertIsNone(result['confirmation'])

    def test_goal02_unrelated_domain_not_merged_into_product_answer(self):
        runtime=self.runtime([ModelTurn(None,[tool('search_products',{'query':'电脑'}),
            tool('search_purchase_history',{'item':'鼠标'},2)]),
            proposal('consult',target_object='product')])
        result=self.send(runtime,runtime.new_session(),'查询电脑价格（不是鼠标的历史价）')
        self.assertEqual(result['result']['history'],[])
        self.assertNotIn('历史成交',result['message'])

    def test_goal03_read_completion_does_not_accept_unrelated_free_text(self):
        runtime=self.runtime([read('search_products',query='电脑'),ModelTurn('鼠标以前很便宜。',[])])
        result=self.send(runtime,runtime.new_session(),'查询电脑价格')
        self.assertNotIn('鼠标以前很便宜',result['message'])
        self.assertIn('5499',result['message'])

    def test_goal04_insufficient_knowledge_is_not_success(self):
        from test_knowledge_rag import FakeKnowledgeIndex
        runtime=self.runtime([read('search_knowledge',query='预算制度'),
            proposal('consult',target_object='knowledge')])
        runtime.knowledge_index=FakeKnowledgeIndex(False)
        result=self.send(runtime,runtime.new_session(),'咨询预算制度')
        self.assertNotEqual(result['outcome_status'],'SUCCESS')
        self.assertEqual(result['route_action'],'ASK')

    def test_goal05_consultation_cannot_promote_product_read_to_create(self):
        runtime=self.runtime([read('search_products',query='电脑'),
                                proposal(item='电脑',quantity=2,purpose='工作使用')])
        result=self.send(runtime,runtime.new_session(),'仅查询电脑价格')
        self.assertIsNone(result['confirmation'])
        self.assertNotEqual(result['route_action'],'CONFIRM')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)


class BudgetChallengeTests(BudgetReadTests):
    def send(self,runtime,state,message=None,action=None):
        before=snapshot_database(self.db)
        result=runtime.handle_message(state,message)
        after=snapshot_database(self.db)
        self.steps.append({'input':message,'response':result,'session':asdict(state),'before':before,'after':after})
        self.assertEqual({k:v for k,v in before['tables'].items() if k!='events'},
                         {k:v for k,v in after['tables'].items() if k!='events'})
        self.assertTrue(all(e['type']=='REQUEST_STATUS_READ'
                            for e in after['tables']['events'][len(before['tables']['events']):]))
        return result

    def test_boundary_b01_total_challenge_is_not_a_status_summary(self):
        runtime=self.runtime([self.query(),proposal('query','correction',target_object='request',request_id=RID)])
        state=runtime.new_session();self.send(runtime,state,f'{RID}为什么预算不足，余额还剩多少？')
        result=self.send(runtime,state,'不是有2万预算吗？')
        self.assertEqual(result['result']['result_type'],'request_budget',result)
        self.assertIn('预算总额',result['message'])
        self.assertRegex(result['message'],r'无法核实|不能核实')
        self.assertNotIn('已使用 ¥16750',result['message'])
        self.assertNotEqual(result['outcome_status'],'SUCCESS')

    def test_boundary_b02_challenge_refreshes_balance_and_preserves_block(self):
        runtime=self.runtime([self.query(),self.query(relation='correction')])
        state=runtime.new_session();self.send(runtime,state,f'查询{RID}预算余额')
        self.call('PATCH','/admin/budgets/B_MKT',{'balance':'1000'})
        result=self.send(runtime,state,'预算总额不是20000元吗？为什么还不够？')
        self.assertEqual(result['result']['available_budget'],'1000.00')
        self.assertEqual(result['result']['shortfall'],'9998.00')
        self.assertIn('不能核实',result['message'])
        self.assertEqual(result['result']['request_state'],'BLOCKED_BUDGET')


@unittest.skipUnless(os.getenv('RUN_BOUNDARY_REAL')=='1','真实模型需显式开启')
class RealBoundaryTests(RuntimeBoundaryTests):
    def real_runtime(self):
        model=OpenAICompatibleModel.from_env()
        self.addCleanup(model.client.close)
        folder=Path(os.environ['CATALOG_EVIDENCE_DIR'])/self._testMethodName
        return AgentRuntime(RecordingModel(model,folder/'model_calls'),self.api,self.trace,
                            model_name=os.environ['AGENT_MODEL_NAME'])

    def test_real_c_price_context(self):
        self.add_desk()
        runtime=self.real_runtime();state=runtime.new_session()
        first=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertIsNone(first['confirmation'],first)
        result=self.send(runtime,state,'了解价格')
        self.assertIsNone(result['confirmation'],result)
        self.assertIn('5499',result['message'])
        self.assertNotIn('电脑桌',result['message'])
        self.assertNotIn('历史成交',result['message'])
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_real_d_create_with_context(self):
        runtime=self.real_runtime();state=runtime.new_session()
        self.send(runtime,state,'申请采购单')
        result=self.send(runtime,state,'电脑，2台，工作使用')
        self.assertEqual(result['route_action'],'CONFIRM',result)
        self.assertEqual(state.pending_action.args['product_code'],'P-IT-002')
        self.assertEqual(state.pending_action.args['quantity'],2)
        self.assertEqual(state.pending_action.args['purpose'],'工作使用')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)
        done=self.send(runtime,state,action=result['confirmation']['action_id'])
        self.assertEqual(done['result']['state'],'BLOCKED_BUDGET')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],1)
        self.assertEqual(snapshot_database(self.db)['counts']['pos'],0)


@unittest.skipUnless(os.getenv('RUN_BOUNDARY_REAL')=='1','真实模型需显式开启')
class RealBudgetChallengeTests(BudgetChallengeTests):
    def test_real_b_total_challenge(self):
        model=OpenAICompatibleModel.from_env()
        self.addCleanup(model.client.close)
        folder=Path(os.environ['CATALOG_EVIDENCE_DIR'])/self._testMethodName
        runtime=AgentRuntime(RecordingModel(model,folder/'model_calls'),self.api,self.trace,
                             model_name=os.environ['AGENT_MODEL_NAME'])
        state=runtime.new_session()
        first=self.send(runtime,state,f'{RID}为什么预算不足？部门预算还剩多少？我超了多少钱？')
        self.assertEqual(first['result']['shortfall'],'7748.00',first)
        self.call('PATCH','/admin/budgets/B_MKT',{'balance':'1000'})
        result=self.send(runtime,state,'不是有2万预算吗？')
        self.assertEqual(result['result']['available_budget'],'1000.00',result)
        self.assertIn('不能核实',result['message'])
        self.assertIn('预算总额',result['message'])
        self.assertNotEqual(result['outcome_status'],'SUCCESS')


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    if os.getenv('BOUNDARY_REAL_ONLY')!='1':
        for cls,prefix in ((RuntimeBoundaryTests,'test_'),(BudgetChallengeTests,'test_boundary_')):
            suite.addTests(cls(name) for name in cls.__dict__ if name.startswith(prefix))
    suite.addTests(RealBoundaryTests(name) for name in RealBoundaryTests.__dict__ if name.startswith('test_real_'))
    suite.addTest(RealBudgetChallengeTests('test_real_b_total_challenge'))
    return suite
