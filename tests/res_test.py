# -*- coding: utf-8 -*-
"""分辨率真实生效端到端测试:所选分辨率必须到达上游请求体(v2 resolution / SGLang short_edge)"""
import json, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8765"
MOCK = "http://127.0.0.1:18999"
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 0)

def req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}

def last_req():
    with urllib.request.urlopen(MOCK + "/last_video_req", timeout=10) as r:
        return json.loads(r.read().decode())

def send_video(res, api="v2", dur=5, ratio="16:9", base=MOCK):
    st, d = req("/api/chat", "POST", {"api_url": base, "api_key": "mock-upstream-key", "protocol": "openai",
        "task_type": "video", "model": "MiniMax-H3", "message": "分辨率测试",
        "video_resolution": res, "video_api": api, "video_duration": dur, "video_ratio": ratio})
    return st, d, last_req()

# ---- v2 风格:resolution 原样到达上游 ----
st, d, lr = send_video("480P")
check("v2 480P 传到上游", st == 200 and lr["endpoint"] == "/v2/video_generation"
      and lr["payload"].get("resolution") == "480P", f"res={lr['payload'].get('resolution')}")
check("v2 duration 传到上游", lr["payload"].get("duration") == 5, f"dur={lr['payload'].get('duration')}")
check("v2 ratio 传到上游", lr["payload"].get("ratio") == "16:9", f"ratio={lr['payload'].get('ratio')}")

st, d, lr = send_video("2K")
check("v2 2K 传到上游", st == 200 and lr["payload"].get("resolution") == "2K", f"res={lr['payload'].get('resolution')}")

st, d, lr = send_video("480P", dur=4)
check("v2 时长下限 4s", lr["payload"].get("duration") == 4, f"dur={lr['payload'].get('duration')}")

# ---- SGLang /v1/videos 风格:auto(新缓存会探测)→ 短边像素映射 ----
st, d, lr = send_video("768P", api="auto", base="http://localhost:18999")
if lr["endpoint"] == "/v1/videos":
    check("SGLang 768P → short_edge=768", st == 200 and lr["payload"]["target"]["short_edge"] == 768,
          f"target={lr['payload'].get('target')}")
    check("SGLang ratio 传到上游", lr["payload"]["target"].get("aspect_ratio") == "16:9")
else:
    check("auto 探测为 v2(缓存) resolution=768P", st == 200 and lr["payload"].get("resolution") == "768P")

st, d, lr = send_video("480P", api="auto", base="http://localhost:18999")
if lr["endpoint"] == "/v1/videos":
    check("SGLang 480P → short_edge=480", st == 200 and lr["payload"]["target"]["short_edge"] == 480,
          f"target={lr['payload'].get('target')}")
else:
    check("v2 480P(缓存)", st == 200 and lr["payload"].get("resolution") == "480P")

st, d, lr = send_video("2K", api="auto", base="http://localhost:18999")
if lr["endpoint"] == "/v1/videos":
    check("SGLang 2K → short_edge=1152", st == 200 and lr["payload"]["target"]["short_edge"] == 1152,
          f"target={lr['payload'].get('target')}")
else:
    check("v2 2K(缓存)", st == 200 and lr["payload"].get("resolution") == "2K")

# adaptive 比例在 SGLang 分支应回退 16:9(v2 分支文生视频同样不允许 adaptive)
st, d, lr = send_video("768P", api="auto", ratio="adaptive", base="http://localhost:18999")
ar = lr["payload"].get("target", {}).get("aspect_ratio") if lr["endpoint"] == "/v1/videos" else lr["payload"].get("ratio")
check("adaptive 回退 16:9", ar == "16:9", f"实际={ar}")

print(f"\n==== 结果: 通过 {ok} / 失败 {fail} ====")
