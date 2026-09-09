# -*- coding: utf-8 -*-
"""新功能 API 测试:上传 size / 渠道开关 / 密钥开关(禁用403) / probe 延迟持久化"""
import json, io, urllib.request, urllib.error

BASE = "http://127.0.0.1:8765"
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

def req(path, method="GET", body=None, headers=None, raw=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}

# ---- 1. 上传 size 字段(图片) ----
png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000100"
                    "0521a10f0000000049454e44ae426082")
boundary = "----t"
mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\n"
      f"Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode()
st, d = req("/api/chat/upload", "POST", raw=mp,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
check("上传图片返回 size", st == 200 and d.get("size") == len(png), f"size={d.get('size')} 实际={len(png)}")
check("上传图片返回 data URL", d.get("type") == "image" and str(d.get("content","")).startswith("data:image/png;base64,"))

# ---- 2. 渠道开关:停用→转发不走该渠道→恢复 ----
st, cfg = req("/api/gateway/config")
chs = cfg["channels"]; keys = cfg["keys"]
mock_a = next((c for c in chs if "Mock" in c["name"]), None)
key_all = next((k for k in keys if not k.get("allowed_channels")), None)
assert mock_a and key_all, "前置:需要 Mock渠道A 与全量密钥"
st, d = req("/api/gateway/channels", "POST", body={**mock_a, "enabled": False})
check("停用渠道A", st == 200)
st, cfg2 = req("/api/gateway/config")
a2 = next(c for c in cfg2["channels"] if c["id"] == mock_a["id"])
check("渠道A enabled=False 已持久化", a2.get("enabled") is False)
# 渠道B还开着,chat 仍应成功(故障转移)
st, d = req("/v1/chat/completions", "POST",
            body={"model": "mock-chat-7b", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key_all['key']}"})
check("停用A后经渠道B仍可对话", st == 200 and d.get("choices"), f"status={st}")
# 恢复
st, d = req("/api/gateway/channels", "POST", body={**mock_a, "enabled": True})
st, cfg3 = req("/api/gateway/config")
a3 = next(c for c in cfg3["channels"] if c["id"] == mock_a["id"])
check("渠道A恢复启用", a3.get("enabled") is not False)

# ---- 3. 密钥开关:禁用→403→恢复 ----
st, d = req("/api/gateway/keys", "POST", body={**key_all, "enabled": False})
check("禁用全量密钥", st == 200)
st, d = req("/v1/chat/completions", "POST",
            body={"model": "mock-chat-7b", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key_all['key']}"})
check("禁用密钥调用被拒(403)", st == 403, f"status={st} detail={str(d.get('detail'))[:40]}")
st, d = req("/api/gateway/keys", "POST", body={**key_all, "enabled": True})
st, d = req("/v1/chat/completions", "POST",
            body={"model": "mock-chat-7b", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key_all['key']}"})
check("恢复启用后可调用", st == 200 and d.get("choices"))

# ---- 4. probe 延迟持久化 ----
st, d = req(f"/api/gateway/channels/{mock_a['id']}/probe", "POST")
check("probe 返回延迟", st == 200 and d.get("latency_ms", 0) > 0, f"ms={d.get('latency_ms')}")
st, cfg4 = req("/api/gateway/config")
a4 = next(c for c in cfg4["channels"] if c["id"] == mock_a["id"])
check("probe 延迟已持久化到渠道", isinstance(a4.get("last_latency_ms"), (int, float)) and a4["last_latency_ms"] > 0
      and a4.get("last_probe_at"), f"last={a4.get('last_latency_ms')}ms at={a4.get('last_probe_at')}")

print(f"\n==== 结果: 通过 {ok} / 失败 {fail} ====")
