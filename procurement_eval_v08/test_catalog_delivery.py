"""DEC-09/REQ-17、BC-33/34；临时库、真实HTTP、可选真实模型，不访问演示服务。"""
from dataclasses import asdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from agent_runtime import AgentRuntime, ModelToolCall, ModelTurn, OpenAICompatibleModel, TraceWriter
from agent_tools import ProcurementApiClient
from backend import App, Store
from evaluation.fixture import snapshot_database
from evaluation.rag_baseline import RecordingModel, save
from test_context_management import CapturingModel
import test_business_consistency as consistency
import test_context_management as context
import test_multiturn_dialogue as multiturn
import test_history_followup as history


def proposal(intent='create', relation='supplement', **fields):
    return ModelTurn(None, [ModelToolCall('catalog-proposal', 'propose_task_state', {
        'intent': intent, 'dialogue_relation': relation,
        'target_object': 'current_task' if intent == 'create' else 'unknown', **fields})])


class CatalogDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'catalog.sqlite'
        self.app = App(('127.0.0.1', 0), Store(self.db))
        self.base = f'http://127.0.0.1:{self.app.server_port}'
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.api = ProcurementApiClient(self.base, 'demo-agent', 5)
        self.trace = TraceWriter(Path(self.tmp.name) / 'traces')
        self.steps = []

    def tearDown(self):
        target = os.getenv('CATALOG_EVIDENCE_DIR')
        if target:
            folder = Path(target) / self._testMethodName
            folder.mkdir(parents=True, exist_ok=True)
            save(folder / 'evidence.json', {'steps': self.steps, 'final': snapshot_database(self.db)})
            shutil.copy2(self.db, folder / 'fixture.sqlite')
            if (Path(self.tmp.name) / 'traces').exists():
                shutil.copytree(Path(self.tmp.name) / 'traces', folder / 'traces', dirs_exist_ok=True)
        self.app.shutdown(); self.app.server_close(); self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method, path, payload=None, token='demo-m02'):
        req = urllib.request.Request(self.base + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def maintain(self, code='P-IT-001', **values):
        response = self.call('PATCH', '/admin/products/' + code,
            {'supplier_name':'启明办公', 'purchase_price':'800.00', **values})
        self.assertEqual(response[0], 200, response)
        return response[1]

    def runtime(self, turns):
        return AgentRuntime(CapturingModel(turns), self.api, self.trace, model_name='offline-catalog')

    def send(self, runtime, state, message=None, action=None):
        before = snapshot_database(self.db)
        result = runtime.confirm(state, action, 'confirm') if action else runtime.handle_message(state, message)
        after = snapshot_database(self.db)
        self.steps.append({'input':message, 'action_id':action, 'response':result,
            'session':asdict(state), 'before':before, 'after':after})
        if not action:
            self.assertEqual(before, after, '未经写前确认不得更改业务数据库')
        return result

    def ready(self, **fields):
        self.maintain()
        runtime = self.runtime([proposal(item='显示器', quantity=2, purpose='隔离验收', **fields)])
        state = runtime.new_session()
        response = self.send(runtime, state, '创建2台显示器采购申请，用于隔离验收')
        self.assertEqual(response['route_action'], 'CONFIRM', response)
        return runtime, state, response

    def test_01_three_fields_create_submit_and_repeat_confirm(self):
        runtime,state,response = self.ready()
        self.assertIn('800.00', response['message']); self.assertIn('启明办公', response['message'])
        action=response['confirmation']['action_id']
        done=self.send(runtime,state,action=action)
        self.assertEqual(done['result']['state'],'SUBMITTED')
        self.assertIn('M01',done['message'])
        self.assertIsNone(state.active_task_type); self.assertIsNone(state.pending_action)
        repeated=self.send(runtime,state,action=action)
        self.assertEqual(repeated,done)
        snap=snapshot_database(self.db)['tables']
        self.assertEqual(len(snap['requests']),1); self.assertEqual(snap['pos'],[])
        self.assertEqual(next(x['balance'] for x in snap['budgets'] if x['id']=='B_MKT'),500000)

    def test_02_missing_product_blocks_zero_writes(self):
        runtime=self.runtime([proposal(item='电视机',quantity=1,purpose='测试')])
        response=self.send(runtime,runtime.new_session(),'创建1台电视机申请，用于测试')
        self.assertEqual(response['route_reason_code'],'CATALOG_DATA_REQUIRED')
        self.assertIn('采购负责人',response['message']); self.assertIsNone(response['confirmation'])
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_03_each_invalid_catalog_field_blocks(self):
        for fields in ({'status':'inactive'},{'supplier_name':'风险供应商'}):
            with self.subTest(fields=fields):
                self.maintain(status='active',**fields) if 'status' not in fields else self.maintain(**fields)
                runtime=self.runtime([proposal(item='显示器',quantity=1,purpose='测试')])
                response=self.send(runtime,runtime.new_session(),'创建1台显示器申请，用于测试')
                self.assertEqual(response['outcome_status'],'BLOCKED')
                self.assertIsNone(response['confirmation'])

    def test_04_employee_values_cannot_override_catalog(self):
        runtime,state,response=self.ready(unit_price='1',supplier_name='风险供应商')
        self.assertEqual(state.pending_action.args['unit_price'],'800.00')
        self.assertEqual(state.pending_action.args['supplier_name'],'启明办公')

    def test_05_catalog_change_invalidates_old_confirmation(self):
        runtime,state,response=self.ready()
        self.maintain(purchase_price='801')
        done=self.send(runtime,state,action=response['confirmation']['action_id'])
        self.assertEqual(done['route_reason_code'],'CATALOG_CHANGED')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_06_backend_rechecks_atomic_catalog_snapshot(self):
        runtime,state,response=self.ready()
        payload={'request_id':'RACE',**state.pending_action.args}
        self.maintain(purchase_price='802')
        code,body=self.call('POST','/requests',payload,token='demo-agent')
        self.assertEqual((code,body['error_code']),(409,'CATALOG_CHANGED'))
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_07_backend_manual_price_and_missing_data_cannot_bypass(self):
        payload={'request_id':'BYPASS','item':'显示器','quantity':2,'purpose':'测试',
                 'supplier_name':'启明办公','unit_price':'1'}
        self.assertEqual(self.call('POST','/requests',payload,token='demo-u01')[0],409)
        self.maintain()
        self.assertEqual(self.call('POST','/requests',payload,token='demo-u01')[1]['error_code'],'CATALOG_VALUE_CONFLICT')
        payload.pop('supplier_name');payload.pop('unit_price')
        self.assertEqual(self.call('POST','/requests',payload,token='demo-u01')[0],201)

    def test_08_catalog_permissions_uniqueness_and_restart(self):
        for token in ('demo-u01','demo-agent'):
            self.assertEqual(self.call('PATCH','/admin/products/P-IT-001',{
                'supplier_name':'启明办公','purchase_price':'1'},token=token)[0],403)
        self.maintain()
        old=self.call('GET','/products/P-IT-001')[1]
        payload={k:old[k] for k in ('name','category','brand_model','specification','unit','reference_price','status')}
        payload['code']='DUP'
        self.assertEqual(self.call('POST','/admin/products',payload)[1]['error_code'],'PRODUCT_DUPLICATE')
        Store(self.db)
        self.assertEqual(old,self.call('GET','/products/P-IT-001')[1])

    def test_09_same_name_different_models_requires_unique_code(self):
        old=self.maintain()
        payload={k:old[k] for k in ('name','category','brand_model','specification','unit','reference_price','status')}
        payload.update(code='OTHER',brand_model='另一个型号',supplier_name='启明办公',
                       reference_price='900',purchase_price='900')
        self.assertEqual(self.call('POST','/admin/products',payload)[0],201)
        runtime=self.runtime([proposal(item='显示器',quantity=1,purpose='测试')])
        response=self.send(runtime,runtime.new_session(),'创建1台显示器申请，用于测试')
        self.assertIsNone(response['confirmation'])

    def test_10_bc33_supplement_never_silently_changes_item(self):
        runtime=self.runtime([proposal(item='电视机',quantity=2),
            proposal(item='彩泥',quantity=2,unit_price='100',purpose='测试用')])
        state=runtime.new_session()
        self.send(runtime,state,'发起2台电视机采购申请')
        response=self.send(runtime,state,'彩泥，测试用，100')
        self.assertEqual(response['route_reason_code'],'CREATE_ITEM_CHANGE_AMBIGUOUS')
        self.assertEqual(state.slots['item'],'电视机');self.assertIsNone(response['confirmation'])

    def test_11_bc34_ambiguous_unsupported_quantity_and_create_preserve_goal(self):
        runtime=self.runtime([
            proposal('ambiguous','new_task',item='电视机',quantity=1,question='是申请还是咨询？'),
            proposal('unsupported','new_task',question='不支持列出所有供应商'),
            proposal('update',quantity=2,item='电视机'),
            proposal('create',purpose='测试用')])
        state=runtime.new_session()
        self.send(runtime,state,'我要采购一台电视机')
        self.send(runtime,state,'查一下所有供应商')
        self.assertEqual(state.clarification['known_slots']['item'],'电视机')
        quantity=self.send(runtime,state,'改成2台的电视机')
        self.assertEqual(quantity['route_reason_code'],'PENDING_GOAL_NOT_PERSISTED_REQUEST')
        self.assertEqual(state.clarification['known_slots']['quantity'],2)
        self.send(runtime,state,'发起申请，用于测试用')
        self.assertEqual((state.active_task_type,state.slots['item'],state.slots['quantity']),('create','电视机',2))
        self.assertNotIn('请明确指定要操作的申请编号',quantity['message'])

    def test_12_explicit_switch_isolates_old_fields(self):
        runtime=self.runtime([proposal(item='显示器',quantity=1,purpose='办公'),
                              proposal(relation='switch_task',item='会议摄像头',quantity=2)])
        state=runtime.new_session();self.maintain()
        old=self.send(runtime,state,'创建1台显示器采购申请，用于办公')
        new=self.send(runtime,state,'另创建2台会议摄像头采购申请')
        self.assertEqual(state.slots,{'item':'会议摄像头','quantity':2})
        self.assertIsNone(new['confirmation'])
        denied=runtime.confirm(state,old['confirmation']['action_id'],'confirm')
        self.assertEqual(denied['route_reason_code'],'CONFIRMATION_NOT_FOUND')

    def test_13_partial_collection_no_price_supplier_question(self):
        self.maintain()
        runtime=self.runtime([proposal(item='显示器'),proposal(quantity=2),proposal(purpose='办公')])
        state=runtime.new_session()
        for text in ('创建显示器采购申请','2台','用于办公'):
            response=self.send(runtime,state,text)
            if response['route_action']=='ASK':
                self.assertNotIn('供应商',response['message']);self.assertNotIn('单价',response['message'])
        self.assertEqual(response['route_action'],'CONFIRM')

    def test_14_new_supplier_budget_and_human_boundary(self):
        self.maintain(supplier_name='新锐科技')
        runtime=self.runtime([proposal(item='显示器',quantity=1,purpose='办公')])
        state=runtime.new_session();pending=self.send(runtime,state,'创建1台显示器申请，用于办公')
        done=self.send(runtime,state,action=pending['confirmation']['action_id'])
        self.assertEqual(done['result']['state'],'PENDING_VENDOR_REVIEW')
        self.assertEqual(snapshot_database(self.db)['tables']['pos'],[])

    def test_15_reference_price_is_the_purchase_price(self):
        product=self.call('GET','/products/P-IT-001')[1]
        self.assertEqual(product['reference_price'],'1299.00')
        self.assertEqual(product['purchase_price'],'1299.00');self.assertEqual(product['supplier']['name'],'新锐科技')

    def test_unique_contains_match_and_multiple_candidates_clarify(self):
        self.call('POST','/admin/products',{
            'code':'P-DESK','name':'电脑桌','category':'办公家具','brand_model':'通用 / Desk',
            'specification':'测试桌型','unit':'张','reference_price':'680','status':'active'})
        runtime=self.runtime([proposal(item='电脑',quantity=1,purpose='办公')])
        state=runtime.new_session();matched=self.send(runtime,state,'创建1台电脑申请，用于办公')
        self.assertEqual(matched['route_action'],'CONFIRM',matched)
        self.assertEqual(state.pending_action.args['item'],'办公笔记本电脑')
        self.call('POST','/admin/products',{
            'code':'P-PC-OTHER','name':'联想电脑','category':'计算机设备','brand_model':'Lenovo / Other',
            'specification':'测试型号','unit':'台','reference_price':'5000','status':'active'})
        runtime=self.runtime([proposal(item='电脑',quantity=1,purpose='办公')])
        ambiguous=self.send(runtime,runtime.new_session(),'创建1台电脑申请，用于办公')
        self.assertEqual(ambiguous['route_reason_code'],'CATALOG_PRODUCT_AMBIGUOUS')
        self.assertIn('办公笔记本电脑',ambiguous['message']);self.assertIn('联想电脑',ambiguous['message'])

    def test_16_budget_block_created_but_not_submitted(self):
        runtime,state,pending=self.ready()
        with self.app.store.connect() as db:
            db.execute("UPDATE budgets SET balance=100 WHERE id='B_MKT'")
        done=self.send(runtime,state,action=pending['confirmation']['action_id'])
        self.assertEqual(done['result']['state'],'BLOCKED_BUDGET')
        self.assertEqual(done['outcome_status'],'BLOCKED')
        snap=snapshot_database(self.db)['tables']
        self.assertEqual(len(snap['requests']),1);self.assertEqual(snap['pos'],[])
        self.assertEqual(next(x['balance'] for x in snap['budgets'] if x['id']=='B_MKT'),100)

    def test_17_supplier_blacklisted_after_confirmation_blocks_before_create(self):
        runtime,state,pending=self.ready()
        with self.app.store.connect() as db:
            db.execute("UPDATE suppliers SET status='blacklisted' WHERE id='S_ACTIVE'")
        done=self.send(runtime,state,action=pending['confirmation']['action_id'])
        self.assertEqual(done['route_reason_code'],'CATALOG_CHANGED')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_18_pending_goal_new_item_does_not_inherit_quantity(self):
        runtime=self.runtime([proposal('ambiguous','new_task',item='电视机',quantity=2,question='申请还是咨询？'),
                              proposal(relation='new_task',item='显示器',purpose='办公')])
        state=runtime.new_session()
        self.send(runtime,state,'我要采购2台电视机')
        answer=self.send(runtime,state,'另创建显示器采购申请，用于办公')
        self.assertEqual(state.slots,{'item':'显示器','purpose':'办公'})
        self.assertIsNone(answer['confirmation']);self.assertIn('数量',answer['message'])

    def test_19_legacy_database_migration_is_additive(self):
        legacy=Path(self.tmp.name)/'legacy.sqlite'
        with closing(sqlite3.connect(legacy)) as db:
            db.execute('CREATE TABLE products(code TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,'
                'brand_model TEXT NOT NULL,specification TEXT NOT NULL,unit TEXT NOT NULL,reference_price INTEGER NOT NULL,'
                'status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
            db.execute("INSERT INTO products VALUES('OLD','历史目录','测试','旧型号','规格','件',123,'inactive','old','old')")
            db.commit()
        store=Store(legacy)
        product=store.get_product('OLD',('AGENT','agent'))
        self.assertEqual((product['reference_price'],product['status']),('1.23','inactive'))
        self.assertEqual(product['supplier']['name'],'新锐科技');self.assertEqual(product['purchase_price'],'1.23')

    def test_20_suspended_create_repeated_ownership_ask_keeps_staged_quantity(self):
        runtime=self.runtime([proposal(item='电视机',quantity=1),
            proposal('consult','new_task',question='供应商查询需名称'),proposal(quantity=2),
            proposal(item='电视机'),proposal()])
        state=runtime.new_session()
        for text in ('创建1台电视机申请','查一下所有供应商','改成2台的电视机','采购电视机','发起申请'):
            result=self.send(runtime,state,text)
            self.assertIsNone(result['confirmation'])
        self.assertEqual(state.resume_proposal['quantity'],2)
        self.send(runtime,state,'继续')
        self.assertEqual((state.slots['item'],state.slots['quantity']),('电视机',2))


class CatalogFixtureMixin:
    """旧安全断言不变；仅把隔离夹具预置为采购负责人已维护的目录数据。"""
    def setUp(self):
        super().setUp()
        self.app.store.update_product('P-IT-001',{'supplier_name':'启明办公','purchase_price':'800'},
                                      ('M02','senior_approver'))
        for item,price in (('键盘','100'),('鼠标','50'),('耳机','50')):
            self.app.store.create_product({'code':'FIX-'+hashlib.sha256(item.encode()).hexdigest()[:12],
                'name':item,'brand_model':'隔离夹具','specification':'合成测试数据','category':'测试','unit':'件',
                'reference_price':price,'status':'active','supplier_name':'启明办公','purchase_price':price},
                ('M02','senior_approver'))

    def call(self,method,path,*args,**kwargs):
        # 旧测试用HTTP建立初始业务对象；此准备动作不是被测Agent的自主工具。
        payload=kwargs.get('data')
        if payload is None:
            payload=next((value for value in args if isinstance(value,dict)),None)
        if method=='POST' and path=='/requests' and payload:
            item=payload.get('item')
            if item and payload.get('unit_price') is not None:
                rows=self.app.store.list_products(('M01','approver'),item)['products']
                exact=[p for p in rows if p['name']==item]
                values={'supplier_name':payload.get('supplier_name','启明办公'),'purchase_price':payload['unit_price']}
                if len(exact)==1:
                    self.app.store.update_product(exact[0]['code'],values,('M02','senior_approver'))
                elif not exact:
                    self.app.store.create_product({'code':'FIX-'+hashlib.sha256(item.encode()).hexdigest()[:12],
                        'name':item,'brand_model':'隔离夹具','specification':'合成测试数据','category':'测试','unit':'件',
                        'reference_price':payload['unit_price'],'status':'active',**values},('M02','senior_approver'))
        return super().call(method,path,*args,**kwargs)


class CatalogConsistencyRegression(CatalogFixtureMixin, consistency.BusinessConsistencyTests):
    def call(self,method,path,*args,**kwargs):
        payload=kwargs.get('data')
        if method=='POST' and path=='/requests' and payload and payload.get('supplier_name')=='风险供应商':
            # 旧提交测试需要已存在的黑名单草稿；用合法创建后供应商变黑的历史状态作夹具，
            # 不再要求新规则允许直接新建黑名单申请。其提交/回复/状态断言原样执行。
            kwargs['data']={**payload,'supplier_name':'启明办公'}
            result=super().call(method,path,*args,**kwargs)
            with self.app.store.connect() as db:
                db.execute("UPDATE requests SET supplier_id='S_BLACK' WHERE id=?",(payload['request_id'],))
            return result
        return super().call(method,path,*args,**kwargs)


class CatalogContextRegression(CatalogFixtureMixin, context.ContextManagementTests):
    pass


class CatalogMultiturnRegression(CatalogFixtureMixin, multiturn.MultiturnDialogueTests):
    pass


class CatalogHistoryRegression(CatalogFixtureMixin, history.HistoryFollowupTests):
    pass


@unittest.skipUnless(os.getenv('RUN_CATALOG_REAL_MODEL')=='1','真实模型需显式开启，不能以离线结果替代')
class CatalogRealAcceptance(CatalogDeliveryTests):
    # 只运行本类定义的真实场景；load_tests排除继承的离线测试。
    def setUp(self):
        super().setUp()
        self.real_folder=Path(os.environ['CATALOG_EVIDENCE_DIR']) / self._testMethodName
        self.real_folder.mkdir(parents=True,exist_ok=False)
        self.real_base=OpenAICompatibleModel.from_env()
        self.model=RecordingModel(self.real_base,self.real_folder/'model_calls')
        self.real_runtime=AgentRuntime(self.model,self.api,self.trace,model_name=os.environ['AGENT_MODEL_NAME'])

    def tearDown(self):
        self.real_base.client.close()
        super().tearDown()

    def test_real_01_three_fields_and_actual_submit(self):
        self.maintain()
        state=self.real_runtime.new_session()
        pending=self.send(self.real_runtime,state,'帮我创建2台显示器采购申请，用于市场部办公')
        self.assertEqual(pending['route_action'],'CONFIRM',pending)
        done=self.send(self.real_runtime,state,action=pending['confirmation']['action_id'])
        self.assertEqual(done['result']['state'],'SUBMITTED')
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],1)

    def test_real_02_missing_catalog_blocks(self):
        state=self.real_runtime.new_session()
        blocked=self.send(self.real_runtime,state,'帮我创建1台显示器采购申请，用于测试')
        self.assertEqual(blocked['route_reason_code'],'CATALOG_DATA_REQUIRED',blocked)
        self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_real_03_bc33_and_bc34_original_dialogue(self):
        state=self.real_runtime.new_session()
        for text in ('我要采购一台电视机','查一下所有供应商','改成2台的电视机','采购电视机','发起申请'):
            response=self.send(self.real_runtime,state,text)
            self.assertIsNone(response['confirmation'])
        if state.suspended_task:
            # 只读插曲可能合法暂停任务；明确同意恢复后仍须保留数量2，不代替写前确认。
            self.send(self.real_runtime,state,'继续')
        self.assertEqual(state.slots.get('item'),'电视机',self.steps)
        self.assertEqual(state.slots.get('quantity'),2,self.steps)
        response=self.send(self.real_runtime,state,'彩泥，测试用，100')
        self.assertEqual(state.slots.get('item'),'电视机',response)
        self.assertIsNone(response['confirmation']);self.assertEqual(snapshot_database(self.db)['counts']['requests'],0)

    def test_real_04_new_rule_knowledge_matches_catalog_contract(self):
        from knowledge_rag import build_default_index
        index,meta=build_default_index()
        self.real_runtime.knowledge_index=index
        save(self.real_folder/'index_meta.json',meta)
        state=self.real_runtime.new_session()
        response=self.send(self.real_runtime,state,'员工新建采购申请时，需要自己填写供应商和单价吗？要提供哪些信息？')
        self.assertEqual(response['route_reason_code'],'READ_VERIFIED',response)
        self.assertIn('目录',response['message']);self.assertIn('用途',response['message'])
        self.assertRegex(response['message'],r'不需要|无需|不用|不必|只需|不要求')
        self.assertIsNone(response['confirmation'])


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    if os.getenv('CATALOG_REAL_ONLY')!='1':
        for cls in (CatalogDeliveryTests,CatalogConsistencyRegression,CatalogContextRegression,
                    CatalogMultiturnRegression,CatalogHistoryRegression):
            suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTests(CatalogRealAcceptance(name) for name in CatalogRealAcceptance.__dict__ if name.startswith('test_real_'))
    return suite


if __name__=='__main__':
    unittest.main(verbosity=2)
