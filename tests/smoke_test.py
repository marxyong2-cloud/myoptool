# -*- coding: utf-8 -*-
"""ModelUse 全量冒烟测试:网关(流式/绑定/Anthropic/v2) + 对话窗口视频 + 会话 + 渠道自动识别。"""
import json, urllib.request, urllib.error

GW = "http://127.0.0.1:8765"

def api(method, path, body=None, headers=None, raw=False):
    req = urllib.request.Request(GW + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    else:
        data = None
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, data, timeout=200) as r:
            b = r.read().decode("utf-8")
            parsed = b if raw else (json.loads(b) if b.strip().startswith(("{", "[")) else b)
            return r.status, parsed, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:300], {}

ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    print(("PASS" if cond else "FAIL"), name, ("| " + str(detail)[:180]) if not cond else "")
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

# ---------- 重建渠道/密钥 ----------
cfg = json.loads(urllib.request.urlopen(GW + "/api/gateway/config").read())
for k in cfg["keys"]:
    api("DELETE", "/api/gateway/keys/" + k["key"])
for c in cfg["channels"]:
    api("DELETE", "/api/gateway/channels/" + c["id"])
api("POST", "/api/gateway/channels", {"name": "Mock渠道A", "base_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai",
    "types": ["chat", "image", "video", "audio", "embedding"],
    "models": ["mock-chat-7b", "mock-vl-2b", "MiniMax-H3"]})
api("POST", "/api/gateway/channels", {"name": "渠道B", "base_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "types": ["chat"],
    "models": ["mock-chat-7b"]})
cfg = json.loads(urllib.request.urlopen(GW + "/api/gateway/config").read())
chA = next(c for c in cfg["channels"] if c["name"] == "Mock渠道A")
chB = next(c for c in cfg["channels"] if c["name"] == "渠道B")
api("POST", "/api/gateway/keys", {"name": "绑定B", "allowed_channels": [chB["id"]]})
api("POST", "/api/gateway/keys", {"name": "全量"})
cfg = json.loads(urllib.request.urlopen(GW + "/api/gateway/config").read())
kB = next(k for k in cfg["keys"] if k["name"] == "绑定B")["key"]
kA = next(k for k in cfg["keys"] if k["name"] == "全量")["key"]
AUTH = {"Authorization": "Bearer " + kA}
AUTHB = {"Authorization": "Bearer " + kB}

# ---------- 1. 网关流式 ----------
st, body, _ = api("POST", "/v1/chat/completions", {"model": "mock-chat-7b", "stream": True,
    "messages": [{"role": "user", "content": "hi"}]}, AUTH, raw=True)
contents = []
for l in body.split("\n"):
    if not l.startswith("data:"):
        continue
    ds = l[5:].strip()
    if not ds or ds == "[DONE]":
        continue
    d = json.loads(ds)
    if d.get("choices") and d["choices"][0].get("delta", {}).get("content"):
        contents.append(d["choices"][0]["delta"]["content"])
check("网关流式内容块", st == 200 and contents == ["你好", ",", "我是", "mock"], repr(body[:200]))

# ---------- 2. 密钥绑定 ----------
st, d, _ = api("GET", "/v1/models", headers=AUTHB)
check("绑定密钥模型列表仅B", st == 200 and [m["id"] for m in d["data"]] == ["mock-chat-7b"])
api("POST", "/api/gateway/channels", {"id": chA["id"], "name": "Mock渠道A",
    "base_url": "http://127.0.0.1:18999", "api_key": "mock-upstream-key", "protocol": "openai",
    "types": ["chat", "image", "video", "audio", "embedding"], "models": ["mock-only-A"]})
st, _, _ = api("POST", "/v1/chat/completions", {"model": "mock-only-A",
    "messages": [{"role": "user", "content": "x"}]}, AUTHB)
check("绑定密钥访问A独占模型被拒(403)", st == 403, "got %s" % st)
st, _, _ = api("POST", "/v1/chat/completions", {"model": "mock-only-A",
    "messages": [{"role": "user", "content": "x"}]}, AUTH)
check("全量密钥可访问A", st == 200, "got %s" % st)
api("POST", "/api/gateway/channels", {"id": chA["id"], "name": "Mock渠道A",
    "base_url": "http://127.0.0.1:18999", "api_key": "mock-upstream-key", "protocol": "openai",
    "types": ["chat", "image", "video", "audio", "embedding"],
    "models": ["mock-chat-7b", "mock-vl-2b", "MiniMax-H3"]})

# ---------- 3. Anthropic ----------
st, d, _ = api("POST", "/v1/messages", {"model": "mock-chat-7b", "max_tokens": 100,
    "system": "你是测试", "messages": [{"role": "user", "content": "你好"}]}, {"x-api-key": kA})
check("/v1/messages 非流式", st == 200 and d.get("type") == "message"
      and "mock echo" in d["content"][0]["text"], str(d)[:150])
st, body, _ = api("POST", "/v1/messages", {"model": "mock-chat-7b", "stream": True,
    "messages": [{"role": "user", "content": "hi"}]}, {"x-api-key": kA}, raw=True)
evs = [l[6:].strip() for l in body.split("\n") if l.startswith("event:")]
check("/v1/messages 流式事件", st == 200 and "message_start" in evs and "message_stop" in evs
      and evs.count("content_block_delta") >= 4, str(evs[:8]))

# ---------- 4. MiniMax v2 网关端点 ----------
st, d, _ = api("POST", "/v2/video_generation", {"model": "MiniMax-H3",
    "content": [{"type": "text", "text": "一只猫在弹钢琴"}],
    "resolution": "2K", "duration": 5, "ratio": "16:9"}, AUTH)
check("v2创建任务", st == 200 and d.get("task_id") == "vt-123", str(d))
st, d, _ = api("GET", "/v2/query/video_generation/vt-123", headers=AUTH)
check("v2查询任务", st == 200 and d["task"]["status"] == "succeeded"
      and d["task"]["content"]["url"].endswith(".mp4"), str(d)[:150])
st, d, _ = api("GET", "/v2/query/video_generation?page_num=1&filter.status=succeeded", headers=AUTH)
check("v2任务列表", st == 200 and d.get("total") == 1, str(d)[:150])
st, d, _ = api("POST", "/v2/video_regeneration", {"model": "MiniMax-H3",
    "source_task_id": "vt-123", "resolution": "2K"}, AUTH)
check("v2再生成(源任务)", st == 200 and d.get("task_id") == "regen-777", str(d))
st, d, _ = api("POST", "/v2/video_regeneration", {"model": "MiniMax-H3", "resolution": "2K",
    "content": [{"type": "text", "text": "更清晰"},
                {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}, "role": "base_video"}]}, AUTH)
check("v2再生成(base_video)", st == 200 and d.get("task_id") == "regen-777", str(d))
st, d, _ = api("POST", "/v2/h3_context_ir", {"model": "MiniMax-H3",
    "content": [{"type": "text", "text": "描述"}], "duration": 5, "ratio": "16:9"}, AUTH)
check("v2 Context-IR", st == 200 and d.get("task_id") == "ir-42", str(d))
req = urllib.request.Request(GW + "/v2/video_generation/vt-123?action=cancelled", method="DELETE")
req.add_header("Authorization", "Bearer " + kA)
with urllib.request.urlopen(req) as r:
    d = json.loads(r.read())
    check("v2取消任务", r.status == 200 and d.get("status") == "cancelled", str(d))
st, d, _ = api("POST", "/v2/video_generation", {"model": "MiniMax-H3",
    "content": [{"type": "text", "text": ""}], "resolution": "2K", "duration": 5}, AUTH)
check("v2参数错误透传(400)", st == 400 and "text" in str(d), str(d)[:120])

# ---------- 5. 对话窗口视频 ----------
st, d, _ = api("POST", "/api/chat", {"api_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "task_type": "video",
    "model": "MiniMax-H3", "message": "史诗级太空预告",
    "video_resolution": "2K", "video_ratio": "16:9", "video_api": "v2", "video_duration": 5})
check("对话-文生视频v2(产物取回)", st == 200 and d.get("type") == "video"
      and d.get("media_url", "").startswith("/api/media/")
      and d.get("style") == "v2" and d.get("job_id") == "vt-123", str(d)[:200])
video1 = d

st, d, _ = api("POST", "/api/chat", {"api_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "task_type": "image_video",
    "model": "MiniMax-H3", "message": "镜头拉远",
    "video_api": "v2", "video_duration": 8,
    "attachments": [
        {"name": "a.png", "type": "image", "content": "data:image/png;base64,AAAA"},
        {"name": "b.png", "type": "image", "content": "data:image/png;base64,BBBB"}]})
check("对话-图生视频v2(首尾帧)", st == 200 and d.get("style") == "v2", str(d)[:200])

st, d, _ = api("POST", "/api/chat", {"api_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "task_type": "video_regen",
    "model": "MiniMax-H3", "message": "",
    "regen_task_id": "vt-123", "video_resolution": "2K"})
check("对话-视频再生成(源任务)", st == 200 and d.get("style") == "v2regen"
      and d.get("job_id") == "regen-777", str(d)[:200])

st, d, _ = api("POST", "/api/chat", {"api_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "task_type": "video_regen",
    "model": "MiniMax-H3", "message": "更清晰",
    "regen_source": video1.get("media_url", ""), "video_resolution": "2K"})
check("对话-视频再生成(本地源视频)", st == 200 and d.get("job_id") == "regen-777", str(d)[:200])

st, d, _ = api("POST", "/api/chat", {"api_url": "http://127.0.0.1:18999",
    "api_key": "mock-upstream-key", "protocol": "openai", "task_type": "video",
    "model": "MiniMax-H3", "message": "sglang style", "video_api": "auto"})
check("对话-视频风格自动探测", st == 200 and d.get("style") in ("videos", "v2"), str(d)[:200])

# ---------- 6. 会话扩展字段 ----------
st, s, _ = api("POST", "/api/chat/sessions", {"title": "冒烟会话"})
sid = s["id"]
st, s2, _ = api("PUT", "/api/chat/sessions/" + sid, {"title": "冒烟会话改", "archived": False,
    "messages": [{"role": "user", "content": "hi"}], "project": "",
    "system_prompt": "你是冒烟测试", "params": {"temperature": 0.5},
    "data_source": chA["id"], "model": "MiniMax-H3"})
check("会话扩展字段保存", st == 200 and s2.get("system_prompt") == "你是冒烟测试"
      and s2.get("params", {}).get("temperature") == 0.5, str(s2)[:150])
api("DELETE", "/api/chat/sessions/" + sid)

# ---------- 7. 渠道保存自动识别 ----------
st, d, _ = api("POST", "/api/gateway/channels", {"name": "自动识别渠道",
    "base_url": "http://127.0.0.1:18999", "api_key": "mock-upstream-key",
    "protocol": "openai", "types": ["chat"], "models": []})
cfg = json.loads(urllib.request.urlopen(GW + "/api/gateway/config").read())
chc = next(c for c in cfg["channels"] if c["name"] == "自动识别渠道")
check("渠道保存自动识别模型", st == 200 and len(chc["models"]) >= 3, str(chc["models"]))
api("DELETE", "/api/gateway/channels/" + chc["id"])

print("\n==== 冒烟结果: 通过 %d / 失败 %d ====" % (ok, fail))
