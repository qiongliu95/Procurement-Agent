"""预算解释专项：真实临时HTTP/SQLite + 脚本模型；可选一条真实模型验收。"""
from datetime import datetime
import json
import os
from pathlib import Path
import unittest

from agent_runtime import AgentRuntime, ModelTurn, OpenAICompatibleModel
from agent_tools import ApiCallResult, MODEL_TOOL_SCHEMAS, ProcurementTransportError
from evaluation.fixture import snapshot_database
from evaluation.rag_baseline import RecordingModel
from test_catalog_delivery import CatalogDeliveryTests, proposal
from test_context_management import tool


RID = 'AGT-5DE8FD4B82FF4ACF'


class BudgetReadTests(CatalogDeliveryTests):
    def setUp(self):
        super().setUp()
        self.maintain('P-IT-002',purchase_price='5499',reference_price='5499')
        self.assertEqual(self.call('PATCH','/admin/budgets/B_MKT',{'balance':'3250'})[0],200)
        for rid in (RID,'BUDGET-HIDDEN'):
            self.assertEqual(self.call('POST','/requests',{'request_id':rid,'item':'办公笔记本电脑',
                'quantity':2,'purpose':'隔离预算验收'},token='demo-u01')[0],201)
        self.assertEqual(self.call('POST',f'/requests/{RID}/submit',{},token='demo-u01')[1]['state'],'BLOCKED_BUDGET')
        with self.app.store.connect() as db:
            db.execute("UPDATE requests SET applicant='U02',department='其他部门',budget_id='B_HIGH' WHERE id='BUDGET-HIDDEN'")

    def query(self,rid=RID,**fields):
        return proposal('query',target_object='request',request_id=rid,query_topic='budget',**fields)

    def test_b01_original_question_uses_current_real_budget_not_status_template(self):
        runtime=self.runtime([proposal('query',target_object='request',request_id=RID)])
        state=runtime.new_session()
        result=self.send(runtime,state,f'{RID} 为什么预算不足？部门预算还剩多少？我超了多少钱？')
        self.assertEqual(result['route_reason_code'],'READ_VERIFIED',result)
        self.assertEqual(result['result']['result_type'],'request_budget')
        self.assertEqual((result['result']['available_budget'],result['result']['request_amount'],result['result']['shortfall']),
                         ('3250.00','10998.00','7748.00'))
        self.assertIn('不是提交时',result['message']);self.assertIn('UTC',result['message'])
        self.assertIsNotNone(datetime.fromisoformat(result['result']['queried_at'].replace('Z','+00:00')).utcoffset())
        self.assertIsNone(state.active_task_type);self.assertIsNone(state.pending_action)

    def test_b02_balance_change_refreshes_and_does_not_release_historical_block(self):
        runtime=self.runtime([self.query(),self.query(),self.query()])
        state=runtime.new_session()
        first=self.send(runtime,state,f'查询{RID}的部门预算余额和缺口')
        for balance,gap in (('1000','9998.00'),('12000','0.00')):
            self.assertEqual(self.call('PATCH','/admin/budgets/B_MKT',{'balance':balance})[0],200)
            result=self.send(runtime,state,'目前还差多少钱？')
            self.assertEqual(result['result']['shortfall'],gap)
            self.assertEqual(result['result']['request_state'],'BLOCKED_BUDGET')
            self.assertNotEqual(first['result']['queried_at'],result['result']['queried_at'])
        self.assertIn('不会自动改变申请状态',result['message'])

    def test_b03_request_permission_and_no_arbitrary_budget_routes(self):
        before=snapshot_database(self.db)
        for token in ('demo-u01','demo-agent'):
            code,data=self.call('GET','/requests/BUDGET-HIDDEN/budget',token=token)
            self.assertEqual((code,data['error_code']),(403,'REQUEST_ACCESS_FORBIDDEN'))
            self.assertNotIn('available_budget',data)
            self.assertEqual(self.call('GET','/budgets/B_HIGH',token=token)[0],404)
            self.assertEqual(self.call('GET',f'/requests/{RID}/budget?budget_id=B_HIGH',token=token)[0],400)
            self.assertEqual(self.call('GET','/admin/overview',token=token)[0],403)
            self.assertEqual(self.call('PATCH','/admin/budgets/B_MKT',{'balance':'999999'},token=token)[0],403)
        self.assertEqual(before,snapshot_database(self.db))

    def test_b04_runtime_access_denial_has_no_amounts_or_success(self):
        runtime=self.runtime([self.query('BUDGET-HIDDEN')])
        result=self.send(runtime,runtime.new_session(),'查询BUDGET-HIDDEN的预算余额')
        self.assertEqual(result['route_reason_code'],'BUDGET_READ_FAILED')
        self.assertNotEqual(result['outcome_status'],'SUCCESS')
        self.assertIsNone(result['result']);self.assertNotIn('20000',result['message'])

    def test_b05_missing_budget_has_explicit_failure_not_guessed_amount(self):
        with self.app.store.connect() as db:
            db.execute("UPDATE budgets SET deleted_at='test-fixture' WHERE id='B_MKT'")
        runtime=self.runtime([self.query()])
        result=self.send(runtime,runtime.new_session(),f'查询{RID}预算')
        self.assertEqual(result['route_reason_code'],'BUDGET_READ_FAILED')
        self.assertIn('不能提供余额',result['message']);self.assertIsNone(result['result'])

    def test_b06_transport_failure_never_falls_back_to_session_balance(self):
        runtime=self.runtime([self.query(),self.query()])
        state=runtime.new_session();self.send(runtime,state,f'查询{RID}预算')
        def unavailable(**kwargs):
            raise ProcurementTransportError('CONTROLLED_BUDGET_UNAVAILABLE',uncertain=False)
        runtime.api.get_request_budget=unavailable
        result=self.send(runtime,state,'目前还差多少钱？')
        self.assertEqual(result['route_reason_code'],'BUDGET_READ_FAILED')
        self.assertIsNone(result['result']);self.assertNotIn('7748',result['message'])

    def test_b07_model_tool_read_is_used_once_and_not_overwritten_by_status(self):
        runtime=self.runtime([
            ModelTurn(None,[tool('get_request_budget',{'request_id':RID})]),
            proposal('query',target_object='request',request_id=RID)])
        result=self.send(runtime,runtime.new_session(),f'说明{RID}的财务缺口')
        self.assertEqual(result['result']['shortfall'],'7748.00')
        events=[json.loads(line) for f in (Path(self.tmp.name)/'traces').glob('*.jsonl')
                for line in f.read_text(encoding='utf-8').splitlines()]
        calls=[e for e in events if e['event_type']=='TOOL_CALL_STARTED']
        self.assertEqual([e['tool_name'] for e in calls],['get_request_budget'])
        self.assertEqual(sum(e['event_type']=='FINAL_RESPONSE' for e in events),1)
        self.assertEqual(sum(e['event_type']=='ROUTE_DECIDED' for e in events),1)

    def test_b08_invalid_or_mismatched_receipt_is_not_verified(self):
        real=self.api.get_request_budget
        for field,value in (('request_id','OTHER'),('available_budget',None),('shortfall','1.00'),
                            ('queried_at',None),('request_amount','NaN')):
            with self.subTest(field=field):
                def corrupt(field=field,value=value,**kwargs):
                    reply=real(**kwargs)
                    return ApiCallResult({**reply.data,field:value},reply.status,reply.tool_call_id,reply.latency_ms)
                self.api.get_request_budget=corrupt
                runtime=self.runtime([self.query()])
                result=self.send(runtime,runtime.new_session(),f'查询{RID}预算')
                self.assertEqual(result['route_reason_code'],'BUDGET_READ_FAILED')
                self.assertIsNone(result['result'])

    def test_b09_plain_status_query_stays_status(self):
        runtime=self.runtime([proposal('query',target_object='request',request_id=RID)])
        result=runtime.handle_message(runtime.new_session(),f'查询{RID}当前申请状态')
        self.assertEqual(result['result']['result_type'],'request')
        self.assertIn('当前状态为 BLOCKED_BUDGET',result['message'])
        self.assertNotIn('余额查询时点',result['message'])

    def test_b10_tool_schema_cannot_accept_budget_id_or_admin_parameters(self):
        spec=next(x['function'] for x in MODEL_TOOL_SCHEMAS if x['function']['name']=='get_request_budget')
        self.assertEqual(set(spec['parameters']['properties']),{'request_id'})
        self.assertFalse(spec['parameters']['additionalProperties'])
        runtime=self.runtime([])
        with self.assertRaisesRegex(ValueError,'INVALID_TOOL_ARGUMENTS'):
            runtime._execute_read_tool(runtime.new_session(),'get_request_budget',
                                      {'request_id':RID,'budget_id':'B_HIGH'},RID)

    def test_b11_permission_changes_are_rechecked_after_previous_success(self):
        runtime=self.runtime([self.query(),self.query()])
        state=runtime.new_session();self.send(runtime,state,f'查询{RID}预算')
        with self.app.store.connect() as db:
            db.execute("UPDATE requests SET applicant='U02' WHERE id=?",(RID,))
        result=self.send(runtime,state,'预算还剩多少？')
        self.assertEqual(result['route_reason_code'],'BUDGET_READ_FAILED')
        self.assertIsNone(result['result'])

    def test_b12_current_budget_question_never_requests_a_personal_limit(self):
        runtime=self.runtime([self.query()])
        result=self.send(runtime,runtime.new_session(),f'为什么{RID}预算不足？')
        self.assertEqual(result['result']['budget_id'],'B_MKT')
        self.assertNotIn('请告诉我',result['message'])
        self.assertEqual(self.steps[-1]['after']['tables']['pos'],[])

    def test_b13_status_query_mentioning_budget_block_is_still_status(self):
        runtime=self.runtime([proposal('query',target_object='request',request_id=RID,query_topic='status')])
        result=runtime.handle_message(runtime.new_session(),f'查询这张预算不足的申请{RID}当前状态')
        self.assertEqual(result['result']['result_type'],'request')
        self.assertNotIn('余额查询时点',result['message'])


@unittest.skipUnless(os.getenv('RUN_BUDGET_REAL')=='1','真实模型需显式开启')
class RealBudgetReadTests(BudgetReadTests):
    def test_real_budget_followup_refresh_and_status(self):
        model=OpenAICompatibleModel.from_env()
        folder=Path(os.environ['CATALOG_EVIDENCE_DIR'])/self._testMethodName
        try:
            runtime=AgentRuntime(RecordingModel(model,folder/'model_calls'),self.api,self.trace,
                                 model_name=os.environ['AGENT_MODEL_NAME'])
            state=runtime.new_session()
            first=self.send(runtime,state,f'针对申请{RID}：为什么预算不足？部门预算还剩多少？我超了多少钱？')
            self.assertEqual(first['result']['shortfall'],'7748.00',first)
            self.assertEqual(self.call('PATCH','/admin/budgets/B_MKT',{'balance':'12000'})[0],200)
            second=self.send(runtime,state,'现在部门预算还剩多少？我还超多少钱？')
            self.assertEqual(second['result']['available_budget'],'12000.00',second)
            self.assertEqual(second['result']['shortfall'],'0.00')
            plain=runtime.handle_message(state,'那这笔申请当前是什么状态？')
            self.assertEqual(plain['result']['result_type'],'request',plain)
            self.assertEqual(plain['result']['state'],'BLOCKED_BUDGET')
        finally:
            model.client.close()


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    if os.getenv('BUDGET_REAL_ONLY')!='1':
        suite.addTests(BudgetReadTests(name) for name in BudgetReadTests.__dict__ if name.startswith('test_b'))
    suite.addTest(RealBudgetReadTests('test_real_budget_followup_refresh_and_status'))
    return suite
