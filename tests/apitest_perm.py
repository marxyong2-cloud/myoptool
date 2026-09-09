# -*- coding: utf-8 -*-
"""API 级权限矩阵测试:super / admin / sub / viewer + 停用 + 升降级 + 共享网关。"""
import json, urllib.request, urllib.error, http.cookiejar

BASE = "http://127.0.0.1:8765"

def client():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def call(op, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        r = op.open(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS" if cond else "FAIL"), name, extra if not cond else "")

super_ = client()
s, d = call(super_, "/api/auth/login", {"username": "admin", "password": "admin123"})
check("super 登录", s == 200 and d.get("user", {}).get("role") == "super", str(d)[:120])

# 清理旧测试账号
s, d = call(super_, "/api/users")
for u in d.get("users", []):
    if u["username"].startswith("_t_"):
        call(super_, "/api/users/" + u["username"], method="DELETE")

# 1. super 创建 viewer / sub / admin
s, d = call(super_, "/api/users", {"username": "_t_viewer", "password": "test123456", "role": "viewer"})
check("super 创建预览用户", s == 200, str(d)[:150])
s, d = call(super_, "/api/users", {"username": "_t_sub", "password": "test123456", "role": "sub", "perms": ["modelstart"], "hosts": []})
check("super 创建子账号(仅 modelstart)", s == 200, str(d)[:150])
s, d = call(super_, "/api/users", {"username": "_t_admin", "password": "test123456", "role": "admin"})
check("super 创建管理员", s == 200, str(d)[:150])

# 2. 普通管理员:共享网关可见 + 可建子账号 + 不可建管理员/不可改管理员
admin_ = client()
call(admin_, "/api/auth/login", {"username": "_t_admin", "password": "test123456"})
s, d = call(admin_, "/api/gateway/config")
check("普通管理员看到共享网关渠道", s == 200 and len(d.get("channels", [])) == 4, str(d)[:100])
s, d = call(admin_, "/api/users", {"username": "_t_sub2", "password": "test123456", "role": "sub", "perms": ["bench"]})
check("普通管理员可创建子账号", s == 200, str(d)[:150])
s, d = call(admin_, "/api/users", {"username": "_t_admin2", "password": "test123456", "role": "admin"})
check("普通管理员不可创建管理员(403)", s == 403, str(d)[:100])
s, d = call(admin_, "/api/users/chenxiangqian", {"password": "xxxYYY999"})
check("普通管理员不可改其他管理员(403)", s == 403, str(d)[:100])
s, d = call(admin_, "/api/users/admin", {"password": "xxxYYY999"})
check("普通管理员不可改 admin 账号(403)", s == 403, str(d)[:100])
s, d = call(admin_, "/api/users/_t_viewer", {"status": "suspended"})
check("普通管理员可停用预览用户", s == 200 and d.get("user", {}).get("status") == "suspended", str(d)[:150])
s, d = call(admin_, "/api/users/_t_viewer", {"status": "active"})
check("普通管理员可启用预览用户", s == 200 and d.get("user", {}).get("status") == "active", str(d)[:150])

# 3. 预览用户:登录/只读/对话可用/网关不可见
viewer_ = client()
s, d = call(viewer_, "/api/auth/login", {"username": "_t_viewer", "password": "test123456"})
check("预览用户可登录", s == 200 and d.get("user", {}).get("role") == "viewer", str(d)[:120])
s, d = call(viewer_, "/api/gateway/config")
check("预览用户网关不可见(403)", s == 403, str(d)[:80])
s, d = call(viewer_, "/api/tasks", method="GET")
check("预览用户 GET 可查看", s == 200, str(d)[:80])
s, d = call(viewer_, "/api/tasks", {"api_url": "http://x", "model": "m"})
check("预览用户创建压测任务被拦(403)", s == 403, str(d)[:100])
s, d = call(viewer_, "/api/modelstart/hosts", method="GET")
check("预览用户主机列表为空", s == 200 and d.get("hosts") == [], str(d)[:100])
s, d = call(viewer_, "/api/viewer/chat-cfg")
check("预览用户对话配置(共享网关渠道)", s == 200 and d.get("configured") and len(d.get("models", [])) > 0, str(d)[:150])
s, d = call(viewer_, "/api/chat/sessions", method="GET")
check("预览用户可看会话列表", s == 200, str(d)[:80])
s, d = call(viewer_, "/api/chat/sessions", {"title": "", "archived": False, "messages": []})
check("预览用户可建对话会话", s == 200, str(d)[:100])
s, d = call(viewer_, "/api/chat", {"api_url": "(viewer-gateway)", "api_key": "", "model": "x", "task_type": "image", "message": "hi"})
check("预览用户文生图被拦(403)", s == 403, str(d)[:100])
s, d = call(viewer_, "/api/modeluse/skills", {"name": "hax", "content": "x"})
check("预览用户改技能库被拦(403)", s == 403, str(d)[:100])

# 4. 升降级 + 停用
s, d = call(super_, "/api/users/_t_sub", {"role": "viewer"})
check("super 子账号降级为预览", s == 200 and d.get("user", {}).get("role") == "viewer", str(d)[:120])
s, d = call(super_, "/api/users/_t_sub", {"role": "sub"})
check("super 预览升级回子账号(模块保留)", s == 200 and d.get("user", {}).get("role") == "sub" and d["user"]["perms"] == ["modelstart"], str(d)[:150])
s, d = call(admin_, "/api/users/_t_sub", {"role": "admin"})
check("普通管理员不可把子账号升为管理员(403)", s == 403, str(d)[:100])
s, d = call(super_, "/api/users/_t_admin", {"role": "sub"})
check("super 管理员降级为子账号(保留全部模块)", s == 200 and len(d.get("user", {}).get("perms", [])) == 5, str(d)[:150])
s, d = call(super_, "/api/users/_t_admin", {"role": "admin"})
check("super 子账号升级回管理员", s == 200 and d.get("user", {}).get("role") == "admin", str(d)[:120])
s, d = call(super_, "/api/users/admin", {"role": "sub"})
check("admin 账号角色不可变更(403)", s == 403, str(d)[:100])
s, d = call(super_, "/api/users/admin", {"status": "suspended"})
check("admin 账号不可停用(400/403)", s in (400, 403), str(d)[:100])
s, d = call(super_, "/api/users/_t_admin", {"status": "suspended"})
check("super 可停用管理员账号", s == 200 and d.get("user", {}).get("status") == "suspended", str(d)[:120])
admin2_ = client()
s, d = call(admin2_, "/api/auth/login", {"username": "_t_admin", "password": "test123456"})
check("停用的管理员无法登录(403)", s == 403, str(d)[:100])
s, d = call(super_, "/api/users/_t_admin", {"status": "active"})
check("重新启用管理员", s == 200, str(d)[:100])
s, d = call(super_, "/api/users/admin", {"password": ""})
check("空密码不修改 admin", s == 200, str(d)[:80])

# 5. 子账号权限:未授权模块 403
sub_ = client()
call(sub_, "/api/auth/login", {"username": "_t_sub", "password": "test123456"})
s, d = call(sub_, "/api/modelstart/hosts", method="GET")
check("子账号(modelstart)可见主机接口", s == 200, str(d)[:80])
s, d = call(sub_, "/api/tasks", method="GET")
check("子账号无 bench 权限被拦(403)", s == 403, str(d)[:100])
s, d = call(sub_, "/api/gateway/config")
check("子账号无 gateway 权限被拦(403)", s == 403, str(d)[:100])

# 6. 子账号有 gateway 权限:可看不可改
s, d = call(super_, "/api/users/_t_sub2", {"perms": ["gateway"]})
check("super 调整子账号模块权限", s == 200, str(d)[:100])
sub2_ = client()
call(sub2_, "/api/auth/login", {"username": "_t_sub2", "password": "test123456"})
s, d = call(sub2_, "/api/gateway/config")
check("子账号(gateway)可查看共享网关", s == 200 and len(d.get("channels", [])) == 4, str(d)[:100])
s, d = call(sub2_, "/api/gateway/channels", {"name": "x", "base_url": "http://1.2.3.4"})
check("子账号(gateway)不可增改渠道(403)", s == 403, str(d)[:100])

# 7. 管理员网关写操作闭环(低优先级假渠道,增完即删)
s, d = call(super_, "/api/gateway/channels", {"name": "_t_ch", "base_url": "http://127.0.0.1:9/v1", "api_key": "", "models": ["m1"], "priority": -99})
check("管理员可添加渠道", s == 200, str(d)[:120])
cid = ""
s, d = call(super_, "/api/gateway/config")
for c in d.get("channels", []):
    if c["name"] == "_t_ch":
        cid = c["id"]
s, d = call(super_, "/api/gateway/channels/" + cid, method="DELETE")
check("管理员可删除渠道", s == 200 and cid, str(d)[:100])

# 清理测试账号
for name in ("_t_viewer", "_t_sub", "_t_sub2", "_t_admin"):
    call(super_, "/api/users/" + name, method="DELETE")
s, d = call(super_, "/api/users")
leftover = [u["username"] for u in d.get("users", []) if u["username"].startswith("_t_")]
check("测试账号已清理", not leftover, str(leftover))

print()
fails = [r for r in results if not r[1]]
print("=== %d/%d 通过 ===" % (len(results) - len(fails), len(results)))
for name, _, extra in fails:
    print("FAIL:", name, extra)
