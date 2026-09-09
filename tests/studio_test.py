# -*- coding: utf-8 -*-
"""动漫工坊 API 测试:片段库列表 + ffmpeg 剪辑合成(裁剪/归一化/淡入淡出/无音轨补静音) + 媒体删除"""
import json, os, urllib.request, urllib.error, subprocess

BASE = "http://127.0.0.1:8765"
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    ok, fail = ok + (1 if cond else 0), fail + (1 if not cond else 0)

def req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}

def ff_duration(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True)
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))

# 0. 测试夹具(两个异构测试段:A 640x360 带音轨 / B 854x480 无音轨);跑完在末尾删除,不污染片库
def ensure_fixture(name, args):
    p = "generated_media/" + name
    if not os.path.exists(p):
        r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args + [p],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("夹具生成失败:", r.stderr[:200])
    return os.path.exists(p)

FIXTURES = ["video_testseg_a.mp4", "video_testseg_b.mp4"]
fa = ensure_fixture(FIXTURES[0],
    ["-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=15",
     "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest"])
fb = ensure_fixture(FIXTURES[1],
    ["-f", "lavfi", "-i", "testsrc2=duration=5:size=854x480:rate=15",
     "-c:v", "libx264", "-preset", "ultrafast"])
check("测试夹具就绪", fa and fb)

# 1. 片段库列表
st, d = req("/api/media/list?kind=video")
names = [i["name"] for i in d.get("items", [])]
check("片段库列出视频", st == 200 and "video_testseg_a.mp4" in names and "video_testseg_b.mp4" in names,
      f"共 {len(names)} 个视频")
check("片段库不含图片/音频", all(i["kind"] == "video" for i in d.get("items", [])))
st, d2 = req("/api/media/list")
check("片段库 kind 过滤(全部含其它类型或为空)", st == 200 and len(d2.get("items", [])) >= len(names))

# 2. 合成:两段裁剪 + 淡入淡出 + 720P 归一化(第二段无音轨→补静音)
created = []
st, d = req("/api/studio/compose", "POST", body={
    "title": "测试动漫", "fade": True, "resolution": "720p",
    "clips": [
        {"name": "video_testseg_a.mp4", "start": 1.0, "end": 4.0},   # 3s
        {"name": "video_testseg_b.mp4", "start": 0.5, "end": 3.5},   # 3s
    ]})
check("合成成功", st == 200 and d.get("ok") and d.get("name", "").startswith("anime_"),
      f"name={d.get('name')} dur={d.get('duration')}s size={d.get('size')}")
if d.get("name"): created.append(d["name"])
out_path = "generated_media/" + d.get("name", "")
real_dur = ff_duration(out_path) if d.get("name") else 0
check("成片时长=裁剪和(6s±0.6)", abs(real_dur - 6.0) < 0.6, f"实际 {real_dur:.2f}s")

# 3. 单段合成
st, d = req("/api/studio/compose", "POST", body={
    "fade": False, "resolution": "480p",
    "clips": [{"name": "video_testseg_b.mp4", "start": 0, "end": 2.0}]})
check("单段合成", st == 200 and d.get("ok"), f"name={d.get('name')}")
if d.get("name"): created.append(d["name"])
p2 = "generated_media/" + d.get("name", "")
if d.get("name"):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", p2], capture_output=True, text=True)
    check("单段成片 480P 分辨率", "854x480" in r.stderr, "")
    check("单段成片时长 2s", abs(ff_duration(p2) - 2.0) < 0.5, f"{ff_duration(p2):.2f}s")

# 4. 错误分支
st, d = req("/api/studio/compose", "POST", body={"clips": []})
check("空时间线报 400", st == 400, f"status={st}")
st, d = req("/api/studio/compose", "POST", body={"clips": [{"name": "../server.py", "start": 0, "end": 1}]})
check("路径穿越被拒", st == 400, f"status={st}")
st, d = req("/api/studio/compose", "POST", body={"clips": [{"name": "video_testseg_a.mp4", "start": 5.0, "end": 5.05}]})
check("起止过短报 400", st == 400, f"status={st} {str(d)[:60]}")

# 5. 成片进入片段库
st, d = req("/api/media/list?kind=video")
anime_count = sum(1 for i in d.get("items", []) if i["name"].startswith("anime_"))
check("成片出现在片段库", anime_count >= 2, f"anime_* 共 {anime_count} 个")

# 6. 删除媒体文件(片段库「🗑」按钮的后端)
dummy = "video_deltest_probe.mp4"
with open("generated_media/" + dummy, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
st, d = req("/api/media/list?kind=video")
check("删除前:探针文件在列表中", any(i["name"] == dummy for i in d.get("items", [])))
st, d = req("/api/media/" + dummy, "DELETE")
check("删除接口成功", st == 200 and d.get("ok") and d.get("name") == dummy, str(d)[:60])
check("服务端文件已删除", not os.path.exists("generated_media/" + dummy))
st, d = req("/api/media/list?kind=video")
check("删除后不在列表中", all(i["name"] != dummy for i in d.get("items", [])))
st, d = req("/api/media/" + dummy, "DELETE")
check("重复删除报 404", st == 404, f"status={st}")
st, d = req("/api/media/..%5Cserver.py", "DELETE")
check("路径穿越被拒(400)", st == 400, f"status={st}")

# 7. 用删除接口清理本次测试产物与夹具(不碰用户文件)
for nm in created + FIXTURES:
    st, d = req("/api/media/" + nm, "DELETE")
    check(f"清理 {nm[:28]}…", st == 200 and not os.path.exists("generated_media/" + nm), f"status={st}")
st, d = req("/api/media/list?kind=video")
leftover = [i["name"] for i in d.get("items", []) if i["name"] in set(created + FIXTURES)]
check("测试产物已全部清理", not leftover, str(leftover))

print(f"\n==== 结果: 通过 {ok} / 失败 {fail} ====")
