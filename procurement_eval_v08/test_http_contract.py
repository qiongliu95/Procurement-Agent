"""Real loopback HTTP + SQLite backend contract tests. NO Dify/LLM invocation."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from http.client import RemoteDisconnected
from pathlib import Path
from backend import App,Store
from checker_v08 import CASES,check,normalize_backend

CASES_BY_ID={c['case_id']:c for c in CASES}
SUPPLIER_NAMES={'S_ACTIVE':'启明办公','S_BLACK':'风险供应商','S_NEW':'新锐科技'}

class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.records=[]
    def setUp(self):
        db_name=self._testMethodName+'.sqlite'
        self.call_sequence=0
        self.app=App(('127.0.0.1',0),Store(Path(self.tmp.name)/db_name))
        self.base=f'http://127.0.0.1:{self.app.server_port}'
        self.thread=threading.Thread(target=self.app.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.app.shutdown();self.app.server_close();self.thread.join(timeout=5)
    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        out=Path(__file__).resolve().parent/'reports'/'backend_contract_results.json'
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps({'type':'real_local_backend_HTTP_SQLite','agent_or_Dify_runs':0,
                                  'backend_cases':cls.records},ensure_ascii=False,indent=2),encoding='utf-8')
    def call(self,method,path,token='demo-u01',data=None,headers=None):
        self.call_sequence+=1
        trace_headers={'X-Run-ID':'http-'+self._testMethodName,
                       'X-Tool-Call-ID':f'{self._testMethodName}-{self.call_sequence}'}
        req=urllib.request.Request(self.base+path,method=method,data=json.dumps(data or {}).encode('utf-8') if method=='POST' else None,
                                   headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                                            **trace_headers,**(headers or {})})
        try:
            with urllib.request.urlopen(req,timeout=6) as resp:return resp.status,json.load(resp)
        except urllib.error.HTTPError as err:return err.code,json.load(err)
    def create(self,rid,*,quantity=2,price='800.00',vendor='S_ACTIVE',budget='B_MKT',item='显示器',purpose='市场部本月使用'):
        # 旧合同场景先用 M02 准备可信目录；被测申请仍走真实 HTTP 和原身份边界。
        catalog_vendor='S_ACTIVE' if vendor=='S_BLACK' else (vendor or 'S_NEW')
        rows=self.app.store.list_products(('M02','senior_approver'),item)['products']
        exact=[product for product in rows if product['name']==item]
        supplier_name=SUPPLIER_NAMES[catalog_vendor]
        if len(exact)==1:
            self.app.store.update_product(exact[0]['code'],{
                'supplier_name':supplier_name,'purchase_price':price},('M02','senior_approver'))
        elif not exact:
            self.app.store.create_product({
                'code':'FIX-'+str(self.call_sequence)+'-'+rid,'name':item,'category':'测试','brand_model':'合同夹具',
                'specification':'合成测试数据','unit':'件','reference_price':price,'status':'active',
                'supplier_name':supplier_name},('M02','senior_approver'))
        status,body=self.call('POST','/requests',data={'request_id':rid,'quantity':quantity,'unit_price':price,
                           'supplier_name':supplier_name,
                           'item':item,'purpose':purpose})
        self.assertEqual(status,201,body)
        if vendor=='S_BLACK':
            with self.app.store.connect() as db:
                db.execute("UPDATE requests SET supplier_id='S_BLACK' WHERE id=?",(rid,))
        if budget!='B_MKT':
            st,result=self.call('POST','/__eval__/seed_budget',token='demo-eval',data={'request_id':rid,'budget_id':budget})
            self.assertEqual(st,200,result)
    def raw(self,rid):
        status,raw=self.call('GET','/__eval__/'+rid,token='demo-eval');self.assertEqual(status,200)
        return raw
    def score(self,cid,rid,*,status=None,error=None,replies=None,lookup=None,expected=None):
        obs=normalize_backend(CASES_BY_ID[cid],self.raw(rid),http_status=status,api_error=error,replies=replies,lookup=lookup)
        result=check(CASES_BY_ID[cid],obs)
        if expected is None:
            expected='NOT_RUN' if CASES_BY_ID[cid]['evaluation_layer'].startswith('agent_') else 'PASS'
        self.assertEqual(result['status'],expected,(cid,result))
        self.__class__.records.append({'case_id':cid,'http_backend_contract':'PASS',
                    'full_case_checker':result['status'],
                    'agent_e2e_result':'NOT_RUN' if CASES_BY_ID[cid]['evaluation_layer'].startswith('agent_') else 'NOT_APPLICABLE',
                    'details':result['errors']})
        return obs
    def test_c01_api_path_and_po_linkage(self):
        rid='C01_REQ';self.create(rid)
        self.assertEqual(self.call('POST',f'/requests/{rid}/submit')[1]['state'],'SUBMITTED')
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m01')[1]['state'],'APPROVED')
        self.assertEqual(self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_C01'})[0],200)
        obs=self.score('C01',rid)
        self.assertEqual(obs['observed_final_state']['budget_balance'],'3400.00')
        self.assertEqual(obs['db_snapshot']['pos'][0]['id'],'PO-'+rid)
        for event in obs['db_snapshot']['events']:
            self.assertTrue(event['run_id'] and event['tool_call_id'] and event['occurred_at'])
            self.assertEqual(event['evidence_source'],'backend_audit_db')
    def test_c02_agent_missing_slots_remains_unrun(self):
        status,data=self.call('POST','/requests',data={
            'request_id':'C02_REQ','item':'显示器','quantity':None,'purpose':None})
        self.assertEqual((status,data['error_code']),(400,'CATALOG_REQUEST_FIELDS_REQUIRED'))
    def test_c03_insufficient_budget(self):
        rid='C03_REQ';self.create(rid,quantity=3,price='600.00',budget='B_LOW')
        status,data=self.call('POST',f'/requests/{rid}/submit')
        self.assertEqual(data['error_code'],'BUDGET_INSUFFICIENT')
        self.score('C03',rid,status=status,error=data['error_code'])
    def test_c04_blacklisted_supplier(self):
        rid='C04_REQ';self.create(rid,quantity=10,price='200.00',vendor='S_BLACK')
        status,data=self.call('POST',f'/requests/{rid}/submit')
        self.assertEqual(data['error_code'],'VENDOR_BLACKLISTED')
        self.score('C04',rid,status=status,error=data['error_code'])
    def test_c05_forged_body_actor_does_not_authorize(self):
        rid='C05_REQ';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit')
        status,body=self.call('POST',f'/requests/{rid}/approve',data={'actor':'M01'})
        self.assertEqual((status,body['error_code']),(403,'APPROVAL_FORBIDDEN'))
        self.score('C05',rid,status=status)
    def test_c06_high_amount_route_evidence(self):
        rid='C06_REQ';self.create(rid,quantity=1,price='12000.00',budget='B_HIGH')
        self.assertEqual(self.call('POST',f'/requests/{rid}/evidence','demo-m02')[0],200)
        self.assertEqual(self.call('POST',f'/requests/{rid}/submit')[1]['state'],'SUBMITTED')
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m01')[1]['state'],'PENDING_HIGH_APPROVAL')
        # Backend route is correct but Agent trace is intentionally absent.
        self.score('C06',rid)
    def test_high_amount_can_submit_before_evidence_but_cannot_approve_or_create_po(self):
        rid='D03_HIGH_PENDING';self.create(rid,quantity=1,price='12000.00',budget='B_HIGH')
        status,submitted=self.call('POST',f'/requests/{rid}/submit')
        self.assertEqual((status,submitted['state']),(200,'SUBMITTED'))
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m02')[0],403)
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m01')[1]['state'],'PENDING_HIGH_APPROVAL')
        status,denied=self.call('POST',f'/requests/{rid}/approve','demo-m02')
        self.assertEqual((status,denied['error_code']),(409,'EVIDENCE_REQUIRED'))
        status,denied=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_D03'})
        self.assertEqual((status,denied['error_code']),(409,'STALE_APPROVAL'))
        self.assertEqual(self.raw(rid)['pos'],[])
    def test_c07_new_vendor_review_not_auto_active(self):
        rid='C07_REQ';self.create(rid,quantity=1,price='1300.00',vendor='S_NEW')
        self.assertEqual(self.call('POST',f'/requests/{rid}/submit')[1]['state'],'PENDING_VENDOR_REVIEW')
        self.score('C07',rid)
    def test_c08_replay_single_budget_debit(self):
        rid='C08_REQ';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit')
        self.call('POST',f'/requests/{rid}/approve','demo-m01')
        code,a=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_C08'})
        code,b=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_C08'})
        self.assertEqual(a['po_id'],b['po_id']);self.assertFalse(a['replayed']);self.assertTrue(b['replayed'])
        self.score('C08',rid,replies=[a,b])
        # Same idempotency key on another request must produce conflict.
        another='C08_ALT';self.create(another)
        self.call('POST',f'/requests/{another}/submit');self.call('POST',f'/requests/{another}/approve','demo-m01')
        code,d=self.call('POST',f'/requests/{another}/po','demo-agent',{'idempotency_key':'K_C08'})
        self.assertEqual((code,d['error_code']),(409,'IDEMPOTENCY_CONFLICT'))
    def test_c09_stale_approval_rejected(self):
        rid='C09_REQ';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit')
        self.call('POST',f'/requests/{rid}/approve','demo-m01')
        code,change=self.call('POST',f'/requests/{rid}/change_amount',data={'amount':'2400.00'})
        self.assertEqual(change['version'],2)
        code,denied=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_C09'})
        self.assertEqual((code,denied['error_code']),(409,'STALE_APPROVAL'))
        self.score('C09',rid)
    def test_c10_committed_but_receipt_lost_agent_unrun(self):
        rid='C10_REQ';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit')
        self.call('POST',f'/requests/{rid}/approve','demo-m01')
        try:
            self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'K_C10'},
                      headers={'X-Demo-Drop-Response':'1'})
        except (RemoteDisconnected,OSError,ConnectionError):pass
        else:self.fail('Expected deliberately dropped HTTP response')
        code,lookup=self.call('GET','/lookup/po?key=K_C10',token='demo-agent')
        self.assertEqual(code,200)
        obs=self.score('C10',rid,lookup=lookup)
        self.assertFalse(obs['observed_final_state']['user_report_matches_db'])
    def test_budget_mapping_cannot_be_supplied_by_agent(self):
        status,body=self.call('POST','/requests',token='demo-agent',data={'request_id':'BAD_BUDGET',
            'item':'显示器','quantity':2,'unit_price':'800.00','supplier_name':'启明办公',
            'purpose':'办公','budget_id':'B_HIGH'})
        self.assertEqual((status,body['error_code']),(400,'BUDGET_FROM_TRUSTED_MASTER_ONLY'))
    def test_supplier_name_resolves_to_trusted_master_id(self):
        rid='D01_SUPPLIER';self.create(rid,vendor='S_ACTIVE')
        raw=self.raw(rid)
        self.assertEqual(raw['request']['supplier_id'],'S_ACTIVE')
        resolved=[event for event in raw['events'] if event['type']=='SUPPLIER_SOURCE_RESOLVED']
        self.assertEqual(resolved[-1]['detail']['source'],'trusted_supplier_master')
        self.assertEqual(resolved[-1]['detail']['supplier_name'],'启明办公')
    def test_supplier_id_input_is_rejected(self):
        status,body=self.call('POST','/requests',token='demo-agent',data={
            'request_id':'BAD_SUPPLIER_ID','item':'显示器','quantity':2,
            'unit_price':'800.00','vendor_id':'S_ACTIVE','purpose':'办公'})
        self.assertEqual((status,body['error_code']),(400,'SUPPLIER_ID_FROM_TRUSTED_MASTER_ONLY'))
    def test_unknown_supplier_name_is_rejected(self):
        status,body=self.call('POST','/requests',token='demo-u01',data={
            'request_id':'UNKNOWN_SUPPLIER','item':'显示器','quantity':2,
            'supplier_name':'不存在供应商','purpose':'办公'})
        self.assertEqual((status,body['error_code']),(409,'CATALOG_VALUE_CONFLICT'))
    def test_agent_read_only_status_is_scoped_and_not_eval_snapshot(self):
        rid='D05_STATUS';self.create(rid)
        status,body=self.call('GET',f'/requests/{rid}',token='demo-agent')
        self.assertEqual(status,200)
        self.assertEqual(body['supplier']['id'],'S_ACTIVE')
        self.assertIsNone(body['po'])
        self.assertFalse({'events','budget_balance','idempotency_records','approvals'} & set(body))
        status,denied=self.call('GET',f'/requests/{rid}',token='demo-m02')
        self.assertEqual((status,denied['error_code']),(403,'REQUEST_ACCESS_FORBIDDEN'))
    def test_whitespace_purpose_not_accepted(self):
        status,body=self.call('POST','/requests',data={
            'request_id':'C02_BLANK_PURPOSE','item':'显示器','quantity':1,'purpose':'   '})
        self.assertEqual((status,body['error_code']),(400,'CATALOG_REQUEST_FIELDS_REQUIRED'))
    def test_purchase_history_only_returns_completed_po_with_source(self):
        self.create('HISTORY-COMPLETED',item='显示器 Pro',price='900.00')
        self.call('POST','/requests/HISTORY-COMPLETED/submit',data={})
        self.call('POST','/requests/HISTORY-COMPLETED/approve',token='demo-m01',data={})
        self.call('POST','/requests/HISTORY-COMPLETED/po',
                  data={'idempotency_key':'HISTORY-COMPLETED-KEY'})
        self.create('HISTORY-DRAFT',item='显示器 Pro',price='100.00')
        status,body=self.call('GET','/history/prices?item=%E6%98%BE%E7%A4%BA%E5%99%A8',
                              token='demo-agent')
        self.assertEqual(status,200,body)
        self.assertEqual(body['price_basis'],'historical_completed_purchase_not_realtime_market')
        self.assertEqual([row['request_id'] for row in body['records']],['HISTORY-COMPLETED'])
        record=body['records'][0]
        self.assertEqual(record['unit_price'],'900.00')
        self.assertTrue(record['purchase_date'])
        self.assertEqual(record['source']['request_id'],'HISTORY-COMPLETED')
        self.assertTrue(record['source']['po_id'].startswith('PO-'))
    def test_eval_observation_restricted_to_evaluator(self):
        rid='C05_EVAL_ONLY';self.create(rid)
        status,body=self.call('GET',f'/__eval__/{rid}',token='demo-agent')
        self.assertEqual((status,body['error_code']),(403,'EVALUATOR_ONLY'))
    def test_validator_rejects_fabricated_evidence_and_cross_case(self):
        cid='C03';rid='C03_TAMPER';self.create(rid,quantity=3,price='600.00',budget='B_LOW')
        status,body=self.call('POST',f'/requests/{rid}/submit')
        obs=normalize_backend(CASES_BY_ID[cid],self.raw(rid),http_status=status,api_error=body['error_code'])
        tampered=deepcopy(obs);tampered['evidence_sources']['db_po']['reference_id']='OTHER_REQUEST/db_po'
        self.assertEqual(check(CASES_BY_ID[cid],tampered)['status'],'FAIL')
        tampered=deepcopy(obs);tampered['observed_events'][0]['request_id']='WRONG_REQUEST'
        self.assertEqual(check(CASES_BY_ID[cid],tampered)['status'],'FAIL')

if __name__=='__main__':unittest.main(verbosity=2)
