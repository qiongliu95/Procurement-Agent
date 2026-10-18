"""Agent 页面/API 的最小会话与输入边界测试。"""
import http.cookiejar
import json
import threading
import unittest
import urllib.error
import urllib.request

from agent_runtime import SessionState
from agent_server import create_server


class StubRuntime:
    def new_session(self, session_id=None):
        return SessionState(session_id or 'stub-session',actor_identity='U01')

    def public_session(self,state):
        return {'session_id':state.session_id,'actor':state.actor_identity,
                'messages':list(state.conversation_tail),'pending_confirmation':None}

    def handle_message(self,state,message):
        state.conversation_tail.extend([
            {'role':'user','content':message},{'role':'assistant','content':'收到：'+message}])
        return {'message':'收到：'+message,'route_action':'ASK','route_reason_code':'STUB',
                'trace_id':'stub-run','confirmation':None,'result':None,'outcome_status':None}

    def confirm(self,state,action_id,decision):
        return {'message':decision,'route_action':'STOP','route_reason_code':'STUB_CONFIRM',
                'trace_id':'stub-run','confirmation':None,'result':None,'outcome_status':None}

    def clear_session(self,state):
        state.conversation_tail=[]
        return {'message':'会话已清空','route_action':'STOP','route_reason_code':'SESSION_CLEARED',
                'trace_id':'stub-run','confirmation':None,'result':None,'outcome_status':None}


class AgentHttpTests(unittest.TestCase):
    def setUp(self):
        self.server=create_server(StubRuntime(),0)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        jar=http.cookiejar.CookieJar()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

    def call(self,path,body=None):
        data=json.dumps(body).encode() if body is not None else None
        request=urllib.request.Request(self.base+path,method='POST' if body is not None else 'GET',
            data=data,headers={'Content-Type':'application/json'})
        try:
            with self.client.open(request,timeout=3) as response:
                return response.status,response.headers,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,error.headers,json.load(error)

    def test_session_is_fixed_to_u01_and_cookie_is_http_only(self):
        status,headers,body=self.call('/api/session')
        self.assertEqual((status,body['actor']),(200,'U01'))
        self.assertIn('HttpOnly',headers.get('Set-Cookie'))
        self.assertIn('SameSite=Strict',headers.get('Set-Cookie'))

    def test_chat_rejects_identity_override(self):
        status,_,body=self.call('/api/chat',{'message':'查询申请','user_id':'M01'})
        self.assertEqual((status,body['error_code']),(400,'INVALID_CHAT_REQUEST'))

    def test_page_does_not_expose_backend_token_or_admin_controls(self):
        with self.client.open(self.base+'/',timeout=3) as response:
            page=response.read().decode()
        self.assertIn('可信身份：U01',page)
        self.assertNotIn('demo-agent',page)
        self.assertNotIn('/__eval__',page)
        self.assertIn("x.textContent='已处理'",page)
        self.assertNotIn("finally{statusEl.textContent='';buttons.forEach(x=>x.disabled=false)}",page)
        self.assertNotIn("data.route_action",page)
        self.assertNotIn("data.trace_id",page)

    def test_page_uses_typed_cards_and_hides_consultation_business_card(self):
        with self.client.open(self.base+'/',timeout=3) as response:
            page=response.read().decode()
        self.assertIn("type==='history'",page)
        self.assertIn("type==='products'",page)
        self.assertIn("type==='knowledge'||type==='rules'||type==='consultation'",page)
        self.assertIn("type!=='request'",page)
        self.assertIn("已核验采购申请",page)
        self.assertIn("addResult(item.result)",page)

    def test_refresh_restores_history_and_clear_endpoint_resets_it(self):
        self.call('/api/chat',{'message':'创建一张申请'})
        _,_,restored=self.call('/api/session')
        self.assertEqual([item['role'] for item in restored['messages']],['user','assistant'])
        status,_,body=self.call('/api/session/clear',{})
        self.assertEqual((status,body['route_reason_code']),(200,'SESSION_CLEARED'))
        _,_,cleared=self.call('/api/session')
        self.assertEqual(cleared['messages'],[])


if __name__=='__main__':
    unittest.main(verbosity=2)
