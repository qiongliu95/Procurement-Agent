"""商品切换澄清定向回归；复用临时HTTP/SQLite夹具，不连接演示服务。"""
import json
import os
from pathlib import Path
import unittest

from agent_runtime import AgentRuntime, OpenAICompatibleModel
from evaluation.rag_baseline import RecordingModel
from test_catalog_delivery import CatalogDeliveryTests, proposal


class ItemSwitchTests(CatalogDeliveryTests):
    def door(self):
        code, data = self.call('POST', '/admin/products', {
            'code':'SWITCH-DOOR', 'name':'智能门禁', 'category':'隔离测试', 'brand_model':'测试型号',
            'specification':'合成验收数据', 'unit':'台', 'reference_price':'100',
            'supplier_name':'启明办公', 'status':'active'})
        self.assertEqual(code, 201, data)

    def pending_switch(self, *, with_catalog=True, extra_turns=()):
        if with_catalog:
            self.door()
        runtime = self.runtime([
            proposal(item='手机', quantity=1, purpose='工作机'),
            proposal(relation='new_task', item='智能门禁', quantity=3, purpose='作为测试机'),
            *extra_turns])
        state = runtime.new_session()
        old = self.send(runtime, state, '手机，1，工作机')
        self.assertEqual(old['route_reason_code'], 'CATALOG_DATA_REQUIRED', old)
        question = self.send(runtime, state, '智能门禁，3，作为测试机')
        self.assertEqual(question['route_reason_code'], 'CREATE_ITEM_CHANGE_AMBIGUOUS')
        self.assertEqual(state.slots, {'item':'手机','quantity':1,'purpose':'工作机'})
        return runtime, state

    def test_s01_original_yes_then_switch_advances(self):
        runtime,state = self.pending_switch()
        self.assertEqual(state.clarification['item_change']['fields'],
                         {'item':'智能门禁','quantity':3,'purpose':'作为测试机'})
        unclear = self.send(runtime,state,'是')
        self.assertEqual(unclear['route_action'],'ASK')
        self.assertEqual(state.slots['item'],'手机')
        done = self.send(runtime,state,'更换')
        self.assertEqual(done['route_action'],'CONFIRM',done)
        self.assertEqual((state.slots['item'],state.slots['quantity'],state.slots['purpose']),
                         ('智能门禁',3,'作为测试机'))
        self.assertIsNone(state.clarification)
        self.assertEqual(len(runtime.model.received),2,'切换选择不需要再请求模型猜目标')
        self.assertEqual(state.pending_action.args['product_code'],'SWITCH-DOOR')
        self.assertIn('300.00',done['message'])
        # 切换选择不是写入授权；仍需具体写前确认才创建/提交。
        confirmed = self.send(runtime,state,action=done['confirmation']['action_id'])
        self.assertEqual(confirmed['result']['state'],'SUBMITTED')
        self.assertEqual(self.steps[-1]['after']['counts']['requests'],1)

    def test_s02_explicit_switch_expressions(self):
        self.door()
        for word in ('更换','确认更换','同意更换','更换为智能门禁','换成智能门禁','是，确认更换','请更换','更换吧'):
            with self.subTest(word=word):
                runtime,state = self.pending_switch(with_catalog=False)
                done = self.send(runtime,state,word)
                self.assertEqual(done['route_action'],'CONFIRM',done)
                self.assertEqual(state.pending_action.args['quantity'],3)
                self.assertIsNone(state.clarification)

    def test_s03_rejection_keeps_original_fields(self):
        for word in ('不更换','不要更换','取消更换','保留原物品','否','拒绝'):
            with self.subTest(word=word):
                runtime,state = self.pending_switch(with_catalog=False)
                result = self.send(runtime,state,word)
                self.assertEqual(result['route_reason_code'],'CREATE_ITEM_CHANGE_REJECTED')
                self.assertEqual(state.slots,{'item':'手机','quantity':1,'purpose':'工作机'})
                self.assertNotIn('item_change',state.clarification)
                self.assertIsNone(result['confirmation'])

    def test_s04_cancellation_abandons_only_temporary_task(self):
        for word in ('取消','取消当前任务','不办了'):
            with self.subTest(word=word):
                runtime,state = self.pending_switch(with_catalog=False)
                result = self.send(runtime,state,word)
                self.assertEqual(result['route_reason_code'],'TEMPORARY_TASK_ABANDONED')
                self.assertIsNone(state.clarification)
                self.assertEqual(state.slots,{})
                self.assertIsNone(state.pending_action)

    def test_s05_other_confirmation_is_not_switch_or_write_authorization(self):
        runtime,state = self.pending_switch(with_catalog=False)
        for word in ('是','确认','同意','好的','可以'):
            result = self.send(runtime,state,word)
            self.assertEqual(result['route_action'],'ASK')
            self.assertEqual(state.slots['item'],'手机')
            self.assertEqual(state.clarification['item_change']['fields']['quantity'],3)
            self.assertIsNone(result['confirmation'])

    def test_s06_switch_then_missing_catalog_checks_new_item_only(self):
        runtime,state = self.pending_switch(with_catalog=False)
        result = self.send(runtime,state,'确认更换')
        self.assertEqual(result['route_reason_code'],'CATALOG_DATA_REQUIRED')
        self.assertEqual(state.slots,{'item':'智能门禁','quantity':3,'purpose':'作为测试机'})
        self.assertNotIn('item_change',state.clarification)
        self.assertNotEqual(state.clarification['reason'],'CREATE_ITEM_CHANGE_AMBIGUOUS')
        events = [json.loads(line) for f in (Path(self.tmp.name)/'traces').glob('*.jsonl')
                  for line in f.read_text(encoding='utf-8').splitlines()]
        reads = [e for e in events if e.get('run_id')==result['trace_id'] and e.get('tool_name')=='search_products'
                 and e['event_type']=='TOOL_CALL_STARTED']
        self.assertEqual([e['sanitized_args']['query'] for e in reads],['智能门禁'])

    def test_s07_stale_candidate_cannot_override_changed_task(self):
        runtime,state = self.pending_switch(with_catalog=False)
        state.slots['quantity']=7  # 模拟候选保存后任务快照变化，确认不能使用过期候选。
        result = self.send(runtime,state,'更换')
        self.assertEqual(result['route_reason_code'],'CREATE_ITEM_CHANGE_STALE')
        self.assertEqual((state.slots['item'],state.slots['quantity']),('手机',7))
        self.assertIsNone(result['confirmation'])

    def test_s08_unrelated_message_drops_old_switch_candidate(self):
        runtime,state = self.pending_switch(with_catalog=False,
            extra_turns=[proposal('unsupported',question='不支持该操作')])
        self.send(runtime,state,'帮我审批')
        self.assertIsNone(state.clarification)
        self.assertEqual(state.slots['item'],'手机')

    def test_s09_no_old_quantity_or_purpose_inherits_when_candidate_missing(self):
        runtime=self.runtime([proposal(item='手机',quantity=1,purpose='工作机'),
                              proposal(item='智能门禁')])
        state=runtime.new_session()
        self.send(runtime,state,'手机，1，工作机')
        self.send(runtime,state,'智能门禁')
        result=self.send(runtime,state,'更换')
        self.assertEqual(state.slots,{'item':'智能门禁'})
        self.assertEqual(set(state.missing_slots),{'quantity','purpose'})
        self.assertIsNone(result['confirmation'])


@unittest.skipUnless(os.getenv('RUN_ITEM_SWITCH_REAL')=='1','真实模型需显式开启')
class RealItemSwitchTests(ItemSwitchTests):
    def test_real_original_dialogue(self):
        self.door()
        folder=Path(os.environ['CATALOG_EVIDENCE_DIR'])/self._testMethodName
        model=OpenAICompatibleModel.from_env()
        try:
            recording=RecordingModel(model,folder/'model_calls')
            runtime=AgentRuntime(recording,self.api,self.trace,model_name=os.environ['AGENT_MODEL_NAME'])
            state=runtime.new_session()
            first=self.send(runtime,state,'手机，1，工作机')
            self.assertEqual(first['route_reason_code'],'CATALOG_DATA_REQUIRED',first)
            second=self.send(runtime,state,'智能门禁，3，作为测试机')
            self.assertEqual(second['route_reason_code'],'CREATE_ITEM_CHANGE_AMBIGUOUS',second)
            self.send(runtime,state,'是')
            ready=self.send(runtime,state,'更换')
            self.assertEqual(ready['route_action'],'CONFIRM',ready)
            self.assertEqual((state.slots['item'],state.slots['quantity']),('智能门禁',3))
            self.assertIn(state.slots['purpose'],{'测试机','作为测试机'})
            self.assertIsNone(state.clarification)
            self.assertEqual(len(list((folder/'model_calls').glob('*.json'))),2)
        finally:
            model.client.close()


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    if os.getenv('ITEM_SWITCH_REAL_ONLY')!='1':
        suite.addTests(ItemSwitchTests(name) for name in ItemSwitchTests.__dict__ if name.startswith('test_s'))
    suite.addTest(RealItemSwitchTests('test_real_original_dialogue'))
    return suite
