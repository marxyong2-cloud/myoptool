# -*- coding: utf-8 -*-
"""detect 增强的离线验证:模拟 SSH 返回,校验解析 / run_cmd 重建 / Pod YAML。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 从 tests/ 目录运行时定位 server.py
import asyncio, io, json, sys
sys.stdout.reconfigure(encoding="utf-8")

import server

PS_OUT = "\n".join([
    json.dumps({"Command": "\"nginx -g 'daemon off;'\"", "CreatedAt": "2026-09-01 10:00:00 +0800 CST",
                "ID": "abc123def456", "Image": "nginx:1.25", "Names": "web", "Networks": "bridge",
                "Ports": "0.0.0.0:8080->80/tcp, :::8080->80/tcp", "RunningFor": "8 days ago",
                "State": "running", "Status": "Up 8 days (healthy)"}),
    json.dumps({"Command": "\"python3 train.py\"", "CreatedAt": "2026-09-08 09:00:00 +0800 CST",
                "ID": "fff222eee333", "Image": "pytorch/pytorch:2.3", "Names": "train", "Networks": "none",
                "Ports": "", "RunningFor": "1 day ago", "State": "exited", "Status": "Exited (0) 2 hours ago"}),
])

INSPECT_OUT = json.dumps([{
    "Id": "abc123def4567890abcdef0011223344556677",
    "Name": "/web",
    "State": {"Status": "running", "Running": True, "StartedAt": "2026-09-01T02:00:00Z",
              "FinishedAt": "0001-01-01T00:00:00Z", "ExitCode": 0, "Health": {"Status": "healthy"}},
    "Config": {"Image": "nginx:1.25",
               "Env": ["PATH=/usr/local/sbin:/usr/bin", "NGINX_VERSION=1.25.3", "HOSTNAME=abc123def456"],
               "Cmd": ["nginx", "-g", "daemon off;"], "Entrypoint": ["/docker-entrypoint.sh"],
               "WorkingDir": "/etc/nginx", "User": "", "Hostname": "abc123def456",
               "Labels": {"maintainer": "NGINX Docker Maintainers", "com.example": "x"}},
    "HostConfig": {
        "Binds": ["/data/web:/usr/share/nginx/html:ro", "webconf:/etc/nginx/conf.d"],
        "PortBindings": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
        "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "bridge", "Privileged": False,
        "ShmSize": 67108864, "Memory": 8589934592, "NanoCpus": 4000000000, "AutoRemove": False,
        "ExtraHosts": ["db:1.2.3.4"],
        "DeviceRequests": [{"Driver": "", "DeviceIDs": ["all"], "Capabilities": [["gpu"]]}]},
    "NetworkSettings": {"IPAddress": "172.17.0.2", "Networks": {"bridge": {"IPAddress": "172.17.0.2"}}},
    "Mounts": [
        {"Type": "bind", "Source": "/data/web", "Destination": "/usr/share/nginx/html", "RW": False},
        {"Type": "volume", "Name": "webconf", "Destination": "/etc/nginx/conf.d", "RW": True}],
}])

K8S_OUT = json.dumps({
    "apiVersion": "v1", "kind": "PodList", "items": [
        {"metadata": {"name": "vllm-0", "namespace": "llm", "creationTimestamp": "2026-09-08T01:00:00Z",
                      "labels": {"app": "vllm", "pod-template-hash": "abc123", "controller-uid": "x"},
                      "ownerReferences": [{"kind": "StatefulSet", "name": "vllm"}]},
         "spec": {"nodeName": "gpu-node1", "restartPolicy": "Always", "serviceAccountName": "default",
                  "containers": [{
                      "name": "vllm", "image": "vllm/vllm-openai:v0.6",
                      "command": ["python3", "-m", "vllm.entrypoints.openai.api_server"],
                      "args": ["--model", "Qwen/Qwen2.5-7B"],
                      "ports": [{"containerPort": 8000, "protocol": "TCP"}],
                      "env": [{"name": "MODEL", "value": "Qwen"},
                              {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "sec", "key": "tk"}}}],
                      "volumeMounts": [{"name": "cfg", "mountPath": "/etc/vllm", "readOnly": True},
                                       {"name": "shm", "mountPath": "/dev/shm"}],
                      "resources": {"requests": {"nvidia.com/gpu": "2", "cpu": "8"},
                                    "limits": {"nvidia.com/gpu": "2", "memory": "64Gi"}}}],
                  "initContainers": [{"name": "init", "image": "busybox"}],
                  "volumes": [{"name": "cfg", "configMap": {"name": "vllm-cfg"}},
                              {"name": "shm", "emptyDir": {"medium": "Memory"}}]},
         "status": {"phase": "Running", "podIP": "10.244.0.5", "hostIP": "192.168.1.10",
                    "startTime": "2026-09-08T01:00:05Z", "qosClass": "Burstable",
                    "containerStatuses": [{"name": "vllm", "ready": True, "restartCount": 1}]}},
        {"metadata": {"name": "standalone", "namespace": "default", "creationTimestamp": "2026-09-09T00:00:00Z"},
         "spec": {"nodeName": "node2", "restartPolicy": "Never",
                  "containers": [{"name": "c1", "image": "alpine", "command": ["sh", "-c"],
                                  "args": ["sleep 100"], "env": [{"name": "A", "value": "b"}]}]},
         "status": {"phase": "Succeeded", "podIP": "10.244.0.9", "hostIP": "192.168.1.11",
                    "containerStatuses": [{"name": "c1", "ready": False, "restartCount": 0}]}},
    ]})

def fake_run(h, cmd, timeout=30):
    if cmd.startswith("docker ps"):
        return {"out": PS_OUT, "err": "", "rc": 0}
    if cmd.startswith("docker inspect"):
        return {"out": INSPECT_OUT, "err": "", "rc": 0}
    if cmd.startswith("kubectl"):
        return {"out": K8S_OUT, "err": "", "rc": 0}
    return {"out": "", "err": "unknown", "rc": 1}

server._ms_host = lambda hid: {"id": hid, "host": "1.2.3.4", "ssh_port": 22, "username": "root"}
server._ms_run = fake_run

res = asyncio.run(server.ms_detect(server.MsDetectReq(host_id="t1")))

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)

# ---- Docker ----
cs = res["containers"]
check(len(cs) == 2, f"docker 2 个容器 (got {len(cs)})")
web = cs[0]
rc = web["run_cmd"]
print("\n--- web run_cmd ---\n" + rc + "\n")
for frag in ["docker run -d", "--name web", "--restart unless-stopped", "-p 8080:80/tcp",
             "-v /data/web:/usr/share/nginx/html:ro", "-v webconf:/etc/nginx/conf.d",
             "-e NGINX_VERSION=1.25.3", "--add-host db:1.2.3.4", "--entrypoint /docker-entrypoint.sh",
             "--memory 8g", "--cpus 4", "--gpus all", "nginx:1.25 nginx -g 'daemon off;'"]:
    check(frag in rc, f"run_cmd 含 {frag!r}")
check("PATH=" not in rc and "HOSTNAME=" not in rc, "run_cmd 过滤 PATH/HOSTNAME 噪声 env")
check(web["port_maps"] == ["8080->80/tcp"], f"port_maps {web['port_maps']}")
check(web["restart"] == "unless-stopped" and web["health"] == "healthy", "restart/health")
check(web["mem_limit"] == 8589934592 and web["cpus"] == 4.0, "mem/cpu 限制")
check(web["gpus"] == "all", "gpus")
check(any("usr/share/nginx/html" in m and "(ro)" in m for m in web["mounts"]), f"mounts {web['mounts']}")
check(web["networks"] == ["bridge"] and web["ip"] == "172.17.0.2", "networks/ip")
check(web["created_at"] == "2026-09-01 10:00:00 +0800 CST", "created_at")
check(web["command"].startswith('"nginx'), f"ps command 字段 {web['command']!r}")
train = cs[1]
check(train["state"] == "exited" and train["status"].startswith("Exited"), "无 inspect 条目的容器基础字段保留")

# ---- K8s ----
ps = res["pods"]
check(len(ps) == 2, f"k8s 2 个 pod (got {len(ps)})")
p0 = ps[0]
check(p0["namespace"] == "llm" and p0["name"] == "vllm-0", "ns/name")
check(p0["ready"] == "1/1" and p0["restarts"] == 1, f"ready/restarts {p0['ready']}/{p0['restarts']}")
check(p0["owner"] == "StatefulSet/vllm", "owner")
check(p0["pod_ip"] == "10.244.0.5" and p0["host_ip"] == "192.168.1.10", "pod/host ip")
check(p0["node"] == "gpu-node1" and p0["qos"] == "Burstable", "node/qos")
check(p0["images"] == ["vllm/vllm-openai:v0.6"], "images")
c0 = p0["containers"][0]
check(c0["ports"] == ["8000/TCP"], f"ports {c0['ports']}")
check("TOKEN=<secret sec/tk>" in c0["env"], f"env secret {c0['env']}")
check("nvidia.com/gpu=2" in c0["resources"] and "memory=64Gi" in c0["resources"], f"resources {c0['resources']}")
check("cfg:/etc/vllm (ro)" in c0["mounts"], f"mounts {c0['mounts']}")
check(p0["init_containers"] == [{"name": "init", "image": "busybox"}], "init containers")
check(p0["run_cmd"] == "", "有 owner 的 pod 不生成 kubectl run")
y = p0["create_yaml"]
print("\n--- vllm-0 create_yaml ---\n" + y + "\n")
for frag in ["kind: Pod", "name: vllm-0", "namespace: llm", "app: vllm",
             "image: vllm/vllm-openai:v0.6", "command: [python3, -m, vllm.entrypoints.openai.api_server]",
             "args: [--model, Qwen/Qwen2.5-7B]", "containerPort: 8000",
             "secretKeyRef", "name: sec", "key: tk", "mountPath: /etc/vllm", "readOnly: true",
             'nvidia.com/gpu: "2"', "memory: 64Gi", "configMap", "name: vllm-cfg", "emptyDir: {}",
             "initContainers", "image: busybox", "restartPolicy: Always"]:
    check(frag in y, f"yaml 含 {frag!r}")
check("pod-template-hash" not in y, "yaml 过滤 pod-template-hash 标签")
check("nodeName" not in y, "yaml 不含调度器字段 nodeName")

p1 = ps[1]
print("--- standalone run_cmd ---\n" + p1["run_cmd"] + "\n")
for frag in ["kubectl run standalone", "-n default", "--image=alpine", "--restart=Never",
             "--env=A=b", "-- sh -c 'sleep 100'"]:
    check(frag in p1["run_cmd"], f"kubectl run 含 {frag!r}")

# YAML 可解析性(有 pyyaml 就顺手验证)
try:
    import yaml
    doc = yaml.safe_load(p0["create_yaml"])
    check(doc["kind"] == "Pod" and doc["spec"]["containers"][0]["image"] == "vllm/vllm-openai:v0.6"
          and doc["spec"]["volumes"][1]["emptyDir"] == {}, "yaml 可被 PyYAML 解析")
except ImportError:
    print("SKIP PyYAML 未安装,跳过 yaml.safe_load 验证")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES"))
sys.exit(1 if fails else 0)
