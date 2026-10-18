"""采购模拟器 v1 新增接口与最小验收流程。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from backend import App, Store


class SimulatorV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=App(('127.0.0.1',0),Store(Path(self.tmp.name)/'simulator.sqlite'))
        self.base=f'http://127.0.0.1:{self.app.server_port}'
        self.thread=threading.Thread(target=self.app.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.shutdown();self.app.server_close();self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self,method,path,token='demo-u01',data=None):
        body=json.dumps(data).encode('utf-8') if data is not None else None
        request=urllib.request.Request(self.base+path,method=method,data=body,headers={
            'Authorization':'Bearer '+token,'Content-Type':'application/json',
            'X-Run-ID':'simulator-v1-tests','X-Tool-Call-ID':self._testMethodName})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.load(error)

    def create(self,rid='V1-REQ',supplier='启明办公',quantity=2,price='800.00'):
        self.call('PATCH','/admin/products/P-IT-001','demo-m02',{
            'supplier_name':supplier,'purchase_price':price})
        status,data=self.call('POST','/requests',data={'request_id':rid,'item':'显示器',
            'quantity':quantity,'unit_price':price,'supplier_name':supplier,'purpose':'办公使用'})
        self.assertEqual(status,201,data)

    def test_minimum_flow_query_and_idempotent_po(self):
        rid='V1-FLOW';self.create(rid)
        self.assertEqual(self.call('POST',f'/requests/{rid}/submit',data={})[1]['state'],'SUBMITTED')
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m01',{})[1]['state'],'APPROVED')
        first=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'V1-KEY'})
        replay=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'V1-KEY'})
        self.assertEqual(first[1]['po_id'],replay[1]['po_id'])
        self.assertFalse(first[1]['replayed']);self.assertTrue(replay[1]['replayed'])
        status,result=self.call('GET',f'/requests/{rid}/po','demo-agent')
        self.assertEqual((status,result['created'],result['po_id']),(200,True,'PO-'+rid))
        status,detail=self.call('GET',f'/requests/{rid}','demo-u01')
        self.assertEqual((status,detail['state'],detail['po']['po_id']),(200,'PO_CREATED','PO-'+rid))
        raw=self.call('GET',f'/__eval__/{rid}','demo-eval')[1]
        self.assertEqual(raw['budget_balance'],340000)
        self.assertEqual(len(raw['pos']),1)
        self.assertEqual(len([x for x in raw['events'] if x['type']=='BUDGET_DEBITED']),1)

    def test_update_creates_new_version_and_po_locks_fields(self):
        rid='V1-UPDATE';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit',data={})
        self.call('POST',f'/requests/{rid}/approve','demo-m01',{})
        status,updated=self.call('PATCH',f'/requests/{rid}/update','demo-agent',{
            'unit_price':'1200.00','expected_version':1})
        self.assertEqual((status,updated['version'],updated['state']),(200,2,'REAPPROVAL_REQUIRED'))
        detail=self.call('GET',f'/requests/{rid}','demo-agent')[1]
        self.assertEqual(detail['amount'],'2400.00')
        self.assertFalse(detail['approval']['approval_valid'])
        status,denied=self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'STALE'})
        self.assertEqual((status,denied['error_code']),(409,'STALE_APPROVAL'))
        self.call('POST',f'/requests/{rid}/submit',data={})
        self.call('POST',f'/requests/{rid}/approve','demo-m01',{})
        self.call('POST',f'/requests/{rid}/po','demo-agent',{'idempotency_key':'FINAL'})
        status,denied=self.call('PATCH',f'/requests/{rid}/update','demo-agent',{
            'purpose':'禁止修改','expected_version':2})
        self.assertEqual((status,denied['error_code']),(409,'PO_ALREADY_CREATED'))

    def test_requester_cannot_approve_and_supplier_match_is_trusted(self):
        rid='V1-AUTH';self.create(rid)
        self.call('POST',f'/requests/{rid}/submit',data={})
        status,denied=self.call('POST',f'/requests/{rid}/approve','demo-u01',{'actor':'M01'})
        self.assertEqual((status,denied['error_code']),(403,'APPROVAL_FORBIDDEN'))
        status,matches=self.call('GET','/suppliers/match?name='+urllib.parse.quote('启明'),'demo-agent')
        self.assertEqual(status,200)
        self.assertEqual(matches['matches'][0],{'id':'S_ACTIVE','name':'启明办公','status':'active'})

    def test_admin_handles_vendor_admission_and_high_evidence(self):
        rid='V1-NEW';self.create(rid,supplier='新锐科技',quantity=1,price='1300.00')
        self.assertEqual(self.call('POST',f'/requests/{rid}/submit',data={})[1]['state'],'PENDING_VENDOR_REVIEW')
        overview=self.call('GET','/admin/overview','demo-m01')[1]
        review=next(x for x in overview['vendor_reviews'] if x['request_id']==rid)
        decided=self.call('POST',f"/admin/vendor-reviews/{review['id']}/decision",'demo-m01',{'decision':'APPROVED'})[1]
        self.assertEqual(decided['state'],'SUBMITTED')
        self.assertEqual(self.call('POST',f'/requests/{rid}/approve','demo-m01',{})[1]['state'],'APPROVED')

        high='V1-HIGH';self.create(high,quantity=1,price='12000.00')
        self.call('POST','/__eval__/seed_budget','demo-eval',{'request_id':high,'budget_id':'B_HIGH'})
        self.assertEqual(self.call('POST',f'/requests/{high}/submit',data={})[1]['state'],'SUBMITTED')
        self.assertEqual(self.call('POST',f'/requests/{high}/approve','demo-m02',{})[0],403)
        self.assertEqual(self.call('POST',f'/requests/{high}/approve','demo-m01',{})[1]['state'],'PENDING_HIGH_APPROVAL')
        self.assertEqual(self.call('POST',f'/requests/{high}/approve','demo-m02',{})[0],409)
        self.call('POST',f'/requests/{high}/evidence','demo-m02',{})
        self.assertEqual(self.call('POST',f'/requests/{high}/approve','demo-m02',{})[1]['state'],'APPROVED')

    def test_single_page_is_served_without_exposing_eval_api(self):
        with urllib.request.urlopen(self.base+'/',timeout=5) as response:
            page=response.read().decode('utf-8')
        self.assertIn('采购业务模拟器 v1',page)
        self.assertNotIn('demo-eval',page)
        self.assertIn('<th>采购价</th>',page)
        self.assertNotIn('旧参考单价',page);self.assertNotIn('有效采购单价',page)
        self.assertIn("$('product-form').hidden=!isM02()",page)

    def test_agent_writes_require_atomic_expected_version(self):
        rid='V1-VERSION'
        self.call('PATCH','/admin/products/P-IT-001','demo-m02',{
            'supplier_name':'启明办公','purchase_price':'800.00'})
        product=self.call('GET','/products/P-IT-001','demo-agent')[1]
        status,created=self.call('POST','/requests','demo-agent',{
            'request_id':rid,'item':'显示器','quantity':1,'unit_price':'800.00',
            'supplier_name':'启明办公','purpose':'版本测试','product_code':'P-IT-001',
            'catalog_version':product['version']})
        self.assertEqual((status,created['amount']),(201,'800.00'))
        status,missing=self.call('POST',f'/requests/{rid}/submit','demo-agent',{})
        self.assertEqual((status,missing['error_code']),(400,'EXPECTED_VERSION_REQUIRED'))
        status,conflict=self.call('POST',f'/requests/{rid}/submit','demo-agent',{'expected_version':2})
        self.assertEqual((status,conflict['error_code']),(409,'VERSION_CONFLICT'))
        status,submitted=self.call('POST',f'/requests/{rid}/submit','demo-agent',{'expected_version':1})
        self.assertEqual((status,submitted['state']),(200,'SUBMITTED'))
        status,conflict=self.call('PATCH',f'/requests/{rid}/update','demo-agent',{
            'quantity':2,'expected_version':2})
        self.assertEqual((status,conflict['error_code']),(409,'VERSION_CONFLICT'))
        status,updated=self.call('PATCH',f'/requests/{rid}/update','demo-agent',{
            'quantity':2,'expected_version':1})
        self.assertEqual((status,updated['version'],updated['amount']),(200,2,'1600.00'))


if __name__=='__main__':
    unittest.main(verbosity=2)
