"""A local stand-in for the Azure OpenAI / Foundry HTTP API, to drive the REAL openai SDK client against.

It reproduces the behaviours that matter: the classic route (/openai/deployments/<name>/chat/completions with an
api-version query and an `api-key` header), the OpenAI-compatible v1 route (/openai/v1/chat/completions with the
deployment as `model`), 401 for a wrong key, 404 DeploymentNotFound, 429 throttling, and a deployment that rejects
JSON mode. It records every request so tests can check exactly what the client sent.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class AzureStub:
    def __init__(self, key="K" * 32, deployment="gpt-prod", classic=True, v1=True, reject_json_mode=False, throttle=0, reply="OK"):
        self.key, self.deployment, self.classic, self.v1 = key, deployment, classic, v1
        self.reject_json_mode, self.throttle, self.reply = reject_json_mode, throttle, reply
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                u = urlparse(self.path)
                stub.requests.append({"path": u.path, "query": parse_qs(u.query), "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if stub.throttle > 0:
                    stub.throttle -= 1
                    return self._send(429, {"error": {"code": "429", "message": "Requests to the ChatCompletions_Create Operation have exceeded call rate limit."}},
                                      {"retry-after-ms": "1"})
                m = re.fullmatch(r"/openai/deployments/([^/]+)/chat/completions", u.path)
                if m and stub.classic:
                    key, dep = self.headers.get("api-key"), m.group(1)
                elif u.path == "/openai/v1/chat/completions" and stub.v1:
                    auth = self.headers.get("Authorization", "")
                    key, dep = self.headers.get("api-key") or (auth[7:] if auth.startswith("Bearer ") else None), body.get("model")
                else:
                    # a Foundry resource that does not serve this route answers like a failed sign-in, not a clean 404
                    return self._send(401, {"error": {"code": "401", "message": "Access denied due to invalid subscription key or wrong API endpoint."}})
                if key != stub.key:
                    return self._send(401, {"error": {"code": "401", "message": "Access denied due to invalid subscription key or wrong API endpoint."}})
                if dep != stub.deployment:
                    return self._send(404, {"error": {"code": "DeploymentNotFound", "message": "The API deployment for this resource does not exist."}})
                if "response_format" in body and stub.reject_json_mode:
                    return self._send(400, {"error": {"code": "BadRequest", "message": "response_format is not supported for this deployment"}})
                self._send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": dep,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": stub.reply}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
