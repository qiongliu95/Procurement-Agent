"""目录权限、主数据维护、默认供应商和软删除的定向测试。"""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

from backend import App, Store


class ProductManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_path=Path(self.tmp.name)/'products.sqlite'
        self.app=App(('127.0.0.1',0),Store(self.db_path))
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
            'X-Run-ID':'directory-tests','X-Tool-Call-ID':self._testMethodName})
        try:
            with urllib.request.urlopen(request,timeout=5) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.load(error)

    @staticmethod
    def new_product(code='P-TEST-001'):
        return {'code':code,'name':'测试扫描仪','category':'办公设备','brand_model':'通用 / Scan-01',
                'specification':'A4 双面扫描','unit':'台','reference_price':'1599.00','status':'active'}

    def test_product_read_permission_and_default_supplier(self):
        status,body=self.call('GET','/products','demo-u01')
        self.assertEqual((status,body['error_code']),(403,'PRODUCT_READ_FORBIDDEN'))
        for token in ('demo-agent','demo-m01','demo-m02'):
            status,data=self.call('GET','/products',token)
            self.assertEqual(status,200,data);self.assertEqual(data['total'],8)
            self.assertTrue(all(x['supplier']['name']=='新锐科技' for x in data['products']))
            self.assertTrue(all(x['purchase_price']==x['reference_price'] for x in data['products']))

    def test_only_m02_can_manage_and_delete_products(self):
        for token in ('demo-u01','demo-agent','demo-m01'):
            status,body=self.call('POST','/admin/products',token,self.new_product())
            self.assertEqual((status,body['error_code']),(403,'PRODUCT_ADMIN_REQUIRED'))
        status,created=self.call('POST','/admin/products','demo-m02',self.new_product())
        self.assertEqual((status,created['purchase_price'],created['supplier']['name']),(201,'1599.00','新锐科技'))
        status,updated=self.call('PATCH','/admin/products/P-TEST-001','demo-m02',{
            'name':'测试高速扫描仪','reference_price':'1699.00','status':'inactive'})
        self.assertEqual((status,updated['changed'],updated['purchase_price']),(200,True,'1699.00'))
        status,deleted=self.call('DELETE','/admin/products/P-TEST-001','demo-m02')
        self.assertEqual((status,deleted['deleted']),(200,True))
        self.assertEqual(self.call('GET','/products/P-TEST-001','demo-agent')[0],404)
        with closing(sqlite3.connect(self.db_path)) as db:
            actions=db.execute("SELECT action FROM product_events WHERE product_code='P-TEST-001' ORDER BY seq").fetchall()
        self.assertEqual(actions,[('CREATED',),('UPDATED',),('DELETED',)])

    def test_supplier_and_budget_crud_permissions_and_guards(self):
        for token in ('demo-u01','demo-agent','demo-m01'):
            self.assertEqual(self.call('POST','/admin/suppliers',token,{'id':'S_TEST','name':'测试供应商'})[0],403)
            self.assertEqual(self.call('POST','/admin/budgets',token,{'id':'B_TEST','balance':'100'})[0],403)
        self.assertEqual(self.call('POST','/admin/suppliers','demo-m02',{'id':'S_TEST','name':'测试供应商'})[1]['status'],'new')
        self.assertEqual(self.call('PATCH','/admin/suppliers/S_TEST','demo-m02',{'status':'active'})[1]['status'],'active')
        self.assertTrue(self.call('DELETE','/admin/suppliers/S_TEST','demo-m02')[1]['deleted'])
        self.assertEqual(self.call('PATCH','/admin/suppliers/S_NEW','demo-m02',{'name':'改名'})[1]['error_code'],
                         'DEFAULT_SUPPLIER_NAME_PROTECTED')
        self.assertEqual(self.call('DELETE','/admin/suppliers/S_NEW','demo-m02')[1]['error_code'],'SUPPLIER_IN_USE')
        self.assertEqual(self.call('POST','/admin/budgets','demo-m02',{'id':'B_TEST','balance':'100'})[1]['balance'],'100.00')
        self.assertEqual(self.call('PATCH','/admin/budgets/B_TEST','demo-m02',{'balance':'250.50'})[1]['balance'],'250.50')
        self.assertTrue(self.call('DELETE','/admin/budgets/B_TEST','demo-m02')[1]['deleted'])
        self.assertEqual(self.call('DELETE','/admin/budgets/B_MKT','demo-m02')[1]['error_code'],'BUDGET_IN_USE')

    def test_search_and_schema_are_compatible(self):
        query=urllib.parse.urlencode({'query':'电脑','status':'active'})
        data=self.call('GET','/products?'+query,'demo-agent')[1]
        self.assertEqual([item['code'] for item in data['products']],['P-IT-002'])
        with closing(sqlite3.connect(self.db_path)) as db:
            product_columns={row[1] for row in db.execute('PRAGMA table_info(products)')}
            supplier_columns={row[1] for row in db.execute('PRAGMA table_info(suppliers)')}
            budget_columns={row[1] for row in db.execute('PRAGMA table_info(budgets)')}
        self.assertTrue({'supplier_id','purchase_price','version','deleted_at'}<=product_columns)
        self.assertIn('deleted_at',supplier_columns);self.assertIn('deleted_at',budget_columns)


if __name__=='__main__':
    unittest.main(verbosity=2)
