"""采购 Agent v1 本地 HTTP 服务；仅监听 loopback。"""
from __future__ import annotations

import json
import os
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent_runtime import AgentRuntime, OpenAICompatibleModel, SessionState, TraceWriter
from agent_tools import ProcurementApiClient
from knowledge_rag import build_default_index


class SessionStore:
    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.sessions: dict[str, tuple[SessionState, threading.RLock]] = {}

    def get(self, session_id: str | None) -> tuple[SessionState, threading.RLock, bool]:
        with self.lock:
            if session_id and session_id in self.sessions:
                state, session_lock = self.sessions[session_id]
                return state, session_lock, False
            state = self.runtime.new_session()
            session_lock = threading.RLock()
            self.sessions[state.session_id] = (state, session_lock)
            return state, session_lock, True


class AgentHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, runtime: AgentRuntime):
        self.runtime = runtime
        self.session_store = SessionStore(runtime)
        super().__init__(address, AgentHandler)


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentHttpServer

    def log_message(self, *_args):
        pass

    def _session_id(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get("procurement_agent_session")
        return morsel.value if morsel else None

    def _session(self):
        return self.server.session_store.get(self._session_id())

    def _reply_json(self, status: int, payload: dict, *, session_id: str | None = None):
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if session_id:
            self.send_header(
                "Set-Cookie",
                f"procurement_agent_session={session_id}; Path=/; HttpOnly; SameSite=Strict",
            )
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _reply_html(self):
        blob = Path(__file__).with_name("agent.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        )
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("INVALID_CONTENT_LENGTH") from exc
        if length < 0 or length > 65536:
            raise ValueError("BODY_TOO_LARGE")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("BAD_JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("BODY_MUST_BE_OBJECT")
        return payload

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/agent.html"):
            self._reply_html()
            return
        if path == "/api/session":
            state, session_lock, _ = self._session()
            with session_lock:
                payload = self.server.runtime.public_session(state)
            self._reply_json(200, payload, session_id=state.session_id)
            return
        self._reply_json(404, {"error_code": "NOT_FOUND"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._body()
            state, session_lock, _ = self._session()
            with session_lock:
                if path == "/api/chat":
                    allowed = {"message"}
                    if set(payload) - allowed or not isinstance(payload.get("message"), str):
                        raise ValueError("INVALID_CHAT_REQUEST")
                    result = self.server.runtime.handle_message(state, payload["message"])
                elif path == "/api/confirm":
                    allowed = {"action_id", "decision"}
                    if set(payload) - allowed:
                        raise ValueError("INVALID_CONFIRM_REQUEST")
                    action_id = payload.get("action_id")
                    decision = payload.get("decision")
                    if not isinstance(action_id, str) or decision not in ("confirm", "cancel"):
                        raise ValueError("INVALID_CONFIRM_REQUEST")
                    result = self.server.runtime.confirm(state, action_id, decision)
                elif path == "/api/session/clear":
                    if payload:
                        raise ValueError("INVALID_CLEAR_REQUEST")
                    result = self.server.runtime.clear_session(state)
                else:
                    self._reply_json(404, {"error_code": "NOT_FOUND"}, session_id=state.session_id)
                    return
            self._reply_json(200, result, session_id=state.session_id)
        except ValueError as exc:
            self._reply_json(400, {"error_code": str(exc)})
        except Exception:
            import traceback
            traceback.print_exc()
            self._reply_json(500, {"error_code": "AGENT_INTERNAL_ERROR"})

def create_server(runtime: AgentRuntime, port: int = 8780) -> AgentHttpServer:
    return AgentHttpServer(("127.0.0.1", port), runtime)


def build_runtime_from_env() -> AgentRuntime:
    print("Agent startup: loading model configuration", flush=True)
    model = OpenAICompatibleModel.from_env()
    print("Agent startup: loading knowledge index", flush=True)
    knowledge_index, _index_meta = build_default_index()
    if os.getenv("AGENT_RAG_WARMUP", "1") == "1":
        # 冷启动可能超过一分钟，先落日志，避免被误认为服务无响应。
        print("Agent startup: warming up knowledge encoder (cold start may take several minutes)", flush=True)
        warmup = knowledge_index.warmup()
        print(f"Knowledge encoder ready; warmup_ms={warmup['encoder_warmup_ms']}", flush=True)
    api = ProcurementApiClient(
        os.getenv("PROCUREMENT_API_BASE_URL", "http://127.0.0.1:8765"),
        os.getenv("PROCUREMENT_AGENT_TOKEN", "demo-agent"),
        float(os.getenv("AGENT_HTTP_TIMEOUT_SECONDS", "10")),
    )
    trace = TraceWriter(os.getenv("AGENT_TRACE_DIR", "runtime_data/traces"))
    return AgentRuntime(
        model,
        api,
        trace,
        model_name=os.getenv("AGENT_MODEL_NAME", "configured-model"),
        confirm_ttl_seconds=int(os.getenv("AGENT_CONFIRM_TTL_SECONDS", "600")),
        max_model_steps=int(os.getenv("AGENT_MAX_MODEL_STEPS", "6")),
        write_retry_limit=int(os.getenv("AGENT_WRITE_RETRY_LIMIT", "1")),
        conversation_turns=int(os.getenv("AGENT_CONVERSATION_TURNS", "12")),
        model_context_messages=int(os.getenv("AGENT_MODEL_CONTEXT_MESSAGES", "6")),
        session_history_messages=int(os.getenv("AGENT_SESSION_HISTORY_MESSAGES", "200")),
        knowledge_index=knowledge_index,
    )


def serve(port: int = 8780):
    print("Agent startup: initializing runtime", flush=True)
    runtime = build_runtime_from_env()
    server = create_server(runtime, port)
    print(f"Agent UI listening on http://127.0.0.1:{server.server_port}; fixed identity U01", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    serve(int(os.getenv("AGENT_PORT", "8780")))
