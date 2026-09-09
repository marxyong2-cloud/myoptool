"""Mock 上游服务(合并版):本地联调 / 冒烟测试用的假 LLM 上游,一个文件覆盖四种场景。

用法:
    python tools/mock_server.py                # 启动全部四种(各占原端口)
    python tools/mock_server.py --mode openai  # 只启动 OpenAI 兼容 mock(端口可用 --port 覆盖)
    python tools/mock_server.py --mode slow --port 18998

模式与端口(默认):
    openai     18999  OpenAI 兼容(对话流式/非流式 + MiniMax v2 视频协议;需 Bearer mock-upstream-key)
    anthropic  18967  Anthropic /v1/messages 协议(含 tool_use 流式分片,验证网关工具调用转换)
    deepseek   8901   DeepSeek 风格(reasoning_content 思维链流式)
    slow       18998  慢速 OpenAI 兼容(每个请求延迟 2.5s;GET /count 查询已收请求数,用于验证"停止探测"等取消逻辑)
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- openai 模式
LAST_VIDEO_REQ = {}   # 记录最后一次视频创建请求 {endpoint, payload}(测试用)
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024   # 假 mp4(带 ftyp 魔数)


class OpenAIMockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if isinstance(obj, (dict, list)) else obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("Authorization", "") == "Bearer mock-upstream-key"

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": "mock-chat-7b", "object": "model"},
                {"id": "mock-vl-2b", "object": "model"},
                {"id": "MiniMax-H3", "object": "model"}]})
        elif p.path == "/last_video_req":
            self._send(200, LAST_VIDEO_REQ)
        elif p.path == "/file.mp4":
            self._send(200, FAKE_MP4, ctype="video/mp4")
        elif p.path.startswith("/v2/query/video_generation/"):
            tid = p.path.rsplit("/", 1)[-1]
            if tid.startswith("ir-"):
                self._send(200, {"task": {"id": tid, "model": "MiniMax-H3", "status": "succeeded",
                    "content": {"prompt": "增强后的结构化视频提示词"},
                    "task_type": "h3_context_ir", "modality": "text"}})
            else:
                self._send(200, {"task": {"id": tid, "model": "MiniMax-H3", "status": "succeeded",
                    "created_at": 1700000000, "updated_at": 1700000010,
                    "content": {"url": "http://127.0.0.1:18999/file.mp4"},
                    "resolution": "2K", "duration": 5, "ratio": "16:9",
                    "task_type": "regeneration" if tid.startswith("regen-") else "generation",
                    "modality": "video",
                    "usage": {"total_seconds": 5, "input_seconds": 0, "output_seconds": 5}}})
        elif p.path == "/v2/query/video_generation":
            self._send(200, {"tasks": [{"id": "vt-123", "model": "MiniMax-H3", "status": "succeeded",
                "resolution": "2K", "duration": 5, "ratio": "16:9", "task_type": "generation"}],
                "page_num": 1, "page_size": 10, "total": 1})
        elif p.path.startswith("/v1/videos/"):
            self._send(200, {"id": p.path.split("/")[3], "status": "completed",
                             "file_path": "/tmp/mock_out.mp4"})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        if p.path.startswith("/v2/video_generation/"):
            tid = p.path.rsplit("/", 1)[-1]
            self._send(200, {"task_id": tid, "status": q.get("action", ["cancelled"])[0]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global LAST_VIDEO_REQ
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if not self._auth():
            self._send(401, {"error": "bad upstream key"})
            return
        if self.path == "/v1/chat/completions":
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for tok in ["你好", ",", "我是", "mock"]:
                    chunk = {"id": "cmpl-mock", "object": "chat.completion.chunk",
                             "model": body.get("model"),
                             "choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.03)
                last = {"id": "cmpl-mock", "object": "chat.completion.chunk", "model": body.get("model"),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}}
                self.wfile.write(f"data: {json.dumps(last, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            else:
                msgs = body.get("messages", [])
                sysmsg = str(next((m["content"] for m in msgs if m["role"] == "system"), ""))[:30]
                last_user = msgs[-1]["content"] if msgs else ""
                self._send(200, {
                    "id": "cmpl-mock", "object": "chat.completion", "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": f"[mock echo] sys={sysmsg} last={last_user}"}}],
                    "usage": {"prompt_tokens": 6, "completion_tokens": 8, "total_tokens": 14}})
        elif self.path == "/v2/video_generation":
            LAST_VIDEO_REQ = {"endpoint": "/v2/video_generation", "payload": body}
            content = body.get("content", [])
            if not any(c.get("type") == "text" and c.get("text") for c in content):
                self._send(400, {"type": "error", "error": {"type": "bad_request_error",
                    "message": "content must include a non-empty text item (2013)"}})
                return
            if not body.get("resolution") or not body.get("duration"):
                self._send(400, {"type": "error", "error": {"type": "bad_request_error",
                    "message": "resolution and duration are required"}})
                return
            self._send(200, {"task_id": "vt-123"})
        elif self.path == "/v2/video_regeneration":
            if body.get("source_task_id") or any(
                    c.get("role") == "base_video" for c in body.get("content", [])):
                self._send(200, {"task_id": "regen-777"})
            else:
                self._send(400, {"type": "error", "error": {"message": "need source_task_id or base_video"}})
        elif self.path == "/v2/h3_context_ir":
            self._send(200, {"task_id": "ir-42"})
        elif self.path == "/v1/videos":
            LAST_VIDEO_REQ = {"endpoint": "/v1/videos", "payload": body}
            self._send(200, {"id": "sglang-video-1"})
        elif self.path == "/v1/video_generation":
            self._send(200, {"task_id": "v1task-9"})
        else:
            self._send(404, {"error": "not found"})


# ------------------------------------------------------------ anthropic 模式
def _sse(ev):
    return f"event: {ev.get('type', 'x')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


class AnthropicMockHandler(BaseHTTPRequestHandler):
    """Anthropic 协议 mock:请求里带 tool_result 时给出最终文本,否则请求调用工具。"""

    def log_message(self, *a):
        pass

    def do_POST(self):
        p = urlparse(self.path)
        if p.path != "/v1/messages":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        has_result = any("tool_result" in json.dumps(m) for m in body.get("messages", []))
        stream = bool(body.get("stream"))
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(_sse({"type": "message_start", "message": {
                "id": "msg_mock", "role": "assistant", "content": [],
                "usage": {"input_tokens": 25, "output_tokens": 0}}}))
            self.wfile.write(_sse({"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}))
            for piece in (["思", "考中"] if not has_result else ["文", "件内容", "如下"]):
                self.wfile.write(_sse({"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": piece}}))
            self.wfile.write(_sse({"type": "content_block_stop", "index": 0}))
            if not has_result:
                self.wfile.write(_sse({"type": "content_block_start", "index": 1,
                                      "content_block": {"type": "tool_use",
                                                        "id": "toolu_mock_1",
                                                        "name": "read_file", "input": {}}}))
                for frag in ['{"pa', 'th": "a.m', 'd"}']:
                    self.wfile.write(_sse({"type": "content_block_delta", "index": 1,
                                          "delta": {"type": "input_json_delta",
                                                    "partial_json": frag}}))
                self.wfile.write(_sse({"type": "content_block_stop", "index": 1}))
            self.wfile.write(_sse({"type": "message_delta",
                                  "delta": {"stop_reason": "tool_use" if not has_result else "end_turn"},
                                  "usage": {"output_tokens": 12}}))
            self.wfile.write(_sse({"type": "message_stop"}))
        else:
            if has_result:
                content = [{"type": "text", "text": "文件内容如下"}]
                sr = "end_turn"
            else:
                content = [{"type": "text", "text": "思考中"},
                           {"type": "tool_use", "id": "toolu_mock_1",
                            "name": "read_file", "input": {"path": "a.md"}}]
                sr = "tool_use"
            resp = {"id": "msg_mock", "type": "message", "role": "assistant",
                    "content": content, "stop_reason": sr, "stop_sequence": None,
                    "usage": {"input_tokens": 25, "output_tokens": 12}}
            out = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


# ------------------------------------------------------------- deepseek 模式
class DeepSeekMockHandler(BaseHTTPRequestHandler):
    """DeepSeek 风格 mock:先流式吐 reasoning_content 思维链,再给最终 content。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "deepseek-v4-reasoner"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(ln) if ln else b"{}")
        if "/chat/completions" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def chunk(delta):
                return b"data: " + json.dumps({"id": "1", "object": "chat.completion.chunk",
                                               "choices": [{"index": 0, "delta": delta}]}).encode() + b"\n\n"
            for piece in ["用户在问天气。", "需要查询当前位置。", "给出简洁回答。"]:
                self.wfile.write(chunk({"reasoning_content": piece}))
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(chunk({"content": "今天天气晴朗,适合出行。"}))
            self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------- slow 模式
SLOW_COUNT = 0
SLOW_LOCK = threading.Lock()


class SlowMockHandler(BaseHTTPRequestHandler):
    """慢速 OpenAI 兼容 mock:每个 chat 请求延迟 2.5s,全部返回成功(让上下文探测一直指数增长,可中途"停止")。"""

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "slow-mock-7b", "object": "model"}]})
        elif p.path == "/count":
            with SLOW_LOCK:
                self._send(200, {"count": SLOW_COUNT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global SLOW_COUNT
        n = int(self.headers.get("Content-Length", 0))
        try:
            self.rfile.read(n)
        except Exception:
            pass
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        with SLOW_LOCK:
            SLOW_COUNT += 1
        time.sleep(2.5)
        self._send(200, {
            "id": "cmpl-slow", "object": "chat.completion", "model": "slow-mock-7b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})


MODES = {
    "openai": (OpenAIMockHandler, 18999),
    "anthropic": (AnthropicMockHandler, 18967),
    "deepseek": (DeepSeekMockHandler, 8901),
    "slow": (SlowMockHandler, 18998),
}


def serve(mode, port):
    ThreadingHTTPServer(("127.0.0.1", port), MODES[mode][0]).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mock LLM 上游(联调/冒烟测试)")
    ap.add_argument("--mode", default="all", choices=list(MODES) + ["all"],
                    help="只启动某一模式;默认 all 全部启动(各占原端口)")
    ap.add_argument("--port", type=int, default=0, help="单模式时覆盖端口(默认用各模式原端口)")
    args = ap.parse_args()

    if args.mode == "all":
        for m, (_h, port) in MODES.items():
            threading.Thread(target=serve, args=(m, port), daemon=True).start()
            print(f"[mock] {m:10s} http://127.0.0.1:{port}")
        print("[mock] 全部已启动,Ctrl+C 退出")
        threading.Event().wait()          # 主线程挂起,daemon 线程持续服务
    else:
        port = args.port or MODES[args.mode][1]
        print(f"[mock] {args.mode} http://127.0.0.1:{port}")
        serve(args.mode, port)
