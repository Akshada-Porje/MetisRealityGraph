import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import metis.llm as llm


def _server(responses):
    calls = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            code, body = responses[min(len(calls), len(responses) - 1)]
            calls.append(code)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


OK = {"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}],
      "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}


def _client(monkeypatch, srv, sleeps):
    monkeypatch.setenv("METIS_ENDPOINT", f"http://127.0.0.1:{srv.server_port}/{{model}}")
    monkeypatch.setattr(llm, "_sleep", lambda s: sleeps.append(s))
    return llm.GeminiClient(api_key="k", model="m", cache_dir=None)


def test_429_waits_for_server_retry_delay_then_succeeds(monkeypatch):
    err = {"error": {"code": 429, "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
    srv, calls = _server([(429, err), (429, err), (200, OK)])
    sleeps = []
    r = _client(monkeypatch, srv, sleeps).generate_json("s", "u")
    assert r.parsed == {"a": 1} and r.prompt_tokens == 7
    assert calls == [429, 429, 200] and sleeps == [35.0, 35.0]


def test_503_backs_off_and_recovers(monkeypatch):
    srv, calls = _server([(503, {"error": {"message": "busy"}}), (200, OK)])
    sleeps = []
    assert _client(monkeypatch, srv, sleeps).generate_json("s", "u").parsed == {"a": 1}
    assert calls == [503, 200] and len(sleeps) == 1


def test_daily_quota_stops_immediately(monkeypatch):
    err = {"error": {"code": 429, "message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel"}}
    srv, calls = _server([(429, err)])
    with pytest.raises(llm.QuotaExhausted):
        _client(monkeypatch, srv, []).generate_json("s", "u")
    assert calls == [429]


def test_400_is_not_retried(monkeypatch):
    srv, calls = _server([(400, {"error": {"message": "bad"}})])
    with pytest.raises(llm.LLMError):
        _client(monkeypatch, srv, []).generate_json("s", "u")
    assert calls == [400]


def test_retry_delay_parsing():
    assert llm.retry_delay_seconds("Please retry in 12.5s.") == 12.5
    assert llm.retry_delay_seconds("garbage") is None
