"""G1 真实模型 Tool Calling Gate；默认 NOT_RUN，需显式开启并提供模型配置。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from agent_runtime import AgentRuntime, OpenAICompatibleModel, TraceWriter
from agent_tools import ProcurementApiClient
from backend import App, Store


REAL_MODEL_READY=(
    os.getenv('RUN_REAL_MODEL_TEST')=='1'
    and all(os.getenv(name) for name in (
        'AGENT_MODEL_API_KEY','AGENT_MODEL_BASE_URL','AGENT_MODEL_NAME'))
)


@unittest.skipUnless(REAL_MODEL_READY,'NOT_RUN: 未显式配置真实模型 Tool Calling Gate')
class RealModelToolCallingGate(unittest.TestCase):
    def test_g1_real_model_reads_actual_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App(('127.0.0.1',0),Store(Path(tmp)/'real-model.sqlite'))
            thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{app.server_port}'
            try:
                request=urllib.request.Request(base+'/requests',method='POST',
                    data=json.dumps({'request_id':'G1-REAL-MODEL','item':'显示器','quantity':1,
                        'unit_price':'800.00','supplier_name':'启明办公','purpose':'G1验收'}).encode(),
                    headers={'Authorization':'Bearer demo-u01','Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3):pass
                model=OpenAICompatibleModel.from_env()
                trace_dir=Path(os.getenv('REAL_MODEL_TRACE_DIR',str(Path(tmp)/'traces')))
                runtime=AgentRuntime(model,ProcurementApiClient(base,'demo-agent',10),
                    TraceWriter(trace_dir),model_name=os.environ['AGENT_MODEL_NAME'])
                result=runtime.handle_message(runtime.new_session('G1-REAL-SESSION'),
                    '请查询申请 G1-REAL-MODEL 的当前真实状态')
                self.assertEqual(result['route_reason_code'],'READ_VERIFIED',result)
                self.assertEqual(result['result']['request_id'],'G1-REAL-MODEL')
            finally:
                app.shutdown();app.server_close();thread.join(timeout=5)

    def test_real_model_history_follow_up_uses_trusted_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App(('127.0.0.1',0),Store(Path(tmp)/'real-model-multiturn.sqlite'))
            thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{app.server_port}'

            def call(method,path,data=None,token='demo-u01'):
                request=urllib.request.Request(base+path,method=method,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3) as response:
                    return json.load(response)

            try:
                call('POST','/requests',{
                    'request_id':'REAL-HISTORY-01','item':'显示器','quantity':2,
                    'unit_price':'880.00','supplier_name':'启明办公','purpose':'多轮验收',
                })
                call('POST','/requests/REAL-HISTORY-01/submit',{})
                call('POST','/requests/REAL-HISTORY-01/approve',{},'demo-m01')
                call('POST','/requests/REAL-HISTORY-01/po',{'idempotency_key':'REAL-HISTORY-01-PO'})
                trace_dir=Path(os.getenv('REAL_MODEL_TRACE_DIR',str(Path(tmp)/'traces')))
                runtime=AgentRuntime(
                    OpenAICompatibleModel.from_env(),ProcurementApiClient(base,'demo-agent',10),
                    TraceWriter(trace_dir),model_name=os.environ['AGENT_MODEL_NAME'])
                state=runtime.new_session('REAL-MULTITURN-SESSION')
                first=runtime.handle_message(state,'查询显示器的历史成交记录')
                self.assertEqual(first['route_reason_code'],'READ_VERIFIED',first)
                self.assertEqual(first['result']['history'][0]['source']['request_id'],'REAL-HISTORY-01')
                second=runtime.handle_message(state,'上一次的供应商、数量和申请编号分别是什么？')
                self.assertEqual(second['route_reason_code'],'READ_VERIFIED',second)
                self.assertIn('启明办公',second['message'])
                self.assertIn('2',second['message'])
                self.assertIn('REAL-HISTORY-01',second['message'])
                self.assertIsNone(second['confirmation'])
            finally:
                app.shutdown();app.server_close();thread.join(timeout=5)


if __name__=='__main__':
    unittest.main(verbosity=2)
