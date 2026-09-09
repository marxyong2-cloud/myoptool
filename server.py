"""LLM API Benchmark Tool - Backend Server (local web version).

Features:
- Protocol auto-detection: OpenAI / Anthropic / Ollama
- Inference framework auto-detection: vLLM / SGLang / Ollama / TGI / LM Studio / ...
- Multi-strategy concurrent streaming benchmark with live SSE progress
- Excel report whose filename and header contain model name + framework
"""

import asyncio
import codecs
import collections
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from html import unescape as _html_unescape
from typing import Any, Dict, List, Optional
from urllib.parse import unquote as _url_unquote

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, model_validator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# ============================================================================
# 账户体系:super 总管理员 / admin 普通管理员 / sub 子账号
# - 每个账号拥有完全独立的数据空间(userdata/<用户名>/):压测报告、会话、渠道/密钥、
#   提示词/技能、部署站的预设/方案/运行历史/上传文件、以及各自的 config
# - SSH 主机是全局池(管理员维护);子账号只能看到并操作被分配的主机
# - GPU 监控等基础设施配置全局共享
# ============================================================================
import hashlib
import secrets
from contextvars import ContextVar

USERS_PATH = os.path.join(BASE_DIR, "users.json")
USER_DATA_DIR = os.path.join(BASE_DIR, "userdata")
_GLOBAL_CFG_KEYS = {"gpu_monitors", "gpu_ssh_monitors", "modelstart_hosts"}
# 账号角色层级(惯例五级):总管理员 > 管理员 > 子账号 > 预览用户;(待审批为自助注册的过渡状态)
#  - super  总管理员:仅内置 admin 账号;管理一切,含管理员账号的设立/降级/停用
#  - admin  管理员:全部功能模块,与总管理员共享网关渠道/密钥与 SSH 主机池;可管理子账号/预览用户
#  - sub    子账号:仅被授予的功能模块 + 被分配的主机,数据独立
#  - viewer 预览用户:全站只读 + 对话功能,无任何编辑/操作/主机权限
_ROLE_LABEL = {"super": "总管理员", "admin": "管理员", "sub": "子账号", "viewer": "预览用户"}
_ROLES = ("super", "admin", "sub", "viewer")
# 子账号功能模块权限(管理员 / 总管理员天然拥有全部)
_PERM_KEYS = ("bench", "modeluse", "modelstart", "ssh_hosts", "gateway")
_PERM_LABEL = {"bench": "压测工作台", "modeluse": "模型工作台", "modelstart": "模型部署站",
               "ssh_hosts": "部署站 SSH 主机分组管理", "gateway": "工作台网关渠道 / 密钥"}
_USERS_LOCK = threading.Lock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}          # token -> {user, ts}
_SESSIONS_LOCK = threading.Lock()
_USER_CTX: ContextVar[str] = ContextVar("_USER_CTX", default="")
_SESSION_TTL = 7 * 86400

def _pw_hash(salt: str, pw: str) -> str:
    return hashlib.sha256((salt + "\x1f" + pw).encode("utf-8")).hexdigest()

def _load_users() -> List[Dict[str, Any]]:
    try:
        with open(USERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("users") if isinstance(data, dict) else []
    except Exception:
        return []

def _save_users(users: List[Dict[str, Any]]) -> None:
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)

def _user_rec(name: str) -> Optional[Dict[str, Any]]:
    return next((u for u in _load_users() if u.get("username") == name), None)

def _init_users() -> None:
    """首次启动创建默认总管理员 admin / admin123(登录后请立即改密)。"""
    if _load_users():
        return
    salt = secrets.token_hex(8)
    _save_users([{"username": "admin", "salt": salt, "hash": _pw_hash(salt, "admin123"),
                  "role": "super", "hosts": [], "perms": list(_PERM_KEYS), "status": "active",
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}])
    print("[账户] 已创建默认总管理员 admin / admin123,请登录后立即修改密码")

def _cur_user() -> str:
    """当前请求所属账号(中间件注入);模块级启动代码默认落在 admin 空间。"""
    return _USER_CTX.get() or "admin"

def _cur_role() -> str:
    rec = _user_rec(_cur_user())
    return (rec or {}).get("role") or "sub"

def _is_admin() -> bool:
    return _cur_role() in ("super", "admin")

def _has_perm(mod: str) -> bool:
    """功能模块权限:管理员天然全有;子账号按被分配的 perms 判断。"""
    if _cur_role() in ("super", "admin"):
        return True
    return mod in ((_user_rec(_cur_user()) or {}).get("perms") or [])

def _clean_perms(perms: Optional[List[str]]) -> List[str]:
    return [p for p in (perms or []) if p in _PERM_KEYS]

def _udir(name: str = "") -> str:
    d = os.path.join(USER_DATA_DIR, name or _cur_user())
    os.makedirs(d, exist_ok=True)
    return d

def _user_public(u: Dict[str, Any]) -> Dict[str, Any]:
    role = u.get("role", "sub")
    if role in ("super", "admin"):
        perms = list(_PERM_KEYS)
    elif role == "viewer":
        perms = []                                  # 预览用户:无功能模块权限(仅全站只读 + 对话)
    else:
        perms = _clean_perms(u.get("perms"))
    return {"username": u.get("username"), "role": role,
            "role_label": _ROLE_LABEL.get(role, "子账号"),
            "hosts": u.get("hosts") or [] if role == "sub" else [], "created": u.get("created", ""),
            "perms": perms, "perm_labels": [_PERM_LABEL.get(p, p) for p in perms],
            "status": u.get("status") or "active"}

def _migrate_admin_data() -> None:
    """首次启用账户体系:把项目目录下的既有数据整体归入 admin 的数据空间。"""
    adir = os.path.join(USER_DATA_DIR, "admin")
    os.makedirs(adir, exist_ok=True)
    # 1) config:预设/方案/报告目录是 admin 的个人配置,从全局 config 中摘出
    ucfg_path = os.path.join(adir, "config.json")
    if not os.path.exists(ucfg_path):
        try:
            g = json.load(open(CONFIG_PATH, encoding="utf-8"))
        except Exception:
            g = {}
        ucfg = {k: g.pop(k) for k in ("modelstart_presets", "modelstart_plans", "reports_dir")
                if k in g}
        if ucfg:
            with open(ucfg_path, "w", encoding="utf-8") as f:
                json.dump(ucfg, f, ensure_ascii=False, indent=2)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=2)
    # 2) SQLite 会话/渠道/密钥/提示词库
    if not os.path.exists(os.path.join(adir, "modeluse.db")) and os.path.exists(DB_PATH_OLD):
        os.replace(DB_PATH_OLD, os.path.join(adir, "modeluse.db"))
        for suffix in ("-wal", "-shm"):
            src, dst = DB_PATH_OLD + suffix, os.path.join(adir, "modeluse.db" + suffix)
            if os.path.exists(src):
                try: os.replace(src, dst)
                except Exception: pass
    # 3) 部署站运行历史 / 预设上传文件 / 生成媒体
    if not os.path.exists(os.path.join(adir, "modelstart_runs.json")) and os.path.exists(os.path.join(BASE_DIR, "modelstart_runs.json")):
        os.replace(os.path.join(BASE_DIR, "modelstart_runs.json"), os.path.join(adir, "modelstart_runs.json"))
    old_files = os.path.join(BASE_DIR, "modelstart_files")
    if not os.path.exists(os.path.join(adir, "modelstart_files")) and os.path.isdir(old_files):
        os.replace(old_files, os.path.join(adir, "modelstart_files"))
    old_media = os.path.join(BASE_DIR, "generated_media")
    if not os.path.exists(os.path.join(adir, "generated_media")) and os.path.isdir(old_media):
        try:
            os.replace(old_media, os.path.join(adir, "generated_media"))
        except Exception:
            pass
    # 4) 压测报告目录(仅默认目录;自定义目录由 admin 自己在个人设置里维护)
    old_reports = os.path.join(BASE_DIR, "reports")
    new_reports = os.path.join(adir, "reports")
    if os.path.isdir(old_reports) and not os.path.isdir(new_reports):
        try:
            os.replace(old_reports, new_reports)
        except Exception:
            os.makedirs(new_reports, exist_ok=True)
            for fn in os.listdir(old_reports):
                if fn.endswith(".xlsx"):
                    try: os.replace(os.path.join(old_reports, fn), os.path.join(new_reports, fn))
                    except Exception: pass

_init_users()

def _load_global_cfg() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _load_config() -> Dict[str, Any]:
    """合并视图:全局基础设施键(监控/SSH 主机池)+ 当前账号的个人配置;
    子账号的主机池会被过滤为仅被分配的主机。"""
    g = _load_global_cfg()
    try:
        with open(os.path.join(_udir(), "config.json"), encoding="utf-8") as f:
            u = json.load(f)
    except Exception:
        u = {}
    cfg = dict(g)
    cfg.pop("modelstart_presets", None)
    cfg.pop("modelstart_plans", None)
    cfg.update(u)
    rec = _user_rec(_cur_user())
    if rec and rec.get("role") == "viewer":
        cfg["modelstart_hosts"] = []             # 预览用户:不分配任何主机(仅查看,无操作权限)
    # 有「SSH 主机分组管理」权限的子账号可管理主机池,需看到全量主机;其余子账号仅被分配的
    elif rec and rec.get("role") == "sub" and "ssh_hosts" not in (rec.get("perms") or []):
        allowed = set(rec.get("hosts") or [])
        cfg["modelstart_hosts"] = [h for h in (g.get("modelstart_hosts") or []) if h.get("id") in allowed]
    return cfg

def _save_config(cfg: Dict[str, Any]) -> None:
    """拆分保存:基础设施键写全局,其余写当前账号的个人配置。"""
    g = _load_global_cfg()
    for k in _GLOBAL_CFG_KEYS:
        if k in cfg:
            g[k] = cfg[k]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=2)
    u = {k: v for k, v in cfg.items() if k not in _GLOBAL_CFG_KEYS}
    with open(os.path.join(_udir(), "config.json"), "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False, indent=2)

def get_reports_dir() -> str:
    """报告存储目录(按账号隔离):优先个人 config 的自定义路径,默认 userdata/<账号>/reports。"""
    cfg = _load_config()
    d = (cfg.get("reports_dir") or "").strip()
    if d:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    d = os.path.join(_udir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d

# ============================================================================
# Data Models
# ============================================================================

class DetectRequest(BaseModel):
    api_url: str
    api_key: str = ""

class StrategyConfig(BaseModel):
    name: str
    concurrency: int = 10
    total_requests: int = 20
    input_tokens: int = 256
    max_output_tokens: int = 256
    lang: str = "en"  # en | zh
    cache_mode: str = "auto"       # auto=跟随全局随机/固定 | cold=冷缓存(唯一前缀) | hot=热缓存(固定同文)
    warmup_requests: int = 0       # 前 N 个请求仅预热,不计入统计

    @model_validator(mode="after")
    def _clamp(self):
        # 与 StrategyRunner 执行上限保持一致,避免 info() 显示的请求数与实际执行数不一致
        self.concurrency = max(1, min(256, self.concurrency))
        self.total_requests = max(1, min(2000, self.total_requests))
        self.warmup_requests = max(0, min(10, self.warmup_requests, self.total_requests - 1))
        if self.cache_mode not in ("auto", "cold", "hot"):
            self.cache_mode = "auto"
        return self

class SuiteRequest(BaseModel):
    api_url: str
    api_key: str = ""
    protocol: str = "openai"      # openai | anthropic | ollama
    model: str
    framework: str = "Unknown"    # filled from /api/detect result
    task_type: str = "chat"       # chat | image | video
    random_prompt: bool = True    # 随机 Prompt:API 生成主题,每个请求文本互不相同
    strategies: List[StrategyConfig]
    slo_ttft_ms: Optional[int] = None   # SLO 阈值:TTFT 上限(ms),空=不启用 Goodput 统计
    slo_tpot_ms: Optional[int] = None   # SLO 阈值:TPOT(逐token间隔)上限(ms)
    sampling: Dict[str, Any] = {}        # 采样参数: temperature / seed / top_p,空=引擎默认
    server_meta: Dict[str, Any] = {}     # /api/detect 抓到的引擎元数据(版本/TP/dtype 等)
    gpu_info: str = ""                   # 硬件环境说明(GPU 型号×数量等,手动填写)

class ProbeRequest(BaseModel):
    api_url: str
    api_key: str = ""
    protocol: str = "openai"
    model: str
    probe_id: str = ""            # 客户端生成的探测 id,用于探测中取消

class ProbeCancelRequest(BaseModel):
    probe_id: str = ""

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    api_url: str
    api_key: str = ""
    protocol: str = "openai"
    task_type: str = "chat"       # chat | image | video | image_video | video_regen | tts | stt
    video_duration: float = 5.0   # 视频时长(秒)
    video_resolution: str = "768P"  # MiniMax v2: 480P | 768P | 2K
    video_ratio: str = "16:9"     # MiniMax v2: adaptive|21:9|16:9|4:3|1:1|3:4|9:16
    video_api: str = "auto"       # auto | v1(旧云接口) | v2(MiniMax H3 v2)
    regen_task_id: str = ""       # 视频再生成:按源任务 id(v2)
    regen_source: str = ""        # 视频再生成:本地 /api/media/<name> 源视频(转 base_video)
    model: str
    message: str
    history: List[ChatMessage] = []
    enable_thinking: bool = True
    web_search: bool = False
    search_count: int = 0         # 联网搜索结果条数(0 = 用 config 默认 10)
    attachments: List[Dict[str, str]] = []   # [{name, type: text|image|video|audio, content}]
    system_prompt: str = ""       # 系统提示词(Prompt 工程)
    params: Dict[str, Any] = {}   # 采样参数: temperature / max_tokens / top_p / top_k / ...
    tts_voice: str = "alloy"      # 语音合成音色
    tts_format: str = "mp3"       # 语音合成格式 mp3/wav/opus/aac/flac

class ChatSessionIn(BaseModel):
    title: str = ""
    archived: bool = False
    messages: List[Dict[str, Any]] = []
    project: str = ""
    system_prompt: str = ""       # ModelUse 会话级系统提示词
    params: Dict[str, Any] = {}   # ModelUse 会话级采样参数
    data_source: str = ""         # ModelUse 会话数据来源: "custom" 或渠道 id
    model: str = ""               # ModelUse 会话记忆的模型名

# ============================================================================
# Protocol & Framework Auto-Detection
# ============================================================================

class ProtocolDetector:
    """Probe an endpoint: identify API protocol, serving framework, models."""

    @staticmethod
    def _normalize(url: str) -> str:
        url = url.strip()
        if not url:
            return url
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url.rstrip("/")

    @staticmethod
    async def _get(session, url, headers=None, timeout=6):
        try:
            async with session.get(
                url, headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=timeout), ssl=False,
            ) as r:
                return r.status, await r.text()
        except Exception:
            return None, ""

    @staticmethod
    async def _post(session, url, payload, headers=None, timeout=8):
        try:
            async with session.post(
                url, json=payload, headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=timeout), ssl=False,
            ) as r:
                return r.status, await r.text()
        except Exception:
            return None, ""

    @staticmethod
    def _json(text):
        try:
            return json.loads(text)
        except Exception:
            return None

    async def _detect_framework(self, session, base) -> Optional[str]:
        """Parallel probe of framework-specific endpoints."""
        paths = ["/api/version", "/get_server_info", "/version", "/info"]
        (s1, b1), (s2, b2), (s3, b3), (s4, b4) = await asyncio.gather(
            *[self._get(session, base + p, timeout=5) for p in paths]
        )
        d = self._json(b1)
        if s1 == 200 and isinstance(d, dict) and d.get("version"):
            return f"Ollama {d['version']}"
        d = self._json(b2)
        if s2 == 200 and isinstance(d, dict) and ("version" in d or "internal_states" in d):
            return f"SGLang {d.get('version', '')}".strip()
        d = self._json(b3)
        if s3 == 200 and isinstance(d, dict) and d.get("version"):
            return f"vLLM {d['version']}"
        d = self._json(b4)
        if s4 == 200 and isinstance(d, dict) and d.get("model_id"):
            return "TGI"
        return None

    async def detect(self, url: str, api_key: str = "") -> Dict[str, Any]:
        url = self._normalize(url)
        result = {
            "url": url, "protocol": "unknown", "framework": "Unknown",
            "models": [], "model_details": [], "server_meta": {},
            "capabilities": [],
        }
        if not url:
            return result
        base = re.sub(r"/v1/?$", "", url)

        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            host = url.lower()

            if "anthropic.com" in host:
                result["protocol"] = "anthropic"
                result["framework"] = "Anthropic"
                result["models"] = [
                    "claude-opus-4-20250514",
                    "claude-sonnet-4-20250514",
                    "claude-3-5-sonnet-20241022",
                    "claude-3-5-haiku-20241022",
                    "claude-3-opus-20240229",
                ]
                result["model_details"] = [{"id": m, "provider": "Anthropic"} for m in result["models"]]
                return result

            if "api.openai.com" in host:
                result["protocol"] = "openai"
                result["framework"] = "OpenAI"
                await self._fetch_openai_models(session, base, api_key, result)
                return result

            # 服务端元数据(SGLang /get_server_info、vLLM /version、Ollama /api/version)
            result["server_meta"] = await self._collect_server_meta(session, base)

            # Root probe -> Ollama
            status, body = await self._get(session, base)
            if status is not None and body and "ollama" in body.lower():
                result["protocol"] = "ollama"
                result["framework"] = await self._detect_framework(session, base) or "Ollama"
                status, body = await self._get(session, f"{base}/api/tags")
                data = self._json(body) if status == 200 else None
                if data:
                    models_raw = data.get("models", [])
                    result["models"] = [m.get("name", "") for m in models_raw if m.get("name")]
                    result["model_details"] = [
                        {
                            "id": m.get("name"),
                            "size_GB": round(m.get("size", 0) / 1e9, 2),
                            "quantization": (m.get("details") or {}).get("quantization_level"),
                            "family": (m.get("details") or {}).get("family"),
                            "params": (m.get("details") or {}).get("parameter_size"),
                            "modified": m.get("modified_at", "")[:19],
                        }
                        for m in models_raw if m.get("name")
                    ]
                return result

            # /v1/models probe -> OpenAI compatible
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            status, body = await self._get(session, f"{base}/v1/models", headers=headers)
            if status in (200, 401, 403):
                result["protocol"] = "openai"
                owners = set()
                data = self._json(body) if status == 200 else None
                if data:
                    models = data.get("data", []) if isinstance(data, dict) else []
                    if isinstance(models, list):
                        result["models"] = sorted(
                            {m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")}
                        )
                        owners = {
                            str(m.get("owned_by", "")).lower()
                            for m in models if isinstance(m, dict)
                        }
                        result["model_details"] = [
                            {k: v for k, v in m.items()
                             if isinstance(v, (str, int, float, bool)) and k != "object"}
                            for m in models if isinstance(m, dict) and m.get("id")
                        ]
                fw = await self._detect_framework(session, base)
                if fw:
                    result["framework"] = fw
                elif "vllm" in owners:
                    result["framework"] = "vLLM"
                elif "sglang" in owners:
                    result["framework"] = "SGLang"
                elif any("lm" in o or "studio" in o for o in owners):
                    result["framework"] = "LM Studio"
                else:
                    result["framework"] = "OpenAI-compatible"
                result["capabilities"] = await self._detect_capabilities(session, base, api_key, "openai")
                return result

            # Fallback
            result["protocol"] = "openai"
            result["framework"] = await self._detect_framework(session, base) or "Unknown"
            return result

    async def _detect_capabilities(self, session, base, api_key, protocol) -> List[str]:
        """Probe which generation types the endpoint supports: chat / image / video."""
        caps = []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        headers["Content-Type"] = "application/json"

        # chat: 已知支持(协议识别时已确认)
        if protocol in ("openai", "ollama", "anthropic"):
            caps.append("chat")

        if protocol == "openai":
            # 文生图: /v1/images/generations
            try:
                s, _ = await self._post(session, f"{base}/v1/images/generations",
                                        {"model": "__probe__", "prompt": "hi", "n": 1}, headers)
                if s is not None and s != 404:
                    caps.append("image")
            except Exception:
                pass
            # 文生视频: /v1/videos (SGLang MiniMax-H3) 或 /v1/video_generation (MiniMax 云)
            for vpath in ("/v1/videos", "/v1/video_generation"):
                try:
                    s, _ = await self._post(session, f"{base}{vpath}",
                                            {"model": "__probe__", "prompt": "hi"}, headers)
                    if s is not None and s != 404:
                        caps.append("video")
                        break
                except Exception:
                    pass
        return caps

    async def _collect_server_meta(self, session, base) -> Dict[str, Any]:
        """Collect whatever descriptive metadata the serving framework exposes."""
        meta: Dict[str, Any] = {}
        try:
            s, b = await self._get(session, f"{base}/get_server_info")
            d = self._json(b)
            if s == 200 and isinstance(d, dict):
                for k in ("model_path", "version", "max_total_num_tokens", "context_length",
                          "max_prefill_tokens", "max_running_requests", "attention_backend",
                          "mem_fraction_static", "tp_size", "dp_size", "dtype"):
                    if k in d and isinstance(d[k], (str, int, float, bool)):
                        meta[k] = d[k]
        except Exception:
            pass
        try:
            s, b = await self._get(session, f"{base}/version")
            d = self._json(b)
            if s == 200 and isinstance(d, dict) and d.get("version"):
                meta.setdefault("version", d["version"])
        except Exception:
            pass
        try:
            s, b = await self._get(session, f"{base}/api/version")
            d = self._json(b)
            if s == 200 and isinstance(d, dict) and d.get("version"):
                meta.setdefault("version", d["version"])
        except Exception:
            pass
        return meta

    async def _fetch_openai_models(self, session, base, api_key, result):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        status, body = await self._get(session, f"{base}/v1/models", headers=headers)
        data = self._json(body) if status == 200 else None
        if data:
            models = data.get("data", [])
            if isinstance(models, list):
                result["models"] = sorted(
                    {m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")}
                )

# ============================================================================
# Token Estimation & Prompt Generation
# ============================================================================

CHARS_PER_TOKEN_EN = 4
CHARS_PER_TOKEN_ZH = 1.8

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    ratio = CHARS_PER_TOKEN_ZH if chinese > len(text) * 0.3 else CHARS_PER_TOKEN_EN
    return max(1, int(len(text) / ratio))

EN_CORPUS = (
    "The quick brown fox jumps over the lazy dog. "
    "In the realm of artificial intelligence, large language models have demonstrated "
    "remarkable capabilities in understanding and generating human-like text across "
    "diverse domains and tasks. From creative writing to technical analysis, these "
    "systems leverage vast training corpora to produce coherent, contextually relevant "
    "outputs that continue to push the boundaries of what machines can accomplish. "
)

ZH_CORPUS = (
    "人工智能技术在近年来取得了突飞猛进的发展,大语言模型作为其中的重要突破,"
    "已经在自然语言处理、代码生成、知识问答等多个领域展现出强大的能力。"
    "随着模型规模的不断扩大和训练方法的持续优化,这些智能系统正在深刻改变"
    "人们与数字技术交互的方式,为各行各业带来前所未有的变革与机遇。"
)

def generate_prompt(target_tokens: int, lang: str = "en") -> str:
    corpus = ZH_CORPUS if lang == "zh" else EN_CORPUS
    ratio = CHARS_PER_TOKEN_ZH if lang == "zh" else CHARS_PER_TOKEN_EN
    target_chars = int(target_tokens * ratio)
    repeats = max(1, target_chars // len(corpus) + 1)
    return (corpus * repeats)[:target_chars]

# ---- 随机 Prompt 池(每个请求文本互不相同) ----
SENT_BANK_ZH = [
    "云计算平台的弹性伸缩机制可以根据负载自动调整计算资源。",
    "高速铁路的轨道检测系统利用传感器数据预防潜在故障。",
    "深海探测机器人需要承受极高的水压并保持通信稳定。",
    "现代城市的交通信号系统通过实时数据优化路口通行效率。",
    "基因测序技术的进步使得个性化医疗成为可能。",
    "可再生能源发电并网需要解决功率波动与储能配合问题。",
    "区块链技术在供应链溯源中保证了数据的不可篡改性。",
    "自动驾驶系统依赖多传感器融合进行环境感知与决策。",
    "工业生产线的视觉质检系统能够识别微米级缺陷。",
    "天文观测站通过射电望远镜阵列捕捉宇宙深处的信号。",
    "农业无人机可以完成播种、施肥与病虫害监测任务。",
    "金融风控系统利用图计算识别复杂的关联欺诈行为。",
    "药物研发流程中分子模拟显著缩短了筛选周期。",
    "智慧物流仓库的调度算法将拣货路径压缩到最短。",
    "卫星遥感数据帮助气象部门提升台风路径预测精度。",
    "教育平台的个性化推荐根据学习行为调整内容难度。",
    "海洋漂浮垃圾清理装置结合洋流模型规划作业路线。",
    "核电站的安全监控系统采用多重冗余设计防止误操作。",
]
SENT_BANK_EN = [
    "The elastic scaling mechanism of cloud platforms adjusts compute resources with load.",
    "High-speed rail track inspection systems use sensor data to prevent failures.",
    "Deep-sea exploration robots must endure extreme pressure and keep communications stable.",
    "Urban traffic signal systems optimize intersection throughput using real-time data.",
    "Advances in genome sequencing make personalized medicine increasingly practical.",
    "Integrating renewable generation requires solving power fluctuation and storage coordination.",
    "Blockchain technology guarantees tamper-resistant records in supply chain tracing.",
    "Autonomous driving stacks rely on multi-sensor fusion for perception and planning.",
    "Industrial vision inspection lines detect defects at the micrometer scale.",
    "Radio telescope arrays capture faint signals from the deep universe.",
    "Agricultural drones handle seeding, fertilizing, and pest monitoring missions.",
    "Graph computing in financial risk control uncovers complex fraud networks.",
    "Molecular simulation shortens the screening cycle in drug discovery pipelines.",
    "Smart warehouse scheduling algorithms compress picking routes to the minimum.",
    "Satellite remote sensing helps meteorologists refine typhoon track forecasts.",
    "Adaptive learning platforms tune content difficulty from behavioral signals.",
    "Ocean cleanup devices plan routes using gyre and current models.",
    "Redundant safety monitoring protects nuclear plants from operator error.",
]
DEFAULT_TOPICS_ZH = ["智慧城市", "航天工程", "新能源", "生物医药", "金融科技", "文化传播",
                     "生态保护", "先进制造", "体育科学", "食品工业", "量子计算", "海洋经济"]
DEFAULT_TOPICS_EN = ["smart cities", "aerospace engineering", "renewable energy", "biotech",
                     "fintech", "media culture", "ecology", "advanced manufacturing",
                     "sports science", "food industry", "quantum computing", "blue economy"]

class PromptBank:
    """随机 prompt 池:API 生成的随机主题 + 句子库随机组合,
    保证测试期间每个请求的文本内容互不相同。"""

    def __init__(self, topics: List[str], lang: str = "zh"):
        self.topics = [t for t in topics if t and t.strip()][:64] or (
            DEFAULT_TOPICS_ZH if lang == "zh" else DEFAULT_TOPICS_EN)
        self.lang = lang
        self._counter = 0

    def build(self, target_tokens: int) -> str:
        import random as _random
        rng = _random.Random()
        topic = self.topics[self._counter % len(self.topics)]
        self._counter += 1
        bank = SENT_BANK_ZH if self.lang == "zh" else SENT_BANK_EN
        ratio = CHARS_PER_TOKEN_ZH if self.lang == "zh" else CHARS_PER_TOKEN_EN
        target_chars = max(40, int(target_tokens * ratio))
        sents = list(bank)
        rng.shuffle(sents)
        rnd = lambda: rng.randint(3, 9999)
        if self.lang == "zh":
            head = f"以下是关于「{topic}」的资料汇总(编号{rng.randint(1000, 9999)}):"
        else:
            head = f"Summary of materials on {topic} (ref {rng.randint(1000, 9999)}):"
        parts = [head]
        used = 0
        i = 0
        while used < target_chars:
            s = sents[i % len(sents)]
            # 随机插入数字/参数,使同一句子在不同请求中亦有差异
            if rng.random() < 0.4:
                s = s.rstrip("。.") + (f"(样本{rnd()}号,置信度{rng.randint(50, 99)}%)" if self.lang == "zh"
                                       else f" (sample {rnd()}, confidence {rng.randint(50, 99)}%)") + ("。" if self.lang == "zh" else ".")
            parts.append(s)
            used += len(s)
            i += 1
        text = "\n".join(parts)
        return text[:target_chars] if len(text) > target_chars else text

async def gen_random_topics(session, base: str, protocol: str, api_key: str,
                            model: str, count: int = 24, lang: str = "zh") -> List[str]:
    """调用被测模型 API 生成一批互不相关的随机主题(一次调用,失败回退默认主题)。"""
    if lang == "zh":
        instruction = (f"请输出 {count} 个互不相关的随机主题短语,每行一个,"
                       "涵盖科技、历史、自然、商业、文化、生活等不同领域。"
                       "不要编号、不要解释、不要引号,直接输出主题行。")
    else:
        instruction = (f"Output {count} unrelated random topic phrases, one per line, "
                       "covering technology, history, nature, business, culture, and daily life. "
                       "No numbering, no explanations, no quotes.")
    if protocol == "anthropic":
        endpoint, headers = f"{base}/v1/messages", {
            "Content-Type": "application/json", "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 400, "stream": True,
                   "thinking": {"type": "disabled"},
                   "messages": [{"role": "user", "content": instruction}]}
    elif protocol == "ollama":
        endpoint, headers = f"{base}/api/chat", {"Content-Type": "application/json"}
        payload = {"model": model, "stream": True, "think": False,
                   "messages": [{"role": "user", "content": instruction}],
                   "options": {"num_predict": 400}}
    else:
        endpoint, headers = f"{base}/v1/chat/completions", {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else ""}
        payload = {"model": model, "max_tokens": 400, "stream": True,
                   "messages": [{"role": "user", "content": instruction}],
                   "chat_template_kwargs": {"enable_thinking": False}}
    text = ""
    try:
        async with session.post(endpoint, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=40, sock_read=35),
                                ssl=False) as resp:
            if resp.status >= 400:
                return []
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buffer = ""
            async for raw in resp.content.iter_chunked(4096):
                buffer += decoder.decode(raw)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    data = None
                    if line.startswith("data:"):
                        ds = line[5:].strip()
                        if ds in ("[DONE]", "done"):
                            continue
                        data = ProtocolDetector._json(ds)
                    elif protocol == "ollama" and line.startswith("{"):
                        data = ProtocolDetector._json(line)
                    if not data:
                        continue
                    if protocol == "anthropic":
                        text += (data.get("delta") or {}).get("text", "")
                    elif protocol == "ollama":
                        text += (data.get("message") or {}).get("content", "")
                    else:
                        ch = data.get("choices") or []
                        if ch:
                            text += (ch[0].get("delta") or {}).get("content", "") or ""
    except Exception:
        return []
    topics = []
    for ln in text.splitlines():
        ln = re.sub(r"^[\s\d\.\-\*、·•]+", "", ln.strip()).strip()
        ln = ln.strip("\"'“”‘’ `").strip()
        if ln.endswith(":") or ln.endswith("："):
            continue
        low = ln.lower()
        if any(k in low for k in ("topic", "主题", "numbering", "phrase", "每行", "不要")):
            continue
        if 1 <= len(ln) <= 40:
            topics.append(ln)
    return topics[:count]

# ============================================================================
# Single Streaming Request
# ============================================================================

async def run_single_stream(
    session: aiohttp.ClientSession,
    protocol: str,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    request_id: int,
    sampling: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = url.rstrip("/")
    base = re.sub(r"/v1/?$", "", url)
    # 采样参数(temperature/seed/top_p):固定后不同轮次的解码路径才可比;空=引擎默认
    sp = {k: v for k, v in (sampling or {}).items()
          if k in ("temperature", "top_p", "seed") and v is not None and v != ""}

    if protocol == "anthropic":
        endpoint = f"{base}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": max_output_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        if "temperature" in sp:
            payload["temperature"] = sp["temperature"]
        if "top_p" in sp:
            payload["top_p"] = sp["top_p"]
    elif protocol == "ollama":
        endpoint = f"{base}/api/chat"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": model,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_predict": max_output_tokens, **sp},
        }
    else:  # openai
        endpoint = f"{base}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else "",
        }
        payload = {
            "model": model,
            "max_tokens": max_output_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        payload.update(sp)

    t_start = time.perf_counter()
    t_first_token = None
    output_text = ""
    token_count = 0
    token_times: List[float] = []
    error = None
    status_code = None

    try:
        async with session.post(
            endpoint, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=300, sock_read=120), ssl=False,
        ) as resp:
            status_code = resp.status
            if status_code >= 400:
                body = await resp.text()
                raise RuntimeError(f"HTTP {status_code}: {body[:300]}")

            ctype = resp.headers.get("Content-Type", "")

            if "text/event-stream" not in ctype and protocol != "ollama":
                # Server ignored stream=True and returned a full JSON body.
                body = await resp.text()
                data = json.loads(body)
                if protocol == "anthropic":
                    parts = [
                        b.get("text", "") for b in data.get("content", [])
                        if isinstance(b, dict)
                    ]
                    output_text = "".join(parts)
                else:
                    choices = data.get("choices") or []
                    if choices:
                        output_text = (choices[0].get("message") or {}).get("content", "") or ""
                token_count = estimate_tokens(output_text)
            else:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                buffer = ""
                async for raw_chunk in resp.content.iter_chunked(4096):
                    buffer += decoder.decode(raw_chunk)
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue

                        data = None
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str in ("[DONE]", "done"):
                                continue
                            data = ProtocolDetector._json(data_str)
                        elif protocol == "ollama" and line.startswith("{"):
                            data = ProtocolDetector._json(line)
                        if not data:
                            continue

                        piece = None
                        if protocol == "anthropic":
                            piece = (data.get("delta") or {}).get("text", "")
                        elif protocol == "ollama":
                            piece = (data.get("message") or {}).get("content", "")
                        else:
                            choices = data.get("choices") or []
                            if choices:
                                piece = (choices[0].get("delta") or {}).get("content", "")

                        if piece:
                            now = time.perf_counter()
                            if t_first_token is None:
                                t_first_token = now
                            output_text += piece
                            token_count += 1
                            token_times.append(now)
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"

    t_end = time.perf_counter()
    if token_count == 0 and output_text:
        token_count = estimate_tokens(output_text)

    if error is None and not output_text:
        error = "响应为空或未解析到流式内容,请检查协议类型与模型名是否正确"

    total_latency = t_end - t_start
    ttft = (t_first_token - t_start) if t_first_token else None

    avg_itl = 0.0
    if len(token_times) > 1 and t_first_token is not None:
        span = token_times[-1] - t_first_token
        avg_itl = span / (len(token_times) - 1)

    output_throughput = token_count / total_latency if total_latency > 0 else 0

    prefill_tps = None
    if ttft and ttft > 0:
        prefill_tps = round(estimate_tokens(prompt) / ttft, 2)
    decode_tps = None
    if ttft is not None and total_latency > ttft and token_count > 1:
        decode_tps = round((token_count - 1) / (total_latency - ttft), 2)

    return {
        "request_id": request_id,
        "success": error is None,
        "status_code": status_code,
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": token_count,
        "output_chars": len(output_text),
        "input_preview": prompt[:300],
        "output_text": output_text[:30000],
        "ttft_ms": round(ttft * 1000, 2) if ttft else None,
        "total_latency_s": round(total_latency, 3),
        "avg_itl_ms": round(avg_itl * 1000, 2),
        "output_tokens_per_s": round(output_throughput, 2),
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "error": error,
    }

# ============================================================================
# Image / Video Generation Executors
# ============================================================================

async def run_single_image(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    request_id: int,
    fetch_media: bool = False,
) -> Dict[str, Any]:
    """文生图: POST /v1/images/generations (OpenAI 风格,同步返回)。
    产物三种形式:http url(云)、b64_json(SGLang 默认)、file_path(服务器内部路径)。
    fetch_media=True 时(对话面板)自动取回到本地 generated_media/。"""
    base = re.sub(r"/v1/?$", "", url.rstrip("/"))
    endpoint = f"{base}/v1/images/generations"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}" if api_key else ""}
    payload = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}

    t_start = time.perf_counter()
    error = None
    status_code = None
    out_url = ""
    out_b64 = ""
    out_path = ""
    media = None
    try:
        async with session.post(endpoint, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=300), ssl=False) as resp:
            status_code = resp.status
            body = await resp.text()
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}: {body[:300]}")
            data = json.loads(body)
            items = data.get("data") or []
            if items:
                it = items[0]
                out_url = str(it.get("url") or "")
                out_b64 = str(it.get("b64_json") or "")
                out_path = str(it.get("file_path") or "")
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"

    output_text = out_url or (f"b64: {len(out_b64)} chars" if out_b64 else out_path)
    if fetch_media and error is None and (out_url or out_b64 or out_path):
        media = await fetch_generated_media(
            session, base, api_key, kind="image",
            url=out_url, b64=out_b64, file_path=out_path)

    total_latency = time.perf_counter() - t_start
    return {
        "request_id": request_id,
        "success": error is None,
        "status_code": status_code,
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": 1 if error is None else 0,   # 1 张图
        "output_chars": len(output_text),
        "input_preview": prompt[:300],
        "output_text": output_text[:30000],
        "media": media,           # 对话模式:本地取回结果 {name,size,source} / {error}
        "ttft_ms": None,          # 同步接口无 TTFT
        "total_latency_s": round(total_latency, 3),
        "avg_itl_ms": 0,
        "output_tokens_per_s": round(1 / total_latency, 4) if total_latency > 0 and error is None else 0,
        "prefill_tps": None,
        "decode_tps": None,
        "error": error,
    }

# 每个主机用哪种视频接口风格,探测一次后缓存:
#   "videos"(/v1/videos SGLang) | "v2"(MiniMax /v2/video_generation) | "generation"(MiniMax v1 云)
_VIDEO_STYLE: Dict[str, str] = {}

def _v2_content(prompt: str, frames: List[tuple], references: List[tuple],
                base_video: str = "") -> List[Dict[str, Any]]:
    """构造 MiniMax v2 content 多模态数组:text + 首尾帧/参考图/参考视频/参考音频 + 再生成源视频。"""
    content: List[Dict[str, Any]] = []
    if prompt:
        content.append({"type": "text", "text": prompt[:7000]})
    for role, m in frames:
        content.append({"type": "image_url", "image_url": {"url": m}, "role": role})
    for role, m in references:
        kind = {"reference_image": "image_url", "reference_video": "video_url",
                "reference_audio": "audio_url"}[role]
        content.append({"type": kind, kind: {"url": m}, "role": role})
    if base_video:
        content.append({"type": "video_url", "video_url": {"url": base_video},
                        "role": "base_video"})
    return content

async def run_single_video(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    request_id: int,
    poll_interval: float = 3.0,
    max_wait: float = 1800.0,
    fetch_media: bool = False,
    duration: float = 5.0,
    image: str = "",
    resolution: str = "",
    ratio: str = "",
    api_pref: str = "auto",
    frames: Optional[List[tuple]] = None,      # [(first_frame|last_frame, url/dataURI)]
    references: Optional[List[tuple]] = None,  # [(reference_image|reference_video|reference_audio, url)]
    regen_task_id: str = "",
    base_video: str = "",
) -> Dict[str, Any]:
    """视频生成,自动兼容三种接口风格(按 404 探测并缓存):
    A) videos:      SGLang/MiniMax-H3 POST /v1/videos -> id;GET /v1/videos/{id}
    B) v2:          MiniMax H3 V2     POST /v2/video_generation -> task_id;
                                       GET /v2/query/video_generation/{task_id} -> task.content.url
    C) generation:  MiniMax v1 云     POST /v1/video_generation;GET /v1/query/video_generation?task_id=
    图生视频:frames(首帧/尾帧,风格 A/B);参考生视频:references(风格 B);
    视频再生成(regen_task_id / base_video)走 POST /v2/video_regeneration。
    """
    base = re.sub(r"/v1/?$", "", url.rstrip("/"))
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}" if api_key else ""}
    if image and not frames:
        frames = [("first_frame", image)]
    frames = frames or []
    references = references or []

    t_start = time.perf_counter()
    t_submit = None
    error = None
    status_code = None
    out_url = ""
    job_id = None
    style_used = None

    # 再生成任务只有 v2 一种风格
    is_regen = bool(regen_task_id or base_video)
    cached = _VIDEO_STYLE.get(base)
    if is_regen:
        order = ["v2regen"]
    elif api_pref == "v2":
        order = ["v2"]
    elif api_pref == "v1":
        order = ["generation"]
    elif cached:
        order = [cached]
    elif "minimax" in base.lower():
        order = ["v2", "generation", "videos"]
    else:
        order = ["videos", "v2", "generation"]

    def build_payload(style: str) -> Dict[str, Any]:
        if style == "videos":          # A: SGLang /v1/videos
            dur = min(max(float(duration or 5.0), 1.0), 30.0)
            # 分辨率真实生效:档位 → 短边像素(2K 16:9 = 2048×1152,短边 1152)
            edge = {"480P": 480, "768P": 768, "2K": 1152}.get(
                (resolution or "768P").upper(), 768)
            ratio_v = ratio if (ratio and ratio != "adaptive") else "16:9"
            p = {
                "model": model,
                "prompt": prompt,
                "task": "t2va",
                "conditions": [],
                "target": {"short_edge": edge, "aspect_ratio": ratio_v,
                           "duration_seconds": dur},
                "seed": 1101 + request_id,
                "n": 1,
                "num_inference_steps": 50,
                "flow_shift": 12.0,
                "audio_flow_shift": 3.0,
            }
            if frames:                 # 图生视频:首/尾帧放 conditions,task 切 i2va
                p["task"] = "i2va"
                p["conditions"] = [{"type": "image_url", "image_url": {"url": m}}
                                   for _, m in frames]
            return p
        if style in ("v2", "v2regen"):
            res = (resolution or "768P").upper()
            if style == "v2regen":
                if regen_task_id:      # 按源任务再生成(768P 源 → 2K)
                    return {"model": model, "source_task_id": regen_task_id,
                            "resolution": res if res == "2K" else "2K"}
                return {"model": model, "content": _v2_content(prompt, frames, references,
                                                               base_video=base_video),
                        "resolution": "2K"}
            content = _v2_content(prompt, frames, references)
            has_media = bool(frames or references)
            r = ratio or ("adaptive" if has_media else "16:9")
            if not has_media and r == "adaptive":
                r = "16:9"             # 文生视频 ratio 必填且不能 adaptive
            p = {"model": model, "content": content,
                 "resolution": res,
                 "duration": int(min(max(float(duration or 5.0), 4.0), 15.0))}
            if ratio or has_media:
                p["ratio"] = r
            return p
        # C: MiniMax v1 云
        p = {"model": model, "prompt": prompt}
        if frames:
            p["first_frame_image"] = frames[0][1]
        return p

    ENDPOINTS = {
        "videos": "/v1/videos",
        "v2": "/v2/video_generation",
        "v2regen": "/v2/video_regeneration",
        "generation": "/v1/video_generation",
    }

    async def submit() -> tuple:
        """按顺序探测可用风格,返回 (style, job_id);404/405 换下一种,其他错误直接抛。"""
        nonlocal status_code
        tried = 0
        for st in order:
            ep = f"{base}{ENDPOINTS[st]}"
            payload = build_payload(st)
            try:
                async with session.post(ep, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=120),
                                        ssl=False) as resp:
                    status_code = resp.status
                    body = await resp.text()
                    if resp.status in (404, 405) and not is_regen and tried < len(order) - 1:
                        tried += 1
                        continue      # 换下一种风格
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
                    data = json.loads(body)
                    jid = (data.get("task_id") or data.get("id") or data.get("job_id")
                           or (data.get("data") or {}).get("id")
                           or (data.get("data") or {}).get("task_id"))
                    if not jid:
                        raise RuntimeError(f"响应中无任务 id: {body[:200]}")
                    cache_st = "v2" if st == "v2regen" else st
                    _VIDEO_STYLE[base] = cache_st
                    return st, str(jid)
            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError):
                raise                 # 连接级错误直接失败,不换风格
        raise RuntimeError("全部视频接口风格均不可用")

    def query_url(style: str, jid: str) -> str:
        if style == "videos":
            return f"{base}/v1/videos/{jid}"
        if style in ("v2", "v2regen"):
            return f"{base}/v2/query/video_generation/{jid}"
        return f"{base}/v1/query/video_generation?task_id={jid}"

    try:
        style_used, job_id = await submit()
        t_submit = time.perf_counter()

        deadline = time.perf_counter() + max_wait
        while time.perf_counter() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                async with session.get(query_url(style_used, job_id), headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30),
                                       ssl=False) as r:
                    if r.status >= 400:
                        continue
                    qdata = json.loads(await r.text())
            except Exception:
                continue
            task_obj = qdata.get("task") or {}
            status_val = str(qdata.get("status") or task_obj.get("status")
                             or (qdata.get("data") or {}).get("status") or "").lower()
            if status_val in ("success", "succeeded", "completed", "done"):
                if style_used in ("v2", "v2regen"):
                    out_url = str((task_obj.get("content") or {}).get("url")
                                  or task_obj.get("file_id") or "") or "completed"
                else:
                    d = qdata.get("data") or {}
                    # 优先取可直接访问的 http 链接(云存储/官方云返回);
                    # 否则保留服务器内部路径(file_path),由调用方经 content 端点或 SSH 取回
                    http_url = next((v for v in (
                        qdata.get("url"), qdata.get("video_url"), qdata.get("download_url"),
                        d.get("url"), d.get("video_url"), d.get("download_url"))
                        if isinstance(v, str) and v.startswith(("http://", "https://"))), None)
                    out_url = (http_url
                               or qdata.get("file_path")
                               or (qdata.get("file_paths") or [None])[0]
                               or d.get("file_path")
                               or qdata.get("video_url")
                               or d.get("video_url")
                               or str(d.get("file_id") or "")
                               or "completed")
                break
            if status_val in ("failed", "fail", "error", "cancelled"):
                err_obj = task_obj.get("error") if isinstance(task_obj.get("error"), dict) \
                    else task_obj.get("error")
                err_detail = (err_obj.get("message") if isinstance(err_obj, dict)
                              else err_obj) or qdata.get("error") \
                    or (qdata.get("data") or {}).get("error") or status_val
                raise RuntimeError(f"视频生成失败: {err_detail}")
        else:
            raise RuntimeError(f"视频生成超时(>{max_wait}s)")
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"

    # 对话模式:把产物取回到本地(b64/链接/content 端点/SSH 见 fetch_generated_media)
    media = None
    if fetch_media and error is None and out_url:
        is_http = str(out_url).startswith(("http://", "https://"))
        is_path = str(out_url).startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", str(out_url)))
        media = await fetch_generated_media(
            session, base, api_key, kind="video",
            url=str(out_url) if is_http else "",
            file_path=str(out_url) if is_path else "",
            file_id=str(out_url) if not (is_http or is_path) else "",
            job_id=job_id or "", style=style_used or "")

    total_latency = time.perf_counter() - t_start
    submit_ms = round((t_submit - t_start) * 1000, 2) if t_submit else None
    return {
        "request_id": request_id,
        "success": error is None,
        "status_code": status_code,
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": 1 if error is None else 0,   # 1 个视频
        "output_chars": len(str(out_url)),
        "input_preview": prompt[:300],
        "output_text": str(out_url)[:30000],
        "job_id": job_id,          # 供再生成(source_task_id)与 content 端点下载用
        "style": style_used,       # videos | v2 | v2regen | generation
        "media": media,            # 对话模式:本地取回结果 {name,size,source} / {error}
        "ttft_ms": submit_ms,      # 视频: TTFT = 任务提交耗时
        "total_latency_s": round(total_latency, 3),
        "avg_itl_ms": 0,
        "output_tokens_per_s": round(1 / total_latency, 4) if total_latency > 0 and error is None else 0,
        "prefill_tps": None,
        "decode_tps": None,
        "error": error,
    }

# ============================================================================
# Aggregation
# ============================================================================

def classify_error(e: Any) -> str:
    """把原始报错归类成可读、可操作的失败原因(与前端 classifyErr 保持一致)。"""
    s = str(e or "")
    if re.search(r"Cannot connect|Connection refused|拒绝网络连接|ClientConnectorError", s, re.I):
        return "连接被拒 — 服务未监听该端口,或地址/端口写错"
    if re.search(r"ConnectTimeout|ConnectionTimeout", s, re.I):
        return "连接超时 — 网络不通或被防火墙丢弃"
    if re.search(r"Timeout|timed?\s*out|超时", s, re.I):
        return "请求超时 — 服务无响应(负载过高/卡死)或响应过慢"
    if re.search(r"ServerDisconnected|ConnectionReset|RemoteDisconnected|连接被重置", s, re.I):
        return "连接被服务端断开 — 服务重启/崩溃或主动断连"
    if re.search(r"\b40[13]\b", s):
        return "认证失败(401/403) — API Key 缺失、无效或无权限"
    if re.search(r"\b404\b", s):
        return "接口不存在(404) — 路径或模型名写错"
    if re.search(r"\b400\b", s):
        return "请求被拒绝(400) — 参数或模型与服务端不匹配"
    if re.search(r"\b429\b", s):
        return "触发限流(429) — 服务端限速,并发过高"
    if re.search(r"\b5\d\d\b", s):
        return "服务端内部错误(5xx) — 被测服务异常,看服务端日志"
    return "其他错误" if s else "未知错误(无错误信息)"


# 错误标准分桶:汇总表按桶给出独立列,避免只看总错误率掩盖错误构成
def error_bucket(e: Any) -> str:
    """把报错归入标准桶:timeout / rate_limited / server_error / client_error /
    connection / empty_response / other(与 classify_error 同源判定,先连接后超时)。"""
    s = str(e or "")
    if re.search(r"Cannot connect|Connection refused|拒绝网络连接|ClientConnectorError"
                 r"|ConnectTimeout|ConnectionTimeout"
                 r"|ServerDisconnected|ConnectionReset|RemoteDisconnected|连接被重置", s, re.I):
        return "connection"
    if re.search(r"Timeout|timed?\s*out|超时", s, re.I):
        return "timeout"
    if re.search(r"\b429\b", s):
        return "rate_limited"
    if re.search(r"\b5\d\d\b", s):
        return "server_error"
    if re.search(r"\b4\d\d\b", s):
        return "client_error"
    if "响应为空" in s:
        return "empty_response"
    return "other"

# 汇总表错误桶展示顺序(xlsx / 前端一致)
ERROR_BUCKET_LABELS = [
    ("timeout", "超时"), ("rate_limited", "限流429"), ("server_error", "服务端5xx"),
    ("client_error", "客户端4xx"), ("connection", "连接异常"), ("empty_response", "空响应"),
    ("other", "其他"),
]

def aggregate_results(results: List[Dict[str, Any]], wall_clock_s: Optional[float] = None,
                      slo_ttft_ms: Optional[int] = None,
                      slo_tpot_ms: Optional[int] = None) -> Dict[str, Any]:
    # 预热请求只用于建连/填充缓存,不计入任何统计(避免首批请求冷启动拉偏分位数)
    warmup_count = sum(1 for r in results if r.get("warmup"))
    measured = [r for r in results if not r.get("warmup")]
    failed = sum(1 for r in measured if not r["success"])
    ok = [r for r in measured if r["success"]]

    # 失败原因分组:可读标签(前端「失败原因汇总」)+ 标准分桶(汇总表独立列)
    err_groups: Dict[str, int] = {}
    err_buckets: Dict[str, int] = {}
    for r in measured:
        if not r["success"]:
            reason = classify_error(r.get("error"))
            err_groups[reason] = err_groups.get(reason, 0) + 1
            b = error_bucket(r.get("error"))
            err_buckets[b] = err_buckets.get(b, 0) + 1

    def pct(arr, p):
        if not arr:
            return None
        k = (len(arr) - 1) * (p / 100)
        f, c = int(k), min(int(k) + 1, len(arr) - 1)
        return arr[f] + (arr[c] - arr[f]) * (k - f)

    def stats(arr, nd):
        if not arr:
            return {}
        return {
            "avg": round(sum(arr) / len(arr), nd),
            "p25": round(pct(arr, 25), nd),
            "p50": round(pct(arr, 50), nd),
            "p75": round(pct(arr, 75), nd),
            "p90": round(pct(arr, 90), nd),
            "p95": round(pct(arr, 95), nd),
            "p99": round(pct(arr, 99), nd),
            "min": round(min(arr), nd),
            "max": round(max(arr), nd),
        }

    def _slo_of(ok_list, denom_n, wall_s):
        """Goodput/SLO:成功 且 TTFT/TPOT 均在阈值内;全失败策略同样要出现在统计里(0 达标)。"""
        if not (slo_ttft_ms or slo_tpot_ms):
            return None

        def _tpot_ms(r):
            if r.get("avg_itl_ms") and r["avg_itl_ms"] > 0:
                return r["avg_itl_ms"]
            # 非流式兜底:(总时长 - TTFT) / (输出tokens - 1)
            if (r.get("ttft_ms") is not None and r.get("output_tokens", 0) > 1
                    and r["total_latency_s"] * 1000 > r["ttft_ms"]):
                return (r["total_latency_s"] * 1000 - r["ttft_ms"]) / (r["output_tokens"] - 1)
            return None

        slo_ok = [r for r in ok_list
                  if (not slo_ttft_ms or (r["ttft_ms"] is not None and r["ttft_ms"] <= slo_ttft_ms))
                  and (not slo_tpot_ms or (_tpot_ms(r) is None or _tpot_ms(r) <= slo_tpot_ms))]
        return {
            "ttft_ms": slo_ttft_ms,
            "tpot_ms": slo_tpot_ms,
            "ok_count": len(slo_ok),
            "rate": round(len(slo_ok) / denom_n, 4) if denom_n else 0,
            "goodput_rps": round(len(slo_ok) / wall_s, 3) if wall_s > 0 else 0,
        }

    if not ok:
        return {
            "success_count": 0,
            "failed_count": failed,
            "error_rate": 1.0,
            "error_groups": err_groups,
            "error_buckets": err_buckets,
            "warmup_count": warmup_count,
            "slo": _slo_of([], len(measured), 0.0),
            "ttft_ms": {}, "latency_s": {}, "itl_ms": {},
            "prefill_tps": {}, "decode_tps": {},
            "output_tokens_per_request": {},
            "throughput_per_request_tps": {},
            "rps": 0,
            "aggregate_input_tps": 0,
            "aggregate_throughput_tps": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "wall_clock_s": 0,
        }

    ttft = sorted(r["ttft_ms"] for r in ok if r["ttft_ms"] is not None)
    lat = sorted(r["total_latency_s"] for r in ok)
    tps = sorted(r["output_tokens_per_s"] for r in ok)
    out_toks = [r["output_tokens"] for r in ok]
    in_toks = [r["input_tokens"] for r in ok]
    itl = sorted(r["avg_itl_ms"] for r in ok if r["avg_itl_ms"] > 0)
    prefill = sorted(r["prefill_tps"] for r in ok if r.get("prefill_tps"))
    decode = sorted(r["decode_tps"] for r in ok if r.get("decode_tps"))

    # 真实墙钟:整轮首请求发出 → 最后一个请求结束。分批执行(请求数>并发)时
    # max(单请求延迟) 会低估墙钟、高估 RPS,优先用执行器实测值
    total_wall = wall_clock_s if (wall_clock_s and wall_clock_s > 0) else (max(lat) if lat else 0)
    total_out = sum(out_toks)
    total_in = sum(in_toks)

    # Goodput / SLO 达标率:成功 且 TTFT/TPOT 均在阈值内(失败请求计入分母,消除幸存者偏差)
    slo = _slo_of(ok, len(measured), total_wall)

    return {
        "success_count": len(ok),
        "failed_count": failed,
        "error_rate": round(failed / len(measured), 4) if measured else 0,
        "error_groups": err_groups,
        "error_buckets": err_buckets,
        "warmup_count": warmup_count,
        "ttft_ms": stats(ttft, 2),
        "latency_s": stats(lat, 3),
        "itl_ms": stats(itl, 2),
        "prefill_tps": stats(prefill, 2),
        "decode_tps": stats(decode, 2),
        "output_tokens_per_request": {
            "avg": round(sum(out_toks) / len(out_toks), 1),
            "min": min(out_toks),
            "max": max(out_toks),
        },
        "throughput_per_request_tps": stats(tps, 2),
        "rps": round(len(ok) / total_wall, 3) if total_wall > 0 else 0,
        "aggregate_input_tps": round(total_in / total_wall, 2) if total_wall > 0 else 0,
        "aggregate_throughput_tps": round(total_out / total_wall, 2) if total_wall > 0 else 0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "wall_clock_s": round(total_wall, 3),
        "slo": slo,
    }

# ============================================================================
# Analysis Report (rule-based comprehensive conclusions)
# ============================================================================

def build_analysis(req: "SuiteRequest", outputs: List[Dict[str, Any]]) -> List[List[str]]:
    """Return list of [检查项, 分析结论] rows based on all strategy summaries."""
    lines: List[List[str]] = []
    valid = [o for o in outputs if o["summary"].get("success_count", 0) > 0]
    # 统计口径:预热请求已从 summary 剔除,分母用「成功+失败」而不是策略配置的请求数
    total_ok = sum(o["summary"]["success_count"] for o in outputs)
    total_fail = sum(o["summary"]["failed_count"] for o in outputs)
    total_warm = sum(o["summary"].get("warmup_count", 0) for o in outputs)
    total_req = total_ok + total_fail

    # 1. 总体概况
    rate = round(total_ok / total_req * 100, 2) if total_req else 0
    warm_note = f",另有 {total_warm} 个预热请求未计入统计" if total_warm else ""
    lines.append(["总体概况",
        f"共执行 {len(outputs)} 个策略、{total_req} 个请求,成功 {total_ok}、失败 {total_fail},"
        f"整体成功率 {rate}%{warm_note}。模型 {req.model}({req.framework})。"])

    # 2.1 错误构成(标准分桶:超时/限流/5xx/连接等) — 提前计算,全失败场景同样需要
    err_comp_line: Optional[List[str]] = None
    if total_fail > 0:
        merged: Dict[str, int] = {}
        for o in outputs:
            for b, c in (o["summary"].get("error_buckets") or {}).items():
                merged[b] = merged.get(b, 0) + c
        if merged:
            label = dict(ERROR_BUCKET_LABELS)
            comp = "、".join(f"{label.get(b, b)} {c} 个" for b, c in
                             sorted(merged.items(), key=lambda kv: -kv[1]))
            top = max(merged, key=merged.get)
            hint = {
                "timeout": "以超时为主 — 负载超过服务承载或服务卡死,建议降低并发/检查 GPU 利用率",
                "rate_limited": "以限流为主 — 服务端限速配置过低,继续加并发无意义,先调大限流阈值",
                "server_error": "以 5xx 为主 — 被测服务内部异常,查看服务端日志定位",
                "connection": "以连接异常为主 — 服务重启/崩溃或网络不稳",
                "client_error": "以 4xx 为主 — 请求参数(模型名/长度上限)与服务端不匹配",
                "empty_response": "以空响应为主 — 协议类型或模型名可能选错",
                "other": "错误类型分散,看明细页逐条排查",
            }.get(top, "")
            err_comp_line = ["错误构成", f"失败请求分布:{comp}。{hint}。"]

    if not valid:
        if err_comp_line:
            lines.append(err_comp_line)
        lines.append(["综合结论", "所有策略均未成功,请检查 API 地址、Key、模型名与协议是否匹配。"])
        return lines

    # 2. 稳定性(错误率)
    worst = max(outputs, key=lambda o: o["summary"]["error_rate"])
    werr = worst["summary"]["error_rate"] * 100
    if werr >= 5:
        lines.append(["稳定性",
            f"「{worst['config']['name']}」错误率 {round(werr, 2)}%(并发 {worst['config']['concurrency']}),"
            f"超过 5% 阈值,高并发下服务出现明显失败,建议降低并发或检查服务日志/资源。"
            "该错误率下各延迟指标不具横向可比性(幸存者偏差)。"])
    elif werr > 0:
        lines.append(["稳定性",
            f"最高错误率 {round(werr, 2)}%(「{worst['config']['name']}」),偶发失败,整体可接受。"])
    else:
        lines.append(["稳定性", "全部策略零失败,服务稳定性良好。"])

    if err_comp_line:
        lines.append(err_comp_line)


    # 2.2 Goodput / SLO 达标率(设置阈值后的最终对比口径)
    slo_rows = [o for o in valid if (o["summary"].get("slo") or {}).get("ok_count") is not None]
    if slo_rows:
        best = max(slo_rows, key=lambda o: o["summary"]["slo"]["goodput_rps"] or 0)
        b = best["summary"]["slo"]
        cond = " 且 ".join(x for x in (
            f"TTFT≤{b['ttft_ms']}ms" if b.get("ttft_ms") else "",
            f"TPOT≤{b['tpot_ms']}ms" if b.get("tpot_ms") else "") if x)
        worst_slo = min(slo_rows, key=lambda o: o["summary"]["slo"]["rate"])
        lines.append(["Goodput/SLO",
            f"按 SLO({cond})统计:最优「{best['config']['name']}」Goodput "
            f"{b['goodput_rps']} req/s(达标率 {round(b['rate'] * 100, 1)}%);"
            f"最低达标率 {round(worst_slo['summary']['slo']['rate'] * 100, 1)}%"
            f"(「{worst_slo['config']['name']}」)。"
            "Goodput 兼顾吞吐与延迟质量,是横向对比的最终口径;"
            "若达标率随并发升高而骤降,说明高并发档位已在用延迟换吞吐。"])

    def g(o, *path):
        v = o["summary"]
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
            if v is None:
                return None
        return v

    # 3. TTFT 随并发扩展
    by_conc = sorted(valid, key=lambda o: o["config"]["concurrency"])
    if len(by_conc) >= 2:
        low, high = by_conc[0], by_conc[-1]
        t_low, t_high = g(low, "ttft_ms", "p50"), g(high, "ttft_ms", "p50")
        c_low, c_high = low["config"]["concurrency"], high["config"]["concurrency"]
        if t_low and t_high and c_high > c_low and t_low > 0:
            ratio = t_high / t_low
            cratio = c_high / c_low
            if ratio > 2:
                verdict = "排队效应显著,TTFT 恶化明显,建议按业务可接受延迟收敛并发上限"
            elif ratio > 1.3:
                verdict = "有一定排队增长,属正常范围"
            else:
                verdict = "增长平缓,服务在当前并发区间仍有承载余量"
            lines.append(["TTFT 扩展性",
                f"并发从 {c_low} 提升到 {c_high}({cratio:.1f} 倍),TTFT P50 从 {t_low}ms 增至 {t_high}ms"
                f"({ratio:.2f} 倍)。{verdict}。"])

            a_low = g(low, "aggregate_throughput_tps")
            a_high = g(high, "aggregate_throughput_tps")
            if a_low and a_high and a_low > 0:
                eff = (a_high / a_low) / cratio * 100
                note = ("接近线性扩展" if eff >= 80 else
                        "扩展效率中等,继续加并发收益递减" if eff >= 50 else
                        "扩展效率低,服务吞吐已接近饱和")
                lines.append(["吞吐扩展效率",
                    f"聚合吞吐从 {a_low} 提升到 {a_high} tokens/s,并发扩展效率约 {round(eff, 1)}%"
                    f"(100% 为理想线性),{note}。"])

    # 4. 长输入(prefill)影响
    short_in = [o for o in valid if o["config"]["input_tokens"] <= 512]
    long_in = [o for o in valid if o["config"]["input_tokens"] >= 1024]
    if short_in and long_in:
        s_ttft = [g(o, "ttft_ms", "p50") for o in short_in]
        l_ttft = [g(o, "ttft_ms", "p50") for o in long_in]
        s_ttft = [x for x in s_ttft if x]
        l_ttft = [x for x in l_ttft if x]
        if s_ttft and l_ttft:
            s_avg = sum(s_ttft) / len(s_ttft)
            l_avg = sum(l_ttft) / len(l_ttft)
            if s_avg > 0:
                r = l_avg / s_avg
                lines.append(["长输入(Prefill)影响",
                    f"短输入策略 TTFT P50 均值 {round(s_avg, 1)}ms,长输入策略 {round(l_avg, 1)}ms,"
                    f"为短输入的 {round(r, 2)} 倍。Prefill 成本随输入长度增长明显,"
                    f"长上下文场景建议关注 KV Cache 命中率与显存占用。"])

    # 5. 解码速度稳定性
    short_out = [o for o in valid if o["config"]["max_output_tokens"] <= 512]
    long_out = [o for o in valid if o["config"]["max_output_tokens"] > 512]
    if short_out and long_out:
        s_dec = [g(o, "decode_tps", "p50") for o in short_out]
        l_dec = [g(o, "decode_tps", "p50") for o in long_out]
        s_dec = [x for x in s_dec if x]
        l_dec = [x for x in l_dec if x]
        if s_dec and l_dec:
            s_avg = sum(s_dec) / len(s_dec)
            l_avg = sum(l_dec) / len(l_dec)
            diff = (l_avg - s_avg) / s_avg * 100 if s_avg else 0
            trend = "长输出场景解码速度下降,可能存在 KV Cache 换出或显存压力" if diff < -15 else \
                    "长输出场景解码速度反而更优(短请求调度开销占比高)" if diff > 15 else \
                    "解码速度在长/短输出场景下保持稳定"
            lines.append(["解码(Decode)速度",
                f"短输出策略解码 P50 均值 {round(s_avg, 1)} tok/s,长输出策略 {round(l_avg, 1)} tok/s,"
                f"差异 {round(diff, 1)}%。{trend}。"])

    # 6. 推荐
    best_tps = max(valid, key=lambda o: g(o, "aggregate_throughput_tps") or 0)
    best_ttft_o = min(valid, key=lambda o: g(o, "ttft_ms", "p50") or float("inf"))
    lines.append(["策略推荐",
        f"吞吐最优:「{best_tps['config']['name']}」聚合 {g(best_tps, 'aggregate_throughput_tps')} tokens/s;"
        f"延迟最优:「{best_ttft_o['config']['name']}」TTFT P50 {g(best_ttft_o, 'ttft_ms', 'p50')}ms。"
        f"高吞吐场景参考前者,交互式低延迟场景参考后者。"])

    # 7. 综合结论
    agg_all = [g(o, "aggregate_throughput_tps") or 0 for o in valid]
    peak = max(agg_all) if agg_all else 0
    lines.append(["综合结论",
        f"本次压测峰值聚合吞吐 {peak} tokens/s,整体成功率 {rate}%。"
        f"详细分位数据见「汇总」页,逐请求数据见各策略明细页。"])

    return lines

# ============================================================================
# AI-Powered Analysis (用被测模型分析压测数据)
# ============================================================================

_AI_ANALYSIS_PROMPT = """你是一位资深 LLM 推理性能分析专家。以下是对模型 {model}({framework})的一次压测汇总数据,请你撰写一份结构化的压测分析报告。

## 压测数据(JSON)

{data}

## 输出要求(严格遵守)

请用中文,严格按以下结构和标题输出,不要输出多余内容:

### 一、总体结论
2-3 句话概括本次压测的整体表现。

### 二、稳定性分析
分析成功率、错误率随并发的变化,指出服务在什么并发级别开始出现不稳定。

### 三、延迟分析
分析 TTFT、端到端延迟、ITL 随并发和输入输出长度的变化趋势,指出排队效应是否明显。

### 四、吞吐与扩展性
分析聚合吞吐、单请求 TPS 的扩展效率,评估并发提升的收益是否线性,指出吞吐拐点。

### 五、瓶颈定位
结合 Prefill/Decode 速度、长短输入输出对比,判断瓶颈在 prefill 还是 decode 阶段,是否可能受显存/KV Cache 限制。

### 六、优化建议
分点列出 3-5 条具体可执行的优化建议(部署参数、并发配置、适用场景等)。

数据字段说明: ttft=首token延迟(ms), latency=端到端延迟(s), itl=token间隔(ms), prefill_tps=输入速度, decode_tps=解码速度, agg_tps=聚合吞吐(tokens/s), rps=每秒请求数。"""

async def generate_ai_analysis(req: SuiteRequest, outputs: List[Dict[str, Any]]) -> Optional[str]:
    """把压测汇总数据发给被测模型,返回 AI 撰写的分析报告。失败返回 None。"""
    try:
        # 组装数据摘要(只保留关键指标,控制长度)
        data = []
        for o in outputs:
            c, s = o["config"], o["summary"]
            data.append({
                "策略": c["name"],
                "并发": c["concurrency"],
                "请求数": c["total_requests"],
                "输入tok": c["input_tokens"],
                "输出tok上限": c["max_output_tokens"],
                "成功率%": round((1 - s["error_rate"]) * 100, 2),
                "ttft_ms": s.get("ttft_ms", {}),
                "latency_s": s.get("latency_s", {}),
                "itl_ms": s.get("itl_ms", {}),
                "prefill_tps": s.get("prefill_tps", {}),
                "decode_tps": s.get("decode_tps", {}),
                "单请求tps": s.get("throughput_per_request_tps", {}),
                "agg_tps": s.get("aggregate_throughput_tps"),
                "rps": s.get("rps"),
                "goodput_rps": (s.get("slo") or {}).get("goodput_rps"),
                "slo达标率": (s.get("slo") or {}).get("rate"),
                "错误分桶": s.get("error_buckets") or {},
                "预热丢弃": s.get("warmup_count", 0),
            })
        prompt = _AI_ANALYSIS_PROMPT.format(
            model=req.model, framework=req.framework,
            data=json.dumps(data, ensure_ascii=False, indent=2),
        )

        base = re.sub(r"/v1/?$", "", req.api_url.rstrip("/"))
        if req.protocol == "anthropic":
            endpoint = f"{base}/v1/messages"
            headers = {"Content-Type": "application/json", "x-api-key": req.api_key or "",
                       "anthropic-version": "2023-06-01"}
            payload = {"model": req.model, "max_tokens": 4096, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}
        elif req.protocol == "ollama":
            endpoint = f"{base}/api/chat"
            headers = {"Content-Type": "application/json"}
            payload = {"model": req.model, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}
        else:
            endpoint = f"{base}/v1/chat/completions"
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {req.api_key}" if req.api_key else ""}
            payload = {"model": req.model, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}

        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=180), ssl=False) as resp:
                if resp.status >= 400:
                    return None
                data = json.loads(await resp.text())
                if req.protocol == "anthropic":
                    return "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict)).strip() or None
                if req.protocol == "ollama":
                    return ((data.get("message") or {}).get("content") or "").strip() or None
                ch = data.get("choices") or []
                if not ch:
                    return None
                return ((ch[0].get("message") or {}).get("content") or "").strip() or None
    except Exception:
        return None

# ============================================================================
# Strategy Runner (one strategy) + Suite Runner (sequential strategies)
# ============================================================================

def sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

class StrategyRunner:
    def __init__(self, req: SuiteRequest, cfg: StrategyConfig, task: "BenchTask" = None):
        self.req = req
        self.cfg = cfg
        self.task = task
        self.results: List[Dict[str, Any]] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done_count = 0
        self.ok_count = 0
        self.fail_count = 0
        self.wall_s = 0.0           # 整轮真实墙钟(首请求发出 → 最后请求结束)

    async def run(self, session: Optional[aiohttp.ClientSession] = None):
        """执行一个策略。可传入任务级共享会话(跨策略复用连接);不传则自建并自行关闭。"""
        cfg = self.cfg
        concurrency = max(1, min(256, cfg.concurrency))
        total = max(1, min(2000, cfg.total_requests))
        # 预热请求数:只执行不计入统计;至少保留 1 个统计请求
        warmup = max(0, min(int(getattr(cfg, "warmup_requests", 0) or 0), total - 1))
        cache_mode = getattr(cfg, "cache_mode", "auto") or "auto"
        prompt = generate_prompt(max(10, cfg.input_tokens), cfg.lang)
        bank = (self.task.prompt_banks.get(cfg.lang)
                if self.task is not None and self.req.task_type == "chat" else None)

        def next_prompt() -> str:
            """缓存模式:
            auto — 跟随全局设置(随机池:每请求文本互不相同 / 固定:同一段文本);
            cold — 冷缓存:每个请求注入唯一前缀,强制前缀缓存不命中(纯引擎 prefill 能力);
            hot  — 热缓存:所有请求同一文本,首请求后前缀缓存全命中(缓存收益上限)。"""
            if cache_mode == "hot":
                return prompt
            text = bank.build(max(10, cfg.input_tokens)) if bank is not None else prompt
            if cache_mode == "cold":
                nonce = uuid.uuid4().hex[:12]
                head = (f"[压测唯一前缀 {nonce},与之前所有请求不同]\n" if cfg.lang == "zh"
                        else f"[benchmark unique prefix {nonce}]\n")
                text = head + text
            return text

        timeout = aiohttp.ClientTimeout(total=600, sock_read=180)
        owns_session = session is None
        if session is None:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=concurrency, ssl=False), timeout=timeout)
        if self.task is not None:
            self.task.active_session = session      # 供「停止」时立即中断在途请求
        self.wall_s = 0.0

        try:
            sem = asyncio.Semaphore(concurrency)
            next_id = 0
            t_wall_start = time.perf_counter()

            async def worker():
                nonlocal next_id
                while True:
                    if self.task is not None:
                        # 暂停/取消闸门:暂停时在途请求完成后停下;取消直接退出
                        await self.task.pause_event.wait()
                        if self.task.cancelled:
                            return
                    async with sem:
                        rid = next_id
                        next_id += 1
                        if rid >= total:
                            return
                        req_prompt = next_prompt()
                        if self.req.task_type == "image":
                            result = await run_single_image(
                                session, self.req.api_url, self.req.api_key,
                                self.req.model, prompt, rid,
                            )
                        elif self.req.task_type == "video":
                            result = await run_single_video(
                                session, self.req.api_url, self.req.api_key,
                                self.req.model, prompt, rid,
                            )
                        else:
                            result = await run_single_stream(
                                session, self.req.protocol, self.req.api_url,
                                self.req.api_key, self.req.model, req_prompt,
                                max(1, cfg.max_output_tokens), rid,
                                sampling=self.req.sampling,
                            )
                        if self.task is not None and self.task.cancelled:
                            return             # 停止后到达的请求(连接被中止)不再计入统计
                        result["warmup"] = rid < warmup
                        self.results.append(result)
                        if result["success"]:
                            self.ok_count += 1
                        else:
                            self.fail_count += 1
                        self.done_count += 1
                        if self.task is not None:
                            self.task.live_done += 1   # 任务级实时进度(供 /api/tasks 轮询)
                        slim = {k: v for k, v in result.items()
                                if k not in ("input_preview", "output_text")}
                        await self.queue.put({
                            "type": "progress",
                            "completed": self.done_count,
                            "total": total,
                            "success": self.ok_count,
                            "failed": self.fail_count,
                            "last": slim,
                        })

            workers = [asyncio.create_task(worker()) for _ in range(concurrency)]

            while not all(w.done() for w in workers):
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                    yield item
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
            while not self.queue.empty():
                yield await self.queue.get()

            await asyncio.gather(*workers, return_exceptions=True)
            self.wall_s = time.perf_counter() - t_wall_start
        finally:
            if owns_session:
                await session.close()

# 连接类错误特征:全部失败请求都属于这些特征时,说明 API 端点不可达,继续跑没有意义
_CONN_REFUSED_MARKERS = (
    "Cannot connect", "ClientConnectorError", "Connection refused",
    "拒绝网络连接", "ConnectionResetError", "ServerDisconnectedError",
)
_CONN_TIMEOUT_MARKERS = ("TimeoutError", "ConnectTimeoutError", "ConnectionTimeoutError")


def _classify_conn_failure(results: List[Dict[str, Any]]) -> Optional[str]:
    """全部失败请求均为连接类错误时返回 'refused' / 'timeout',否则 None。"""
    errs = [str(r.get("error") or "") for r in results if not r.get("success")]
    if not errs:
        return None
    if all(any(m in e for m in _CONN_REFUSED_MARKERS) for e in errs):
        return "refused"
    if all(any(m in e for m in _CONN_REFUSED_MARKERS + _CONN_TIMEOUT_MARKERS)
           for e in errs):
        return "timeout"
    return None


class BenchTask:
    """One benchmark suite run as an independent background task."""

    def __init__(self, req: SuiteRequest):
        self.id = uuid.uuid4().hex[:8]
        self.req = req
        self.owner = _cur_user()       # 压测任务归属:按账号隔离
        host = re.sub(r"^https?://", "", req.api_url).rstrip("/")
        self.name = f"{req.model} @ {host}"
        self.strategies: List[StrategyConfig] = list(req.strategies)
        self.status = "pending"      # pending | running | paused | done | cancelled | error
        self.current_index = -1
        self.started_at = datetime.now()
        self.outputs: List[Dict[str, Any]] = []
        self.live_done = 0           # 已完成请求数(实时,跨策略累计)
        self.analysis: List[List[str]] = []
        self.ai_analysis: Optional[str] = None
        self.excel_file: Optional[str] = None
        self.error: Optional[str] = None

        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.cancelled = False
        self.prompt_banks: Dict[str, PromptBank] = {}   # lang -> 随机 prompt 池

        self._seq = 0
        self.events: List[Dict[str, Any]] = []
        self.listeners: List[asyncio.Queue] = []
        self.runner_task: Optional[asyncio.Task] = None
        # 当前进行中的请求会话(预热 / 策略执行):停止任务时立即关闭,中断全部在途请求
        self.active_session: Optional[aiohttp.ClientSession] = None
        self.deleted = False      # 已删除:后台协程收尾时不再写持久化记录

    @classmethod
    def from_record(cls, rec: Dict[str, Any]) -> "BenchTask":
        """从持久化记录重建任务(服务重启后,仍处于待启动状态的任务可继续启动)。"""
        req = SuiteRequest(
            api_url=rec.get("api_url", ""), api_key=rec.get("api_key", ""),
            protocol=rec.get("protocol", "openai"), model=rec.get("model", ""),
            framework=rec.get("framework", "Unknown"), task_type=rec.get("task_type", "chat"),
            random_prompt=bool(rec.get("random_prompt", True)),
            strategies=[StrategyConfig(**s) for s in rec.get("strategies", [])],
            slo_ttft_ms=rec.get("slo_ttft_ms"), slo_tpot_ms=rec.get("slo_tpot_ms"),
            sampling=rec.get("sampling") or {}, server_meta=rec.get("server_meta") or {},
            gpu_info=rec.get("gpu_info", ""),
        )
        t = cls(req)
        t.id = rec["id"]
        t.owner = rec.get("owner") or t.owner
        t.name = rec.get("name") or t.name
        return t

    def start(self) -> bool:
        if self.status != "pending":
            return False
        self.status = "running"
        self.started_at = datetime.now()
        self.runner_task = asyncio.create_task(self._run())
        self.emit({"type": "started"})
        return True

    # ---------- events ----------
    def emit(self, obj: Dict[str, Any]) -> None:
        self._seq += 1
        obj["_seq"] = self._seq
        self.events.append(obj)
        for q in list(self.listeners):
            q.put_nowait(obj)

    async def event_stream(self):
        """Replay buffered events (progress compressed), then stream live ones."""
        q: asyncio.Queue = asyncio.Queue()
        self.listeners.append(q)
        try:
            replay = list(self.events)
            last_prog: Dict[int, Dict[str, Any]] = {}
            for e in replay:
                if e.get("type") == "progress":
                    last_prog[e.get("strategy_index", -1)] = e
            for e in replay:
                if e.get("type") == "progress" and last_prog.get(e.get("strategy_index", -1)) is not e:
                    continue
                yield sse(e)
            n = len(replay)
            terminal = replay and replay[-1].get("type") in ("complete", "cancelled", "error")
            while not terminal:
                obj = await q.get()
                if obj["_seq"] <= n:
                    continue
                yield sse(obj)
                if obj["type"] in ("complete", "cancelled", "error"):
                    terminal = True
        finally:
            if q in self.listeners:
                self.listeners.remove(q)

    # ---------- info ----------
    def info(self) -> Dict[str, Any]:
        done_reqs = max(self.live_done, sum(
            o["summary"]["success_count"] + o["summary"]["failed_count"] for o in self.outputs
        ))
        return {
            "id": self.id,
            "name": self.name,
            "model": self.req.model,
            "api_url": self.req.api_url,
            "framework": self.req.framework,
            "protocol": self.req.protocol,
            "task_type": self.req.task_type,
            "status": self.status,
            "current_index": self.current_index,
            "strategy_count": len(self.strategies),
            "strategies": [s.model_dump() for s in self.strategies],
            "done_requests": done_reqs,
            "total_requests": sum(s.total_requests for s in self.strategies),
            "excel_file": self.excel_file,
            "error": self.error,
            "created_ts": self.started_at.timestamp(),
            "created_at": self.started_at.strftime("%m-%d %H:%M:%S"),
        }

    # ---------- controls ----------
    def pause(self) -> bool:
        if self.status != "running":
            return False
        self.status = "paused"
        self.pause_event.clear()
        self.emit({"type": "paused"})
        return True

    def resume(self) -> bool:
        if self.status != "paused":
            return False
        self.status = "running"
        self.pause_event.set()
        self.emit({"type": "resumed"})
        return True

    def cancel(self) -> bool:
        if self.status in ("done", "cancelled", "error"):
            return False
        self.cancelled = True
        self.pause_event.set()
        # 立即中断在途请求:直接关闭当前请求会话(连接全部中止),
        # 不必等每个在途请求自然结束(流式请求可能长达数分钟)
        sess = self.active_session
        if sess is not None and not sess.closed:
            try:
                asyncio.get_running_loop().create_task(sess.close())
            except RuntimeError:
                pass
        if self.status == "pending":
            self.status = "cancelled"
            self.emit({"type": "cancelled", "excel_file": None, "analysis": []})
        return True

    def update_remaining(self, new_strategies: List[StrategyConfig]) -> bool:
        """Replace strategies after the current one (only while paused)."""
        if self.status != "paused":
            return False
        keep = self.strategies[: self.current_index + 1]
        self.strategies = keep + list(new_strategies)
        self.emit({
            "type": "strategies_updated",
            "strategies": [s.model_dump() for s in self.strategies],
            "current_index": self.current_index,
        })
        return True

    def rename(self, name: str) -> None:
        self.name = name
        self.emit({"type": "renamed", "name": name})

    # ---------- main loop ----------
    async def _warmup(self) -> None:
        """启动前连接 / 服务预热(全部不计入统计):
        ① 先发 1 个长超时请求,等待推理框架完成冷启动(权重加载 / CUDA Graph,可能分钟级);
        ② 再并发发几个短请求,建立多路连接 —— 避免首批正式请求因建连 / 冷启动异常失败。"""
        prompt = generate_prompt(16, "zh")
        for label, timeout_s, batch in (("唤醒服务", 180, 1), ("并发建连", 45, 5)):
            if self.cancelled:
                return
            self.emit({"type": "status", "message": f"预热{label}中(不计入统计)…"})
            connector = aiohttp.TCPConnector(limit=max(2, batch), ssl=False)
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=timeout_s, sock_read=timeout_s))
            self.active_session = session
            ok = 0
            try:
                async def one() -> bool:
                    try:
                        r = await run_single_stream(
                            session, self.req.protocol, self.req.api_url,
                            self.req.api_key, self.req.model, prompt, 2, 0,
                            sampling=self.req.sampling)
                        return bool(r.get("success"))
                    except Exception:
                        return False
                ok = sum(await asyncio.gather(*[one() for _ in range(batch)]))
            finally:
                self.active_session = None
                await session.close()
            self.emit({"type": "status", "message": f"预热{label}完成:{ok}/{batch} 成功(不计入统计)"})

    async def _run(self):
        try:
            # ---------- 连接预热:首批请求常因建连 / 服务冷启动异常,先唤醒再正式压测 ----------
            if self.req.task_type == "chat":
                await self._warmup()
            # 随机 Prompt 模式:先用被测 API 生成随机主题,构建每个请求互不相同的文本池
            if self.req.random_prompt and self.req.task_type == "chat":
                base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(self.req.api_url))
                langs = sorted({s.lang for s in self.strategies} | {"zh"})
                self.emit({"type": "status", "message": "随机 Prompt 模式:正在通过 API 生成随机主题..."})
                async with aiohttp.ClientSession() as session:
                    for lang in langs:
                        topics = await gen_random_topics(
                            session, base, self.req.protocol, self.req.api_key,
                            self.req.model, count=24, lang=lang)
                        self.prompt_banks[lang] = PromptBank(topics, lang)
                        note = f"「{'默认主题' if not topics else 'API 随机主题'}」" \
                               f"{'(API 生成失败,已回退)' if not topics else ''}"
                        self.emit({"type": "status",
                                   "message": f"随机 Prompt 池就绪[{lang}]:{len(self.prompt_banks[lang].topics)} 个{note},"
                                              f"测试期间每个请求使用不同文本"})
            # 任务级共享会话:跨策略复用已建立的连接(预热 / 上一策略的连接直接带入下一策略),
            # 停止任务时统一关闭即可立即中断全部在途请求
            max_conc = max([s.concurrency for s in self.strategies] + [1])
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=max_conc + 8, ssl=False),
                timeout=aiohttp.ClientTimeout(total=600, sock_read=180))
            self.active_session = session
            try:
                while self.current_index + 1 < len(self.strategies):
                    await self.pause_event.wait()
                    if self.cancelled:
                        break
                    self.current_index += 1
                    cfg = self.strategies[self.current_index]
                    self.emit({
                        "type": "strategy_start",
                        "index": self.current_index,
                        "total": len(self.strategies),
                        "name": cfg.name,
                        "config": cfg.model_dump(),
                    })
                    runner = StrategyRunner(self.req, cfg, task=self)
                    async for evt in runner.run(session=session):
                        if evt.get("type") == "ping":
                            continue
                        evt["strategy_index"] = self.current_index
                        self.emit(evt)
                    summary = aggregate_results(
                        runner.results, wall_clock_s=runner.wall_s,
                        slo_ttft_ms=self.req.slo_ttft_ms, slo_tpot_ms=self.req.slo_tpot_ms)
                    self.outputs.append({
                        "config": cfg.model_dump(),
                        "summary": summary,
                        "results": runner.results,
                    })
                    self.emit({
                        "type": "strategy_complete",
                        "index": self.current_index,
                        "name": cfg.name,
                        "summary": summary,
                        "config": cfg.model_dump(),
                    })
                    _persist_task(self)     # 每个策略完成即留存(中断也能看到已完成部分)

                    # 连接失败快速止损:某策略 0 成功且错误全部为连接类时,端点不可达,
                    # 继续跑剩余策略只会重复失败——立即终止并给出可操作提示。
                    # refused(连接被拒)任意策略直接止损;timeout 仅首个策略止损,
                    # 以免服务过载导致的整体超时被误判为不可达。
                    fail_kind = (_classify_conn_failure(runner.results)
                                 if summary.get("failed_count") and not summary.get("success_count")
                                 else None)
                    if fail_kind == "refused" or (fail_kind == "timeout" and self.current_index == 0):
                        detail = ("连接被拒(TCP connection refused),服务未在该端口监听或地址/端口有误"
                                  if fail_kind == "refused"
                                  else "连接超时,网络不可达或被防火墙丢弃")
                        self.status = "error"
                        self.error = (
                            f"API 无法连接,已在第 {self.current_index + 1} 个策略后停止后续策略。"
                            f"{self.req.api_url} 的全部请求均为连接类错误:{detail}。"
                            "请检查:① API 地址与端口是否正确(可在服务器上执行 ss -tlnp 确认监听端口);"
                            "② 服务是否正在运行;③ 或先在「模型对话」页点「自动检测」验证连通性。"
                        )
                        self.emit({"type": "error", "message": self.error})
                        _persist_task(self)
                        return
            finally:
                self.active_session = None
                await session.close()

            if self.outputs:
                self.analysis = build_analysis(self.req, self.outputs)
                # AI 深度分析(用被测模型),失败则仅用规则报告;
                # 已停止的任务跳过 AI 分析 —— 否则停止后还要等模型响应,停止无法即时生效
                if not self.cancelled:
                    self.emit({"type": "status", "message": "正在用被测模型生成 AI 分析报告..."})
                    self.ai_analysis = await generate_ai_analysis(self.req, self.outputs)
                self.excel_file = write_suite_excel(
                    self.req, self.outputs, self.started_at, self.analysis, self.ai_analysis)

            if self.cancelled:
                self.status = "cancelled"
                self.emit({
                    "type": "cancelled",
                    "excel_file": self.excel_file,
                    "analysis": self.analysis,
                })
            else:
                self.status = "done"
                self.emit({
                    "type": "complete",
                    "excel_file": self.excel_file,
                    "model": self.req.model,
                    "framework": self.req.framework,
                    "analysis": self.analysis,
                    "ai_analysis": self.ai_analysis,
                    "strategies": [
                        {"name": o["config"]["name"], "summary": o["summary"]}
                        for o in self.outputs
                    ],
                })
            _persist_task(self)
        except Exception as e:
            self.status = "error"
            self.error = f"{type(e).__name__}: {str(e)[:300]}"
            self.emit({"type": "error", "message": self.error})
            _persist_task(self)

# ============================================================================
# Excel Report
# ============================================================================

_HEADER_FILL = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=14, color="1F4E78")
_KEY_FONT = Font(bold=True, color="1F4E78")
_OK_FILL = PatternFill("solid", fgColor="E2EFDA")
_WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
_BAD_FILL = PatternFill("solid", fgColor="FCE4D6")
_BORDER = Border(
    left=Side("thin"), right=Side("thin"),
    top=Side("thin"), bottom=Side("thin"),
)

def sanitize_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", (s or "").strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "na"

def sanitize_sheet_name(s: str) -> str:
    s = re.sub(r'[\\/?*\[\]:]', "", s or "")
    return s[:28] or "sheet"

def clean_text(t) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", t or "")

def write_suite_excel(req: SuiteRequest, outputs: List[Dict[str, Any]], started_at: datetime,
                      analysis: List[List[str]], ai_analysis: Optional[str] = None) -> str:
    wb = Workbook()

    # ---------- Sheet 1: 汇总 ----------
    ws = wb.active
    ws.title = "汇总"

    ws.cell(row=1, column=1, value="LLM API 压测报告").font = _TITLE_FONT
    sm = req.server_meta or {}
    deploy_bits = [f"{k}={sm[k]}" for k in
                   ("tp_size", "dp_size", "dtype", "context_length", "max_total_num_tokens",
                    "max_prefill_tokens", "max_running_requests", "attention_backend",
                    "mem_fraction_static") if k in sm]
    sampling_bits = [f"{k}={v}" for k, v in (req.sampling or {}).items() if v not in (None, "")]
    slo_bits = [x for x in (
        f"TTFT≤{req.slo_ttft_ms}ms" if req.slo_ttft_ms else "",
        f"TPOT≤{req.slo_tpot_ms}ms" if req.slo_tpot_ms else "") if x]
    info_rows = [
        ("模型名称", req.model),
        ("分布式架构(推理框架)", req.framework),
        ("引擎版本", sm.get("version") or "未探测到(可先在首页「自动检测」获取)"),
        ("模型路径", sm.get("model_path") or "—"),
        ("部署参数", "; ".join(deploy_bits) or "—"),
        ("GPU/硬件环境", (req.gpu_info or "").strip() or "未填写(建议注明 GPU 型号×数量/显存/互联)"),
        ("任务类型", {"chat": "对话", "image": "文生图", "video": "文生视频"}.get(req.task_type, req.task_type)),
        ("API 地址", req.api_url),
        ("协议", req.protocol),
        ("采样参数", "; ".join(sampling_bits) or "引擎默认(未固定,不同轮次解码路径可能有随机差异)"),
        ("Prompt 模式", "随机池(每请求文本不同)" if req.random_prompt else "固定(全部请求同一文本)"),
        ("SLO 阈值", " 且 ".join(slo_bits) or "未设置(不统计 Goodput)"),
        ("测试时间", started_at.strftime("%Y-%m-%d %H:%M:%S")),
        ("测试策略数", len(outputs)),
    ]
    for i, (k, v) in enumerate(info_rows, start=2):
        c1 = ws.cell(row=i, column=1, value=k)
        c1.font = _KEY_FONT
        c1.border = _BORDER
        c2 = ws.cell(row=i, column=2, value=v)
        c2.border = _BORDER

    header_row = len(info_rows) + 3   # 环境信息行数可变(引擎版本/采样参数等),表头随之下移
    headers = [
        "策略", "并发", "请求数", "预热丢弃", "输入tok", "输出tok上限", "实际输出tok(avg)",
        "语言", "缓存模式",
        "成功率%", "错误率%", "超时%", "限流429%", "服务端5xx%", "其他失败%",
        "TTFT avg", "TTFT P25", "TTFT P50", "TTFT P75", "TTFT P90", "TTFT P95", "TTFT P99",
        "延迟 avg(s)", "延迟 P50", "延迟 P90", "延迟 P99",
        "ITL avg(ms)", "ITL P50(ms)", "ITL P90(ms)", "ITL P99(ms)",
        "输入TPS avg", "解码TPS avg", "单请求TPS avg",
        "RPS", "聚合输入TPS", "聚合输出TPS", "Goodput(req/s)", "SLO达标%",
        "总输出tok", "耗时(s)", "失败原因汇总",
    ]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=header_row, column=col, value=h)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _BORDER

    def _cache_mode_label(c: Dict[str, Any]) -> str:
        m = c.get("cache_mode") or "auto"
        if m == "cold":
            return "冷(唯一前缀)"
        if m == "hot":
            return "热(固定同文)"
        return "随机池" if req.random_prompt else "固定文本"

    err_col = headers.index("错误率%") + 1
    for r, out in enumerate(outputs, header_row + 1):
        cfg, s = out["config"], out["summary"]
        measured_n = s["success_count"] + s["failed_count"]

        def _bkt(key, _n=measured_n, _s=s):
            n = (_s.get("error_buckets") or {}).get(key, 0)
            return round(n / _n * 100, 2) if _n else 0

        succ_rate = round(s["success_count"] / measured_n * 100, 2) if measured_n else 0
        # 其他失败 = 失败总数 − 已单列的桶(超时/限流/5xx),含 4xx、连接、空响应等
        other_fail = max(0, s["failed_count"] - sum(
            (s.get("error_buckets") or {}).get(k, 0)
            for k in ("timeout", "rate_limited", "server_error")))
        other_fail_pct = round(other_fail / measured_n * 100, 2) if measured_n else 0
        slo = s.get("slo") or {}
        row_vals = [
            cfg["name"], cfg["concurrency"], cfg["total_requests"], s.get("warmup_count", 0),
            cfg["input_tokens"], cfg["max_output_tokens"],
            s["output_tokens_per_request"].get("avg"), cfg.get("lang", "en"),
            _cache_mode_label(cfg),
            succ_rate, round(s["error_rate"] * 100, 2),
            _bkt("timeout"), _bkt("rate_limited"), _bkt("server_error"), other_fail_pct,
            s["ttft_ms"].get("avg"), s["ttft_ms"].get("p25"), s["ttft_ms"].get("p50"),
            s["ttft_ms"].get("p75"), s["ttft_ms"].get("p90"), s["ttft_ms"].get("p95"),
            s["ttft_ms"].get("p99"),
            s["latency_s"].get("avg"), s["latency_s"].get("p50"),
            s["latency_s"].get("p90"), s["latency_s"].get("p99"),
            s["itl_ms"].get("avg"), s["itl_ms"].get("p50"), s["itl_ms"].get("p90"),
            s["itl_ms"].get("p99"),
            s.get("prefill_tps", {}).get("avg"),
            s.get("decode_tps", {}).get("avg"),
            s["throughput_per_request_tps"].get("avg"),
            s.get("rps"),
            s.get("aggregate_input_tps"),
            s.get("aggregate_throughput_tps"),
            slo.get("goodput_rps"),
            round(slo["rate"] * 100, 1) if slo.get("rate") is not None else None,
            s.get("total_output_tokens"),
            s.get("wall_clock_s"),
            "; ".join(f"{k} × {v}" for k, v in (s.get("error_groups") or {}).items()) or None,
        ]
        for col, val in enumerate(row_vals, 1):
            c = ws.cell(row=r, column=col, value=val)
            c.border = _BORDER
            c.alignment = Alignment(horizontal="center", vertical="center")
        err_cell = ws.cell(row=r, column=err_col)
        err_rate = s["error_rate"] * 100
        err_cell.fill = _OK_FILL if err_rate < 1 else (_WARN_FILL if err_rate < 5 else _BAD_FILL)

    ws.column_dimensions["A"].width = 30
    for col_idx in range(2, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 12
    ws.column_dimensions[get_column_letter(len(headers))].width = 42   # 失败原因汇总
    ws.freeze_panes = f"A{header_row + 1}"

    # ---------- Sheet 2: 分析报告 ----------
    wsA = wb.create_sheet("分析报告", 1)
    wsA.cell(row=1, column=1, value="压测结果分析报告").font = _TITLE_FONT
    meta_rows = [
        ("模型名称", req.model),
        ("分布式架构(推理框架)", req.framework),
        ("测试时间", started_at.strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for i, (k, v) in enumerate(meta_rows, start=2):
        wsA.cell(row=i, column=1, value=k).font = _KEY_FONT
        wsA.cell(row=i, column=2, value=v)

    ar_header = 6
    for col, h in enumerate(["检查项", "分析结论"], 1):
        c = wsA.cell(row=ar_header, column=col, value=h)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _BORDER
    for r, (item, text) in enumerate(analysis, ar_header + 1):
        c1 = wsA.cell(row=r, column=1, value=item)
        c1.font = _KEY_FONT
        c1.border = _BORDER
        c1.alignment = Alignment(vertical="top")
        c2 = wsA.cell(row=r, column=2, value=text)
        c2.border = _BORDER
        c2.alignment = Alignment(vertical="top", wrap_text=True)

    # AI 深度分析(用被测模型生成)
    if ai_analysis:
        ai_start = ar_header + len(analysis) + 3
        t = wsA.cell(row=ai_start, column=1, value="AI 深度分析报告 (由被测模型生成)")
        t.font = _TITLE_FONT
        row = ai_start + 1
        for line in ai_analysis.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("###"):
                c = wsA.cell(row=row, column=1, value=line.lstrip("# ").strip())
                c.font = _KEY_FONT
                c.fill = PatternFill("solid", fgColor="D9E1F2")
                c.border = _BORDER
                wsA.cell(row=row, column=2, value="").border = _BORDER
            elif line.startswith("##"):
                c = wsA.cell(row=row, column=1, value=line.lstrip("# ").strip())
                c.font = Font(bold=True, size=12, color="C00000")
                c.border = _BORDER
                wsA.cell(row=row, column=2, value="").border = _BORDER
            else:
                c = wsA.cell(row=row, column=2, value=clean_text(line))
                c.border = _BORDER
                c.alignment = Alignment(vertical="top", wrap_text=True)
                wsA.cell(row=row, column=1, value="").border = _BORDER
            row += 1

    wsA.column_dimensions["A"].width = 20
    wsA.column_dimensions["B"].width = 110

    # ---------- Per-strategy detail sheets ----------
    for i, out in enumerate(outputs):
        cfg, results = out["config"], out["results"]
        ws2 = wb.create_sheet(f"{i + 1}-{sanitize_sheet_name(cfg['name'])}")

        ws2.cell(
            row=1, column=1,
            value=(f"策略: {cfg['name']}  模型: {req.model}  架构: {req.framework}"
                   + (f"  (已丢弃 {sum(1 for x in results if x.get('warmup'))} 个预热请求,不计入统计)"
                      if any(x.get("warmup") for x in results) else "")),
        ).font = _KEY_FONT
        detail_headers = [
            "请求ID", "状态", "HTTP", "输入tokens", "输出tokens",
            "TTFT(ms)", "总延迟(s)", "平均ITL(ms)",
            "输入TPS", "解码TPS", "输出TPS",
            "输入内容预览", "输出内容", "错误",
        ]
        for col, h in enumerate(detail_headers, 1):
            c = ws2.cell(row=2, column=col, value=h)
            c.fill = _HEADER_FILL
            c.font = _HEADER_FONT
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = _BORDER

        detail_rows = [row for row in sorted(results, key=lambda x: x["request_id"])
                       if not row.get("warmup")]
        # 大请求量(2000 上限放开后)时收紧输出预览长度,防止明细页把 xlsx 撑到上百 MB
        out_cap = 30000 if len(detail_rows) <= 200 else 1000
        for r, row in enumerate(detail_rows, 3):
            values = [
                row["request_id"],
                "成功" if row["success"] else "失败",
                row.get("status_code"),
                row["input_tokens"],
                row["output_tokens"],
                row.get("ttft_ms"),
                row.get("total_latency_s"),
                row.get("avg_itl_ms"),
                row.get("prefill_tps"),
                row.get("decode_tps"),
                row.get("output_tokens_per_s"),
                clean_text(row.get("input_preview", ""))[:300],
                clean_text(row.get("output_text", ""))[:out_cap],
                clean_text(row.get("error") or "")[:200],
            ]
            for col, val in enumerate(values, 1):
                c = ws2.cell(row=r, column=col, value=val)
                c.border = _BORDER
                c.alignment = Alignment(horizontal="center", vertical="center")
            if not row["success"]:
                for col in range(1, len(values) + 1):
                    ws2.cell(row=r, column=col).fill = _BAD_FILL

        for col_idx, width in enumerate(
            [8, 8, 8, 12, 12, 12, 12, 13, 11, 11, 11, 40, 60, 40], 1
        ):
            ws2.column_dimensions[get_column_letter(col_idx)].width = width
        ws2.auto_filter.ref = f"A2:N{len(detail_rows) + 2}"

    filename = "benchmark_{}_{}_{}.xlsx".format(
        sanitize_filename(req.model),
        sanitize_filename(req.framework),
        started_at.strftime("%Y%m%d_%H%M%S"),
    )
    wb.save(os.path.join(get_reports_dir(), filename))
    # 边车元数据,供报告列表展示模型/架构
    try:
        meta = {
            "filename": filename,
            "model": req.model,
            "framework": req.framework,
            "api_url": req.api_url,
            "strategy_count": len(outputs),
            "created_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(os.path.join(get_reports_dir(), filename[:-5] + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        pass
    return filename

# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(title="LLM API Benchmark Tool")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ============================================================================
# 鉴权中间件:未登录一律 401;公开路径 = 登录接口 / 静态资源 / 根页面 / 网关代理(自带密钥鉴权)
# ============================================================================
_AUTH_PUBLIC_PREFIXES = ("/static/", "/api/auth/login", "/api/auth/register", "/v1/")
_AUTH_PUBLIC_PATHS = {"/", "/favicon.ico", "/modelstart.html", "/modeluse.html"}

# 子账号功能模块 → 对应 API 路径前缀(未授予权限的模块一律 403;管理员不受限)
_PERM_PATHS = (
    ("bench", ("/api/tasks", "/api/reports", "/api/kv-cache", "/api/detect", "/api/probe-limits",
               "/api/server-info", "/api/server-metrics", "/api/download/", "/api/preview/")),
    ("modeluse", ("/api/modeluse/", "/api/studio/")),
    ("modelstart", ("/api/modelstart/",)),
    ("gateway", ("/api/gateway/",)),
)

def _session_user(request: Request) -> Optional[str]:
    tok = request.cookies.get("auth_token", "")
    if not tok:
        return None
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(tok)
        if s and (time.time() - s["ts"]) < _SESSION_TTL:
            s["ts"] = time.time()               # 滑动续期
            return s["user"]
    return None

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    p = request.url.path
    if p in _AUTH_PUBLIC_PATHS or p.startswith(_AUTH_PUBLIC_PREFIXES):
        u = _session_user(request)
        _USER_CTX.set(u or "")
        return await call_next(request)
    u = _session_user(request)
    if not u:
        return JSONResponse({"detail": "未登录或会话已过期,请重新登录"}, status_code=401)
    rec = _user_rec(u)
    if not rec:                                  # 账号已被删除:会话立即失效
        return JSONResponse({"detail": "账号不存在或已被删除"}, status_code=401)
    _USER_CTX.set(u)
    if rec.get("status") == "suspended":          # 账号被停用:会话立即失效
        with _SESSIONS_LOCK:
            _SESSIONS.pop(request.cookies.get("auth_token", ""), None)
        return JSONResponse({"detail": "该账号已被管理员停用,请联系管理员启用"}, status_code=401)
    if rec.get("role") == "sub":                  # 子账号按模块权限拦截未授权的 API
        for mod, prefixes in _PERM_PATHS:
            if p.startswith(prefixes) and mod not in (rec.get("perms") or []):
                return JSONResponse({"detail": f"没有「{_PERM_LABEL.get(mod, mod)}」功能模块的使用权限,请联系管理员开通"},
                                    status_code=403)
    elif rec.get("role") == "viewer":             # 预览用户:全站只读,仅可使用对话(会话/上传/发送)
        if p.startswith("/api/gateway/"):
            return JSONResponse({"detail": "预览账号无网关访问权限"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") \
                and not p.startswith(("/api/chat", "/api/auth/logout", "/api/auth/password")):
            return JSONResponse({"detail": "预览账号仅可查看与使用对话功能,无编辑 / 操作权限"}, status_code=403)
    return await call_next(request)

class LoginReq(BaseModel):
    username: str = ""
    password: str = ""

@app.post("/api/auth/login")
async def auth_login(body: LoginReq):
    rec = _user_rec((body.username or "").strip())
    if not rec or _pw_hash(rec.get("salt", ""), body.password or "") != rec.get("hash"):
        raise HTTPException(401, "用户名或密码错误")
    if rec.get("status") == "pending":
        raise HTTPException(403, "该账号正在等待管理员审批,通过后方可登录使用")
    if rec.get("status") == "suspended":
        raise HTTPException(403, "该账号已被管理员停用,请联系管理员启用")
    tok = secrets.token_hex(24)
    with _SESSIONS_LOCK:
        _SESSIONS[tok] = {"user": rec["username"], "ts": time.time()}
    resp = JSONResponse({"ok": True, "user": _user_public(rec)})
    resp.set_cookie("auth_token", tok, httponly=True, samesite="lax",
                    max_age=_SESSION_TTL, path="/")
    return resp

class RegisterReq(BaseModel):
    username: str = ""
    password: str = ""

@app.post("/api/auth/register")
async def auth_register(body: RegisterReq):
    """自助注册:提交后进入待审批列表,任一管理员通过后方可登录。"""
    name = (body.username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]{2,24}", name):
        raise HTTPException(400, "用户名需 2-24 位,可含中文/字母/数字/_/-")
    if len((body.password or "").strip()) < 6:
        raise HTTPException(400, "密码至少 6 位")
    users = _load_users()
    if any(u["username"] == name for u in users):
        raise HTTPException(400, "用户名已存在")
    salt = secrets.token_hex(8)
    users.append({"username": name, "salt": salt, "hash": _pw_hash(salt, body.password.strip()),
                  "role": "sub", "hosts": [], "perms": [], "status": "pending",
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    _save_users(users)
    return {"ok": True, "msg": "注册申请已提交,待任一管理员审批通过后即可登录"}

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    tok = request.cookies.get("auth_token", "")
    with _SESSIONS_LOCK:
        _SESSIONS.pop(tok, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("auth_token", path="/")
    return resp

@app.get("/api/auth/me")
async def auth_me():
    rec = _user_rec(_cur_user()) or {}
    if not rec:
        raise HTTPException(401, "未登录")
    return _user_public(rec)

class PasswordReq(BaseModel):
    old_password: str = ""
    new_password: str = ""

@app.post("/api/auth/password")
async def auth_password(body: PasswordReq):
    if len((body.new_password or "").strip()) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    users = _load_users()
    me = next((u for u in users if u["username"] == _cur_user()), None)
    if not me:
        raise HTTPException(401, "未登录")
    if _pw_hash(me.get("salt", ""), body.old_password or "") != me.get("hash"):
        raise HTTPException(400, "原密码不正确")
    me["salt"] = secrets.token_hex(8)
    me["hash"] = _pw_hash(me["salt"], body.new_password.strip())
    _save_users(users)
    return {"ok": True}

# ---------- 用户管理(仅管理员;admin 只能管理子账号/预览用户,super 管理全部) ----------
class UserCreateReq(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "sub"                # admin | sub | viewer
    hosts: List[str] = []
    perms: List[str] = []            # 子账号功能模块权限(按勾选授予;不勾选 = 无)

class UserUpdateReq(BaseModel):
    password: str = ""
    role: str = ""                   # 升级 / 降级:admin | sub | viewer
    hosts: Optional[List[str]] = None
    perms: Optional[List[str]] = None
    status: str = ""                 # active | suspended(暂停使用开关)

class ApproveReq(BaseModel):
    approve: bool = True
    perms: List[str] = []            # 通过时授予的功能模块(按勾选授予)

def _mgmt_guard() -> None:
    if not _is_admin():
        raise HTTPException(403, "仅管理员可以管理账号")

def _kill_sessions(name: str) -> None:
    """让某账号的全部在线会话立即失效(停用 / 降级时调用)。"""
    with _SESSIONS_LOCK:
        for tok in [k for k, v in _SESSIONS.items() if v.get("user") == name]:
            _SESSIONS.pop(tok, None)

@app.get("/api/users")
async def users_list():
    _mgmt_guard()
    return {"users": [_user_public(u) for u in _load_users()]}

@app.post("/api/users")
async def users_create(body: UserCreateReq):
    _mgmt_guard()
    name = (body.username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]{2,24}", name):
        raise HTTPException(400, "用户名需 2-24 位,可含中文/字母/数字/_/-")
    if len((body.password or "").strip()) < 6:
        raise HTTPException(400, "密码至少 6 位")
    role = body.role if body.role in ("admin", "sub", "viewer") else "sub"
    if role == "admin" and _cur_role() != "super":
        raise HTTPException(403, "仅总管理员可以创建管理员账号")
    users = _load_users()
    if any(u["username"] == name for u in users):
        raise HTTPException(400, "用户名已存在")
    salt = secrets.token_hex(8)
    rec = {"username": name, "salt": salt, "hash": _pw_hash(salt, body.password.strip()),
           "role": role, "status": "active",
           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if role == "sub":                            # 子账号:按勾选授予模块 + 分配主机
        rec["hosts"] = [h for h in body.hosts if isinstance(h, str)][:200]
        rec["perms"] = _clean_perms(body.perms)
    else:                                        # 管理员 / 预览用户:不分配主机与模块
        rec["hosts"] = []
        rec["perms"] = []
    users.append(rec)
    _save_users(users)
    return {"ok": True, "user": _user_public(users[-1])}

@app.post("/api/users/{name}")
async def users_update(name: str, body: UserUpdateReq):
    """编辑账号:重置密码 / 升降级 / 暂停启用 / 调整主机与模块权限。"""
    _mgmt_guard()
    me, my_role = _cur_user(), _cur_role()
    users = _load_users()
    rec = next((u for u in users if u["username"] == name), None)
    if not rec:
        raise HTTPException(404, "账号不存在")
    if name == "admin" and my_role != "super":
        raise HTTPException(403, "只有总管理员可以修改 admin 账号")
    if rec.get("role") == "admin" and my_role != "super":
        raise HTTPException(403, "仅总管理员可以修改管理员账号")
    # ---- 重置密码 ----
    if (body.password or "").strip():
        if len(body.password.strip()) < 6:
            raise HTTPException(400, "密码至少 6 位")
        rec["salt"] = secrets.token_hex(8)
        rec["hash"] = _pw_hash(rec["salt"], body.password.strip())
    # ---- 暂停 / 启用开关 ----
    if body.status in ("active", "suspended"):
        if name == "admin":
            raise HTTPException(400, "总管理员账号不可停用")
        if name == me:
            raise HTTPException(400, "不能停用当前登录账号")
        if rec.get("role") == "admin" and my_role != "super":
            raise HTTPException(403, "仅总管理员可以停用管理员账号")
        if body.status != (rec.get("status") or "active"):
            rec["status"] = body.status
            if body.status == "suspended":
                _kill_sessions(name)
    # ---- 升级 / 降级开关 ----
    new_role = body.role if body.role in ("admin", "sub", "viewer") else ""
    if new_role and new_role != rec.get("role"):
        if name == "admin":
            raise HTTPException(403, "总管理员账号角色不可变更")
        if rec.get("role") == "admin" and my_role != "super":
            raise HTTPException(403, "仅总管理员可以降级管理员账号")
        if new_role == "admin" and my_role != "super":
            raise HTTPException(403, "仅总管理员可以设置管理员角色")
        old_role = rec.get("role")
        rec["role"] = new_role
        if old_role == "admin" and new_role == "sub" and body.perms is None:
            rec["perms"] = list(_PERM_KEYS)      # 管理员降级为子账号:保留全部模块,可再逐项收回
        if new_role == "admin":                  # 升为管理员:全部功能,不绑主机
            rec["hosts"] = []
            rec["perms"] = []
            _kill_sessions(name)                 # 权限变大:重登以刷新前端模块入口
        if new_role == "viewer":
            _kill_sessions(name)                 # 降为预览:重登进入只读模式
    # ---- 主机 / 模块权限(仅子账号) ----
    if body.hosts is not None:
        rec["hosts"] = [h for h in body.hosts if isinstance(h, str)][:200] if rec.get("role") == "sub" else []
    if body.perms is not None and rec.get("role") == "sub":
        rec["perms"] = _clean_perms(body.perms)
    rec.setdefault("status", "active")
    _save_users(users)
    return {"ok": True, "user": _user_public(rec)}

@app.post("/api/users/{name}/approve")
async def users_approve(name: str, body: ApproveReq):
    """审批自助注册的子账号:通过 = 激活并授予权限;拒绝 = 删除该申请。"""
    _mgmt_guard()
    users = _load_users()
    rec = next((u for u in users if u["username"] == name), None)
    if not rec or rec.get("status") != "pending":
        raise HTTPException(404, "待审批账号不存在")
    if body.approve:
        rec["status"] = "active"
        rec["perms"] = _clean_perms(body.perms)
        _save_users(users)
        return {"ok": True, "user": _user_public(rec)}
    _save_users([u for u in users if u["username"] != name])
    return {"ok": True}

@app.delete("/api/users/{name}")
async def users_delete(name: str):
    _mgmt_guard()
    if name == _cur_user():
        raise HTTPException(400, "不能删除当前登录账号")
    if name == "admin":
        raise HTTPException(403, "总管理员账号不可删除")
    users = _load_users()
    rec = next((u for u in users if u["username"] == name), None)
    if not rec:
        raise HTTPException(404, "账号不存在")
    if rec.get("role") in ("super", "admin") and _cur_role() != "super":
        raise HTTPException(403, "仅总管理员可以删除管理员账号")
    _save_users([u for u in users if u["username"] != name])
    # 连带删除该账号的全部数据空间(会话/压测/预设/方案/报告等,不可恢复)
    try:
        d = os.path.join(USER_DATA_DIR, name)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    _kill_sessions(name)
    return {"ok": True}

# ---------- SSH 主机分配:管理员把全局主机池中的主机分配给子账号 ----------
class HostAssignReq(BaseModel):
    host_id: str = ""
    users: List[str] = []            # 被分配的子账号名列表

@app.post("/api/modelstart/hosts-assign")
async def ms_hosts_assign(body: HostAssignReq):
    _mgmt_guard()
    hid = (body.host_id or "").strip()
    pool = _load_global_cfg().get("modelstart_hosts") or []
    if not any(h.get("id") == hid for h in pool):
        raise HTTPException(404, "主机不存在")
    users = _load_users()
    subs = {u["username"] for u in users if u.get("role") == "sub"}
    for u in users:
        if u.get("role") != "sub":
            continue
        hosts = set(u.get("hosts") or [])
        if u["username"] in body.users:
            hosts.add(hid)
        else:
            hosts.discard(hid)
        u["hosts"] = sorted(hosts)
    _save_users(users)
    return {"ok": True, "users": [u["username"] for u in users
                                  if u.get("role") == "sub" and hid in (u.get("hosts") or [])]}

detector = ProtocolDetector()

@app.middleware("http")
async def no_store(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.post("/api/detect")
async def detect_api(req: DetectRequest):
    try:
        return await detector.detect(req.api_url, req.api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Detection failed: {e}")

TASKS: Dict[str, BenchTask] = {}
# 已持久化的压测任务记录(id → record):按账号存 userdata/<账号>/bench_tasks.json,
# 退出登录 / 服务重启后任务仍留在各自账号的任务列表中,结果与报告可回看
TASK_HISTORY: Dict[str, Dict[str, Any]] = {}

def _bench_history_path(owner: str) -> str:
    return os.path.join(USER_DATA_DIR, owner, "bench_tasks.json")

def _save_bench_history(owner: str) -> None:
    recs = sorted((r for r in TASK_HISTORY.values() if r.get("owner") == owner),
                  key=lambda r: r.get("created_ts", 0))
    try:
        d = os.path.join(USER_DATA_DIR, owner)
        os.makedirs(d, exist_ok=True)
        tmp = _bench_history_path(owner) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _bench_history_path(owner))
    except Exception as e:
        print(f"[bench] 保存任务记录失败: {e}")

def _persist_task(task: BenchTask) -> None:
    """任务当前状态写入归属账号的记录文件(创建 / 每策略完成 / 终态 / 改名时调用)。"""
    if task.deleted:
        return
    rec = task.info()
    rec.update({
        "owner": task.owner,
        "created_at_full": task.started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "random_prompt": task.req.random_prompt,
        "sampling": task.req.sampling,
        "slo_ttft_ms": task.req.slo_ttft_ms,
        "slo_tpot_ms": task.req.slo_tpot_ms,
        "server_meta": task.req.server_meta,
        "gpu_info": task.req.gpu_info,
        "api_key": task.req.api_key,
        "summaries": [
            {"name": o["config"]["name"], "config": o["config"], "summary": o["summary"]}
            for o in task.outputs
        ],
        "analysis": task.analysis,
        "ai_analysis": task.ai_analysis,
    })
    TASK_HISTORY[task.id] = rec
    _save_bench_history(task.owner)

def _remove_task_record(tid: str, owner: str) -> None:
    if TASK_HISTORY.pop(tid, None) is not None:
        _save_bench_history(owner)

def _load_bench_history() -> None:
    """启动时载入各账号的压测任务记录;重启前仍在运行 / 暂停的任务标记为中断。"""
    if not os.path.isdir(USER_DATA_DIR):
        return
    for owner in os.listdir(USER_DATA_DIR):
        p = _bench_history_path(owner)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                recs = json.load(f)
        except Exception:
            continue
        dirty = False
        for rec in recs:
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            if rec.get("status") in ("running", "paused"):
                rec["status"] = "error"
                rec["error"] = "服务重启导致任务中断;已完成策略的结果与报告仍可查看"
                dirty = True
            TASK_HISTORY[rec["id"]] = rec
        if dirty:
            _save_bench_history(owner)

_load_bench_history()

@app.post("/api/tasks")
async def create_task(req: SuiteRequest):
    if not req.api_url:
        raise HTTPException(400, "API 地址不能为空")
    if not req.model:
        raise HTTPException(400, "请选择或输入模型")
    if not req.strategies:
        raise HTTPException(400, "至少勾选一个测试策略")
    task = BenchTask(req)
    TASKS[task.id] = task
    _persist_task(task)        # 创建即留存:退出登录 / 重启后仍在任务列表
    return task.info()

def _task_guard(tid: str) -> BenchTask:
    """任务归属校验:压测任务按账号隔离,只能操作自己的任务。"""
    task = TASKS.get(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    if getattr(task, "owner", "") != _cur_user():
        raise HTTPException(404, "任务不存在")
    return task

def _history_guard(tid: str) -> Dict[str, Any]:
    """持久化记录(非运行中任务)归属校验。"""
    rec = TASK_HISTORY.get(tid)
    if not rec or rec.get("owner") != _cur_user():
        raise HTTPException(404, "任务不存在")
    return rec

@app.post("/api/tasks/{tid}/start")
async def start_task(tid: str):
    task = TASKS.get(tid)
    if task is None:
        # 服务重启后:从持久化记录恢复仍处于待启动状态的任务
        rec = TASK_HISTORY.get(tid)
        if not rec or rec.get("owner") != _cur_user() or rec.get("status") != "pending":
            raise HTTPException(404, "任务不存在")
        task = BenchTask.from_record(rec)
        TASKS[tid] = task
    elif getattr(task, "owner", "") != _cur_user():
        raise HTTPException(404, "任务不存在")
    if not task.start():
        raise HTTPException(400, "任务已启动或已结束")
    return {"ok": True}

def _public_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """持久化记录返回给前端前剔除敏感字段(api_key 仅存盘用于重启恢复,不回传)。"""
    return {k: v for k, v in rec.items() if k != "api_key"}

@app.get("/api/tasks")
async def list_tasks():
    me = _cur_user()
    out = [t.info() for t in TASKS.values() if getattr(t, "owner", "") == me]
    out += [_public_record(r) for r in TASK_HISTORY.values()
            if r.get("owner") == me and r.get("id") not in TASKS]
    return sorted(out, key=lambda r: r.get("created_ts", 0), reverse=True)

@app.get("/api/tasks/{tid}")
async def get_task(tid: str):
    task = TASKS.get(tid)
    if task is not None:
        _task_guard(tid)
        info = task.info()
        info["analysis"] = task.analysis
        info["ai_analysis"] = task.ai_analysis
        info["summaries"] = [
            {"name": o["config"]["name"], "config": o["config"], "summary": o["summary"]}
            for o in task.outputs
        ]
        return info
    rec = _history_guard(tid)
    return _public_record(rec)

@app.get("/api/tasks/{tid}/events")
async def task_events(tid: str):
    task = _task_guard(tid)
    return StreamingResponse(task.event_stream(), media_type="text/event-stream")

@app.post("/api/tasks/{tid}/pause")
async def pause_task(tid: str):
    task = _task_guard(tid)
    if not task.pause():
        raise HTTPException(400, "任务当前状态不可暂停")
    _persist_task(task)
    return {"ok": True}

@app.post("/api/tasks/{tid}/resume")
async def resume_task(tid: str):
    task = _task_guard(tid)
    if not task.resume():
        raise HTTPException(400, "任务当前状态不可继续")
    _persist_task(task)
    return {"ok": True}

class StrategiesUpdate(BaseModel):
    strategies: List[StrategyConfig]

@app.put("/api/tasks/{tid}/strategies")
async def update_task_strategies(tid: str, body: StrategiesUpdate):
    task = _task_guard(tid)
    if not task.update_remaining(body.strategies):
        raise HTTPException(400, "仅暂停状态可修改剩余策略")
    _persist_task(task)
    return {"ok": True, "remaining": len(task.strategies) - task.current_index - 1}

@app.post("/api/tasks/{tid}/cancel")
async def cancel_task(tid: str):
    task = TASKS.get(tid)
    if task is not None:
        _task_guard(tid)
        if not task.cancel():
            raise HTTPException(400, "任务已结束")
        return {"ok": True}
    # 持久化记录:待启动状态的任务也可直接停止
    rec = _history_guard(tid)
    if rec.get("status") == "pending":
        rec["status"] = "cancelled"
        _save_bench_history(rec.get("owner", ""))
    return {"ok": True}

class TaskRename(BaseModel):
    name: str

@app.patch("/api/tasks/{tid}")
async def rename_task(tid: str, body: TaskRename):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    name = name[:80]
    task = TASKS.get(tid)
    if task is not None:
        _task_guard(tid)
        task.rename(name)
        _persist_task(task)
    else:
        rec = _history_guard(tid)
        rec["name"] = name
        _save_bench_history(rec.get("owner", ""))
    return {"ok": True, "name": name}

@app.delete("/api/tasks/{tid}")
async def delete_task(tid: str):
    me = _cur_user()
    task = TASKS.get(tid)
    if task is not None:
        if getattr(task, "owner", "") != me:
            raise HTTPException(404, "任务不存在")
        task.deleted = True
        task.cancel()
        # 让事件流尽快收尾
        try:
            task.emit({"type": "cancelled", "excel_file": task.excel_file,
                       "analysis": task.analysis, "deleted": True})
        except Exception:
            pass
        del TASKS[tid]
        _remove_task_record(tid, me)
        return {"ok": True}
    rec = _history_guard(tid)
    _remove_task_record(tid, me)
    return {"ok": True}

# ============================================================================
# Web Search (联网搜索:服务端真实抓取并注入上下文,不依赖目标 API 内置工具)
# ============================================================================

SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def _search_cfg() -> Dict[str, Any]:
    """联网搜索配置(config.json):engine=auto|bing|duckduckgo|searxng,
    searxng_url=自建实例地址,web_search_count=结果条数(3-20)。"""
    cfg = _load_config()
    try:
        count = int(cfg.get("web_search_count") or 10)
    except (TypeError, ValueError):
        count = 10
    return {
        "engine": str(cfg.get("web_search_engine") or "auto").strip().lower(),
        "searxng_url": str(cfg.get("searxng_url") or "").strip(),
        "count": max(3, min(count, 20)),
    }

def _strip_html(html: str) -> str:
    return _html_unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))).strip()

async def _search_searxng(session, query: str, count: int, base_url: str) -> List[Dict[str, str]]:
    """自建/可信 SearXNG 实例(JSON API)。"""
    async with session.get(
        f"{base_url.rstrip('/')}/search",
        params={"q": query, "format": "json"},
        headers={"User-Agent": SEARCH_UA},
        timeout=aiohttp.ClientTimeout(total=15), ssl=False,
    ) as r:
        if r.status != 200:
            return []
        data = await r.json(content_type=None)
    out = []
    for x in (data.get("results") or [])[:count]:
        url = str(x.get("url") or "")
        title = _strip_html(str(x.get("title") or ""))
        if url.startswith("http") and title:
            out.append({"title": title, "url": url,
                        "snippet": _strip_html(str(x.get("content") or ""))[:300]})
    return out

async def _search_bing(session, query: str, count: int) -> List[Dict[str, str]]:
    """Bing 网页版,无需 API Key,国内可直连;结果不足时翻页(每页约 10 条,最多 3 页)。"""
    out: List[Dict[str, str]] = []
    for page in range(3):
        need = count - len(out)
        if need <= 0:
            break
        try:
            async with session.get(
                "https://www.bing.com/search",
                params={"q": query, "count": str(need), "first": str(page * 10 + 1)},
                headers={"User-Agent": SEARCH_UA,
                         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                timeout=aiohttp.ClientTimeout(total=15), ssl=False,
            ) as r:
                if r.status != 200:
                    break
                html = await r.text()
        except Exception:
            break
        found = 0
        for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.DOTALL):
            block = m.group(0)
            am = re.search(r'<h2[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not am:
                continue
            pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
            url = _html_unescape(am.group(1))
            if any(x["url"] == url for x in out):
                continue
            out.append({"title": _strip_html(am.group(2)), "url": url,
                        "snippet": (_strip_html(pm.group(1))[:300] if pm else "")})
            found += 1
        if not found:
            break   # 本页无新结果,不再翻页
    return out[:count]

async def _search_duckduckgo(session, query: str, count: int) -> List[Dict[str, str]]:
    """DuckDuckGo 纯 HTML 版,无需 Key(部分地区不可达,作为备选)。"""
    async with session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": SEARCH_UA},
        timeout=aiohttp.ClientTimeout(total=15), ssl=False,
    ) as r:
        if r.status != 200:
            return []
        html = await r.text()
    out = []
    for m in re.finditer(r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
        href = _html_unescape(m.group(1))
        um = re.search(r"[?&]uddg=([^&]+)", href)
        if um:
            href = _url_unquote(um.group(1))
        title = _strip_html(m.group(2))
        if href.startswith("http") and title:
            out.append({"title": title, "url": href, "snippet": ""})
    snippets = [s for s in (_strip_html(x) for x in re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL))]
    for i, item in enumerate(out[:count]):
        if i < len(snippets):
            item["snippet"] = snippets[i][:300]
    return out[:count]

# 提问/指令性词句:整句直接搜会跑偏,先剔除再作为查询词
_SEARCH_ZH_PATTERNS = [
    "请简要回答", "请简短回答", "请简要说明", "请简单介绍", "请介绍一下", "请回答",
    "请告诉我", "请帮我", "请问", "帮我", "帮忙", "麻烦",
    "简要回答", "简短回答", "简要说明", "简要介绍", "简单介绍", "介绍一下", "介绍下",
    "回答一下", "解释一下", "说明一下", "告诉我", "说说", "讲讲", "查一下", "查查",
    "怎么样", "怎样", "如何", "是多少", "什么是", "甚么是", "为什么", "为何",
    "哪些", "哪个", "哪里", "怎么",
    "简要", "简述", "详细", "请", "呢", "吗", "啊", "吧", "嘛", "哈",
]
_SEARCH_EN_TOKENS = {"please", "briefly", "shortly", "answer", "answers", "tell",
                     "me", "what", "whats", "what's", "how", "why", "explain",
                     "describe", "give", "list", "some", "is", "are", "the", "a", "an"}

def _build_search_query(message: str) -> str:
    """从用户消息提炼搜索关键词:只取第一行,剔除中英提问/指令词,避免整句搜索跑偏。"""
    text = (message or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0][:80]
    for pat in _SEARCH_ZH_PATTERNS:
        text = text.replace(pat, " ")
    kept = []
    for tk in re.split(r"[\s,。;:、,.!?~?!?]+", text):
        if not tk:
            continue
        if tk.lower().strip("'\u2019") in _SEARCH_EN_TOKENS:
            continue
        kept.append(tk)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()[:60]

async def _model_reachable(session, base: str, protocol: str, api_key: str) -> Optional[str]:
    """联网搜索前的模型可用性快检:连不通直接报错,避免模型不可用时白搜。
    返回 None 表示可用,否则返回错误说明。"""
    headers: Dict[str, str] = {}
    if protocol == "ollama":
        url = f"{base}/api/tags"
    else:
        url = f"{base}/v1/models"
        if protocol == "anthropic":
            headers = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
        elif api_key:
            headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=6), ssl=False) as r:
            if r.status in (401, 403):
                return None   # 鉴权失败不代表服务不可达,交给正式请求去报错
            if r.status >= 400:
                return f"模型服务返回 HTTP {r.status}"
            return None
    except Exception as e:
        return f"{type(e).__name__}: 无法连接模型服务"

async def _model_gen_queries(session, base: str, protocol: str, api_key: str,
                             model: str, message: str) -> List[str]:
    """让模型为用户问题提炼 2~3 条搜索关键词(结合模型语义理解,比本地启发式准)。
    失败或输出异常时返回空列表,由调用方回退。"""
    sys_prompt = ("为用户问题提炼 2~3 条用于网络搜索的关键词短语。要求:浓缩成主题关键词,"
                  "不要复述整个问题;中文问题用中文关键词,产品名/技术名词保留英文原文。"
                  "示例:问题「今天上海天气怎么样?」→ 关键词「上海 今日天气」。"
                  "每行一条,不要编号、不要解释、不要引号、不要前导语。\n\n用户问题: "
                  + message[:500])
    if protocol == "anthropic":
        endpoint, headers = f"{base}/v1/messages", {
            "Content-Type": "application/json", "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 120, "stream": True,
                   "thinking": {"type": "disabled"},
                   "messages": [{"role": "user", "content": sys_prompt}]}
    elif protocol == "ollama":
        endpoint, headers = f"{base}/api/chat", {"Content-Type": "application/json"}
        payload = {"model": model, "stream": True, "think": False,
                   "messages": [{"role": "user", "content": sys_prompt}],
                   "options": {"num_predict": 120}}
    else:
        endpoint, headers = f"{base}/v1/chat/completions", {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else ""}
        payload = {"model": model, "max_tokens": 120, "stream": True,
                   "messages": [{"role": "user", "content": sys_prompt}],
                   "chat_template_kwargs": {"enable_thinking": False}}
    try:
        text = ""
        async with session.post(endpoint, json=payload, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=25, sock_read=20),
                                ssl=False) as resp:
            if resp.status >= 400:
                return []
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buffer = ""
            async for raw in resp.content.iter_chunked(4096):
                buffer += decoder.decode(raw)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    data = None
                    if line.startswith("data:"):
                        ds = line[5:].strip()
                        if ds in ("[DONE]", "done"):
                            continue
                        data = ProtocolDetector._json(ds)
                    elif protocol == "ollama" and line.startswith("{"):
                        data = ProtocolDetector._json(line)
                    if not data:
                        continue
                    if protocol == "anthropic":
                        text += (data.get("delta") or {}).get("text", "")
                    elif protocol == "ollama":
                        text += (data.get("message") or {}).get("content", "")
                    else:
                        ch = data.get("choices") or []
                        if ch:
                            text += (ch[0].get("delta") or {}).get("content", "") or ""
        queries = []
        for ln in text.splitlines():
            ln = re.sub(r"^[\s\d\.\-\*、·•]+", "", ln.strip()).strip()
            ln = ln.strip("\"'“”‘’ `").strip()
            # 过滤说明性前导语与指令回声(如 "Keywords could be:" / "Condense into topic keywords")
            if not ln or ln.endswith(":") or ln.endswith("：") or "→" in ln:
                continue
            low = ln.lower()
            if any(k in low for k in ("keyword", "condense", "question", "phrase",
                                      "示例", "每行", "不要", "输出", "关键词", "主题词", "浓缩")):
                continue
            if low in ("keywords", "keywords could be", "search queries", "search terms",
                       "关键词", "关键词如下", "关键词是", "以下是关键词", "如下"):
                continue
            if 2 <= len(ln) <= 60:
                queries.append(ln)
        return queries[:2]
    except Exception:
        return []

async def do_web_search(query: str) -> List[Dict[str, str]]:
    """联网搜索入口:按 config.json 配置依次尝试引擎,返回 [{title,url,snippet}]。"""
    cfg = _search_cfg()
    query = re.sub(r"\s+", " ", (query or "")).strip()[:120]
    if not query:
        return []
    engine = cfg["engine"]
    if engine in ("searxng", "bing", "duckduckgo"):
        plan = [engine]
    else:  # auto:自建 SearXNG(若有)→ Bing → DuckDuckGo
        plan = (["searxng"] if cfg["searxng_url"] else []) + ["bing", "duckduckgo"]
    async with aiohttp.ClientSession() as session:
        for name in plan:
            try:
                if name == "searxng":
                    if not cfg["searxng_url"]:
                        continue
                    res = await _search_searxng(session, query, cfg["count"], cfg["searxng_url"])
                elif name == "bing":
                    res = await _search_bing(session, query, cfg["count"])
                else:
                    res = await _search_duckduckgo(session, query, cfg["count"])
                if res:
                    return res
            except Exception:
                continue
    return []

# ============================================================================
# GPU Monitor (GPU监控:管理内嵌看板地址,数据来自 GPU 服务器上的 gpu-monitor 服务)
# ============================================================================

def _gpu_monitors() -> List[Dict[str, str]]:
    """config.json 里的监控服务列表:[{name, url}]。"""
    cfg = _load_config()
    mons = cfg.get("gpu_monitors")
    out: List[Dict[str, str]] = []
    if isinstance(mons, list):
        for m in mons:
            if isinstance(m, dict) and str(m.get("url") or "").strip():
                out.append({"name": str(m.get("name") or "").strip(),
                            "url": str(m.get("url")).strip()})
    return out

def _norm_monitor_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url

@app.get("/api/gpu-monitors")
async def gpu_monitors_get():
    return {"monitors": _gpu_monitors()}

class GpuMonitorItem(BaseModel):
    name: str = ""
    url: str

class GpuMonitorsSave(BaseModel):
    monitors: List[GpuMonitorItem] = []

@app.post("/api/gpu-monitors")
async def gpu_monitors_save(body: GpuMonitorsSave):
    clean, seen = [], set()
    for m in body.monitors[:12]:
        url = _norm_monitor_url(m.url)
        if not url or url in seen:
            continue
        seen.add(url)
        clean.append({"name": (m.name or "").strip()[:30], "url": url[:200]})
    cfg = _load_config()
    cfg["gpu_monitors"] = clean
    _save_config(cfg)
    return {"ok": True, "monitors": clean}

@app.post("/api/gpu-monitors/probe")
async def gpu_monitors_probe(body: GpuMonitorItem):
    """探测 gpu-monitor 服务是否可达,可达则返回主机/驱动/GPU 数概要。"""
    url = _norm_monitor_url(body.url)
    if not url:
        raise HTTPException(400, "地址不能为空")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url}/api/summary",
                timeout=aiohttp.ClientTimeout(total=5), ssl=False,
            ) as r:
                if r.status != 200:
                    return {"ok": False, "message": f"HTTP {r.status}"}
                data = await r.json(content_type=None)
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: 连接失败(服务未部署或端口未放行)"}
    if not data.get("ok"):
        return {"ok": False, "message": f"服务可达但无数据: {data.get('error', '未知')}"}
    return {"ok": True, "message": f"主机 {data.get('host', '?')} · {data.get('gpu_count', '?')} 卡 · 驱动 {data.get('driver', '?')}"}

# ============================================================================
# GPU Monitor via SSH (SSH 直连主机执行 mthreads-gmi,免部署监控服务)
# ============================================================================

GPU_SSH_SPLIT = "===GPUMONSPLIT==="
GPU_SSH_CMD = (
    "mthreads-gmi -q --json; echo " + GPU_SSH_SPLIT +
    "; mthreads-gmi; echo " + GPU_SSH_SPLIT +
    "; mthreads-gmi -pm; echo " + GPU_SSH_SPLIT +
    "; mthreads-gmi topo -m; echo " + GPU_SSH_SPLIT +
    "; uname -r; echo " + GPU_SSH_SPLIT +
    "; lsmod | grep -iE 'musa|mtt|metax|gpu' | head -12; echo " + GPU_SSH_SPLIT +
    "; grep '^cpu ' /proc/stat; cat /proc/loadavg; echo " + GPU_SSH_SPLIT +
    "; sleep 1; grep '^cpu ' /proc/stat; echo " + GPU_SSH_SPLIT +
    "; grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo; echo " + GPU_SSH_SPLIT +
    "; cat /proc/net/dev; echo " + GPU_SSH_SPLIT +
    "; GW=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}'); echo GATEWAY=$GW; " +
    "ping -c1 -W1 $GW 2>&1 | grep -E 'time=|loss|unreachable|transmitted'; echo ===INET===; " +
    "ping -c1 -W2 223.5.5.5 2>&1 | grep -E 'time=|loss|unreachable|transmitted'"
)

def _gpu_ssh_monitors() -> List[Dict[str, Any]]:
    cfg = _load_config()
    mons = cfg.get("gpu_ssh_monitors")
    out: List[Dict[str, Any]] = []
    if isinstance(mons, list):
        for m in mons:
            if isinstance(m, dict) and str(m.get("host") or "").strip():
                out.append({
                    "name": str(m.get("name") or "").strip(),
                    "host": str(m["host"]).strip(),
                    "ssh_port": int(m.get("ssh_port") or 22),
                    "username": str(m.get("username") or "root").strip(),
                    "password": str(m.get("password") or ""),
                })
    return out

class GpuSshItem(BaseModel):
    name: str = ""
    host: str
    ssh_port: int = 22
    username: str = "root"
    password: str = ""

class GpuSshSave(BaseModel):
    monitors: List[GpuSshItem] = []

@app.get("/api/gpu-ssh")
async def gpu_ssh_list():
    return {"monitors": _gpu_ssh_monitors()}

@app.post("/api/gpu-ssh")
async def gpu_ssh_save(body: GpuSshSave):
    clean, seen = [], set()
    for m in body.monitors[:12]:
        host = str(m.host or "").strip()
        if not host or (host, m.ssh_port) in seen:
            continue
        seen.add((host, m.ssh_port))
        clean.append({"name": (m.name or "").strip()[:30], "host": host[:100],
                      "ssh_port": int(m.ssh_port or 22), "username": (m.username or "root").strip()[:60],
                      "password": m.password[:120]})
    cfg = _load_config()
    cfg["gpu_ssh_monitors"] = clean
    _save_config(cfg)
    return {"ok": True, "monitors": clean}

# ---- SSH 执行(线程池 + 连接缓存) ----
_SSH_LOCK = threading.Lock()
_SSH_CLIENTS: Dict[str, Any] = {}

def _ssh_pkey(m: Dict[str, Any]):
    """解析 PEM 私钥(支持 Ed25519/ECDSA/RSA,可带口令)。"""
    import io
    import paramiko
    data = (m.get("private_key") or "").strip()
    if not data:
        raise RuntimeError("密钥内容为空")
    passphrase = m.get("passphrase") or None
    err = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(data), password=passphrase)
        except Exception as e:
            err = e
    raise RuntimeError(f"私钥解析失败(支持 Ed25519/ECDSA/RSA PEM):{str(err)[:160]}")

def _ssh_client(key: str, m: Dict[str, Any], fresh: bool = False):
    """按 host:port:user 缓存 SSH 连接,fresh=True 时强制重建。支持密码 / 私钥两种登录。"""
    with _SSH_LOCK:
        if not fresh:
            c = _SSH_CLIENTS.get(key)
            if c is not None and c.get_transport() and c.get_transport().is_active():
                return c
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if m.get("auth") == "key" and (m.get("private_key") or "").strip():
            c.connect(m["host"], port=m.get("ssh_port", 22), username=m.get("username", "root"),
                      pkey=_ssh_pkey(m), timeout=6, banner_timeout=6,
                      look_for_keys=False, allow_agent=False)
        else:
            c.connect(m["host"], port=m.get("ssh_port", 22), username=m.get("username", "root"),
                      password=m.get("password", ""), timeout=6, banner_timeout=6,
                      look_for_keys=False, allow_agent=False)
        _SSH_CLIENTS[key] = c
        return c

def _ssh_exec(key: str, m: Dict[str, Any], cmd: str) -> str:
    """执行远端命令,连接失效自动重建重试一次。"""
    import paramiko
    for attempt in range(2):
        try:
            c = _ssh_client(key, m, fresh=(attempt == 1))
            _, stdout, _ = c.exec_command(cmd, timeout=15)
            return stdout.read().decode("utf-8", "replace")
        except paramiko.AuthenticationException:
            raise RuntimeError("SSH 认证失败:用户名或密码错误")
        except Exception as e:
            if attempt == 1:
                raise RuntimeError(f"SSH 执行失败: {type(e).__name__}: {str(e)[:120]}")
            with _SSH_LOCK:
                _SSH_CLIENTS.pop(key, None)

def _num(s, dflt=0.0):
    if s is None:
        return dflt
    m2 = re.search(r"-?\d+(?:\.\d+)?", str(s))
    return float(m2.group()) if m2 else dflt

def _gmi_parse(detail_text: str, summary_text: str, pm_text: str,
               topo_text: str, kernel_ver: str, modules_text: str) -> Dict[str, Any]:
    """解析 mthreads-gmi 输出为看板摘要(逻辑源自 gpu-monitor 项目)。"""
    detail = _norm_keys_json(detail_text)
    gpus = []
    for g in detail.get("GPU", []):
        pci = g.get("PCI", {}) or {}
        link = pci.get("GPU Link Info", {}) or {}
        gen = link.get("PCIe Generation", {}) or {}
        width = link.get("Link Width", {}) or {}
        mem = g.get("FB Memory Usage", {}) or {}
        util = g.get("Utilization", {}) or {}
        power = g.get("Power Readings", {}) or {}
        clocks = g.get("Clocks", {}) or {}
        ecc = g.get("ECC Mode", {}) or {}
        gpus.append({
            "index": int(_num(g.get("Index", "0"))),
            "name": g.get("Product Name", "?"),
            "uuid": g.get("GPU UUID", ""),
            "serial": g.get("Serial Number", ""),
            "bios": g.get("MTBios Version", ""),
            "bus_id": pci.get("Bus ID", ""),
            "slot": pci.get("Slot ID(Name)", ""),
            "pcie_gen": "%s x %s" % (gen.get("Current", "?"), width.get("Current", "?")),
            "mem_used_mib": int(_num(mem.get("Used"))),
            "mem_total_mib": int(_num(mem.get("Total"))),
            "util_gpu": _num(util.get("Gpu")),
            "util_mem": _num(util.get("Memory")),
            "util_enc": _num(util.get("Encoder")),
            "util_dec": _num(util.get("Decoder")),
            "temp_c": _num(g.get("Temperature", {}).get("GPU Current Temp")),
            "power_w": _num(power.get("Power Draw")),
            "power_limit_w": _num(power.get("Current Power Limit")),
            "sm_clock_mhz": _num(clocks.get("Graphics")),
            "mem_clock_mhz": _num(clocks.get("Memory")),
            "ecc_edc": ecc.get("EDC", "?"),
            "ecc_on_die": ecc.get("On-die", "?"),
            "perf_state": g.get("Performance State", "N/A"),
        })
    if not gpus:
        raise RuntimeError("未解析到 GPU 信息(目标主机可能未安装 MUSA 驱动或无 mthreads-gmi)")

    procs: Dict[tuple, Dict[str, Any]] = {}
    in_proc = False
    for line in (summary_text or "").splitlines():
        if "Processes:" in line:
            in_proc = True
            continue
        if not in_proc:
            continue
        t = line.strip()
        if not t or set(t) <= set("+-"):
            continue
        low = t.lower()
        if "no running process" in low or "pid" in low or "usage" in low or "process name" in low:
            continue
        m3 = re.match(r"^(\d+)\s+(\d+)\s+(.+?)\s+(\d+)\s*MiB\s*$", t)
        if m3:
            key = (int(m3.group(1)), int(m3.group(2)))
            procs[key] = {"gpu": key[0], "pid": key[1], "name": m3.group(3).strip(),
                          "mem_mib": int(m3.group(4)), "gpu_util": None, "mem_util": None}
    for line in (pm_text or "").splitlines():
        m3 = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s*%\s+(\d+)\s*%\s*$", line)
        if m3:
            key = (int(m3.group(1)), int(m3.group(2)))
            e = procs.setdefault(key, {"gpu": key[0], "pid": key[1], "name": "?",
                                       "mem_mib": None, "gpu_util": None, "mem_util": None})
            e["mem_util"] = int(m3.group(3))
            e["gpu_util"] = int(m3.group(4))
    names = {e["pid"]: e["name"] for e in procs.values() if e["name"] != "?"}
    for e in procs.values():
        if e["name"] == "?" and e["pid"] in names:
            e["name"] = names[e["pid"]]

    numa, cpu_aff = {}, {}
    for line in (topo_text or "").splitlines():
        if not line.startswith("GPU"):
            continue
        m3 = re.match(r"^(GPU\d+)\s+.*?(\d+(?:-\d+)?(?:,\d+(?:-\d+)*)*)\s+(\d+)\s*$", line)
        if m3:
            cpu_aff[m3.group(1)] = m3.group(2)
            numa[m3.group(1)] = m3.group(3)

    total_mem = sum(g["mem_total_mib"] for g in gpus)
    used_mem = sum(g["mem_used_mib"] for g in gpus)
    return {
        "ok": True,
        "ts": time.time(),
        "host": detail.get("Hostname", "") or "",
        "driver": detail.get("Driver Version", "?"),
        "gpu_count": len(gpus),
        "gpus": gpus,
        "processes": sorted(procs.values(), key=lambda p: (p["gpu"], p["pid"])),
        "topo_text": (topo_text or "")[:8000],
        "numa": numa,
        "cpu_aff": cpu_aff,
        "kernel": {
            "uname": (kernel_ver or "").strip(),
            "modules": [l.strip() for l in (modules_text or "").splitlines() if l.strip()][:12],
        },
        "totals": {
            "mem_used_mib": used_mem,
            "mem_total_mib": total_mem,
            "power_w": round(sum(g["power_w"] for g in gpus), 1),
            "avg_util": round(sum(g["util_gpu"] for g in gpus) / len(gpus), 1),
            "max_temp_c": max(g["temp_c"] for g in gpus),
        },
    }

def _norm_keys_json(text: str) -> Dict[str, Any]:
    try:
        d = json.loads(text)
    except Exception:
        return {}
    def norm(obj):
        if isinstance(obj, dict):
            return {k.strip(): norm(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [norm(x) for x in obj]
        return obj
    return norm(d) if isinstance(d, dict) else {}

def _cpu_times(line: str):
    """解析 /proc/stat 的 cpu 行,返回 (busy, idle) jiffies。"""
    vals = [float(x) for x in line.split()[1:11]]
    if len(vals) < 4:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
    busy = vals[0] + vals[1] + vals[2] + sum(vals[5:8])
    return busy, idle

def _sys_parse(parts: List[str]) -> Dict[str, Any]:
    """解析 SSH 采集的系统指标段(CPU/内存/网卡流量/网络连通性)。任一段缺失时对应字段为 None,不影响 GPU 监控。"""
    out: Dict[str, Any] = {
        "cpu_pct": None, "load": None,
        "mem_total_kib": None, "mem_used_kib": None, "mem_avail_kib": None,
        "swap_total_kib": None, "swap_used_kib": None,
        "net_rx_bytes": None, "net_tx_bytes": None,
        "gw": None, "gw_ms": None, "inet_ms": None,
    }
    def sec(i: int) -> str:
        return parts[i] if i < len(parts) else ""
    # CPU:两段 /proc/stat 采样求差 + 负载
    t1 = t2 = None
    for ln in sec(6).splitlines():
        if ln.strip().startswith("cpu "):
            t1 = _cpu_times(ln)
        elif out["load"] is None:
            m = re.match(r"^\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+\d+/\d+", ln)
            if m:
                out["load"] = [float(m.group(1)), float(m.group(2)), float(m.group(3))]
    for ln in sec(7).splitlines():
        if ln.strip().startswith("cpu "):
            t2 = _cpu_times(ln)
    if t1 and t2:
        db, di = t2[0] - t1[0], t2[1] - t1[1]
        if db + di > 0:
            out["cpu_pct"] = round(100.0 * db / (db + di), 1)
    # 内存 / swap
    mi: Dict[str, int] = {}
    for ln in sec(8).splitlines():
        m = re.match(r"^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):\s+(\d+)\s*kB", ln.strip())
        if m:
            mi[m.group(1)] = int(m.group(2))
    if "MemTotal" in mi:
        out["mem_total_kib"] = mi["MemTotal"]
        avail = mi.get("MemAvailable")
        if avail is None:
            avail = mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0)
        out["mem_avail_kib"] = avail
        out["mem_used_kib"] = max(0, mi["MemTotal"] - avail)
    if "SwapTotal" in mi:
        out["swap_total_kib"] = mi["SwapTotal"]
        out["swap_used_kib"] = max(0, mi["SwapTotal"] - mi.get("SwapFree", mi["SwapTotal"]))
    # 网卡流量(排除 lo,汇总物理/虚拟网卡)
    rx = tx = 0
    seen = False
    for ln in sec(9).splitlines():
        if ":" not in ln:
            continue
        ifc, rest = ln.split(":", 1)
        ifc = ifc.strip()
        if not ifc or ifc == "lo":
            continue
        cols = rest.split()
        if len(cols) >= 16:
            try:
                rx += int(cols[0])
                tx += int(cols[8])
                seen = True
            except ValueError:
                continue
    if seen:
        out["net_rx_bytes"] = rx
        out["net_tx_bytes"] = tx
    # 网络环境检查:网关 ping + 外网 ping
    net = sec(10)
    if "===INET===" in net:
        gw_part, inet_part = net.split("===INET===", 1)
    else:
        gw_part, inet_part = net, ""
    m = re.match(r"^GATEWAY=(\S*)", gw_part.strip(), re.M)
    if m:
        out["gw"] = m.group(1) or None
    def _ping_ms(txt: str):
        mm = re.search(r"time=([\d.]+)\s*ms", txt)
        return round(float(mm.group(1)), 1) if mm else None
    out["gw_ms"] = _ping_ms(gw_part)
    out["inet_ms"] = _ping_ms(inet_part)
    return out

# 每主机历史(2s × 1800 点 ≈ 1 小时)
_SSH_HISTORY: Dict[str, Any] = {}

def _gpu_ssh_query_blocking(m: Dict[str, Any]) -> Dict[str, Any]:
    key = f"{m['host']}:{m.get('ssh_port', 22)}:{m.get('username', 'root')}"
    t0 = time.time()
    out = _ssh_exec(key, m, GPU_SSH_CMD)
    collect_s = round(time.time() - t0, 2)
    parts = out.split(GPU_SSH_SPLIT)
    while len(parts) < 11:
        parts.append("")
    snap = _gmi_parse(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
    sy = _sys_parse(parts)
    sy["collect_s"] = collect_s
    snap["sys"] = sy
    h = _SSH_HISTORY.setdefault(m["host"], collections.deque(maxlen=1800))
    n = snap["gpu_count"]
    u, p, mm, c = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    for g in snap["gpus"]:
        i = g["index"]
        if 0 <= i < n:
            u[i], p[i], mm[i], c[i] = g["util_gpu"], g["power_w"], g["mem_used_mib"], g["temp_c"]
    mem_pct = round(100.0 * sy["mem_used_kib"] / sy["mem_total_kib"], 1) \
        if sy.get("mem_total_kib") else None
    h.append({"t": round(snap["ts"], 1), "u": u, "p": p, "m": mm, "c": c,
              "cpu": sy.get("cpu_pct"), "mem": mem_pct,
              "rx": sy.get("net_rx_bytes"), "tx": sy.get("net_tx_bytes"),
              "gw": sy.get("gw_ms"), "net": sy.get("inet_ms")})
    return snap

def _find_ssh_monitor(name_or_host: str) -> Optional[Dict[str, Any]]:
    mons = _gpu_ssh_monitors()
    for m in mons:
        if m["name"] == name_or_host or m["host"] == name_or_host:
            return m
    return mons[0] if mons else None

@app.post("/api/gpu-ssh/probe")
async def gpu_ssh_probe(body: GpuSshItem):
    """探测 SSH 直连可用性并获取 GPU 概要。"""
    m = {"name": body.name, "host": body.host.strip(), "ssh_port": body.ssh_port or 22,
         "username": body.username or "root", "password": body.password}
    if not m["host"]:
        raise HTTPException(400, "主机地址不能为空")
    try:
        snap = await asyncio.to_thread(_gpu_ssh_query_blocking, m)
        return {"ok": True,
                "message": f"连接成功:{snap['gpu_count']} 卡 · 驱动 {snap['driver']} · 内核 {snap['kernel']['uname']}"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}

@app.get("/api/gpu-ssh/poll")
async def gpu_ssh_poll(name: str):
    m = _find_ssh_monitor(name)
    if not m:
        raise HTTPException(404, "未配置该 SSH 监控主机")
    try:
        snap = await asyncio.to_thread(_gpu_ssh_query_blocking, m)
        return snap
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

@app.get("/api/gpu-ssh/history")
async def gpu_ssh_history(name: str):
    h = _SSH_HISTORY.get(_find_ssh_monitor(name)["host"] if _find_ssh_monitor(name) else "", None)
    return list(h) if h else []

# ============================================================================
# Server Metrics & KV Cache Test (服务实时指标 / KV Cache 前缀缓存效率实测)
# ============================================================================

def _parse_prometheus(text: str) -> Dict[str, float]:
    """从 Prometheus 文本中提取指标值(同指标多份取最后样本)。"""
    out: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+"
            r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", line)
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                continue
    return out

class MetricsRequest(BaseModel):
    api_url: str
    api_key: str = ""

def _metrics_summary(vals: Dict[str, float]) -> Dict[str, Any]:
    """从 Prometheus 指标字典提取跨框架(vLLM/SGLang)关键指标摘要。"""
    framework = None
    if any(k.startswith("vllm:") for k in vals):
        framework = "vLLM"
    elif any(k.startswith("sglang:") for k in vals):
        framework = "SGLang"
    elif any(k.startswith("tgi_") or ":tgi" in k for k in vals):
        framework = "TGI"

    def pick(*names):
        for n in names:
            v = vals.get(n)
            if v is not None:
                return v
        return None

    kv_raw = pick("vllm:gpu_cache_usage_perc", "gpu_cache_usage_perc",
                  "sglang:token_usage", "sglang:kv_cache_usage")
    kv_pct = None
    if kv_raw is not None:
        kv_pct = round(kv_raw * 100, 1) if kv_raw <= 1.0 else round(kv_raw, 1)

    hits = vals.get("vllm:prefix_cache_hits")
    misses = vals.get("vllm:prefix_cache_misses")
    hit_rate = None
    if hits is not None and misses is not None and (hits + misses) > 0:
        hit_rate = round(hits / (hits + misses) * 100, 1)
    else:
        hr = pick("sglang:cache_hit_rate", "cache_hit_rate")
        if hr is not None:
            hit_rate = round(hr * 100, 1) if hr <= 1.5 else round(hr, 1)

    def _int(v):
        return int(v) if v is not None else None

    metrics = {
        "kv_cache_usage_perc": kv_pct,
        "prefix_cache_hits": _int(hits),
        "prefix_cache_misses": _int(misses),
        "prefix_cache_hit_rate": hit_rate,
        "running_requests": _int(pick("vllm:num_requests_running", "sglang:num_running_reqs",
                                       "num_requests_running")),
        "waiting_requests": _int(pick("vllm:num_requests_waiting", "sglang:num_queue_reqs",
                                       "sglang:num_waiting_reqs", "num_requests_waiting")),
        "gen_throughput": pick("sglang:gen_throughput"),
    }
    return {"framework": framework, "metrics": metrics, "metric_count": len(vals)}

# ---- SSH 通道兜底:直连被网关拦截时,经 SSH 在主机内部取数 ----

def _ssh_monitor_for_host(host: str) -> Optional[Dict[str, Any]]:
    for m in _gpu_ssh_monitors():
        if m["host"] == host:
            return m
    return None

def _ssh_curl(host: str, port: int, path: str, timeout: int = 5) -> Optional[str]:
    """经 SSH 在目标主机内部 curl 127.0.0.1:port/path,失败返回 None。"""
    m = _ssh_monitor_for_host(host)
    if not m:
        return None
    try:
        key = f"{m['host']}:{m.get('ssh_port', 22)}:{m.get('username', 'root')}"
        out = _ssh_exec(key, m, f"curl -s --max-time {timeout} http://127.0.0.1:{port}{path}")
        out = (out or "").strip()
        # curl 失败时输出为空或错误说明
        if out and not out.startswith("curl:") and len(out) > 10:
            return out
        return None
    except Exception:
        return None

def _ssh_discover_metrics(host: str, api_port: int) -> Optional[Dict[str, Any]]:
    """SSH 进入主机,发现全部监听端口,逐个探测 /metrics(绕过网关,可找到 worker 端口)。
    返回 {port, text} 或 None。"""
    m = _ssh_monitor_for_host(host)
    if not m:
        return None
    try:
        key = f"{m['host']}:{m.get('ssh_port', 22)}:{m.get('username', 'root')}"
        out = _ssh_exec(key, m,
                        "ss -tlnp 2>/dev/null | grep LISTEN | grep -oE ':[0-9]+' | tr -d ':' | sort -un | head -24")
        ports = []
        for p in (out or "").split():
            try:
                p = int(p)
                if p not in ports and 1024 <= p <= 65535:
                    ports.append(p)
            except ValueError:
                continue
        if api_port in ports:
            ports.remove(api_port)
        for p in [api_port] + ports[:10]:
            text = _ssh_curl(host, p, "/metrics", timeout=3)
            if text and re.search(r"(vllm:|sglang:|# HELP|# TYPE)", text):
                return {"port": p, "text": text}
    except Exception:
        return None
    return None

@app.post("/api/server-metrics")
async def server_metrics(req: MetricsRequest):
    """拉取推理服务 /metrics,按框架(vLLM / SGLang 等)适配提取 KV Cache 等关键指标。
    直连失败时若配置了该主机的 SSH 监控,自动经 SSH 通道兜底。"""
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    mhost = re.match(r"https?://([^/:]+)(?::(\d+))?", base)
    host = mhost.group(1) if mhost else ""
    port = int(mhost.group(2)) if (mhost and mhost.group(2)) else 80

    text, source = None, None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/metrics",
                                   timeout=aiohttp.ClientTimeout(total=8), ssl=False) as r:
                if r.status == 200:
                    t = (await r.text()).strip()
                    if len(t) > 10:
                        text, source = t, "direct"
    except Exception:
        pass
    if not text and host:
        found = await asyncio.to_thread(_ssh_discover_metrics, host, port)
        if found:
            text, source = found["text"], f"ssh(:{found['port']})"
    if not text:
        return {"ok": False,
                "message": "/metrics 无法访问(直连被拦截且未配置该主机的 SSH 监控)"
                if host else "/metrics 无法访问"}

    vals = _parse_prometheus(text)
    summary = _metrics_summary(vals)
    has_any = any(v is not None for v in summary["metrics"].values())
    return {
        "ok": has_any,
        "framework": summary["framework"],
        "source": source,
        "message": None if has_any else "已获取 /metrics,但未识别到 vLLM / SGLang 风格指标",
        "metrics": summary["metrics"],
        "metric_count": summary["metric_count"],
    }

class ServerInfoRequest(BaseModel):
    api_url: str
    api_key: str = ""

async def _direct_get(session, url: str, timeout: int = 5) -> Optional[str]:
    """直接 GET,返回非空文本或 None。"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), ssl=False) as r:
            if r.status != 200:
                return None
            t = (await r.text()).strip()
            return t if len(t) > 0 else None
    except Exception:
        return None

def _try_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except Exception:
        return None

@app.post("/api/server-info")
async def server_info(req: ServerInfoRequest):
    """全量服务信息:探测 API 能访问到的所有端点并汇总。
    - /health /version /server_info(vLLM) /get_server_info(SGLang) /v1/models /metrics
    - 直连被网关拦截的端点,若配置了该主机的 SSH 监控,自动经 SSH 通道在主机内部获取。"""
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    mhost = re.match(r"https?://([^/:]+)(?::(\d+))?", base)
    host = mhost.group(1) if mhost else ""
    port = int(mhost.group(2)) if (mhost and mhost.group(2)) else 80

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[
            _direct_get(session, f"{base}/health"),
            _direct_get(session, f"{base}/version"),
            _direct_get(session, f"{base}/server_info"),
            _direct_get(session, f"{base}/get_server_info"),
            _direct_get(session, f"{base}/v1/models"),
            _direct_get(session, f"{base}/metrics", timeout=6),
        ])
    health_t, version_t, vinfo_t, sinfo_t, models_t, metrics_t = results

    sources: Dict[str, str] = {}
    # SSH 兜底:health/version/server_info/get_server_info/metrics
    if host:
        for name, path, got in (("metrics", "/metrics", metrics_t),
                                ("server_info", "/server_info", vinfo_t),
                                ("get_server_info", "/get_server_info", sinfo_t),
                                ("health", "/health", health_t),
                                ("version", "/version", version_t)):
            if got:
                continue
            via_ssh = await asyncio.to_thread(_ssh_curl, host, port, path)
            if via_ssh:
                if name == "metrics":
                    metrics_t = via_ssh
                elif name == "server_info":
                    vinfo_t = via_ssh
                elif name == "get_server_info":
                    sinfo_t = via_ssh
                elif name == "health":
                    health_t = via_ssh
                elif name == "version":
                    version_t = via_ssh
                sources[name] = "ssh"
        # metrics 仍无:端口发现(找 worker 端口)
        if not metrics_t:
            found = await asyncio.to_thread(_ssh_discover_metrics, host, port)
            if found:
                metrics_t = found["text"]
                sources["metrics"] = f"ssh(:{found['port']})"

    vinfo = _try_json(vinfo_t)      # vLLM /server_info
    sinfo = _try_json(sinfo_t)      # SGLang /get_server_info
    version = _try_json(version_t) or (version_t if version_t and len(version_t) < 60 else None)
    models = []
    md = _try_json(models_t)
    if isinstance(md, dict) and isinstance(md.get("data"), list):
        models = [m.get("id", "") for m in md["data"] if isinstance(m, dict)]

    metrics_block = None
    if metrics_t and re.search(r"(vllm:|sglang:|# HELP|# TYPE)", metrics_t):
        summary = _metrics_summary(_parse_prometheus(metrics_t))
        if any(v is not None for v in summary["metrics"].values()) or summary["framework"]:
            metrics_block = summary

    # 框架判定:优先 server_info 字段特征
    framework = (metrics_block or {}).get("framework")
    if not framework:
        if vinfo and any("vllm" in str(v).lower() for v in vinfo.values()):
            framework = "vLLM"
        elif sinfo and any(k in sinfo for k in ("max_total_num_tokens", "schedule_policy", "page_size")):
            framework = "SGLang"

    any_ok = bool(health_t or version or vinfo or sinfo or models or metrics_block)
    return {
        "ok": any_ok,
        "message": None if any_ok else "所有信息端点均不可访问(可尝试在 GPU 监控中配置该主机的 SSH 直连,自动经 SSH 通道获取)",
        "health": (health_t or "")[:20] or None,
        "version": version,
        "framework": framework,
        "server_info": vinfo,           # vLLM 服务配置(全量)
        "sglang_info": sinfo,           # SGLang 服务配置(全量)
        "models": models,
        "metrics": metrics_block,
        "sources": sources,
    }

class KvCacheTestRequest(BaseModel):
    api_url: str
    api_key: str = ""
    protocol: str = "openai"
    model: str
    input_tokens: int = 1024

async def _kv_probe_ttft(session, base: str, protocol: str, api_key: str,
                         model: str, prompt: str) -> float:
    """发一次 max_tokens=1 的流式请求(关闭思维链),返回首事件 TTFT(秒)。"""
    if protocol == "anthropic":
        endpoint = f"{base}/v1/messages"
        headers = {"Content-Type": "application/json", "x-api-key": api_key or "",
                   "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": 1, "stream": True,
                   "thinking": {"type": "disabled"},
                   "messages": [{"role": "user", "content": prompt}]}
    elif protocol == "ollama":
        endpoint = f"{base}/api/chat"
        headers = {"Content-Type": "application/json"}
        payload = {"model": model, "stream": True, "think": False,
                   "messages": [{"role": "user", "content": prompt}],
                   "options": {"num_predict": 1}}
    else:
        endpoint = f"{base}/v1/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}" if api_key else ""}
        payload = {"model": model, "max_tokens": 1, "stream": True,
                   "messages": [{"role": "user", "content": prompt}]}
        # vLLM / Qwen 风格关闭思维链,确保首 token 即正文,不测入思考时间
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    t0 = time.perf_counter()
    first = None
    async with session.post(endpoint, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=120, sock_read=60),
                            ssl=False) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        async for raw in resp.content.iter_chunked(4096):
            buffer += decoder.decode(raw)
            if first is None and ("data:" in buffer or "{" in buffer):
                first = time.perf_counter()
                break   # 只要首事件,立即结束(不再读完剩余流)
    if first is None:
        raise RuntimeError("未收到任何流式响应")
    return first - t0

@app.post("/api/kv-cache-test")
async def kv_cache_test(req: KvCacheTestRequest):
    """KV Cache(前缀缓存)效率实测:同一段长 prompt 连发两次,
    若服务开启 prefix caching,第二次的 prefill(TTFT)应显著快于第一次。"""
    prompt = generate_prompt(max(64, min(req.input_tokens, 32768)), "zh")
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    ttfts = []
    async with aiohttp.ClientSession() as session:
        for i in range(2):
            try:
                ttfts.append(await _kv_probe_ttft(session, base, req.protocol,
                                                  req.api_key, req.model, prompt))
            except Exception as e:
                raise HTTPException(400, f"第 {i + 1} 次请求失败: {e}")
    cold_s, warm_s = ttfts[0], ttfts[1]
    in_toks = estimate_tokens(prompt)
    cold_ms = round(cold_s * 1000, 1)
    warm_ms = round(warm_s * 1000, 1)
    speedup = round(cold_s / warm_s, 2) if warm_s > 0 else None
    cold_prefill = round(in_toks / cold_s, 1) if cold_s > 0 else None
    warm_prefill = round(in_toks / warm_s, 1) if warm_s > 0 else None
    return {
        "ok": True,
        "input_tokens": in_toks,
        "cold": {"ttft_ms": cold_ms, "prefill_tps": cold_prefill},
        "warm": {"ttft_ms": warm_ms, "prefill_tps": warm_prefill},
        "speedup": speedup,
        "cached": bool(speedup and speedup >= 1.5),
        "verdict": ("前缀缓存生效,KV Cache 复用显著加速 prefill" if speedup and speedup >= 1.5
                    else "加速不明显:服务可能未开启 prefix caching,或输入较短/缓存已被逐出"),
    }

class KvSuiteRequest(BaseModel):
    api_url: str
    api_key: str = ""
    protocol: str = "openai"
    model: str
    input_tokens: int = 1024
    mode: str = "multiturn"     # multiturn | shared | eviction
    concurrency: int = 8        # shared 模式并发数

def _distinct_prompt(target_tokens: int, idx: int) -> str:
    """生成内容互不相同、长度相近的 prompt(用于冷启动对照与缓存扰动)。"""
    zh = idx % 2 == 0
    corpus = ZH_CORPUS if zh else EN_CORPUS
    ratio = CHARS_PER_TOKEN_ZH if zh else CHARS_PER_TOKEN_EN
    target_chars = int(target_tokens * ratio)
    start = (idx * 113) % len(corpus)
    text = corpus[start:] + corpus * (target_chars // len(corpus) + 2)
    return text[:target_chars]

async def _vllm_prefix_hits(session, base: str) -> Optional[float]:
    """读取 vLLM /metrics 的 prefix_cache_hits 计数(不可达则 None)。"""
    try:
        async with session.get(f"{base}/metrics",
                               timeout=aiohttp.ClientTimeout(total=5), ssl=False) as r:
            if r.status != 200:
                return None
            return _parse_prometheus(await r.text()).get("vllm:prefix_cache_hits")
    except Exception:
        return None

@app.post("/api/kv-cache-suite")
async def kv_cache_suite(req: KvSuiteRequest):
    """KV Cache 进阶测试套件(业界 prefix caching 基准的通用场景):
    - multiturn 多轮增量:上下文逐轮增长,验证每轮是否只 prefill 新增部分
    - shared 共享前缀并发:同长前缀+不同后缀并发请求,验证并发下命中
    - eviction 缓存逐出:热身后注入扰动长文本,验证容量/LRU 逐出行为
    附带 vLLM /metrics 前缀缓存命中计数差作为客观佐证。"""
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    toks = max(256, min(req.input_tokens, 32768))
    mode = req.mode if req.mode in ("multiturn", "shared", "eviction") else "multiturn"

    async with aiohttp.ClientSession() as session:
        hits_before = await _vllm_prefix_hits(session, base)
        headers: List[str] = []
        rows: List[List[str]] = []
        verdict = ""

        try:
            if mode == "multiturn":
                # 多轮对话:每轮共享前文前缀,只新增尾部内容
                questions = ["\n\n请用一句话总结以上内容。",
                             "\n\n请列出以上内容的三个要点。",
                             "\n\n请基于以上内容写一段点评。"]
                context = generate_prompt(toks, "zh")
                prev_total = 0
                ttfts = []
                for i, q in enumerate(questions):
                    context = context + q if i == 0 else context + f"\n回答:好的,这是第{i}轮回答。\n" + q
                    t = await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                              req.model, context)
                    total = estimate_tokens(context)
                    new = total - prev_total
                    inc_tps = round(new / t, 1) if t > 0 else None
                    rows.append([str(i + 1), str(total), str(new),
                                 f"{round(t * 1000, 1)} ms", str(inc_tps)])
                    ttfts.append(t)
                    prev_total = total
                headers = ["轮次", "上下文tok", "本轮新增tok", "TTFT", "增量prefill tok/s"]
                first, last = ttfts[0], ttfts[-1]
                if last < first * 0.5:
                    verdict = f"✅ 增量缓存生效:第3轮 TTFT({round(last*1000,1)}ms)远低于第1轮({round(first*1000,1)}ms),长上下文续写几乎只 prefill 新增部分"
                elif last < first:
                    verdict = f"⚠ 部分生效:末轮 TTFT({round(last*1000,1)}ms)低于首轮({round(first*1000,1)}ms),但未达数量级提升"
                else:
                    verdict = f"❌ 未见增量缓存:每轮 TTFT 随上下文线性增长({round(first*1000,1)}ms → {round(last*1000,1)}ms),疑似每轮全量 prefill"

            elif mode == "shared":
                # 共享前缀并发:冷启动对照 → 前缀预热 → K 路并发(同前缀+不同短后缀)
                conc = max(2, min(req.concurrency, 32))
                cold_prompt = _distinct_prompt(toks, 97)
                t_cold = await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                              req.model, cold_prompt)
                shared = generate_prompt(toks, "zh")
                await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                     req.model, shared + "\n\n预热问题。")
                suffixes = [f"\n\n请回答问题{i}:" + _distinct_prompt(48, i) for i in range(conc)]
                t_all = await asyncio.gather(*[
                    _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                   req.model, shared + s) for s in suffixes])
                avg = sum(t_all) / len(t_all)
                mx = max(t_all)
                headers = ["输入tok", "并发数", "冷启动TTFT(对照)", "并发平均TTFT", "并发最大TTFT", "加速比(冷/均)"]
                speedup = round(t_cold / avg, 2) if avg > 0 else None
                rows.append([str(toks), str(conc), f"{round(t_cold*1000,1)} ms",
                             f"{round(avg*1000,1)} ms", f"{round(mx*1000,1)} ms", f"{speedup}×"])
                if avg < t_cold * 0.5:
                    verdict = f"✅ 并发命中:平均 TTFT({round(avg*1000,1)}ms)显著低于冷启动({round(t_cold*1000,1)}ms),共享前缀只 prefill 一次,各请求仅计算各自后缀"
                else:
                    verdict = f"❌ 并发未命中:平均 TTFT({round(avg*1000,1)}ms)接近冷启动({round(t_cold*1000,1)}ms),并发请求可能各自全量 prefill 或缓存未开启"

            else:  # eviction
                target = generate_prompt(toks, "zh")
                # 冷对照:同长度不同内容 → 判断前缀缓存是否真的生效,否则逐出测试无意义
                t_cold0 = await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                               req.model, _distinct_prompt(toks, 55))
                await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                     req.model, target)
                t_warm = await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                              req.model, target)
                if t_warm > t_cold0 * 0.67:
                    headers = ["输入tok", "冷启动TTFT", "热身TTFT", "判定"]
                    rows.append([str(toks), f"{round(t_cold0*1000,1)} ms",
                                 f"{round(t_warm*1000,1)} ms", "⚠ 前缀缓存未生效"])
                    verdict = (f"⚠ 热身 TTFT({round(t_warm*1000,1)}ms)与冷启动({round(t_cold0*1000,1)}ms)接近:"
                               "该服务前缀缓存未开启,逐出测试无参考意义(请先看「冷热对比」结果)")
                else:
                    churn_n = 8
                    churn_tok_total = 0
                    for i in range(churn_n):
                        p = _distinct_prompt(toks, i)
                        churn_tok_total += estimate_tokens(p)
                        await _kv_probe_ttft(session, base, req.protocol, req.api_key, req.model, p)
                    t_after = await _kv_probe_ttft(session, base, req.protocol, req.api_key,
                                                   req.model, target)
                    headers = ["输入tok", "热身TTFT", "扰动请求数", "扰动总tok", "逐出后TTFT", "判定"]
                    survived = t_after <= t_warm * 2
                    rows.append([str(toks), f"{round(t_warm*1000,1)} ms", str(churn_n),
                                 str(churn_tok_total), f"{round(t_after*1000,1)} ms",
                                 "✅ 未逐出" if survived else "⚠ 已逐出"])
                    if survived:
                        verdict = f"✅ 缓存容量充足:注入 {churn_tok_total} tok 扰动后目标前缀仍在缓存,TTFT 保持 {round(t_after*1000,1)}ms"
                    else:
                        verdict = f"⚠ 缓存被逐出:扰动后目标前缀丢失,TTFT 从 {round(t_warm*1000,1)}ms 回升到 {round(t_after*1000,1)}ms(KV Cache 容量有限,属正常 LRU 行为)"
        except Exception as e:
            raise HTTPException(400, f"测试失败: {str(e)[:200]}")

        hits_after = await _vllm_prefix_hits(session, base)
        metrics_delta = None
        if hits_before is not None and hits_after is not None:
            metrics_delta = {"prefix_cache_hits": round(hits_after - hits_before, 0)}

    return {"ok": True, "mode": mode, "input_tokens": toks,
            "headers": headers, "rows": rows, "verdict": verdict,
            "metrics_delta": metrics_delta}

# ============================================================================
# Chat Proxy (对话面板:浏览器 -> 本服务 -> 目标 API,避免跨域)
# ============================================================================

async def _chat_sse_stream(req: ChatRequest):
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    messages = [{"role": m.role, "content": m.content} for m in req.history]
    # 系统提示词(Prompt 工程):会话级角色设定,插入历史最前
    if req.system_prompt.strip():
        messages.insert(0, {"role": "system", "content": req.system_prompt.strip()})

    # 联网搜索:模型可用性快检 → 模型提炼多路搜索词 → 多路搜索合并去重 → 注入上下文
    search_results: List[Dict[str, str]] = []
    search_ms = None
    if req.web_search:
        t0 = time.perf_counter()
        async with aiohttp.ClientSession() as session:
            err = await _model_reachable(session, base, req.protocol, req.api_key)
            if err:
                yield sse({"type": "error",
                           "message": f"模型不可用,已取消联网搜索: {err}"})
                return
            # 结合模型语义提炼搜索词(内部已滤指令回声,最多 2 路)
            queries = await _model_gen_queries(session, base, req.protocol, req.api_key,
                                               req.model, req.message)
        # 本地启发式关键词始终作为保底搜索路径,避免模型输出异常时搜索跑偏
        q0 = _build_search_query(req.message) or (req.message or "").strip()[:60]
        if q0 and q0 not in queries:
            queries.append(q0)
        queries = queries[:3]
        yield sse({"type": "searching", "query": " | ".join(queries)})
        # 多路搜索,按 URL 去重合并,条数取请求值或 config 默认(3~20)
        count = max(3, min(req.search_count or _search_cfg()["count"], 20))
        seen, merged = set(), []
        for q in queries:
            if len(merged) >= count:
                break
            for r in await do_web_search(q):
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                merged.append(r)
                if len(merged) >= count:
                    break
        search_results = merged
        search_ms = round((time.perf_counter() - t0) * 1000, 1)
        yield sse({"type": "search", "results": search_results, "ms": search_ms})

    # 组装当前消息:文本附件拼进 prompt,图片/视频/音频附件用多模态格式
    text_parts = []
    img_parts = []
    video_parts = []
    audio_parts = []
    for att in req.attachments or []:
        if att.get("type") == "image":
            img_parts.append({"type": "image_url", "image_url": {"url": att.get("content", "")}})
        elif att.get("type") == "video":
            video_parts.append({"type": "video_url", "video_url": {"url": att.get("content", "")}})
        elif att.get("type") == "audio":
            # OpenAI input_audio 格式(Qwen-Audio / GPT-4o-audio 等语音理解模型)
            data_url = att.get("content", "")
            b64 = data_url.split(",", 1)[-1] if data_url.startswith("data:") else data_url
            fmt = "wav"
            m = re.search(r"data:audio/(\w+)", data_url)
            if m:
                fmt = {"mpeg": "mp3", "mp3": "mp3", "wav": "wav", "wave": "wav",
                       "ogg": "ogg", "flac": "flac", "aac": "aac", "mp4": "mp4",
                       "x-m4a": "mp4", "m4a": "mp4", "webm": "webm",
                       }.get(m.group(1), "wav")
            audio_parts.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
        else:
            text_parts.append(f"【文件: {att.get('name', 'file')}】\n{att.get('content', '')}")
    user_text = req.message
    if text_parts:
        user_text = "\n\n".join(text_parts) + "\n\n" + req.message if req.message else "\n\n".join(text_parts)
    if search_results:
        sources = "\n\n".join(
            f"[{i + 1}] {r['title']}\n{r['url']}\n{r['snippet']}"
            for i, r in enumerate(search_results)
        )
        user_text = ((user_text + "\n\n") if user_text else "") + (
            "【联网搜索结果】以下是与本次问题相关的实时网络资料,回答时请优先参考,"
            "并用 [序号] 标注引用来源;与问题无关的资料可忽略:\n\n" + sources
        )

    if (img_parts or video_parts or audio_parts) and req.protocol == "openai":
        # OpenAI 多模态格式:image_url / video_url / input_audio(Qwen-VL、SGLang、GLM 等通用)
        content = ([{"type": "text", "text": user_text}] if user_text else []) \
            + img_parts + video_parts + audio_parts
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})

    # 采样参数(Prompt 工程面板):仅透传模型通用键,忽略空值
    _GEN_KEYS = ("temperature", "max_tokens", "top_p", "top_k", "frequency_penalty",
                 "presence_penalty", "repetition_penalty", "stop", "seed")
    gen = {k: req.params[k] for k in _GEN_KEYS
           if isinstance(req.params, dict) and req.params.get(k) not in (None, "")}

    if req.protocol == "anthropic":
        endpoint = f"{base}/v1/messages"
        headers = {"Content-Type": "application/json", "x-api-key": req.api_key or "",
                   "anthropic-version": "2023-06-01"}
        payload = {"model": req.model, "max_tokens": 4096, "stream": True, "messages": messages}
        if not req.enable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if "temperature" in gen: payload["temperature"] = gen["temperature"]
        if "top_p" in gen: payload["top_p"] = gen["top_p"]
        if "stop" in gen: payload["stop_sequences"] = gen["stop"] if isinstance(gen["stop"], list) else [gen["stop"]]
        if "max_tokens" in gen: payload["max_tokens"] = gen["max_tokens"]
    elif req.protocol == "ollama":
        endpoint = f"{base}/api/chat"
        headers = {"Content-Type": "application/json"}
        payload = {"model": req.model, "stream": True, "messages": messages,
                   "think": req.enable_thinking}
        opts = {}
        for k in ("temperature", "top_p", "top_k", "seed", "stop"):
            if k in gen: opts[k if k != "stop" else "stop"] = gen[k]
        if "max_tokens" in gen: opts["num_predict"] = gen["max_tokens"]
        if opts: payload["options"] = opts
    else:
        endpoint = f"{base}/v1/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {req.api_key}" if req.api_key else ""}
        payload = {"model": req.model, "stream": True, "messages": messages}
        # SGLang / vLLM / Qwen 风格开关,不支持的服务会忽略未知字段
        if not req.enable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload.update(gen)

    t_start = time.perf_counter()
    t_first = None
    full_text = ""
    full_think = ""

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(endpoint, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=300, sock_read=120),
                                    ssl=False) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    yield sse({"type": "error", "message": f"HTTP {resp.status}: {body[:300]}"})
                    return
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" not in ctype and req.protocol != "ollama":
                    # 非流式回退
                    body = await resp.text()
                    data = json.loads(body)
                    if req.protocol == "anthropic":
                        text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
                        think = "".join(b.get("thinking", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "thinking")
                    else:
                        ch = data.get("choices") or []
                        msg = (ch[0].get("message") or {}) if ch else {}
                        text = msg.get("content", "") or ""
                        think = msg.get("reasoning_content", "") or ""
                    # 内联 <think> 标签拆出
                    m = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
                    if m and not think:
                        think = m.group(1).strip()
                        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                    full_text = text
                    full_think = think
                    t_first = time.perf_counter()
                    if think:
                        yield sse({"type": "think", "content": think})
                    yield sse({"type": "content", "content": text})
                else:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    buffer = ""
                    inline_think_open = False
                    async for raw in resp.content.iter_chunked(4096):
                        buffer += decoder.decode(raw)
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            data = None
                            if line.startswith("data:"):
                                ds = line[5:].strip()
                                if ds in ("[DONE]", "done"):
                                    continue
                                data = ProtocolDetector._json(ds)
                            elif req.protocol == "ollama" and line.startswith("{"):
                                data = ProtocolDetector._json(line)
                            if not data:
                                continue
                            piece = None
                            think_piece = None
                            if req.protocol == "anthropic":
                                delta = data.get("delta") or {}
                                if delta.get("type") == "thinking_delta" or data.get("type") == "content_block_delta" and delta.get("thinking"):
                                    think_piece = delta.get("thinking", "")
                                else:
                                    piece = delta.get("text", "")
                            elif req.protocol == "ollama":
                                msg = data.get("message") or {}
                                think_piece = msg.get("thinking", "")
                                piece = msg.get("content", "")
                            else:
                                ch = data.get("choices") or []
                                if ch:
                                    delta = ch[0].get("delta") or {}
                                    piece = delta.get("content", "")
                                    think_piece = (delta.get("reasoning_content")
                                                   or delta.get("reasoning") or "")
                            # 内联 <think> 标签处理(openai 风格内容里)
                            if piece and "<think>" in piece:
                                inline_think_open = True
                                before, _, after = piece.partition("<think>")
                                piece = before
                                think_piece = (think_piece or "") + after
                            if inline_think_open and piece and "</think>" in piece:
                                inline_think_open = False
                                before, _, after = piece.partition("</think>")
                                think_piece = (think_piece or "") + before
                                piece = after
                            elif inline_think_open and piece:
                                think_piece = (think_piece or "") + piece
                                piece = None

                            if think_piece:
                                full_think += think_piece
                                yield sse({"type": "think", "content": think_piece})
                            if piece:
                                if t_first is None:
                                    t_first = time.perf_counter()
                                full_text += piece
                                yield sse({"type": "content", "content": piece})
        except Exception as e:
            yield sse({"type": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"})
            return

    t_end = time.perf_counter()
    out_tokens = estimate_tokens(full_text)
    decode_s = (t_end - t_first) if t_first else None
    yield sse({"type": "metrics", "data": {
        "ttft_ms": round((t_first - t_start) * 1000, 1) if t_first else None,
        "duration_s": round(t_end - t_start, 2),
        "output_tokens": out_tokens,
        "think_tokens": estimate_tokens(full_think) if full_think else 0,
        "decode_tps": round(out_tokens / decode_s, 2) if decode_s and decode_s > 0 else None,
        "search_ms": search_ms,
    }})
    yield sse({"type": "done"})

def _tts_ext(fmt: str) -> str:
    return {"mp3": ".mp3", "wav": ".wav", "opus": ".opus", "aac": ".aac",
            "flac": ".flac", "pcm": ".pcm"}.get((fmt or "mp3").lower(), ".mp3")

def _tts_mime(fmt: str) -> str:
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg",
            "aac": "audio/aac", "flac": "audio/flac",
            "pcm": "audio/pcm"}.get((fmt or "mp3").lower(), "audio/mpeg")

async def _run_tts(req: ChatRequest) -> Dict[str, Any]:
    """语音合成:POST {base}/v1/audio/speech,产物落盘 generated_media 并返回回放地址。"""
    import base64 as _b64
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    fmt = (req.tts_format or "mp3").lower()
    payload = {"model": req.model, "input": req.message,
               "voice": req.tts_voice or "alloy", "response_format": fmt}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {req.api_key}" if req.api_key else ""}
    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=600, sock_read=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base}/v1/audio/speech", json=payload,
                                headers=headers, ssl=False) as resp:
            body = await resp.read()
            if resp.status >= 400:
                raise HTTPException(502, f"HTTP {resp.status}: {body[:300].decode('utf-8', 'replace')}")
            ctype = resp.headers.get("Content-Type", "")
            if not (ctype.startswith("audio/") or ctype == "application/octet-stream" or not ctype):
                raise HTTPException(502, f"上游未返回音频(Content-Type: {ctype}): {body[:200].decode('utf-8', 'replace')}")
    name = f"tts_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{_tts_ext(fmt)}"
    dest = os.path.join(_media_dir(), name)
    with open(dest, "wb") as f:
        f.write(body)
    return {"type": "audio", "url": f"(语音合成 {len(body)} 字节)",
            "text": req.message, "latency_s": round(time.perf_counter() - t0, 2),
            "media_url": f"/api/media/{name}", "media_name": name,
            "media_size": len(body), "media_mime": _tts_mime(fmt)}

async def _run_stt(req: ChatRequest) -> Dict[str, Any]:
    """语音识别:音频附件转发到 {base}/v1/audio/transcriptions(multipart)。"""
    import base64 as _b64
    audios = [a for a in (req.attachments or []) if a.get("type") == "audio"]
    if not audios:
        raise HTTPException(400, "语音识别需要至少一个音频附件(🎤 上传或录制)")
    base = re.sub(r"/v1/?$", "", ProtocolDetector._normalize(req.api_url))
    headers = {"Authorization": f"Bearer {req.api_key}" if req.api_key else ""}
    t0 = time.perf_counter()
    texts = []
    timeout = aiohttp.ClientTimeout(total=600, sock_read=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for att in audios:
            data_url = att.get("content", "")
            raw = _b64.b64decode(data_url.split(",", 1)[-1]) if data_url.startswith("data:") else _b64.b64decode(data_url)
            fname = att.get("name") or "audio.mp3"
            form = aiohttp.FormData()
            form.add_field("file", raw, filename=fname,
                           content_type="application/octet-stream")
            form.add_field("model", req.model)
            if req.message:
                form.add_field("language", req.message.strip()[:8])  # 可选语言提示
            async with session.post(f"{base}/v1/audio/transcriptions",
                                    data=form, headers=headers, ssl=False) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise HTTPException(502, f"HTTP {resp.status}: {body[:300]}")
                data = json.loads(body)
                texts.append(str(data.get("text") or data.get("content") or ""))
    text = "\n".join(t for t in texts if t)
    if not text:
        raise HTTPException(502, "上游未返回识别文本")
    return {"type": "transcription", "text": text,
            "latency_s": round(time.perf_counter() - t0, 2)}

@app.post("/api/chat")
async def chat_proxy(req: ChatRequest):
    # 预览账号:不使用页面配置,统一走管理员在共享网关配置的渠道(服务端注入,密钥不外露)
    if _cur_role() == "viewer":
        if (req.task_type or "chat") != "chat":
            raise HTTPException(403, "预览账号仅可使用文本对话")
        ch = _gw_best_chat_channel()
        if not ch:
            raise HTTPException(400, "管理员尚未在「模型工作台 · 网关渠道」配置可用渠道,暂无法使用对话")
        req.api_url = ch.get("base_url", "")
        req.api_key = ch.get("api_key", "")
        req.protocol = ch.get("protocol") or "openai"
        if not (req.model or "").strip():
            models = [m for m in (ch.get("models") or []) if isinstance(m, str)]
            req.model = models[0] if models else ""
    if not req.api_url or not req.model:
        raise HTTPException(400, "缺少 api_url / model")
    if req.task_type == "chat":
        if not req.message and not req.attachments:
            raise HTTPException(400, "缺少 message")
        return StreamingResponse(_chat_sse_stream(req), media_type="text/event-stream")

    # 文生图 / 文生视频 / 图生视频:同步返回;产物自动取回本地(窗口内展示/播放 + 下载)
    timeout = aiohttp.ClientTimeout(total=1900, sock_read=120)
    if req.task_type == "image":
        if not req.message:
            raise HTTPException(400, "缺少 prompt")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            r = await run_single_image(session, req.api_url, req.api_key, req.model,
                                       req.message, 0, fetch_media=True)
            if r["success"]:
                return {"type": "image", "url": r["output_text"],
                        "latency_s": r["total_latency_s"], **_media_fields(r.get("media"))}
            raise HTTPException(502, r["error"] or "图片生成失败")
    elif req.task_type in ("video", "image_video", "video_regen"):
        if not req.message and req.task_type in ("video",):
            raise HTTPException(400, "缺少 prompt")
        atts = req.attachments or []
        frames: List[tuple] = []       # 首帧/尾帧(图生视频)
        references: List[tuple] = []   # 参考图/视频/音频(多模态参考生视频)
        if req.task_type == "image_video":
            imgs = [a for a in atts if a.get("type") == "image"]
            if not imgs:
                raise HTTPException(400, "图生视频需要 📎 上传首帧图(可再传一张作尾帧)")
            frames.append(("first_frame", imgs[0].get("content", "")))
            if len(imgs) > 1:
                frames.append(("last_frame", imgs[1].get("content", "")))
        elif req.task_type == "video":
            for a in atts:             # 文生视频 + 参考媒体 = 多模态参考生视频(r2va)
                if a.get("type") == "image":
                    references.append(("reference_image", a.get("content", "")))
                elif a.get("type") == "video":
                    references.append(("reference_video", a.get("content", "")))
                elif a.get("type") == "audio":
                    references.append(("reference_audio", a.get("content", "")))
        base_video = ""
        if req.task_type == "video_regen":
            if not req.regen_task_id and not req.regen_source:
                raise HTTPException(400, "视频再生成需要源任务 id 或源视频")
            if req.regen_source:
                # 本地 /api/media/<name> 源视频 → data URI 作 base_video
                import base64 as _b64
                m = re.match(r"/api/media/([A-Za-z0-9._-]+)", req.regen_source)
                if not m:
                    raise HTTPException(400, "regen_source 仅支持 /api/media/ 本地文件")
                src = os.path.join(_media_dir(), m.group(1))
                if not os.path.isfile(src):
                    raise HTTPException(404, "源视频文件不存在(可能已被清理)")
                with open(src, "rb") as f:
                    base_video = ("data:video/mp4;base64,"
                                  + _b64.b64encode(f.read()).decode())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            r = await run_single_video(session, req.api_url, req.api_key, req.model,
                                       req.message, 0, fetch_media=True,
                                       duration=req.video_duration,
                                       resolution=req.video_resolution,
                                       ratio=req.video_ratio,
                                       api_pref=req.video_api,
                                       frames=frames, references=references,
                                       regen_task_id=req.regen_task_id,
                                       base_video=base_video)
            if r["success"]:
                return {"type": "video", "url": r["output_text"],
                        "latency_s": r["total_latency_s"],
                        "job_id": r.get("job_id") or "",
                        "style": r.get("style") or "",
                        **_media_fields(r.get("media"))}
            raise HTTPException(502, r["error"] or "视频生成失败")
    elif req.task_type == "tts":
        if not req.message:
            raise HTTPException(400, "语音合成需要输入文本")
        return await _run_tts(req)
    elif req.task_type == "stt":
        return await _run_stt(req)
    else:
        raise HTTPException(400, f"未知任务类型: {req.task_type}")

# ============================================================================
# Generated Media(文生视频产物取回本地,页面内播放 + 下载)
# ============================================================================

MEDIA_DIR = os.path.join(BASE_DIR, "generated_media")   # 旧版全局目录(启动时迁入 admin)

def _media_dir() -> str:
    """生成媒体目录(按账号隔离):动漫工坊片段 / TTS 语音等留痕文件。"""
    d = os.path.join(_udir(), "generated_media")
    os.makedirs(d, exist_ok=True)
    return d

_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".aac": "audio/aac", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".wma": "audio/x-ms-wma", ".amr": "audio/amr",
    ".pcm": "audio/pcm",
}

def _media_ext(url: str, dflt: str) -> str:
    m = re.search(r"\.(mp4|m4v|mov|webm|mkv|avi|png|jpe?g|webp|gif)(?:[?#]|$)", (url or "").lower())
    return "." + m.group(1) if m else dflt

def _media_dest(url: str, kind: str) -> tuple:
    """生成本地落盘文件名:video_时间戳_随机串.ext"""
    dflt = ".mp4" if kind == "video" else ".png"
    name = f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{_media_ext(url, dflt)}"
    return name, os.path.join(_media_dir(), name)

async def _http_save(session: aiohttp.ClientSession, url: str, dest: str,
                     headers: Optional[Dict[str, str]] = None) -> int:
    """流式下载 url 到 dest,返回字节数;失败抛异常。"""
    async with session.get(url, headers=headers or {}, ssl=False,
                           timeout=aiohttp.ClientTimeout(total=600, sock_read=180)) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
        n = 0
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 20):
                f.write(chunk)
                n += len(chunk)
        if n == 0:
            raise RuntimeError("响应内容为空")
        return n

def _sniff_media_ext(head: bytes, dflt: str) -> str:
    """按文件头魔数判断扩展名(b64 产物没有文件名)。"""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    return dflt

def _ssh_sftp_fetch(host: str, remote_path: str, dest: str) -> None:
    """经已配置的 SSH 直连监控,把 GPU 服务器上的生成文件拉到本地。"""
    m = _ssh_monitor_for_host(host)
    if not m:
        raise RuntimeError(f"主机 {host} 未配置 SSH 直连监控,无法取回服务器内部文件")
    key = f"{m['host']}:{m.get('ssh_port', 22)}:{m.get('username', 'root')}"
    import paramiko
    for attempt in range(2):
        try:
            c = _ssh_client(key, m, fresh=(attempt == 1))
            sftp = c.open_sftp()
            try:
                sftp.get(remote_path, dest)
            finally:
                sftp.close()
            if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
                raise RuntimeError("远端文件为空或不存在")
            return
        except RuntimeError:
            raise
        except Exception as e:
            if attempt == 1:
                raise RuntimeError(f"SSH 取回失败: {type(e).__name__}: {str(e)[:120]}")

async def fetch_generated_media(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    kind: str = "video",
    url: str = "",
    b64: str = "",
    file_path: str = "",
    file_id: str = "",
    job_id: str = "",
    style: str = "",
) -> Dict[str, Any]:
    """文生图/文生视频产物统一取回到本地 generated_media/,供页面内展示播放与下载。

    渠道按优先级:
    1) b64 产物(文生图常见)→ 直接解码落盘;
    2) http(s) 链接(云存储/官方云)→ 直接下载;
    3) 相对路径 url(SGLang /v1/images/{id}/content)→ 拼 base 下载;
    4) SGLang 视频:GET {base}/v1/videos/{job_id}/content;
    5) MiniMax 云:file_id 经 /v1/files/retrieve 换 download_url;
    6) 服务器内部路径:该主机已配置 SSH 直连监控时 SFTP 拉回。
    成功返回 {"name","size","source"},全部失败返回 {"error": 原因}。
    """
    import base64 as _b64mod
    url, b64, file_path = str(url or ""), str(b64 or ""), str(file_path or "")
    host = re.sub(r"^https?://", "", base or "").split("/")[0].split(":")[0]
    errs = []

    def auth_for(target: str) -> Dict[str, str]:
        # 鉴权头只发给被测 API 本身,第三方下载链接(如预签名 OSS)不带
        if api_key and target.startswith(base.rstrip("/")):
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    # 1) b64 产物直接解码
    if b64:
        try:
            raw = _b64mod.b64decode(b64)
            if not raw:
                raise RuntimeError("b64 内容为空")
            name = f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}" \
                   + _sniff_media_ext(raw[:16], ".png" if kind == "image" else ".mp4")
            dest = os.path.join(_media_dir(), name)
            with open(dest, "wb") as f:
                f.write(raw)
            return {"name": name, "size": len(raw), "source": "b64"}
        except Exception as e:
            errs.append(f"b64 解码失败: {e}")

    # 2) 绝对 http 链接 / 3) 相对路径(SGLang content 端点)
    if url.startswith(("http://", "https://")) or url.startswith("/"):
        try:
            full = url if url.startswith(("http://", "https://")) else f"{base}{url}"
            name, dest = _media_dest(full, kind)
            return {"name": name, "size": await _http_save(session, full, dest, auth_for(full)),
                    "source": "url"}
        except Exception as e:
            errs.append(f"链接下载失败: {e}")

    # 4) SGLang /v1/videos:{job_id}/content 端点返回视频文件
    if kind == "video" and style == "videos" and job_id:
        try:
            full = f"{base}/v1/videos/{job_id}/content"
            name, dest = _media_dest(full, kind)
            return {"name": name, "size": await _http_save(session, full, dest, auth_for(full)),
                    "source": "content"}
        except Exception as e:
            errs.append(f"content 端点下载失败: {e}")

    # 5) MiniMax 云:file_id 经 /v1/files/retrieve 换 download_url
    if file_id:
        try:
            async with session.get(f"{base}/v1/files/retrieve", params={"file_id": file_id},
                                   headers=auth_for(base), ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status < 400:
                    fobj = ((await resp.json(content_type=None)) or {}).get("file") or {}
                    dl = fobj.get("download_url") or ""
                    if dl:
                        name, dest = _media_dest(dl, kind)
                        return {"name": name, "size": await _http_save(session, dl, dest, auth_for(dl)),
                                "source": "files"}
        except Exception as e:
            errs.append(f"files/retrieve 失败: {e}")

    # 6) 服务器内部路径:经 SSH 直连监控 SFTP 拉回
    if file_path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", file_path):
        try:
            name, dest = _media_dest(file_path, kind)
            await asyncio.to_thread(_ssh_sftp_fetch, host, file_path, dest)
            return {"name": name, "size": os.path.getsize(dest), "source": "ssh"}
        except Exception as e:
            errs.append(str(e))

    return {"error": " ; ".join(errs)[:300] or "产物无 URL 且服务器未配置 SSH 直连监控,无法取回"}

def _media_fields(media: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """对话响应中的媒体字段:取回成功给本地回放地址,失败给原因。"""
    if not media:
        return {}
    if media.get("name"):
        return {"media_url": f"/api/media/{media['name']}",
                "media_name": media["name"],
                "media_size": media.get("size") or 0}
    if media.get("error"):
        return {"media_error": str(media["error"])[:300]}
    return {}

@app.delete("/api/media/{name}")
async def delete_media(name: str):
    """删除本地媒体文件(片段库「🗑」按钮,服务端文件一并删除)。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise HTTPException(400, "非法文件名")
    path = os.path.join(_media_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在(可能已被清理)")
    try:
        os.remove(path)
    except OSError as e:
        raise HTTPException(500, f"删除失败:{e}")
    return {"ok": True, "name": name}


@app.get("/api/media/list")
async def media_list(kind: str = ""):
    """generated_media 列表(动漫工坊片段库 / 已生成媒体浏览)。kind=video|image|audio 过滤。
    注意:必须注册在 /api/media/{name} 之前,否则 list 会被当作 {name} 拦截。"""
    items = []
    for fn in os.listdir(_media_dir()):
        path = os.path.join(_media_dir(), fn)
        if not os.path.isfile(path) or fn.startswith("."):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext in (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"):
            cat = "video"
        elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            cat = "image"
        elif ext in (".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".m4a", ".wma", ".amr"):
            cat = "audio"
        else:
            continue
        if kind and cat != kind:
            continue
        st = os.stat(path)
        items.append({"name": fn, "url": f"/api/media/{fn}", "kind": cat,
                      "size": st.st_size,
                      "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}


@app.api_route("/api/media/{name}", methods=["GET", "HEAD"])
async def get_media(name: str, request: Request, download: int = 0):
    """本地生成媒体回放:支持 Range 请求(视频进度条拖动),?download=1 触发浏览器下载。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise HTTPException(404, "文件不存在")
    path = os.path.join(_media_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在(可能已被清理)")
    size = os.path.getsize(path)
    media_type = _MEDIA_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")

    if request.method == "HEAD":
        return Response(status_code=200, headers={
            "Content-Type": media_type, "Content-Length": str(size),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"',
        })

    range_hdr = request.headers.get("range", "").strip()
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_hdr)
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:                      # bytes=-N:取末尾 N 字节
            start = max(size - int(m.group(2)), 0)
            end = size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

        def _chunks():
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = f.read(min(left, 1 << 20))
                    if not buf:
                        break
                    left -= len(buf)
                    yield buf
        return StreamingResponse(
            _chunks(), status_code=206, media_type=media_type,
            headers={"Content-Range": f"bytes {start}-{end}/{size}",
                     "Accept-Ranges": "bytes",
                     "Content-Disposition": f'inline; filename="{name}"'})

    if download:
        return FileResponse(path, media_type=media_type, filename=name)
    return FileResponse(path, media_type=media_type,
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Disposition": f'inline; filename="{name}"'})

# ============================================================================
# 动漫工坊(Anime Studio):ffmpeg 剪辑合成
# ============================================================================

def _find_ffmpeg() -> str:
    """ffmpeg 可执行路径:PATH 优先,其次 imageio-ffmpeg 包自带的二进制。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    raise HTTPException(400, "未找到 ffmpeg:请安装 ffmpeg 并加入 PATH,或 pip install imageio-ffmpeg")


def _ff_run(exe: str, args: List[str], timeout: int = 600) -> None:
    """运行 ffmpeg,非零退出码抛出含 stderr 摘要的异常。"""
    r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 退出码 {r.returncode}: {(r.stderr or '').strip()[:300]}")


def _ff_info(exe: str, path: str) -> Dict[str, Any]:
    """解析 ffmpeg -i 的 stderr,取时长/分辨率/是否有音轨。"""
    r = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    info = r.stderr or ""
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    dim = None
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", info)
    if m:
        dim = (int(m.group(1)), int(m.group(2)))
    return {"duration": dur, "dim": dim, "has_audio": "Audio:" in info}


def _compose_sync(clips: List[Dict[str, Any]], fade: bool, resolution: str) -> Dict[str, Any]:
    """同步合成:逐段裁剪(归一化分辨率/帧率/音轨)→ concat 拼接 → 落盘 generated_media。"""
    exe = _find_ffmpeg()
    tmp = os.path.join(_media_dir(), ".studio_tmp")
    os.makedirs(tmp, exist_ok=True)
    try:
        # 目标分辨率:orig = 跟随第一段
        tgt = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(resolution.lower())
        if tgt is None:
            first_info = _ff_info(exe, os.path.join(_media_dir(), clips[0]["name"]))
            if not first_info["dim"]:
                raise RuntimeError("无法识别第一段视频分辨率")
            tgt = first_info["dim"]
        w, h = tgt

        seg_files: List[str] = []
        total = 0.0
        for i, c in enumerate(clips):
            name = c["name"]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                raise RuntimeError(f"非法文件名: {name}")
            src = os.path.join(_media_dir(), name)
            if not os.path.isfile(src):
                raise RuntimeError(f"片段不存在: {name}")
            info = _ff_info(exe, src)
            dur = info["duration"] or 0
            start = max(0.0, float(c.get("start") or 0))
            end = float(c.get("end") or 0)
            if dur:
                end = min(end, dur)
            if end - start < 0.2:
                raise RuntimeError(f"片段「{name}」起止无效(需 ≥0.2s): {start:.1f}~{end:.1f}s")
            seg_len = end - start
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
            if fade and i == 0:
                vf += ",fade=t=in:st=0:d=0.5"
            if fade and i == len(clips) - 1:
                vf += f",fade=t=out:st={max(0, seg_len - 0.5):.2f}:d=0.5"
            seg = os.path.join(tmp, f"seg_{i:03d}.mp4")
            args = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src]
            if info["has_audio"]:
                args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                         "-c:a", "aac", "-ar", "44100", "-ac", "2"]
            else:   # 无音轨补静音,保证各段流结构一致可 concat
                args += ["-f", "lavfi", "-t", f"{seg_len + 1:.2f}", "-i", "anullsrc=r=44100:cl=stereo",
                         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest"]
            args.append(seg)
            _ff_run(exe, args)
            seg_files.append(seg)
            total += seg_len

        # concat 拼接(全部段已归一化,流复制即可)
        out_name = f"anime_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4"
        out = os.path.join(_media_dir(), out_name)
        if len(seg_files) == 1:
            shutil.copyfile(seg_files[0], out)
        else:
            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for s in seg_files:
                    f.write("file '" + s.replace("\\", "/") + "'\n")
            _ff_run(exe, ["-f", "concat", "-safe", "0", "-i", lst,
                          "-c", "copy", "-movflags", "+faststart", out])
        return {"name": out_name, "url": f"/api/media/{out_name}",
                "size": os.path.getsize(out), "duration": round(total, 2)}
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


@app.post("/api/studio/compose")
async def studio_compose(body: Dict[str, Any]):
    """动漫工坊合成:clips=[{name,start,end}] 按顺序裁剪拼接,fade=首尾淡入淡出。"""
    clips = body.get("clips") or []
    if not clips:
        raise HTTPException(400, "时间线为空:请先从右侧片段库加入要剪辑的片段")
    if len(clips) > 30:
        raise HTTPException(400, "片段过多(单次最多 30 段)")
    fade = bool(body.get("fade", False))
    resolution = str(body.get("resolution") or "orig")
    loop = asyncio.get_event_loop()
    try:
        r = await loop.run_in_executor(None, lambda: _compose_sync(clips, fade, resolution))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"合成失败: {e}")
    return {"ok": True, **r}

# ============================================================================
# ModelUse 数据库(SQLite,项目目录下 modeluse.db)
# 本页面全部数据统一入库:会话/消息、网关渠道/密钥/日志/统计、技能与提示词库。
# 首次启动自动把旧 JSON(chat_sessions/gateway_config/modeluse_library)迁移入库,
# 迁移成功后原文件改名为 *.bak 保留备份。
# ============================================================================

import sqlite3

DB_PATH_OLD = os.path.join(BASE_DIR, "modeluse.db")   # 旧版全局库(启用账户体系时迁入 admin)
_DB_READY: set = set()

def _db_path() -> str:
    """按当前账号解析 SQLite 路径:每账号独立库(会话/渠道/密钥/网关日志/提示词技能)。"""
    return os.path.join(_udir(), "modeluse.db")

def _db_conn() -> sqlite3.Connection:
    p = _db_path()
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    u = _cur_user()
    if u not in _DB_READY:                     # 新账号首次访问:建表
        conn.executescript(_DB_SCHEMA)
        conn.commit()
        _DB_READY.add(u)
    return conn

# ---- 网关共享库:渠道/密钥/日志/统计为全站共享基础设施(总管理员与各管理员共用一套) ----
GW_SHARED_DIR = os.path.join(USER_DATA_DIR, "_shared")
GW_DB_PATH = os.path.join(GW_SHARED_DIR, "gateway.db")
_GW_DB_READY = False

_GW_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT DEFAULT '',
  protocol TEXT DEFAULT 'openai', types TEXT DEFAULT '["chat"]', models TEXT DEFAULT '[]',
  priority INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, note TEXT DEFAULT '',
  created_at TEXT DEFAULT '', used INTEGER DEFAULT 0,
  last_latency_ms REAL, last_probe_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS gw_keys(
  key TEXT PRIMARY KEY, name TEXT, allowed_channels TEXT DEFAULT '[]',
  allowed_models TEXT DEFAULT '[]', quota INTEGER DEFAULT 0,
  expires_at TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  used INTEGER DEFAULT 0, created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS gateway_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, channel TEXT,
  model TEXT, endpoint TEXT, status INTEGER, latency_ms REAL,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

def _gw_conn() -> sqlite3.Connection:
    """共享网关库连接:与账号个人库分开,渠道/密钥对管理员统一可见。"""
    global _GW_DB_READY
    os.makedirs(GW_SHARED_DIR, exist_ok=True)
    conn = sqlite3.connect(GW_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _GW_DB_READY:
        conn.executescript(_GW_DB_SCHEMA)
        conn.commit()
        _GW_DB_READY = True
    return conn
CHAT_SESSIONS_PATH = os.path.join(BASE_DIR, "chat_sessions.json")        # 旧版(迁移源)
GATEWAY_CONFIG_PATH = os.path.join(BASE_DIR, "gateway_config.json")      # 旧版(迁移源)
MODELUSE_LIB_PATH = os.path.join(BASE_DIR, "modeluse_library.json")      # 旧版(迁移源)
_DB_LOCK = threading.Lock()

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, title TEXT, archived INTEGER DEFAULT 0, project TEXT DEFAULT '',
  created_at TEXT DEFAULT '', system_prompt TEXT DEFAULT '', params TEXT DEFAULT '{}',
  data_source TEXT DEFAULT '', model TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, seq INTEGER,
  role TEXT, content TEXT DEFAULT '', extra TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT DEFAULT '',
  protocol TEXT DEFAULT 'openai', types TEXT DEFAULT '["chat"]', models TEXT DEFAULT '[]',
  priority INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, note TEXT DEFAULT '',
  created_at TEXT DEFAULT '', used INTEGER DEFAULT 0,
  last_latency_ms REAL, last_probe_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS gw_keys(
  key TEXT PRIMARY KEY, name TEXT, allowed_channels TEXT DEFAULT '[]',
  allowed_models TEXT DEFAULT '[]', quota INTEGER DEFAULT 0,
  expires_at TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  used INTEGER DEFAULT 0, created_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS gateway_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, channel TEXT,
  model TEXT, endpoint TEXT, status INTEGER, latency_ms REAL,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS library(
  id TEXT PRIMARY KEY, kind TEXT, icon TEXT DEFAULT '', name TEXT,
  description TEXT DEFAULT '', category TEXT DEFAULT '',
  variables TEXT DEFAULT '[]', content TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_messages_sid ON messages(session_id, seq);
"""

def _bak(path: str) -> None:
    try:
        os.replace(path, path + ".bak")
    except Exception:
        pass

def _db_init_and_migrate() -> None:
    """建表 + 旧 JSON 一次性迁移(仅当对应表为空且旧文件存在)。"""
    with _DB_LOCK:
        conn = _db_conn()
        try:
            conn.executescript(_DB_SCHEMA)
            # 会话
            if conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0 \
                    and os.path.exists(CHAT_SESSIONS_PATH):
                try:
                    with open(CHAT_SESSIONS_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list) and data:
                        for s in data:
                            _db_upsert_session(conn, s)
                        conn.commit()
                        print(f"[DB] 已迁移 {len(data)} 个会话 chat_sessions.json → modeluse.db")
                except Exception as e:
                    print(f"[DB] 会话迁移失败(保留原 JSON): {e}")
                else:
                    _bak(CHAT_SESSIONS_PATH)
            # 网关
            if conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0 \
                    and os.path.exists(GATEWAY_CONFIG_PATH):
                try:
                    with open(GATEWAY_CONFIG_PATH, encoding="utf-8") as f:
                        cfg = json.load(f)
                    for c in cfg.get("channels") or []:
                        conn.execute(
                            "INSERT OR REPLACE INTO channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c.get("id"), c.get("name"), c.get("base_url"), c.get("api_key", ""),
                             c.get("protocol", "openai"), json.dumps(c.get("types") or ["chat"], ensure_ascii=False),
                             json.dumps(c.get("models") or [], ensure_ascii=False), int(c.get("priority") or 0),
                             1 if c.get("enabled", True) else 0, c.get("note", ""), c.get("created_at", ""),
                             int(c.get("used") or 0), c.get("last_latency_ms"), c.get("last_probe_at", "")))
                    for k in cfg.get("keys") or []:
                        conn.execute(
                            "INSERT OR REPLACE INTO gw_keys VALUES(?,?,?,?,?,?,?,?,?)",
                            (k.get("key"), k.get("name"), json.dumps(k.get("allowed_channels") or [], ensure_ascii=False),
                             json.dumps(k.get("allowed_models") or [], ensure_ascii=False), int(k.get("quota") or 0),
                             k.get("expires_at", ""), 1 if k.get("enabled", True) else 0,
                             int(k.get("used") or 0), k.get("created_at", "")))
                    for g in cfg.get("logs") or []:
                        conn.execute(
                            "INSERT INTO gateway_logs(ts,key,channel,model,endpoint,status,latency_ms,tokens_in,tokens_out)"
                            " VALUES(?,?,?,?,?,?,?,?,?)",
                            (g.get("ts", ""), g.get("key", ""), g.get("channel", ""), g.get("model", ""),
                             g.get("endpoint", ""), int(g.get("status") or 0), float(g.get("latency_ms") or 0),
                             int(g.get("tokens_in") or 0), int(g.get("tokens_out") or 0)))
                    stats = cfg.get("stats") or {}
                    if stats:
                        conn.execute("INSERT OR REPLACE INTO kv VALUES('gateway_stats',?)",
                                     (json.dumps(stats, ensure_ascii=False),))
                    conn.commit()
                    print(f"[DB] 已迁移网关配置(渠道 {len(cfg.get('channels') or [])} / 密钥 {len(cfg.get('keys') or [])}"
                          f" / 日志 {len(cfg.get('logs') or [])}) → modeluse.db")
                except Exception as e:
                    print(f"[DB] 网关迁移失败(保留原 JSON): {e}")
                else:
                    _bak(GATEWAY_CONFIG_PATH)
            # 技能/提示词库
            if conn.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 0 \
                    and os.path.exists(MODELUSE_LIB_PATH):
                try:
                    with open(MODELUSE_LIB_PATH, encoding="utf-8") as f:
                        lib = json.load(f)
                    n = _db_write_lib(conn, lib)
                    conn.commit()
                    print(f"[DB] 已迁移技能/提示词库 {n} 项 → modeluse.db")
                except Exception as e:
                    print(f"[DB] 技能库迁移失败(保留原 JSON): {e}")
                else:
                    _bak(MODELUSE_LIB_PATH)
        finally:
            conn.close()

def _db_upsert_session(conn: sqlite3.Connection, s: Dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                 (s.get("id"), s.get("title", ""), 1 if s.get("archived") else 0,
                  s.get("project", ""), s.get("created_at", ""), s.get("system_prompt", ""),
                  json.dumps(s.get("params") or {}, ensure_ascii=False),
                  s.get("data_source", ""), s.get("model", "")))
    conn.execute("DELETE FROM messages WHERE session_id=?", (s.get("id"),))
    for i, m in enumerate(s.get("messages") or []):
        extra = {k: v for k, v in m.items() if k not in ("role", "content")}
        conn.execute("INSERT INTO messages(session_id,seq,role,content,extra) VALUES(?,?,?,?,?)",
                     (s.get("id"), i, m.get("role", "user"), m.get("content", ""),
                      json.dumps(extra, ensure_ascii=False)))

def _db_write_lib(conn: sqlite3.Connection, lib: Dict[str, Any]) -> int:
    conn.execute("DELETE FROM library")
    n = 0
    for s in lib.get("skills") or []:
        conn.execute("INSERT OR REPLACE INTO library(id,kind,icon,name,description,category,variables,content)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (s.get("id") or f"sk-{uuid.uuid4().hex[:6]}", "skill", s.get("icon", "🛠"),
                      s.get("name", ""), s.get("description", ""), "", "[]", s.get("content", "")))
        n += 1
    for p in lib.get("prompts") or []:
        conn.execute("INSERT OR REPLACE INTO library(id,kind,icon,name,description,category,variables,content)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (p.get("id") or f"pr-{uuid.uuid4().hex[:6]}", "prompt", "",
                      p.get("name", ""), "", p.get("category", ""),
                      json.dumps(p.get("variables") or [], ensure_ascii=False), p.get("content", "")))
        n += 1
    return n

# 启用账户体系:先把既有全局数据归入 admin 空间,再初始化(迁移后)的库
_migrate_admin_data()
_db_init_and_migrate()

def _migrate_shared_gateway() -> None:
    """网关(渠道/密钥/日志/统计)改为全站共享:首次启动把 admin 库中的网关表迁入共享库。"""
    src = os.path.join(USER_DATA_DIR, "admin", "modeluse.db")
    if not os.path.exists(src):
        return
    try:
        with _DB_LOCK:
            s = sqlite3.connect(src, timeout=15)
            s.row_factory = sqlite3.Row
            d = _gw_conn()
            try:
                has_data = d.execute("SELECT COUNT(*) FROM channels").fetchone()[0] \
                    or d.execute("SELECT COUNT(*) FROM gw_keys").fetchone()[0]
                if has_data:                       # 共享库已有数据:不重复迁移
                    return
                n = 0
                for t in ("channels", "gw_keys", "gateway_logs"):
                    try:
                        rows = s.execute(f"SELECT * FROM {t}").fetchall()
                    except Exception:
                        rows = []
                    if rows:
                        cols = len(rows[0].keys())
                        d.executemany(f"INSERT INTO {t} VALUES({','.join('?' * cols)})",
                                      [tuple(r) for r in rows])
                        n += len(rows)
                row = s.execute("SELECT v FROM kv WHERE k='gateway_stats'").fetchone()
                if row:
                    d.execute("INSERT OR REPLACE INTO kv VALUES('gateway_stats',?)", (row["v"],))
                d.commit()
                if n:
                    print(f"[网关] 已把 admin 的 {n} 条网关数据(渠道/密钥/日志)迁入全站共享库")
            finally:
                s.close()
                d.close()
    except Exception as e:
        print(f"[网关] 共享库迁移失败(不影响启动): {e}")

_migrate_shared_gateway()

# ---------- 会话(读/写 SQLite,消息存 messages 表,其余字段存 extra JSON) ----------

def _load_chat_sessions() -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = _db_conn()
        try:
            out = []
            for r in conn.execute("SELECT * FROM sessions ORDER BY rowid").fetchall():
                s = {"id": r["id"], "title": r["title"], "archived": bool(r["archived"]),
                     "project": r["project"] or "", "messages": [],
                     "created_at": r["created_at"] or "",
                     "system_prompt": r["system_prompt"] or "",
                     "params": json.loads(r["params"] or "{}"),
                     "data_source": r["data_source"] or "", "model": r["model"] or ""}
                for m in conn.execute("SELECT role,content,extra FROM messages WHERE session_id=? ORDER BY seq",
                                       (r["id"],)).fetchall():
                    msg = json.loads(m["extra"] or "{}")
                    msg["role"] = m["role"]
                    msg["content"] = m["content"] or ""
                    s["messages"].append(msg)
                out.append(s)
            return out
        finally:
            conn.close()

def _save_chat_sessions(sessions: List[Dict[str, Any]]) -> None:
    with _DB_LOCK:
        conn = _db_conn()
        try:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            for s in sessions:
                _db_upsert_session(conn, s)
            conn.commit()
        finally:
            conn.close()

@app.get("/api/chat/sessions")
async def list_chat_sessions():
    return _load_chat_sessions()

@app.post("/api/chat/sessions")
async def create_chat_session(body: ChatSessionIn):
    sessions = _load_chat_sessions()
    s = {
        "id": uuid.uuid4().hex[:8],
        "title": body.title or f"会话 {datetime.now().strftime('%m-%d %H:%M')}",
        "archived": False,
        "project": "",
        "messages": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system_prompt": body.system_prompt or "",
        "params": body.params or {},
        "data_source": body.data_source or "",
        "model": body.model or "",
    }
    sessions.append(s)
    _save_chat_sessions(sessions)
    return s

@app.put("/api/chat/sessions/{sid}")
async def update_chat_session(sid: str, body: ChatSessionIn):
    sessions = _load_chat_sessions()
    for s in sessions:
        if s["id"] == sid:
            if body.title:
                s["title"] = body.title
            s["archived"] = body.archived
            s["messages"] = body.messages
            # 归档保留项目名;取消归档清空,避免残留导致再次归档时串组
            s["project"] = (body.project or "").strip() if body.archived else ""
            # ModelUse 会话级设置(压测主页不传则保留原值)
            if body.system_prompt or body.params or body.data_source or body.model:
                s["system_prompt"] = body.system_prompt
                s["params"] = body.params or {}
                s["data_source"] = body.data_source or ""
                s["model"] = body.model or ""
            _save_chat_sessions(sessions)
            return s
    raise HTTPException(404, "会话不存在")

@app.delete("/api/chat/sessions/{sid}")
async def delete_chat_session(sid: str):
    sessions = _load_chat_sessions()
    new = [s for s in sessions if s["id"] != sid]
    if len(new) == len(sessions):
        raise HTTPException(404, "会话不存在")
    _save_chat_sessions(new)
    return {"ok": True}

class ProjectOp(BaseModel):
    project: str
    new_name: str = ""

@app.post("/api/chat/projects/rename")
async def rename_chat_project(body: ProjectOp):
    """归档项目重命名:批量修改该项目下全部会话。"""
    new_name = (body.new_name or "").strip()
    if not body.project or not new_name:
        raise HTTPException(400, "原项目名与新项目名均不能为空")
    sessions = _load_chat_sessions()
    n = 0
    for s in sessions:
        if s.get("archived") and (s.get("project") or "默认项目") == body.project:
            s["project"] = new_name
            n += 1
    _save_chat_sessions(sessions)
    return {"ok": True, "renamed": n}

@app.post("/api/chat/projects/delete")
async def delete_chat_project(body: ProjectOp):
    """删除归档项目:连同该项目下全部会话一起删除,不可恢复。"""
    if not body.project:
        raise HTTPException(400, "项目名不能为空")
    sessions = _load_chat_sessions()
    new = [s for s in sessions
           if not (s.get("archived") and (s.get("project") or "默认项目") == body.project)]
    deleted = len(sessions) - len(new)
    _save_chat_sessions(new)
    return {"ok": True, "deleted": deleted}

# ============================================================================
# Chat File Upload (文档解析 / 图片转 base64)
# ============================================================================

from fastapi import UploadFile, File

_TEXT_EXTS = {"txt", "md", "markdown", "log", "csv", "json", "xml", "yaml", "yml",
              "py", "js", "ts", "java", "c", "cpp", "h", "go", "rs", "sh", "bat",
              "sql", "html", "css", "ini", "cfg", "conf", "toml", "vue", "jsx", "tsx"}
_IMG_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
_VIDEO_EXTS = {"mp4", "mov", "webm", "mkv", "avi", "m4v"}
_VIDEO_MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
               "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo"}
_AUDIO_EXTS = {"mp3", "wav", "ogg", "opus", "flac", "aac", "m4a", "wma", "amr", "webm"}
_AUDIO_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
               "opus": "audio/ogg", "flac": "audio/flac", "aac": "audio/aac",
               "m4a": "audio/mp4", "wma": "audio/x-ms-wma", "amr": "audio/amr",
               "webm": "audio/webm"}

@app.post("/api/chat/upload")
async def chat_upload(file: UploadFile = File(...)):
    """上传文件: 文档提取文本,图片/视频/音频转 base64 data URL(多模态输入)。"""
    import base64
    import io
    name = file.filename or "file"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ctype = (file.content_type or "").lower()
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")

    # 图片: 转 base64
    if ext in _IMG_EXTS:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/png")
        return {"name": name, "type": "image", "size": len(data),
                "content": f"data:{mime};base64,{base64.b64encode(data).decode()}"}

    # 音频: 转 base64(语音理解 input_audio / 语音识别 STT)。
    # .webm/.mp4 等容器扩展名音视频共用,优先按上传 MIME 区分(浏览器录音为 audio/webm)
    if ext in _AUDIO_EXTS or ctype.startswith("audio/"):
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(400, "音频超过 25MB,过大无法处理")
        mime = _AUDIO_MIME.get(ext, "") or (ctype if ctype.startswith("audio/") else "audio/mpeg")
        return {"name": name, "type": "audio", "size": len(data),
                "content": f"data:{mime};base64,{base64.b64encode(data).decode()}"}

    # 视频: 转 base64(多模态模型视觉输入,以 video_url 格式发送)
    if ext in _VIDEO_EXTS or ctype.startswith("video/"):
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(400, "视频超过 50MB,过大无法作为多模态输入")
        mime = _VIDEO_MIME.get(ext, "video/mp4")
        return {"name": name, "type": "video", "size": len(data),
                "content": f"data:{mime};base64,{base64.b64encode(data).decode()}"}

    # 纯文本类
    if ext in _TEXT_EXTS:
        return {"name": name, "type": "text", "size": len(data),
                "content": data.decode("utf-8", errors="replace")}

    # PDF
    if ext == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
            return {"name": name, "type": "text", "size": len(data), "content": text.strip() or "(PDF 无文本层)"}
        except Exception as e:
            raise HTTPException(400, f"PDF 解析失败: {e}")

    # Word
    if ext in ("docx", "doc"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
            return {"name": name, "type": "text", "size": len(data), "content": text.strip() or "(Word 无文本)"}
        except Exception as e:
            raise HTTPException(400, f"Word 解析失败(仅支持 .docx): {e}")

    raise HTTPException(400, f"不支持的文件类型: .{ext}")

# ============================================================================
# ModelUse 网关(FastAPI Gateway):渠道聚合 / 密钥管理 / 协议转换 / 用量统计
#  - 管理面:/api/gateway/*(渠道 CRUD、密钥、日志、统计)
#  - 对外面:/v1/*(OpenAI 兼容,鉴权 + 转发 + OpenAI/Anthropic/Ollama 协议互转)
#  - 存储:SQLite(全站共享库 _shared/gateway.db:渠道/密钥/日志对总管理员与各管理员一套共用;
#    子账号授予 gateway 权限后仅可查看;预览账号不可见)
# ============================================================================

_GATEWAY_LOCK = threading.Lock()

def _gw_default_cfg() -> Dict[str, Any]:
    return {"channels": [], "keys": [], "logs": [],
            "stats": {"total": 0, "success": 0, "fail": 0,
                      "tokens_in": 0, "tokens_out": 0, "by_channel": {}}}

def _load_gateway() -> Dict[str, Any]:
    out = _gw_default_cfg()
    with _DB_LOCK:
        conn = _gw_conn()
        try:
            out["channels"] = [{
                "id": r["id"], "name": r["name"], "base_url": r["base_url"],
                "api_key": r["api_key"] or "", "protocol": r["protocol"] or "openai",
                "types": json.loads(r["types"] or '["chat"]'),
                "models": json.loads(r["models"] or "[]"),
                "priority": r["priority"] or 0, "enabled": bool(r["enabled"]),
                "note": r["note"] or "", "created_at": r["created_at"] or "",
                "used": r["used"] or 0,
                "last_latency_ms": r["last_latency_ms"], "last_probe_at": r["last_probe_at"] or "",
            } for r in conn.execute("SELECT * FROM channels ORDER BY rowid").fetchall()]
            out["keys"] = [{
                "key": r["key"], "name": r["name"],
                "allowed_channels": json.loads(r["allowed_channels"] or "[]"),
                "allowed_models": json.loads(r["allowed_models"] or "[]"),
                "quota": r["quota"] or 0, "expires_at": r["expires_at"] or "",
                "enabled": bool(r["enabled"]), "used": r["used"] or 0,
                "created_at": r["created_at"] or "",
            } for r in conn.execute("SELECT * FROM gw_keys ORDER BY rowid").fetchall()]
            out["logs"] = [{
                "ts": r["ts"], "key": r["key"], "channel": r["channel"], "model": r["model"],
                "endpoint": r["endpoint"], "status": r["status"],
                "latency_ms": r["latency_ms"], "tokens_in": r["tokens_in"] or 0,
                "tokens_out": r["tokens_out"] or 0,
            } for r in conn.execute("SELECT * FROM gateway_logs ORDER BY id").fetchall()]
            row = conn.execute("SELECT v FROM kv WHERE k='gateway_stats'").fetchone()
            if row:
                try:
                    stats = json.loads(row["v"])
                    if isinstance(stats, dict):
                        out["stats"].update(stats)
                except Exception:
                    pass
            return out
        finally:
            conn.close()

def _save_gateway(cfg: Dict[str, Any]) -> None:
    with _DB_LOCK:
        conn = _gw_conn()
        try:
            conn.execute("DELETE FROM channels")
            for c in cfg.get("channels") or []:
                conn.execute("INSERT INTO channels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (c.get("id"), c.get("name"), c.get("base_url"), c.get("api_key", ""),
                              c.get("protocol", "openai"),
                              json.dumps(c.get("types") or ["chat"], ensure_ascii=False),
                              json.dumps(c.get("models") or [], ensure_ascii=False),
                              int(c.get("priority") or 0), 1 if c.get("enabled", True) else 0,
                              c.get("note", ""), c.get("created_at", ""), int(c.get("used") or 0),
                              c.get("last_latency_ms"), c.get("last_probe_at", "")))
            conn.execute("DELETE FROM gw_keys")
            for k in cfg.get("keys") or []:
                conn.execute("INSERT INTO gw_keys VALUES(?,?,?,?,?,?,?,?,?)",
                             (k.get("key"), k.get("name"),
                              json.dumps(k.get("allowed_channels") or [], ensure_ascii=False),
                              json.dumps(k.get("allowed_models") or [], ensure_ascii=False),
                              int(k.get("quota") or 0), k.get("expires_at", ""),
                              1 if k.get("enabled", True) else 0,
                              int(k.get("used") or 0), k.get("created_at", "")))
            conn.execute("DELETE FROM gateway_logs")
            for g in cfg.get("logs") or []:
                conn.execute("INSERT INTO gateway_logs(ts,key,channel,model,endpoint,status,latency_ms,"
                             "tokens_in,tokens_out) VALUES(?,?,?,?,?,?,?,?,?)",
                             (g.get("ts", ""), g.get("key", ""), g.get("channel", ""), g.get("model", ""),
                              g.get("endpoint", ""), int(g.get("status") or 0),
                              float(g.get("latency_ms") or 0),
                              int(g.get("tokens_in") or 0), int(g.get("tokens_out") or 0)))
            conn.execute("INSERT OR REPLACE INTO kv VALUES('gateway_stats',?)",
                         (json.dumps(cfg.get("stats") or {}, ensure_ascii=False),))
            conn.commit()
        finally:
            conn.close()

def _gw_with_lock(fn):
    """串行化 读-改-写,避免并发请求丢计数。"""
    def wrapper(*args, **kwargs):
        with _GATEWAY_LOCK:
            return fn(*args, **kwargs)
    return wrapper

@_gw_with_lock
def _gw_log_call(key_rec: Dict[str, Any], channel_name: str, model: str,
                 endpoint: str, status: int, latency_ms: float, usage: Dict[str, Any]):
    cfg = _load_gateway()
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "key": (key_rec or {}).get("name", "-"),
        "channel": channel_name or "-",
        "model": model or "-",
        "endpoint": endpoint,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "tokens_in": (usage or {}).get("prompt_tokens", 0) or 0,
        "tokens_out": (usage or {}).get("completion_tokens", 0) or 0,
    }
    cfg["logs"].append(entry)
    if len(cfg["logs"]) > 500:
        cfg["logs"] = cfg["logs"][-500:]
    st = cfg["stats"]
    st["total"] += 1
    if status < 400:
        st["success"] += 1
    else:
        st["fail"] += 1
    st["tokens_in"] += entry["tokens_in"]
    st["tokens_out"] += entry["tokens_out"]
    if channel_name:
        st["by_channel"][channel_name] = st["by_channel"].get(channel_name, 0) + 1
    # 密钥计数
    if key_rec:
        for k in cfg["keys"]:
            if k["key"] == key_rec["key"]:
                k["used"] = int(k.get("used", 0)) + 1
                break
    _save_gateway(cfg)

class GwChannelIn(BaseModel):
    id: str = ""
    name: str
    base_url: str
    api_key: str = ""
    protocol: str = "openai"            # openai | anthropic | ollama
    types: List[str] = ["chat"]         # chat | image | video | audio | embedding
    models: List[str] = []
    priority: int = 0
    enabled: bool = True
    note: str = ""

class GwKeyIn(BaseModel):
    key: str = ""                       # 空 = 新建(自动生成),有值 = 更新
    name: str = "默认密钥"
    allowed_channels: List[str] = []    # 渠道 id 绑定(空 = 全部渠道)
    allowed_models: List[str] = []      # 空 = 不限
    quota: int = 0                      # 0 = 不限
    expires_at: str = ""                # "" = 永不过期(YYYY-MM-DD)
    enabled: bool = True

def _gw_admin_guard() -> None:
    """网关为管理员共享配置:子账号(即便被授予网关权限)仅可查看,不可增删改。"""
    if not _is_admin():
        raise HTTPException(403, "网关渠道 / 密钥由管理员统一配置,当前账号仅可查看")

@app.get("/api/gateway/config")
async def gw_get_config():
    cfg = _load_gateway()
    return {"channels": cfg["channels"], "keys": cfg["keys"],
            "stats": cfg["stats"], "logs_count": len(cfg["logs"])}

@app.post("/api/gateway/channels")
async def gw_save_channel(ch: GwChannelIn):
    _gw_admin_guard()
    cfg = _load_gateway()
    base = re.sub(r"/v1/?$", "", (ch.base_url or "").strip().rstrip("/"))
    if not ch.name.strip() or not base:
        raise HTTPException(400, "名称与 Base URL 不能为空")
    # 模型列表为空时自动探测(添加渠道即自动识别模型名称)
    models = ch.models or []
    if not models:
        try:
            result = await detector.detect(base, ch.api_key or "")
            models = result.get("models") or []
        except Exception:
            models = []
    if ch.id:
        for c in cfg["channels"]:
            if c["id"] == ch.id:
                c.update({"name": ch.name.strip(), "base_url": base, "api_key": ch.api_key,
                          "protocol": ch.protocol, "types": ch.types or ["chat"],
                          "models": models, "priority": ch.priority,
                          "enabled": ch.enabled, "note": ch.note})
                break
        else:
            raise HTTPException(404, "渠道不存在")
    else:
        cfg["channels"].append({
            "id": uuid.uuid4().hex[:8], "name": ch.name.strip(), "base_url": base,
            "api_key": ch.api_key, "protocol": ch.protocol,
            "types": ch.types or ["chat"], "models": models,
            "priority": ch.priority, "enabled": ch.enabled, "note": ch.note,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "used": 0,
        })
    _save_gateway(cfg)
    return {"ok": True, "models": models}

@app.delete("/api/gateway/channels/{cid}")
async def gw_delete_channel(cid: str):
    _gw_admin_guard()
    cfg = _load_gateway()
    new = [c for c in cfg["channels"] if c["id"] != cid]
    if len(new) == len(cfg["channels"]):
        raise HTTPException(404, "渠道不存在")
    cfg["channels"] = new
    _save_gateway(cfg)
    return {"ok": True}

@app.post("/api/gateway/channels/{cid}/probe")
async def gw_probe_channel(cid: str):
    """渠道连通性测试:复用协议自动检测,返回协议/框架/模型列表/延迟;延迟持久化到渠道(列表展示)。"""
    cfg = _load_gateway()
    ch = next((c for c in cfg["channels"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "渠道不存在")
    t0 = time.perf_counter()
    try:
        result = await detector.detect(ch["base_url"], ch.get("api_key", ""))
    except Exception as e:
        ch["last_probe_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ch["last_latency_ms"] = -1      # -1 = 最近一次探测失败
        _save_gateway(cfg)
        raise HTTPException(502, f"检测失败: {e}")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    ch["last_probe_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch["last_latency_ms"] = ms
    _save_gateway(cfg)
    return {"latency_ms": ms, "protocol": result.get("protocol"),
            "framework": result.get("framework"),
            "models": result.get("models") or [],
            "capabilities": result.get("capabilities") or []}

@app.post("/api/gateway/keys")
async def gw_save_key(k: GwKeyIn):
    _gw_admin_guard()
    cfg = _load_gateway()
    if k.key:
        rec = next((x for x in cfg["keys"] if x["key"] == k.key), None)
        if not rec:
            raise HTTPException(404, "密钥不存在")
        rec.update({"name": k.name, "allowed_channels": k.allowed_channels,
                    "allowed_models": k.allowed_models,
                    "quota": k.quota, "expires_at": k.expires_at, "enabled": k.enabled})
    else:
        import secrets
        new_key = f"sk-{_cur_user()}-{secrets.token_hex(12)}"
        cfg["keys"].append({"key": new_key, "name": k.name or "默认密钥",
                            "allowed_channels": k.allowed_channels,
                            "allowed_models": k.allowed_models, "quota": k.quota,
                            "expires_at": k.expires_at, "enabled": k.enabled,
                            "used": 0, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    _save_gateway(cfg)
    return {"ok": True}

@app.delete("/api/gateway/keys/{key}")
async def gw_delete_key(key: str):
    _gw_admin_guard()
    cfg = _load_gateway()
    new = [x for x in cfg["keys"] if x["key"] != key]
    if len(new) == len(cfg["keys"]):
        raise HTTPException(404, "密钥不存在")
    cfg["keys"] = new
    _save_gateway(cfg)
    return {"ok": True}

@app.get("/api/gateway/logs")
async def gw_get_logs():
    return _load_gateway()["logs"]

@app.get("/api/gateway/stats")
async def gw_get_stats():
    cfg = _load_gateway()
    st = dict(cfg["stats"])
    lat = [l["latency_ms"] for l in cfg["logs"] if l.get("latency_ms") is not None]
    st["avg_latency_ms"] = round(sum(lat) / len(lat), 1) if lat else 0
    st["p95_latency_ms"] = round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else 0
    return st

@app.post("/api/gateway/logs/clear")
async def gw_clear_logs():
    _gw_admin_guard()
    cfg = _load_gateway()
    cfg["logs"] = []
    cfg["stats"] = _gw_default_cfg()["stats"]
    for k in cfg["keys"]:
        k["used"] = 0
    _save_gateway(cfg)
    return {"ok": True}

# ---- 预览用户对话:统一走管理员配置的网关渠道(不暴露密钥,页面无可编辑配置) ----

def _gw_best_chat_channel() -> Optional[Dict[str, Any]]:
    """共享网关中优先级最高的可用 chat 渠道(预览账号对话后端)。"""
    chs = [c for c in _load_gateway()["channels"]
           if c.get("enabled", True) and "chat" in (c.get("types") or ["chat"])]
    chs.sort(key=lambda c: (-int(c.get("priority", 0) or 0), c.get("created_at", "")))
    return chs[0] if chs else None

@app.get("/api/viewer/chat-cfg")
async def viewer_chat_cfg():
    """预览账号的对话配置(只读):渠道名 / 可用模型;密钥不下发,由服务端代理时使用。"""
    ch = _gw_best_chat_channel()
    if not ch:
        return {"configured": False, "models": [], "model": "", "channel": ""}
    models = [m for m in (ch.get("models") or []) if isinstance(m, str)]
    return {"configured": True, "channel": ch.get("name", ""),
            "models": models, "model": models[0] if models else ""}

# ---- 鉴权 / 渠道解析 ----

def _gw_check_key(request: Request, model: str = "") -> Dict[str, Any]:
    """鉴权:Authorization: Bearer <key> 或 Anthropic 风格 x-api-key: <key>。
    密钥格式 sk-<账号>-<随机>:网关代理无会话 cookie,从密钥本身路由到对应账号的渠道库。"""
    auth = request.headers.get("authorization", "")
    key = ""
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    if not key:
        key = request.headers.get("x-api-key", "").strip()
    if not key:
        raise HTTPException(401, "缺少鉴权:Authorization: Bearer <密钥> 或 x-api-key: <密钥>")
    user = "admin"
    if key.startswith("sk-"):
        head = key[3:]
        if "-" in head:
            cand = head.rsplit("-", 1)[0]
            if cand and re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]{2,24}", cand):
                user = cand
    _USER_CTX.set(user)
    rec = next((k for k in _load_gateway()["keys"] if k["key"] == key), None)
    if not rec:
        raise HTTPException(401, "无效密钥")
    if not rec.get("enabled", True):
        raise HTTPException(403, "密钥已禁用")
    if rec.get("expires_at"):
        try:
            if datetime.now() > datetime.fromisoformat(rec["expires_at"]):
                raise HTTPException(403, "密钥已过期")
        except ValueError:
            pass
    if rec.get("quota", 0) and int(rec.get("used", 0)) >= rec["quota"]:
        raise HTTPException(429, "密钥配额已用尽")
    if model:
        allowed = rec.get("allowed_models") or []
        if allowed and model not in allowed:
            raise HTTPException(403, f"密钥无权访问模型 {model}")
    return rec

def _gw_channels_for(model: str, kind: str) -> List[Dict[str, Any]]:
    cfg = _load_gateway()
    chs = [c for c in cfg["channels"]
           if c.get("enabled", True) and kind in (c.get("types") or ["chat"])
           and model in (c.get("models") or [])]
    chs.sort(key=lambda c: (-int(c.get("priority", 0) or 0), c.get("created_at", "")))
    return chs

def _gw_filter_channels(key_rec: Dict[str, Any],
                        channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """密钥-渠道绑定:allowed_channels 非空时只允许其内的渠道。"""
    allowed_ch = (key_rec or {}).get("allowed_channels") or []
    if not allowed_ch:
        return channels
    out = [c for c in channels if c.get("id") in allowed_ch]
    if not out:
        raise HTTPException(403, "密钥未绑定可用渠道(无权访问)")
    return out

def _gw_sse_chunk(alias: str, delta: Optional[Dict[str, Any]] = None,
                  finish_reason: Optional[str] = None,
                  usage: Optional[Dict[str, Any]] = None) -> str:
    chunk: Dict[str, Any] = {"id": "chatcmpl-gw", "object": "chat.completion.chunk",
                             "created": int(time.time()), "model": alias,
                             "choices": [{"index": 0, "delta": delta or {},
                                          "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

async def _iter_data_lines(content) -> Any:
    """逐行产出 SSE data: 载荷或裸 JSON 行(ollama)。"""
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    async for raw in content.iter_chunked(4096):
        buffer += decoder.decode(raw)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                ds = line[5:].strip()
                if ds and ds != "[DONE]":
                    yield ds
            elif line.startswith("{"):
                yield line

# ---- 协议转换:OpenAI <-> Anthropic / Ollama ----

def _part_to_anthropic(part: Any) -> Optional[Dict[str, Any]]:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    t = part.get("type")
    if t == "text":
        return {"type": "text", "text": part.get("text", "")}
    if t == "image_url":
        url = (part.get("image_url") or {}).get("url", "")
        m = re.match(r"data:([^;]+);base64,(.*)", url, re.DOTALL)
        if m:
            return {"type": "image",
                    "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
        if url:
            return {"type": "image", "source": {"type": "url", "url": url}}
        return None
    if t in ("input_audio", "video_url"):
        return {"type": "text", "text": "[该渠道协议不支持此多模态输入类型]"}
    return None

def _openai_to_anthropic(body: Dict[str, Any], upstream_model: str) -> Dict[str, Any]:
    msgs, system_text = [], []
    for m in body.get("messages", []):
        role, content = m.get("role", "user"), m.get("content")
        if role == "system":
            system_text.append(content if isinstance(content, str)
                               else json.dumps(content, ensure_ascii=False))
            continue
        if role == "tool":
            rc = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False)
            msgs.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": rc or ""}]})
            continue
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in (_part_to_anthropic(p) for p in content) if b]
        else:
            blocks = []
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            blocks.append({"type": "tool_use", "id": tc.get("id") or "call_gw",
                           "name": fn.get("name", ""),
                           "input": args if isinstance(args, dict) else {}})
        if not blocks and m.get("tool_calls"):
            blocks = [{"type": "text", "text": ""}]   # Anthropic 要求非空 content
        if blocks:
            msgs.append({"role": role if role in ("user", "assistant") else "user",
                         "content": blocks})
    out: Dict[str, Any] = {"model": upstream_model,
                           "max_tokens": body.get("max_tokens") or 4096,
                           "messages": msgs}
    if system_text:
        out["system"] = "\n".join(system_text)
    tools = []
    for t in body.get("tools") or []:
        fn = (t or {}).get("function") or {}
        if fn.get("name"):
            tools.append({"name": fn.get("name", ""),
                          "description": fn.get("description", "") or "",
                          "input_schema": fn.get("parameters")
                                          or {"type": "object", "properties": {}}})
    if tools:
        out["tools"] = tools
    tc = body.get("tool_choice")
    if tc == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(tc, dict) and (tc.get("function") or {}).get("name"):
        out["tool_choice"] = {"type": "tool",
                              "name": tc["function"]["name"]}
    elif tc == "auto":
        out["tool_choice"] = {"type": "auto"}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stream"):
        out["stream"] = True
    return out

def _anthropic_to_openai(data: Dict[str, Any], alias: str) -> Dict[str, Any]:
    text, think = "", ""
    tool_calls: List[Dict[str, Any]] = []
    for b in data.get("content", []):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text += b.get("text", "")
        elif b.get("type") == "thinking":
            think += b.get("thinking", "") or b.get("text", "") or ""
        elif b.get("type") == "tool_use":
            tool_calls.append({"id": b.get("id", ""), "type": "function",
                               "function": {"name": b.get("name", ""),
                                            "arguments": json.dumps(
                                                b.get("input") or {},
                                                ensure_ascii=False)}})
    msg: Dict[str, Any] = {"role": "assistant", "content": text}
    if think:
        msg["reasoning_content"] = think
    if tool_calls:
        msg["tool_calls"] = tool_calls
    sr = data.get("stop_reason")
    finish = "tool_calls" if tool_calls else (
        "length" if sr == "max_tokens" else "stop")
    u = data.get("usage") or {}
    pin, pout = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
    return {"id": data.get("id", "chatcmpl-gw"), "object": "chat.completion",
            "created": int(time.time()), "model": alias,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout,
                      "total_tokens": pin + pout}}

def _openai_to_ollama(body: Dict[str, Any], upstream_model: str) -> Dict[str, Any]:
    msgs = []
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text", "")) for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
        msg: Dict[str, Any] = {"role": m.get("role", "user"), "content": content or ""}
        if m.get("role") == "tool" and m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
        if m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                tcs.append({"function": {"name": fn.get("name", ""),
                                         "arguments": args or {}}})
            msg["tool_calls"] = tcs
        msgs.append(msg)
    out: Dict[str, Any] = {"model": upstream_model, "messages": msgs}
    if body.get("stream"):
        out["stream"] = True
    if body.get("tools"):
        out["tools"] = body["tools"]     # ollama 兼容 OpenAI 工具定义格式
    opts: Dict[str, Any] = {}
    for k in ("temperature", "top_p", "seed"):
        if body.get(k) is not None:
            opts[k] = body[k]
    if body.get("max_tokens") is not None:
        opts["num_predict"] = body["max_tokens"]
    if opts:
        out["options"] = opts
    return out

def _ollama_to_openai(data: Dict[str, Any], alias: str) -> Dict[str, Any]:
    msg = data.get("message") or {}
    m: Dict[str, Any] = {"role": "assistant", "content": msg.get("content", "")}
    if msg.get("thinking"):
        m["reasoning_content"] = msg["thinking"]
    if msg.get("tool_calls"):
        m["tool_calls"] = [{"id": (tc or {}).get("id") or f"call_gw_{i}",
                            "type": "function",
                            "function": {"name": ((tc or {}).get("function")
                                                  or {}).get("name", ""),
                                         "arguments": json.dumps(
                                             ((tc or {}).get("function")
                                              or {}).get("arguments") or {},
                                             ensure_ascii=False)}}
                           for i, tc in enumerate(msg["tool_calls"])]
    finish = "tool_calls" if m.get("tool_calls") else (data.get("done_reason") or "stop")
    pin = data.get("prompt_eval_count", 0) or 0
    pout = data.get("eval_count", 0) or 0
    return {"id": "chatcmpl-gw", "object": "chat.completion",
            "created": int(time.time()), "model": alias,
            "choices": [{"index": 0, "message": m,
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": pin, "completion_tokens": pout,
                      "total_tokens": pin + pout}}

# ---- 对外端点:模型列表 / 对话(OpenAI + Anthropic 双协议) / 图 / 视频 / 音频 / 嵌入 ----

@app.get("/v1/models")
async def gw_models(request: Request):
    rec = _gw_check_key(request)
    allowed_ch = rec.get("allowed_channels") or []
    allowed_m = rec.get("allowed_models") or []
    seen, data = set(), []
    for ch in _load_gateway()["channels"]:
        if not ch.get("enabled", True):
            continue
        if allowed_ch and ch["id"] not in allowed_ch:
            continue
        for m in ch.get("models", []):
            if m in seen:
                continue
            if allowed_m and m not in allowed_m:
                continue
            seen.add(m)
            data.append({"id": m, "object": "model", "created": 0,
                         "owned_by": ch.get("name", "gateway")})
    return {"object": "list", "data": data}

def _gw_upstream_chat_request(ch: Dict[str, Any], body: Dict[str, Any],
                              alias: str) -> tuple:
    """按渠道协议构建上游请求(输入为 OpenAI 格式): (endpoint, headers, payload, proto)。"""
    base = ch["base_url"]
    proto = ch.get("protocol", "openai")
    if proto == "anthropic":
        return (f"{base}/v1/messages",
                {"Content-Type": "application/json", "x-api-key": ch.get("api_key", ""),
                 "anthropic-version": "2023-06-01"},
                _openai_to_anthropic(body, alias), proto)
    if proto == "ollama":
        return (f"{base}/api/chat", {"Content-Type": "application/json"},
                _openai_to_ollama(body, alias), proto)
    return (f"{base}/v1/chat/completions",
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {ch.get('api_key', '')}" if ch.get("api_key") else ""},
            {**body, "model": alias}, proto)

def _a_event(ev_type: str, obj: Any) -> str:
    return f"event: {ev_type}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"

async def _gw_stream_chat(key_rec: Dict[str, Any], channels: List[Dict[str, Any]],
                          body: Dict[str, Any], alias: str, endpoint_label: str,
                          anthropic_out: bool = False,
                          raw_body: Optional[Dict[str, Any]] = None):
    """对话流式转发生成器。上游连接的生命周期必须在生成器内部:
    StreamingResponse 在 handler 返回后才被消费,handler 里的 async with 届时已退出。
    anthropic_out=True 输出 Anthropic SSE 事件(/v1/messages 用)。"""
    t0 = time.perf_counter()
    last_err = "无渠道可达"
    for ch in channels:
        endpoint, headers, payload, proto = _gw_upstream_chat_request(ch, body, alias)
        # Anthropic 客户端 → Anthropic 渠道:原始体直接透传(仅改 model)
        if anthropic_out and proto == "anthropic" and raw_body is not None:
            payload = {**raw_body, "model": alias}
        try:
            timeout = aiohttp.ClientTimeout(total=600, sock_read=300)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers,
                                        ssl=False) as resp:
                    if resp.status >= 500:
                        last_err = f"HTTP {resp.status}"
                        continue          # 上游故障 → 下一渠道
                    if resp.status >= 400:
                        text = await resp.text()
                        _gw_log_call(key_rec, ch["name"], alias, endpoint_label,
                                     resp.status, (time.perf_counter() - t0) * 1000, {})
                        if anthropic_out:
                            yield _a_event("error", {"type": "error",
                                "error": {"type": "upstream_error", "message": text[:400]}})
                        else:
                            yield ("data: " + json.dumps(
                                {"error": {"message": text[:400], "code": resp.status}},
                                ensure_ascii=False) + "\n\ndata: [DONE]\n\n")
                        return

                    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}
                    text_chars = 0
                    passthrough = (proto == "anthropic" and anthropic_out
                                   and raw_body is not None)
                    finish_reason = "stop"
                    a_state = {"thinking_open": False, "text_open": False,
                               "blocks": 0, "tools": {}}
                    if anthropic_out and not passthrough:
                        yield _a_event("message_start", {"type": "message_start", "message": {
                            "id": "msg_gw_" + uuid.uuid4().hex[:12], "type": "message",
                            "role": "assistant", "model": alias, "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}})
                    try:
                        async for ds in _iter_data_lines(resp.content):
                            try:
                                data = json.loads(ds)
                            except Exception:
                                continue
                            if passthrough:
                                if isinstance(data, dict):
                                    et = data.get("type")
                                    if et == "message_start":
                                        msg = data.get("message") or {}
                                        msg["model"] = alias
                                        u = msg.get("usage") or {}
                                        usage_acc["prompt_tokens"] = u.get("input_tokens", 0) or 0
                                    elif et == "message_delta":
                                        u = data.get("usage") or {}
                                        usage_acc["completion_tokens"] = u.get("output_tokens", 0) or 0
                                    elif et == "content_block_delta":
                                        d = data.get("delta") or {}
                                        text_chars += len(d.get("text") or d.get("thinking") or "")
                                yield _a_event(data.get("type", "unknown") if isinstance(data, dict)
                                               else "unknown", data)
                                continue

                            # 统一解析为 (文本增量, 思维增量, 工具增量, usage, done)
                            # tool_frags 统一采用 OpenAI 流式 tool_calls 片段格式
                            piece = think_piece = None
                            tool_frags: List[Dict[str, Any]] = []
                            role_frag = None
                            usage_now = None
                            done = False
                            if proto == "anthropic":
                                et = data.get("type")
                                if et == "content_block_delta":
                                    d = data.get("delta") or {}
                                    if d.get("type") == "thinking_delta":
                                        think_piece = d.get("thinking") or None
                                    elif d.get("type") == "input_json_delta":
                                        tool_frags = [{"index": data.get("index", 0),
                                                       "function": {"arguments":
                                                           d.get("partial_json") or ""}}]
                                    else:
                                        piece = d.get("text") or None
                                elif et == "content_block_start":
                                    cb = data.get("content_block") or {}
                                    if cb.get("type") == "tool_use":
                                        tool_frags = [{"index": data.get("index", 0),
                                                       "id": cb.get("id") or "",
                                                       "type": "function",
                                                       "function": {"name": cb.get("name", ""),
                                                                    "arguments": ""}}]
                                elif et == "message_start":
                                    u = (data.get("message") or {}).get("usage") or {}
                                    usage_acc["prompt_tokens"] = u.get("input_tokens", 0) or 0
                                elif et == "message_delta":
                                    u = data.get("usage") or {}
                                    usage_acc["completion_tokens"] = u.get("output_tokens", 0) or 0
                                    sr = (data.get("delta") or {}).get("stop_reason")
                                    if sr:
                                        finish_reason = {"tool_use": "tool_calls",
                                                         "max_tokens": "length"}.get(sr, "stop")
                                elif et == "message_stop":
                                    done = True
                            elif proto == "ollama":
                                msg = data.get("message") or {}
                                think_piece = msg.get("thinking") or None
                                piece = msg.get("content") or None
                                tcs = msg.get("tool_calls")
                                if isinstance(tcs, list) and tcs:
                                    acc = a_state.setdefault("ollama_tools", [])
                                    for j, tc in enumerate(tcs):
                                        fn = (tc or {}).get("function") or {}
                                        if j < len(acc):
                                            if fn.get("name") and not acc[j]["function"].get("name"):
                                                acc[j]["function"]["name"] = fn["name"]
                                            if isinstance(fn.get("arguments"), dict):
                                                acc[j]["function"]["_args"].update(fn["arguments"])
                                        else:
                                            acc.append({"id": (tc or {}).get("id") or "",
                                                        "function": {"name": fn.get("name", ""),
                                                                     "_args": dict(fn.get("arguments") or {})}})
                                if data.get("done"):
                                    done = True
                                    usage_acc["prompt_tokens"] = data.get("prompt_eval_count", 0) or 0
                                    usage_acc["completion_tokens"] = data.get("eval_count", 0) or 0
                                    for j, acc_tc in enumerate(a_state.get("ollama_tools") or []):
                                        fn = acc_tc["function"]
                                        tool_frags.append({
                                            "index": j,
                                            "id": acc_tc.get("id") or f"call_gw_{j}",
                                            "type": "function",
                                            "function": {"name": fn.get("name", ""),
                                                         "arguments": json.dumps(
                                                             fn.get("_args") or {},
                                                             ensure_ascii=False)}})
                            else:   # openai
                                chs = data.get("choices") or []
                                if chs:
                                    delta = chs[0].get("delta") or {}
                                    piece = delta.get("content") or None
                                    think_piece = (delta.get("reasoning_content")
                                                   or delta.get("reasoning") or None)
                                    if delta.get("tool_calls"):
                                        tool_frags = delta["tool_calls"]
                                    role_frag = delta.get("role") or None
                                    if chs[0].get("finish_reason"):
                                        done = True
                                        finish_reason = chs[0]["finish_reason"]
                                u = data.get("usage")
                                if isinstance(u, dict):
                                    usage_now = u
                            if usage_now:
                                usage_acc["prompt_tokens"] = (usage_now.get("prompt_tokens", 0)
                                                              or usage_acc["prompt_tokens"])
                                usage_acc["completion_tokens"] = (usage_now.get("completion_tokens", 0)
                                                                  or usage_acc["completion_tokens"])

                            if anthropic_out:
                                if think_piece:
                                    if not a_state["thinking_open"]:
                                        a_state["thinking_open"] = True
                                        a_state["blocks"] += 1
                                        yield _a_event("content_block_start", {
                                            "type": "content_block_start",
                                            "index": a_state["blocks"] - 1,
                                            "content_block": {"type": "thinking", "thinking": ""}})
                                    yield _a_event("content_block_delta", {
                                        "type": "content_block_delta",
                                        "index": a_state["blocks"] - 1,
                                        "delta": {"type": "thinking_delta",
                                                  "thinking": think_piece}})
                                if piece:
                                    if a_state["thinking_open"]:
                                        a_state["thinking_open"] = False
                                        yield _a_event("content_block_stop", {
                                            "type": "content_block_stop",
                                            "index": a_state["blocks"] - 1})
                                    if not a_state["text_open"]:
                                        a_state["text_open"] = True
                                        a_state["blocks"] += 1
                                        yield _a_event("content_block_start", {
                                            "type": "content_block_start",
                                            "index": a_state["blocks"] - 1,
                                            "content_block": {"type": "text", "text": ""}})
                                    text_chars += len(piece)
                                    yield _a_event("content_block_delta", {
                                        "type": "content_block_delta",
                                        "index": a_state["blocks"] - 1,
                                        "delta": {"type": "text_delta", "text": piece}})
                                # 工具调用片段 → Anthropic tool_use 块事件
                                for tf in tool_frags:
                                    if not isinstance(tf, dict):
                                        continue
                                    idx = tf.get("index", 0)
                                    fn = tf.get("function") or {}
                                    st = a_state["tools"].get(idx)
                                    if st is None:
                                        if a_state["thinking_open"]:
                                            a_state["thinking_open"] = False
                                            yield _a_event("content_block_stop", {
                                                "type": "content_block_stop",
                                                "index": a_state["blocks"] - 1})
                                        if a_state["text_open"]:
                                            a_state["text_open"] = False
                                            yield _a_event("content_block_stop", {
                                                "type": "content_block_stop",
                                                "index": a_state["blocks"] - 1})
                                        a_state["blocks"] += 1
                                        st = {"block": a_state["blocks"] - 1,
                                              "id": tf.get("id") or f"toolu_gw_{idx}"}
                                        a_state["tools"][idx] = st
                                        yield _a_event("content_block_start", {
                                            "type": "content_block_start",
                                            "index": st["block"],
                                            "content_block": {"type": "tool_use",
                                                              "id": st["id"],
                                                              "name": fn.get("name", ""),
                                                              "input": {}}})
                                    if fn.get("arguments"):
                                        text_chars += len(fn["arguments"])
                                        yield _a_event("content_block_delta", {
                                            "type": "content_block_delta",
                                            "index": st["block"],
                                            "delta": {"type": "input_json_delta",
                                                      "partial_json": fn["arguments"]}})
                            else:
                                if role_frag:
                                    yield _gw_sse_chunk(alias, {"role": role_frag})
                                if think_piece:
                                    yield _gw_sse_chunk(alias, {"reasoning_content": think_piece})
                                if piece:
                                    text_chars += len(piece)
                                    yield _gw_sse_chunk(alias, {"content": piece})
                                if tool_frags:
                                    text_chars += sum(
                                        len((tf or {}).get("function", {}).get("arguments") or "")
                                        for tf in tool_frags if isinstance(tf, dict))
                                    yield _gw_sse_chunk(alias, {"tool_calls": tool_frags})
                            if done and anthropic_out and not passthrough:
                                break
                    except Exception:
                        pass

                    if not usage_acc["completion_tokens"] and text_chars:
                        usage_acc["completion_tokens"] = estimate_tokens("x" * text_chars)
                    if anthropic_out:
                        if not passthrough:
                            if a_state["thinking_open"]:
                                yield _a_event("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": a_state["blocks"] - 1})
                            if a_state["text_open"]:
                                yield _a_event("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": a_state["blocks"] - 1})
                            for st in a_state["tools"].values():
                                yield _a_event("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": st["block"]})
                            stop_reason = ("tool_use" if a_state["tools"]
                                           else "max_tokens" if finish_reason == "length"
                                           else "end_turn")
                            yield _a_event("message_delta", {
                                "type": "message_delta",
                                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": {"output_tokens": usage_acc["completion_tokens"]}})
                            yield _a_event("message_stop", {"type": "message_stop"})
                    else:
                        yield _gw_sse_chunk(alias, {}, finish_reason, usage_acc)
                        yield "data: [DONE]\n\n"
                    _gw_log_call(key_rec, ch["name"], alias, endpoint_label, 200,
                                 (time.perf_counter() - t0) * 1000,
                                 {"prompt_tokens": usage_acc["prompt_tokens"],
                                  "completion_tokens": usage_acc["completion_tokens"]})
                    return
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    if anthropic_out:
        yield _a_event("error", {"type": "error",
            "error": {"type": "api_error", "message": f"所有渠道均不可达: {last_err}"}})
    else:
        yield ("data: " + json.dumps(
            {"error": {"message": f"所有渠道均不可达: {last_err}"}},
            ensure_ascii=False) + "\n\ndata: [DONE]\n\n")

@app.post("/v1/chat/completions")
async def gw_chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    alias = str(body.get("model") or "")
    if not alias:
        raise HTTPException(400, "缺少 model 字段")
    key_rec = _gw_check_key(request, alias)
    channels = _gw_filter_channels(key_rec, _gw_channels_for(alias, "chat"))
    if not channels:
        raise HTTPException(404, f"无可用渠道提供模型 {alias}")

    if body.get("stream"):
        return StreamingResponse(
            _gw_stream_chat(key_rec, channels, body, alias, "/v1/chat/completions"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Gateway-Endpoint": "/v1/chat/completions"})

    t0 = time.perf_counter()
    last_err = "无渠道可达"
    for ch in channels:
        endpoint, headers, payload, proto = _gw_upstream_chat_request(ch, body, alias)
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600, sock_read=300)) as session:
                async with session.post(endpoint, json=payload, headers=headers,
                                        ssl=False) as resp:
                    if resp.status >= 500:
                        last_err = f"HTTP {resp.status}"
                        continue
                    text = await resp.text()
                    if resp.status >= 400:
                        _gw_log_call(key_rec, ch["name"], alias, "/v1/chat/completions",
                                     resp.status, (time.perf_counter() - t0) * 1000, {})
                        return Response(content=text, status_code=resp.status,
                                        media_type="application/json")
                    try:
                        data = json.loads(text)
                    except Exception:
                        _gw_log_call(key_rec, ch["name"], alias, "/v1/chat/completions",
                                     502, (time.perf_counter() - t0) * 1000, {})
                        raise HTTPException(502, "上游返回非 JSON")
                    if proto == "anthropic":
                        out = _anthropic_to_openai(data, alias)
                    elif proto == "ollama":
                        out = _ollama_to_openai(data, alias)
                    else:
                        out = data
                        if isinstance(out, dict):
                            out["model"] = alias
                    u = (out or {}).get("usage") or {}
                    _gw_log_call(key_rec, ch["name"], alias, "/v1/chat/completions",
                                 200, (time.perf_counter() - t0) * 1000, u)
                    return JSONResponseCompat(out)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    raise HTTPException(502, f"所有渠道均不可达: {last_err}")

# ---- Anthropic 协议接入(/v1/messages):请求/响应双向转换 ----

def _anthropic_block_to_openai(b: Any) -> Optional[Dict[str, Any]]:
    if isinstance(b, str):
        return {"type": "text", "text": b}
    if not isinstance(b, dict):
        return None
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": b.get("text", "")}
    if t == "image":
        src = b.get("source") or {}
        if src.get("type") == "base64":
            return {"type": "image_url", "image_url": {
                "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}}
        if src.get("type") == "url":
            return {"type": "image_url", "image_url": {"url": src.get("url", "")}}
        return None
    return None    # thinking 等块不回传上游

def _anthropic_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append({"type": "function", "function": {
            "name": t.get("name", ""),
            "description": t.get("description", "") or "",
            "parameters": t.get("input_schema")
                          or {"type": "object", "properties": {}}}})
    return out

def _anthropic_req_to_openai(body: Dict[str, Any]) -> Dict[str, Any]:
    msgs: List[Dict[str, Any]] = []
    sys = body.get("system")
    if sys:
        if isinstance(sys, list):
            sys = "\n".join(str(b.get("text", "")) for b in sys if isinstance(b, dict))
        msgs.append({"role": "system", "content": str(sys)})
    for m in body.get("messages", []):
        role = m.get("role", "user")
        c = m.get("content")
        if isinstance(c, str):
            c = [{"type": "text", "text": c}]
        if not isinstance(c, list):
            c = []
        # tool_result(用户消息内)→ 独立的 role=tool 消息,必须在 assistant tool_calls 之后
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                rc = b.get("content")
                if isinstance(rc, list):
                    rc = "\n".join(str(x.get("text", "")) for x in rc
                                   if isinstance(x, dict))
                elif not isinstance(rc, str):
                    rc = json.dumps(rc, ensure_ascii=False)
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                             "content": rc or ""})
        text = "".join(b.get("text", "") for b in c
                       if isinstance(b, dict) and b.get("type") == "text")
        images = [p for p in (_anthropic_block_to_openai(b) for b in c
                              if isinstance(b, dict) and b.get("type") == "image") if p]
        tool_uses = [b for b in c
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
        if tool_uses:
            msgs.append({"role": "assistant" if role == "assistant" else role,
                         "content": text,
                         "tool_calls": [{"id": b.get("id", ""), "type": "function",
                                         "function": {
                                             "name": b.get("name", ""),
                                             "arguments": json.dumps(
                                                 b.get("input") or {},
                                                 ensure_ascii=False)}}
                                        for b in tool_uses]})
        elif images:
            parts = ([{"type": "text", "text": text}] if text else []) + images
            msgs.append({"role": role, "content": parts})
        elif text:
            msgs.append({"role": role, "content": text})
        # 仅含 tool_result 的消息已拆为 role=tool,此处不再追加空消息
    out: Dict[str, Any] = {"model": body.get("model"), "messages": msgs,
                           "max_tokens": body.get("max_tokens") or 4096}
    tools = _anthropic_tools_to_openai(body.get("tools"))
    if tools:
        out["tools"] = tools
    tc = body.get("tool_choice")
    if tc == "auto":
        out["tool_choice"] = "auto"
    elif tc == "required":
        out["tool_choice"] = "required"
    elif isinstance(tc, dict):
        if tc.get("type") == "any":
            out["tool_choice"] = "required"
        elif tc.get("type") == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc["name"]}}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True
    return out

def _openai_resp_to_anthropic(data: Dict[str, Any], alias: str) -> Dict[str, Any]:
    msg = (((data or {}).get("choices") or [{}])[0].get("message")) or {}
    content: List[Dict[str, Any]] = []
    if msg.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": msg["reasoning_content"]})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments", "")}
        content.append({"type": "tool_use", "id": tc.get("id") or f"call_gw_{i}",
                        "name": fn.get("name", ""),
                        "input": args if isinstance(args, dict) else {}})
    if not content:
        content.append({"type": "text", "text": ""})
    fr = (((data or {}).get("choices") or [{}])[0].get("finish_reason")) or "stop"
    if msg.get("tool_calls"):
        stop_reason = "tool_use"
    elif fr == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    u = (data or {}).get("usage") or {}
    return {"id": "msg_gw_" + uuid.uuid4().hex[:12], "type": "message",
            "role": "assistant", "model": alias, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0) or 0,
                      "output_tokens": u.get("completion_tokens", 0) or 0}}

@app.post("/v1/messages")
async def gw_messages(request: Request):
    """Anthropic 协议入口:请求转内部 OpenAI 格式分发,响应转回 Anthropic 格式;
    Anthropic 协议渠道直接透传。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    alias = str(body.get("model") or "")
    if not alias:
        raise HTTPException(400, "缺少 model 字段")
    key_rec = _gw_check_key(request, alias)
    channels = _gw_filter_channels(key_rec, _gw_channels_for(alias, "chat"))
    if not channels:
        raise HTTPException(404, f"无可用渠道提供模型 {alias}")

    openai_body = _anthropic_req_to_openai(body)
    if body.get("stream"):
        return StreamingResponse(
            _gw_stream_chat(key_rec, channels, openai_body, alias, "/v1/messages",
                            anthropic_out=True, raw_body=body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Gateway-Endpoint": "/v1/messages"})

    t0 = time.perf_counter()
    last_err = "无渠道可达"
    for ch in channels:
        endpoint, headers, payload, proto = _gw_upstream_chat_request(ch, openai_body, alias)
        if proto == "anthropic":
            payload = {**body, "model": alias}     # 同协议直通
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600, sock_read=300)) as session:
                async with session.post(endpoint, json=payload, headers=headers,
                                        ssl=False) as resp:
                    if resp.status >= 500:
                        last_err = f"HTTP {resp.status}"
                        continue
                    text = await resp.text()
                    if resp.status >= 400:
                        _gw_log_call(key_rec, ch["name"], alias, "/v1/messages",
                                     resp.status, (time.perf_counter() - t0) * 1000, {})
                        return Response(content=text, status_code=resp.status,
                                        media_type="application/json")
                    try:
                        data = json.loads(text)
                    except Exception:
                        _gw_log_call(key_rec, ch["name"], alias, "/v1/messages",
                                     502, (time.perf_counter() - t0) * 1000, {})
                        raise HTTPException(502, "上游返回非 JSON")
                    if proto == "anthropic":
                        out = data if isinstance(data, dict) else {}
                        if isinstance(out, dict):
                            out["model"] = alias
                        u = (out.get("usage") or {})
                        usage_log = {"prompt_tokens": u.get("input_tokens", 0) or 0,
                                     "completion_tokens": u.get("output_tokens", 0) or 0}
                    elif proto == "ollama":
                        out = _openai_resp_to_anthropic(_ollama_to_openai(data, alias), alias)
                        usage_log = {"prompt_tokens": out["usage"]["input_tokens"],
                                     "completion_tokens": out["usage"]["output_tokens"]}
                    else:
                        out = _openai_resp_to_anthropic(data, alias)
                        usage_log = {"prompt_tokens": out["usage"]["input_tokens"],
                                     "completion_tokens": out["usage"]["output_tokens"]}
                    _gw_log_call(key_rec, ch["name"], alias, "/v1/messages",
                                 200, (time.perf_counter() - t0) * 1000, usage_log)
                    return JSONResponseCompat(out)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    raise HTTPException(502, f"所有渠道均不可达: {last_err}")

@app.post("/v1/messages/count_tokens")
async def gw_messages_count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    _gw_check_key(request, str(body.get("model") or ""))
    text = json.dumps(body.get("messages", []), ensure_ascii=False)
    sys = body.get("system")
    if sys:
        text += json.dumps(sys, ensure_ascii=False)
    return {"input_tokens": estimate_tokens(text)}

def JSONResponseCompat(data: Any):
    """普通 dict/列表直接返回,保持与 FastAPI 默认一致。"""
    from fastapi.responses import JSONResponse
    return JSONResponse(data)

def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s or "", safe="")

async def _gw_forward_json(request: Request, kind: str, path: str,
                           method: str = "POST"):
    """图片 / 视频 / 嵌入等 OpenAI 风格接口的通用转发(JSON 体,改写 model)。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    alias = str(body.get("model") or "")
    if not alias:
        raise HTTPException(400, "缺少 model 字段")
    key_rec = _gw_check_key(request, alias)
    channels = _gw_filter_channels(key_rec, _gw_channels_for(alias, kind))
    if not channels:
        raise HTTPException(404, f"无可用渠道提供模型 {alias}")
    t0 = time.perf_counter()
    last_err = "无渠道可达"
    for ch in channels:
        base = ch["base_url"]
        url = f"{base}{path}"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {ch.get('api_key', '')}" if ch.get("api_key") else ""}
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=900, sock_read=300)) as session:
                async with session.request(method, url, json={**body, "model": alias},
                                           headers=headers, ssl=False) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        last_err = f"HTTP {resp.status}"
                        continue
                    ctype = resp.headers.get("Content-Type", "application/json")
                    _gw_log_call(key_rec, ch["name"], alias, path, resp.status,
                                 (time.perf_counter() - t0) * 1000, {})
                    if resp.status >= 400:   # 上游参数/鉴权错误:原状态码透传
                        return Response(content=text, status_code=resp.status,
                                        media_type=ctype or "application/json")
                    if "json" in ctype:
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                data["model"] = alias
                            return JSONResponseCompat(data)
                        except Exception:
                            pass
                    return Response(content=text, status_code=resp.status,
                                    media_type=ctype or "application/json")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    raise HTTPException(502, f"所有渠道均不可达: {last_err}")

@app.post("/v1/images/generations")
async def gw_images(request: Request):
    return await _gw_forward_json(request, "image", "/v1/images/generations")

@app.post("/v1/embeddings")
async def gw_embeddings(request: Request):
    return await _gw_forward_json(request, "embedding", "/v1/embeddings")

@app.post("/v1/videos")
async def gw_videos_submit(request: Request):
    return await _gw_forward_json(request, "video", "/v1/videos")

@app.get("/v1/videos/{vid}")
async def gw_videos_query(vid: str, request: Request):
    return await _gw_forward_query(request, "video", f"/v1/videos/{vid}")

@app.get("/v1/videos/{vid}/content")
async def gw_videos_content(vid: str, request: Request):
    return await _gw_forward_query(request, "video", f"/v1/videos/{vid}/content", stream=True)

@app.post("/v1/video_generation")
async def gw_video_generation(request: Request):
    """MiniMax 云风格视频接口兼容。"""
    return await _gw_forward_json(request, "video", "/v1/video_generation")

@app.get("/v1/query/video_generation")
async def gw_query_video_generation(request: Request, task_id: str = ""):
    if not task_id:
        raise HTTPException(400, "缺少 task_id")
    return await _gw_forward_query(request, "video",
                                   f"/v1/query/video_generation?task_id={task_id}")

# ---- MiniMax 视频生成 V2(H3):创建 / 查询 / 列表 / 再生成 / Context-IR / 取消删除 ----

@app.post("/v2/video_generation")
async def gw_v2_video_create(request: Request):
    """创建视频生成任务(content 多模态数组:t2va / i2va 首尾帧 / r2va 参考)。"""
    return await _gw_forward_json(request, "video", "/v2/video_generation")

@app.get("/v2/query/video_generation/{task_id}")
async def gw_v2_video_query(task_id: str, request: Request):
    return await _gw_forward_query(request, "video",
                                   f"/v2/query/video_generation/{task_id}")

@app.get("/v2/query/video_generation")
async def gw_v2_video_list(request: Request):
    """任务列表:原样透过滤参数(page_num/page_size/filter.*)。"""
    qs = request.url.query
    path = "/v2/query/video_generation" + (f"?{qs}" if qs else "")
    return await _gw_forward_query(request, "video", path)

@app.post("/v2/video_regeneration")
async def gw_v2_video_regen(request: Request):
    """视频再生成:source_task_id 或 content 中 base_video,768P 源 → 2K。"""
    return await _gw_forward_json(request, "video", "/v2/video_regeneration")

@app.post("/v2/h3_context_ir")
async def gw_v2_context_ir(request: Request):
    """H3-Context-IR:多模态上下文 → 结构化视频提示词。"""
    return await _gw_forward_json(request, "video", "/v2/h3_context_ir")

@app.delete("/v2/video_generation/{task_id}")
async def gw_v2_video_delete(task_id: str, request: Request):
    """取消(queued)或删除(succeeded/failed)任务:?action=cancelled|deleted。"""
    action = request.query_params.get("action", "cancelled")
    return await _gw_forward_query(
        request, "video", f"/v2/video_generation/{task_id}?action={action}",
        method="DELETE")

async def _gw_forward_query(request: Request, kind: str, path: str,
                            stream: bool = False, method: str = "GET"):
    """GET/DELETE 类转发:遍历该类型全部启用渠道(受密钥渠道绑定限制),找到能答的那个。"""
    key_rec = _gw_check_key(request)
    chs = [c for c in _load_gateway()["channels"]
           if c.get("enabled", True) and kind in (c.get("types") or [])]
    chs = _gw_filter_channels(key_rec, chs)
    last_err = "无渠道可达"
    for ch in chs:
        url = f"{ch['base_url']}{path}"
        headers = {"Authorization": f"Bearer {ch.get('api_key', '')}" if ch.get("api_key") else ""}
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=900, sock_read=300)) as session:
                async with session.request(method, url, headers=headers, ssl=False) as resp:
                    if resp.status == 404:
                        last_err = "HTTP 404"
                        continue
                    if stream:
                        # 二进制内容透传(视频文件)
                        data = await resp.read()
                        return Response(content=data, status_code=resp.status,
                                        media_type=resp.headers.get("Content-Type", "application/octet-stream"))
                    text = await resp.text()
                    ctype = resp.headers.get("Content-Type", "application/json")
                    if "json" in ctype:
                        try:
                            return JSONResponseCompat(json.loads(text))
                        except Exception:
                            pass
                    return Response(content=text, status_code=resp.status, media_type=ctype)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue
    raise HTTPException(502, f"查询失败: {last_err}")

@app.post("/v1/audio/speech")
async def gw_audio_speech(request: Request):
    """语音合成:上游二进制音频透传给调用方。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    alias = str(body.get("model") or "")
    if not alias:
        raise HTTPException(400, "缺少 model 字段")
    key_rec = _gw_check_key(request, alias)
    channels = _gw_filter_channels(key_rec, _gw_channels_for(alias, "audio"))
    if not channels:
        raise HTTPException(404, f"无可用渠道提供模型 {alias}")
    t0 = time.perf_counter()
    for ch in channels:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {ch.get('api_key', '')}" if ch.get("api_key") else ""}
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600, sock_read=180)) as session:
                async with session.post(f"{ch['base_url']}/v1/audio/speech",
                                        json={**body, "model": alias},
                                        headers=headers, ssl=False) as resp:
                    data = await resp.read()
                    if resp.status >= 500:
                        continue
                    _gw_log_call(key_rec, ch["name"], alias, "/v1/audio/speech", resp.status,
                                 (time.perf_counter() - t0) * 1000, {})
                    return Response(content=data, status_code=resp.status,
                                    media_type=resp.headers.get("Content-Type", "audio/mpeg"))
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError):
            continue
    raise HTTPException(502, "语音合成失败:无可用渠道")

async def _gw_forward_multipart(request: Request, path: str):
    """语音识别/翻译:multipart 文件原样转发(仅改 model 字段)。"""
    form = await request.form()
    model = str(form.get("model") or "")
    if not model:
        raise HTTPException(400, "缺少 model 字段")
    key_rec = _gw_check_key(request, model)
    channels = _gw_filter_channels(key_rec, _gw_channels_for(model, "audio"))
    if not channels:
        raise HTTPException(404, f"无可用渠道提供模型 {model}")
    t0 = time.perf_counter()
    # 读出上传文件字节
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "缺少 file 字段")
    raw = await upload.read()
    fname = upload.filename or "audio.mp3"
    extra_fields = {k: str(v) for k, v in form.multi_items()
                    if k not in ("file", "model")}
    for ch in channels:
        headers = {"Authorization": f"Bearer {ch.get('api_key', '')}" if ch.get("api_key") else ""}
        try:
            mp = aiohttp.FormData()
            mp.add_field("file", raw, filename=fname,
                         content_type="application/octet-stream")
            mp.add_field("model", model)
            for k, v in extra_fields.items():
                mp.add_field(k, v)
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=600, sock_read=180)) as session:
                async with session.post(f"{ch['base_url']}{path}", data=mp,
                                        headers=headers, ssl=False) as resp:
                    text = await resp.text()
                    if resp.status >= 500:
                        continue
                    ctype = resp.headers.get("Content-Type", "application/json")
                    _gw_log_call(key_rec, ch["name"], model, path, resp.status,
                                 (time.perf_counter() - t0) * 1000, {})
                    if resp.status >= 400:   # 上游错误原状态码透传
                        return Response(content=text, status_code=resp.status,
                                        media_type=ctype or "application/json")
                    if "json" in ctype:
                        try:
                            return JSONResponseCompat(json.loads(text))
                        except Exception:
                            pass
                    return Response(content=text, status_code=resp.status, media_type=ctype)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                asyncio.TimeoutError, ConnectionError):
            continue
    raise HTTPException(502, "语音识别失败:无可用渠道")

@app.post("/v1/audio/transcriptions")
async def gw_audio_transcriptions(request: Request):
    return await _gw_forward_multipart(request, "/v1/audio/transcriptions")

@app.post("/v1/audio/translations")
async def gw_audio_translations(request: Request):
    return await _gw_forward_multipart(request, "/v1/audio/translations")

# ============================================================================
# ModelUse 技能库 / 提示词库(服务端存储,任何浏览器打开 /modeluse 都能用)
# ============================================================================

_SEED_SKILLS = [
    {"id": "sk-translate", "icon": "🌐", "name": "translate",
     "description": "中英互译,保留格式与术语表",
     "content": "你是资深翻译。请把用户提供的文本在中文与英文之间互译(输入是英文则译成中文,反之译成英文)。\n要求:\n- 保留原文的 Markdown 格式、列表与代码块\n- 专业术语保持一致,首次出现时在括号内附原文\n- 只输出译文,不要解释"},
    {"id": "sk-code-review", "icon": "🔍", "name": "code-review",
     "description": "按严重级别输出代码审查意见",
     "content": "你是严格的代码审查员。审查用户给出的代码,按以下结构输出:\n1. 🔴 严重问题(会导致 bug/安全问题)\n2. 🟡 建议改进(可读性/性能)\n3. 🟢 优点\n每条注明行号与修改建议,最后给出修改后的完整代码。"},
    {"id": "sk-sql-opt", "icon": "🗄", "name": "sql-optimize",
     "description": "SQL 优化:执行计划思路 + 改写",
     "content": "你是数据库优化专家。分析用户给出的 SQL:\n1. 指出全表扫描、索引失效、隐式转换等问题\n2. 给出建议的索引(含列顺序理由)\n3. 给出改写后的 SQL\n4. 说明预期收益。数据库类型未知时先询问。"},
    {"id": "sk-weekly", "icon": "📋", "name": "weekly-report",
     "description": "把流水记录整理成结构化周报",
     "content": "你是周报撰写助手。把用户提供的零散工作记录整理成周报:\n## 本周完成\n## 数据与结果(量化)\n## 问题与风险\n## 下周计划\n语言精炼,量化结果优先,不要编造数据。"},
    {"id": "sk-api-doc", "icon": "📘", "name": "api-doc",
     "description": "把接口代码转成 REST API 文档",
     "content": "你是 API 文档工程师。阅读用户给出的接口代码,输出 Markdown 文档:接口说明、方法与路径、参数表(名称/类型/必填/说明)、请求与响应示例、错误码。示例必须与代码一致。"},
    {"id": "sk-troubleshoot", "icon": "🧰", "name": "troubleshoot",
     "description": "系统性排障:现象→假设→验证步骤",
     "content": "你是排障专家。针对用户描述的故障:\n1. 复述现象与影响面\n2. 列出 3 个最可能的原因(按概率排序)\n3. 每个原因给出具体验证命令/步骤\n4. 给出定位后的修复方向。信息不足时先列出需要补充的信息。"},
]

_SEED_PROMPTS = [
    {"id": "pr-role", "category": "角色设定", "name": "领域专家角色",
     "content": "你是一位{{领域}}资深专家,拥有 10 年以上实践经验。回答时:\n- 先给结论,再给依据\n- 主动指出常见误区\n- 涉及取舍时列出对比表", "variables": ["领域"]},
    {"id": "pr-cot", "category": "思维框架", "name": "思维链推理",
     "content": "请一步步思考后回答:\n1. 拆解问题中的已知条件与目标\n2. 列出可能的解决路径并选择最优\n3. 逐步执行,每步给出依据\n4. 最后用一段话总结答案", "variables": []},
    {"id": "pr-polish", "category": "文本处理", "name": "文案润色",
     "content": "请润色以下文案,目标读者是{{读者}},语气要求{{语气}}。保持原意,输出润色后的版本,并用列表说明主要修改点:\n\n{{原文}}", "variables": ["读者", "语气", "原文"]},
    {"id": "pr-summary", "category": "文本处理", "name": "长文摘要",
     "content": "请把以下内容压缩为摘要:\n- 一句话结论(≤30 字)\n- 3~5 条要点(每条 ≤20 字)\n- 保留关键数字与专有名词\n\n{{原文}}", "variables": ["原文"]},
    {"id": "pr-fewshot", "category": "思维框架", "name": "少样本分类",
     "content": "请对输入文本进行{{任务}}分类。参考示例:\n输入:今天天气真好 → 积极\n输入:服务器又挂了 → 消极\n现在分类以下输入,只输出类别:\n{{输入}}", "variables": ["任务", "输入"]},
    {"id": "pr-unit-test", "category": "开发辅助", "name": "生成单元测试",
     "content": "请为以下{{语言}}代码生成单元测试:覆盖正常路径、边界条件、异常分支;使用该语言主流测试框架;每个用例一句注释说明意图。代码:\n\n{{代码}}", "variables": ["语言", "代码"]},
    {"id": "pr-explain", "category": "学习辅导", "name": "费曼式讲解",
     "content": "请用费曼技巧讲解「{{概念}}」:先用生活化类比讲给外行,再逐层深入到原理,最后给出 3 个自测问题与答案。", "variables": ["概念"]},
    {"id": "pr-email", "category": "职场写作", "name": "商务邮件",
     "content": "帮我写一封{{语言风格}}的商务邮件,收件方是{{收件方}},目的是{{目的}}。要求主题明确、正文 ≤200 字、结尾给出明确的下一步动作。", "variables": ["语言风格", "收件方", "目的"]},
]

def _load_lib() -> Dict[str, Any]:
    with _DB_LOCK:
        conn = _db_conn()
        try:
            skills, prompts = [], []
            for r in conn.execute("SELECT * FROM library ORDER BY rowid").fetchall():
                if r["kind"] == "skill":
                    skills.append({"id": r["id"], "icon": r["icon"] or "🛠", "name": r["name"],
                                   "description": r["description"] or "", "content": r["content"] or ""})
                elif r["kind"] == "prompt":
                    prompts.append({"id": r["id"], "category": r["category"] or "", "name": r["name"],
                                    "content": r["content"] or "",
                                    "variables": json.loads(r["variables"] or "[]")})
            if not skills and not prompts:      # 空库 → 写入种子数据
                lib = {"skills": _SEED_SKILLS, "prompts": _SEED_PROMPTS}
                _db_write_lib(conn, lib)
                conn.commit()
                return lib
            return {"skills": skills, "prompts": prompts}
        finally:
            conn.close()

def _save_lib(lib: Dict[str, Any]) -> None:
    with _DB_LOCK:
        conn = _db_conn()
        try:
            _db_write_lib(conn, lib)
            conn.commit()
        finally:
            conn.close()

@app.get("/api/modeluse/skills")
async def mu_skills():
    return _load_lib()["skills"]

class LibItemIn(BaseModel):
    id: str = ""
    name: str
    icon: str = "🛠"
    description: str = ""
    content: str = ""
    category: str = ""
    variables: List[str] = []

@app.post("/api/modeluse/skills")
async def mu_save_skill(item: LibItemIn):
    lib = _load_lib()
    rec = {"id": item.id or f"sk-{uuid.uuid4().hex[:6]}", "icon": item.icon or "🛠",
           "name": item.name.strip(), "description": item.description,
           "content": item.content}
    if not rec["name"] or not rec["content"].strip():
        raise HTTPException(400, "名称与内容不能为空")
    if item.id and any(s["id"] == item.id for s in lib["skills"]):
        lib["skills"] = [rec if s["id"] == item.id else s for s in lib["skills"]]
    else:
        lib["skills"].append(rec)
    _save_lib(lib)
    return {"ok": True, "id": rec["id"]}

@app.delete("/api/modeluse/skills/{sid}")
async def mu_del_skill(sid: str):
    lib = _load_lib()
    lib["skills"] = [s for s in lib["skills"] if s["id"] != sid]
    _save_lib(lib)
    return {"ok": True}

@app.get("/api/modeluse/prompts")
async def mu_prompts():
    return _load_lib()["prompts"]

@app.post("/api/modeluse/prompts")
async def mu_save_prompt(item: LibItemIn):
    lib = _load_lib()
    rec = {"id": item.id or f"pr-{uuid.uuid4().hex[:6]}", "category": item.category or "通用",
           "name": item.name.strip(), "content": item.content,
           "variables": item.variables or re.findall(r"\{\{(.+?)\}\}", item.content)}
    if not rec["name"] or not rec["content"].strip():
        raise HTTPException(400, "名称与内容不能为空")
    if item.id and any(p["id"] == item.id for p in lib["prompts"]):
        lib["prompts"] = [rec if p["id"] == item.id else p for p in lib["prompts"]]
    else:
        lib["prompts"].append(rec)
    _save_lib(lib)
    return {"ok": True, "id": rec["id"]}

@app.delete("/api/modeluse/prompts/{pid}")
async def mu_del_prompt(pid: str):
    lib = _load_lib()
    lib["prompts"] = [p for p in lib["prompts"] if p["id"] != pid]
    _save_lib(lib)
    return {"ok": True}

# ============================================================================
# Context Limit Probe (max input / output tokens)
# ============================================================================

# 进行中的探测注册表: probe_id -> 取消事件(点击「探测」按钮可停止跟随探测)
_PROBE_CANCELS: Dict[str, asyncio.Event] = {}

class ProbeCancelled(Exception):
    """用户主动停止上下文探测"""

def _build_probe_call(protocol, base, api_key, model, prompt, max_tokens):
    if protocol == "anthropic":
        return (
            f"{base}/v1/messages",
            {"Content-Type": "application/json", "x-api-key": api_key or "",
             "anthropic-version": "2023-06-01"},
            {"model": model, "max_tokens": max_tokens,
             "messages": [{"role": "user", "content": prompt}]},
        )
    if protocol == "ollama":
        return (
            f"{base}/api/chat",
            {"Content-Type": "application/json"},
            {"model": model, "stream": False,
             "messages": [{"role": "user", "content": prompt}],
             "options": {"num_predict": max_tokens}},
        )
    return (
        f"{base}/v1/chat/completions",
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {api_key}" if api_key else ""},
        {"model": model, "max_tokens": max_tokens,
         "messages": [{"role": "user", "content": prompt}]},
    )

async def _try_call(session, endpoint, headers, payload):
    try:
        async with session.post(endpoint, json=payload, headers=headers, ssl=False) as r:
            body = await r.text()
            if r.status < 400:
                return True, ""
            return False, f"HTTP {r.status}: {body[:150]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"

async def _search_limit(check, lo: int, cap: int, cancel_ev: Optional[asyncio.Event] = None):
    """Exponential growth to bracket, then binary search. check(n)->bool."""
    ok_n, fail_n = None, None
    n = lo
    while n <= cap:
        if cancel_ev is not None and cancel_ev.is_set():
            raise ProbeCancelled()
        if await check(n):
            ok_n = n
            n *= 2
        else:
            fail_n = n
            break
    if ok_n is None:
        return {"value": None, "note": f"最小探测值 {lo} 即失败,请检查模型名/鉴权/服务状态"}
    if fail_n is None:
        return {"value": ok_n, "note": f"≥{ok_n}(已达探测上限 {cap},未触发限制)"}
    while fail_n - ok_n > 512:
        if cancel_ev is not None and cancel_ev.is_set():
            raise ProbeCancelled()
        mid = (ok_n + fail_n) // 2
        if await check(mid):
            ok_n = mid
        else:
            fail_n = mid
    return {"value": ok_n, "note": f"约在 {ok_n} ~ {fail_n} tokens 之间"}

@app.post("/api/probe-limits")
async def probe_limits(req: ProbeRequest):
    if not req.api_url or not req.model:
        raise HTTPException(400, "请先填写 API 地址和模型")
    url = ProtocolDetector._normalize(req.api_url)
    base = re.sub(r"/v1/?$", "", url)
    timeout = aiohttp.ClientTimeout(total=600, sock_read=120)
    meta: Dict[str, Any] = {}
    cancel_ev = asyncio.Event()
    if req.probe_id:
        _PROBE_CANCELS[req.probe_id] = cancel_ev
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Metadata hints (SGLang /get_server_info, vLLM /v1/models max_model_len)
            try:
                s, b = await detector._get(session, f"{base}/get_server_info")
                d = detector._json(b)
                if s == 200 and isinstance(d, dict):
                    for k in ("max_total_num_tokens", "context_length", "max_prefill_tokens"):
                        if isinstance(d.get(k), int):
                            meta[k] = d[k]
            except Exception:
                pass
            try:
                headers = {"Authorization": f"Bearer {req.api_key}"} if req.api_key else {}
                s, b = await detector._get(session, f"{base}/v1/models", headers=headers)
                d = detector._json(b)
                if s == 200 and isinstance(d, dict):
                    for m in d.get("data", []):
                        if isinstance(m, dict) and m.get("id") == req.model:
                            if isinstance(m.get("max_model_len"), int):
                                meta["max_model_len"] = m["max_model_len"]
            except Exception:
                pass

            async def check_input(n):
                prompt = generate_prompt(n, "en")
                ep, hd, pl = _build_probe_call(
                    req.protocol, base, req.api_key, req.model, prompt, 1)
                ok, _ = await _try_call(session, ep, hd, pl)
                return ok

            async def check_output(n):
                ep, hd, pl = _build_probe_call(
                    req.protocol, base, req.api_key, req.model, "Hi", n)
                ok, _ = await _try_call(session, ep, hd, pl)
                return ok

            input_res = await _search_limit(check_input, lo=512, cap=1048576, cancel_ev=cancel_ev)
            if cancel_ev.is_set():
                raise ProbeCancelled()
            output_res = await _search_limit(check_output, lo=256, cap=262144, cancel_ev=cancel_ev)

        return {
            "metadata": meta,
            "max_input_tokens": input_res,
            "max_output_tokens": output_res,
        }
    except ProbeCancelled:
        raise HTTPException(400, "探测已停止")
    finally:
        if req.probe_id:
            _PROBE_CANCELS.pop(req.probe_id, None)

@app.post("/api/probe-limits/cancel")
async def probe_limits_cancel(req: ProbeCancelRequest):
    """停止进行中的上下文探测(按 probe_id 匹配,当前正在执行的请求完成后不再继续)"""
    ev = _PROBE_CANCELS.get((req.probe_id or "").strip())
    if ev is None:
        return {"ok": False, "message": "没有进行中的探测"}
    ev.set()
    return {"ok": True}

@app.get("/api/download/{filename}")
async def download(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(get_reports_dir(), safe)
    if not os.path.exists(path):
        raise HTTPException(404, "Report not found")
    return FileResponse(
        path, filename=safe,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

class ConfigUpdate(BaseModel):
    reports_dir: str = ""

@app.get("/api/config")
async def get_config():
    cfg = _load_config()
    return {
        "reports_dir": get_reports_dir(),
        "custom": bool((cfg.get("reports_dir") or "").strip()),
    }

@app.post("/api/config")
async def set_config(body: ConfigUpdate):
    d = body.reports_dir.strip()
    cfg = _load_config()
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"目录无效或不可写: {e}")
        if not os.path.isdir(d):
            raise HTTPException(400, "路径不是目录")
        cfg["reports_dir"] = os.path.abspath(d)
    else:
        cfg.pop("reports_dir", None)
    _save_config(cfg)
    return {"ok": True, "reports_dir": get_reports_dir()}

@app.get("/api/reports")
async def list_reports():
    items = []
    for f in os.listdir(get_reports_dir()):
        if not f.endswith(".xlsx"):
            continue
        p = os.path.join(get_reports_dir(), f)
        st = os.stat(p)
        item = {
            "filename": f,
            "size_kb": round(st.st_size / 1024, 1),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "model": "",
            "framework": "",
        }
        # 优先读边车元数据
        sidecar = os.path.join(get_reports_dir(), f[:-5] + ".json")
        if os.path.exists(sidecar):
            try:
                with open(sidecar, encoding="utf-8") as sf:
                    meta = json.load(sf)
                item["model"] = meta.get("model", "")
                item["framework"] = meta.get("framework", "")
            except Exception:
                pass
        # 旧文件:从文件名回退解析 benchmark_<model>_<framework>_<ts>.xlsx
        if not item["model"]:
            m = re.match(r"benchmark_(.+)_(\d{8}_\d{6})\.xlsx$", f)
            if m:
                body = m.group(1)
                # 框架名在生成时已知,用它做分隔
                fw_match = re.search(
                    r"_(vLLM|SGLang|Ollama|TGI|OpenAI-compatible|OpenAI|Anthropic|LM-Studio|Unknown)[-_0-9A-Za-z.]*$",
                    body,
                )
                if fw_match:
                    item["model"] = body[: fw_match.start()]
                    item["framework"] = fw_match.group(0).lstrip("_")
                else:
                    item["model"] = body
        items.append(item)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items

@app.delete("/api/reports/{filename}")
async def delete_report(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(get_reports_dir(), safe)
    if not os.path.exists(path):
        raise HTTPException(404, "Report not found")
    os.remove(path)
    sidecar = os.path.join(get_reports_dir(), safe[:-5] + ".json")
    if os.path.exists(sidecar):
        os.remove(sidecar)
    return {"ok": True}

@app.get("/api/preview/{filename}")
async def preview(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(get_reports_dir(), safe)
    if not os.path.exists(path):
        raise HTTPException(404, "Report not found")
    wb = load_workbook(path, read_only=True, data_only=True)
    sheets = []
    for name in wb.sheetnames:
        ws = wb[name]
        max_rows = 60 if name == "汇总" else 50
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append([
                "" if v is None else (str(v)[:500] + "…" if len(str(v)) > 500 else v)
                for v in row
            ])
        sheets.append({
            "name": name,
            "rows": rows,
            "truncated": (ws.max_row or 0) > max_rows,
            "total_rows": ws.max_row or 0,
        })
    wb.close()
    return {"filename": safe, "sheets": sheets}

@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-store"})

@app.get("/modeluse")
async def modeluse_page():
    """模型工作台:SPA 内 hash 路由页,旧链接重定向。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/#modeluse")

# ---- 旧版原生 JS 页面(保留一个版本作回滚与对照,之后移除) ----
@app.get("/legacy-index")
async def legacy_index_page():
    return FileResponse(os.path.join(STATIC_DIR, "legacy-index.html"))

@app.get("/legacy-modeluse")
async def legacy_modeluse_page():
    return FileResponse(os.path.join(STATIC_DIR, "legacy-modeluse.html"))

# ---- 兼容别名(切换前的开发路由) ----
@app.get("/modeluse-v2")
async def modeluse_v2_page():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/#modeluse")

@app.get("/v2")
async def index_v2_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-store"})

@app.get("/api/readme")
async def readme():
    path = os.path.join(BASE_DIR, "README.md")
    if not os.path.exists(path):
        return {"content": "# 使用说明\n\nREADME.md 不存在"}
    with open(path, encoding="utf-8") as f:
        return {"content": f.read()}



# 内嵌贪吃蛇游戏页面(压测等待时的小游戏),减少部署文件数
GAME_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🐍 贪吃蛇</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            display: flex;
            justify-content: center;
            align-items: flex-start;
            min-height: 100vh;
            padding: 6px 8px;
            background: #1a1a2e;
            font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
            user-select: none;
            -webkit-user-select: none;
        }
        .game-wrapper { text-align: center; }
        .game-title {
            font-size: 0;
            color: transparent;
            margin-bottom: 0;
            letter-spacing: 0;
        }
        .game-container {
            position: relative;
            display: inline-block;
            background: #16213e;
            border-radius: 16px;
            padding: 12px;
            box-shadow: 0 0 40px rgba(78, 204, 163, 0.2), 0 0 80px rgba(78, 204, 163, 0.1);
        }
        canvas {
            display: block;
            border-radius: 8px;
            background: #0f0f1a;
            box-shadow: inset 0 0 20px rgba(0, 0, 0, 0.4);
        }
        .score-board {
            display: flex;
            justify-content: space-around;
            align-items: center;
            margin-top: 10px;
            padding: 0 10px;
        }
        .score-item { text-align: center; }
        .score-label {
            font-size: 13px;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-bottom: 4px;
        }
        .score-value {
            font-size: 24px;
            font-weight: bold;
            color: #e94560;
            font-variant-numeric: tabular-nums;
            font-family: 'Courier New', monospace;
        }
        .best-value { color: #f5a623; }
        .btn-group {
            display: flex;
            gap: 10px;
            justify-content: center;
            margin-top: 10px;
            flex-wrap: wrap;
        }
        .speed-group {
            display: flex;
            gap: 8px;
            justify-content: center;
            margin-top: 14px;
            align-items: center;
            flex-wrap: wrap;
        }
        .speed-label { color: #888; font-size: 13px; letter-spacing: 1px; }
        .speed-btn {
            padding: 8px 18px;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid #3a3a7a;
            border-radius: 20px;
            cursor: pointer;
            background: #232354;
            color: #4ecca3;
            transition: all 0.2s;
            font-family: inherit;
            letter-spacing: 1px;
        }
        .speed-btn:hover { border-color: #4ecca3; }
        .speed-btn.active {
            background: linear-gradient(135deg, #4ecca3, #38b990);
            color: #1a1a2e;
            border-color: transparent;
            box-shadow: 0 3px 12px rgba(78, 204, 163, 0.4);
        }
        .btn {
            padding: 10px 26px;
            font-size: 15px;
            font-weight: 700;
            border: none;
            border-radius: 30px;
            cursor: pointer;
            letter-spacing: 2px;
            transition: all 0.25s ease;
            outline: none;
            font-family: inherit;
        }
        .btn-start {
            background: linear-gradient(135deg, #4ecca3, #38b990);
            color: #1a1a2e;
            box-shadow: 0 4px 15px rgba(78, 204, 163, 0.4);
        }
        .btn-start:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 25px rgba(78, 204, 163, 0.6);
        }
        .btn-start:active { transform: translateY(0); }
        .btn-pause {
            background: linear-gradient(135deg, #e94560, #d03550);
            color: #fff;
            box-shadow: 0 4px 15px rgba(233, 69, 96, 0.4);
        }
        .btn-pause:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 25px rgba(233, 69, 96, 0.6);
        }
        .btn-pause:active { transform: translateY(0); }
        .btn-pause.paused {
            background: linear-gradient(135deg, #f5a623, #e09610);
            box-shadow: 0 4px 15px rgba(245, 166, 35, 0.4);
        }
        .btn-pause.paused:hover {
            box-shadow: 0 6px 25px rgba(245, 166, 35, 0.6);
        }
        .btn-back {
            background: linear-gradient(135deg, #5b7cfa, #4361ee);
            color: #fff;
            box-shadow: 0 4px 15px rgba(91, 124, 250, 0.4);
        }
        .btn-back:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 25px rgba(91, 124, 250, 0.6);
        }
        .overlay {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            background: rgba(15, 15, 26, 0.85);
            border-radius: 8px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.3s ease;
            z-index: 10;
        }
        .overlay.visible {
            opacity: 1;
            pointer-events: all;
        }
        .overlay-text {
            font-size: 28px;
            font-weight: 700;
            color: #fff;
            margin-bottom: 8px;
            letter-spacing: 3px;
        }
        .overlay-sub {
            font-size: 14px;
            color: #aaa;
            letter-spacing: 1px;
        }
        .direction-hint {
            margin-top: 8px;
            color: #666;
            font-size: 13px;
            letter-spacing: 1px;
        }
        .direction-hint span { color: #4ecca3; font-weight: bold; }
        .mobile-controls { display: none; margin-top: 12px; }
        .dpad {
            display: grid;
            grid-template-columns: 70px 70px 70px;
            grid-template-rows: 70px 70px 70px;
            gap: 4px;
            justify-content: center;
        }
        .dpad-btn {
            background: #232354;
            border: 2px solid #3a3a7a;
            border-radius: 12px;
            font-size: 26px;
            color: #4ecca3;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s;
            -webkit-tap-highlight-color: transparent;
        }
        .dpad-btn:active {
            background: #4ecca3;
            color: #1a1a2e;
            transform: scale(0.9);
        }
        .dpad-empty { visibility: hidden; }
        @media (max-width: 600px) {
            .game-title { font-size: 28px; margin-bottom: 6px; }
            .btn { padding: 10px 22px; font-size: 15px; }
            .mobile-controls { display: block; }
            .direction-hint { display: none; }
            .score-value { font-size: 24px; }
            canvas { width: 100%; height: auto; }
        }
    </style>
</head>
<body>
    <div class="game-wrapper">
        <h1 class="game-title">🐍 贪吃蛇</h1>
        <div class="game-container">
            <canvas id="gameCanvas"></canvas>
            <div class="overlay visible" id="startOverlay">
                <div class="overlay-text">🐍 贪吃蛇</div>
                <div class="overlay-sub">点击"开始游戏"准备出发！</div>
            </div>
            <div class="overlay" id="pauseOverlay">
                <div class="overlay-text">⏸ 已暂停</div>
                <div class="overlay-sub">按"继续"或空格键恢复游戏</div>
            </div>
            <div class="overlay" id="gameOverOverlay">
                <div class="overlay-text" id="gameOverText">💀 游戏结束</div>
                <div class="overlay-sub" id="gameOverSub"></div>
            </div>
        </div>
        <div class="score-board">
            <div class="score-item">
                <div class="score-label">🍎 当前分数</div>
                <div class="score-value" id="scoreDisplay">0</div>
            </div>
            <div class="score-item">
                <div class="score-label">🏆 最高分数</div>
                <div class="score-value best-value" id="bestScoreDisplay">0</div>
            </div>
        </div>
        <div class="btn-group">
            <button class="btn btn-start" id="btnStart">▶ 开始游戏</button>
            <button class="btn btn-pause" id="btnPause" disabled>⏸ 暂停</button>
            <button class="btn btn-back" id="btnBack">🏠 返回首页</button>
        </div>
        <div class="speed-group" id="speedGroup">
            <span class="speed-label">速度:</span>
        </div>
        <div class="direction-hint">
            使用 <span>↑ ↓ ← →</span> 或 <span>W A S D</span> 控制方向
        </div>
        <div class="mobile-controls" id="mobileControls">
            <div class="dpad">
                <div class="dpad-empty"></div>
                <button class="dpad-btn" data-dir="up">▲</button>
                <div class="dpad-empty"></div>
                <button class="dpad-btn" data-dir="left">◀</button>
                <div class="dpad-empty"></div>
                <button class="dpad-btn" data-dir="right">▶</button>
                <div class="dpad-empty"></div>
                <button class="dpad-btn" data-dir="down">▼</button>
                <div class="dpad-empty"></div>
            </div>
        </div>
    </div>
    <script>
(function() {
    const canvas = document.getElementById('gameCanvas');
    const ctx = canvas.getContext('2d');
    const scoreDisplay = document.getElementById('scoreDisplay');
    const bestScoreDisplay = document.getElementById('bestScoreDisplay');
    const btnStart = document.getElementById('btnStart');
    const btnPause = document.getElementById('btnPause');
    const startOverlay = document.getElementById('startOverlay');
    const pauseOverlay = document.getElementById('pauseOverlay');
    const gameOverOverlay = document.getElementById('gameOverOverlay');
    const gameOverText = document.getElementById('gameOverText');
    const gameOverSub = document.getElementById('gameOverSub');

    const GRID_SIZE = 24;
    const COLS = 26;
    const ROWS = 26;
    // 4 档速度: 慢 / 标准 / 快 / 极速 (每步毫秒数)
    const SPEED_LEVELS = [
        { label: '🐢 慢速', ms: 300 },
        { label: '🚶 标准', ms: 180 },
        { label: '🏃 快速', ms: 100 },
        { label: '⚡ 极速', ms: 50 },
    ];
    var speedIdx = 1;

    canvas.width = COLS * GRID_SIZE;
    canvas.height = ROWS * GRID_SIZE;

    var snake = [];
    var food = null;
    var direction = { x: 1, y: 0 };
    var nextDirection = { x: 1, y: 0 };
    var score = 0;
    var bestScore = 0;
    var gameLoopId = null;
    var gameState = 'idle';

    try {
        var saved = localStorage.getItem('snakeBestScore');
        if (saved !== null) bestScore = parseInt(saved, 10) || 0;
    } catch(e) {}
    bestScoreDisplay.textContent = bestScore;

    function randomGridPos() {
        return {
            x: Math.floor(Math.random() * COLS),
            y: Math.floor(Math.random() * ROWS)
        };
    }

    function posInSnake(pos) {
        return snake.some(function(seg) {
            return seg.x === pos.x && seg.y === pos.y;
        });
    }

    function spawnFood() {
        var pos;
        do { pos = randomGridPos(); } while (posInSnake(pos));
        food = pos;
    }

    function initSnake() {
        var startX = Math.floor(COLS / 2);
        var startY = Math.floor(ROWS / 2);
        snake = [
            { x: startX, y: startY },
            { x: startX - 1, y: startY },
            { x: startX - 2, y: startY }
        ];
        direction = { x: 1, y: 0 };
        nextDirection = { x: 1, y: 0 };
        score = 0;
        scoreDisplay.textContent = '0';
    }

    function hideAllOverlays() {
        startOverlay.classList.remove('visible');
        pauseOverlay.classList.remove('visible');
        gameOverOverlay.classList.remove('visible');
    }

    function showOverlay(overlay) {
        hideAllOverlays();
        overlay.classList.add('visible');
    }

    function updatePauseButton() {
        if (gameState === 'paused') {
            btnPause.textContent = '▶ 继续';
            btnPause.classList.add('paused');
            btnPause.disabled = false;
        } else if (gameState === 'playing') {
            btnPause.textContent = '⏸ 暂停';
            btnPause.classList.remove('paused');
            btnPause.disabled = false;
        } else {
            btnPause.textContent = '⏸ 暂停';
            btnPause.classList.remove('paused');
            btnPause.disabled = true;
        }
    }

    function updateStartButton() {
        btnStart.textContent = gameState === 'playing' ? '🔄 重新开始' : '▶ 开始游戏';
    }

    function gameStep() {
        direction = { x: nextDirection.x, y: nextDirection.y };
        var head = snake[0];
        var newHead = { x: head.x + direction.x, y: head.y + direction.y };

        if (newHead.x < 0 || newHead.x >= COLS || newHead.y < 0 || newHead.y >= ROWS) {
            endGame('wall');
            return;
        }
        if (posInSnake(newHead)) {
            endGame('self');
            return;
        }

        snake.unshift(newHead);

        if (newHead.x === food.x && newHead.y === food.y) {
            score += 10;
            scoreDisplay.textContent = score;
            spawnFood();
        } else {
            snake.pop();
        }
    }

    function endGame(reason) {
        clearInterval(gameLoopId);
        gameLoopId = null;
        gameState = 'over';

        if (reason === 'wall') {
            gameOverText.textContent = '💀 撞墙了！';
        } else if (reason === 'self') {
            gameOverText.textContent = '💀 咬到自己了！';
        } else {
            gameOverText.textContent = '💀 游戏结束';
        }
        gameOverSub.textContent = '得分：' + score;

        if (score > bestScore) {
            bestScore = score;
            bestScoreDisplay.textContent = bestScore;
            try { localStorage.setItem('snakeBestScore', bestScore); } catch(e) {}
        }

        showOverlay(gameOverOverlay);
        updatePauseButton();
        updateStartButton();
    }

    function startGame() {
        if (gameLoopId) { clearInterval(gameLoopId); gameLoopId = null; }
        initSnake();
        spawnFood();
        hideAllOverlays();
        gameState = 'playing';
        updatePauseButton();
        updateStartButton();
        gameLoopId = setInterval(function() { gameStep(); draw(); }, SPEED_LEVELS[speedIdx].ms);
        draw();
    }

    function togglePause() {
        if (gameState === 'playing') {
            clearInterval(gameLoopId);
            gameLoopId = null;
            gameState = 'paused';
            showOverlay(pauseOverlay);
        } else if (gameState === 'paused') {
            hideAllOverlays();
            gameState = 'playing';
            gameLoopId = setInterval(function() { gameStep(); draw(); }, SPEED_LEVELS[speedIdx].ms);
            draw();
        }
        updatePauseButton();
    }

    function draw() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // 网格线
        ctx.strokeStyle = 'rgba(78, 204, 163, 0.06)';
        ctx.lineWidth = 0.5;
        for (var x = 0; x <= COLS; x++) {
            ctx.beginPath();
            ctx.moveTo(x * GRID_SIZE, 0);
            ctx.lineTo(x * GRID_SIZE, canvas.height);
            ctx.stroke();
        }
        for (var y = 0; y <= ROWS; y++) {
            ctx.beginPath();
            ctx.moveTo(0, y * GRID_SIZE);
            ctx.lineTo(canvas.width, y * GRID_SIZE);
            ctx.stroke();
        }

        // 食物
        if (food) {
            var fx = food.x * GRID_SIZE + GRID_SIZE / 2;
            var fy = food.y * GRID_SIZE + GRID_SIZE / 2;
            var r = GRID_SIZE / 2 - 2;

            var glow = ctx.createRadialGradient(fx, fy, r * 0.3, fx, fy, r * 1.8);
            glow.addColorStop(0, 'rgba(233, 69, 96, 0.5)');
            glow.addColorStop(1, 'rgba(233, 69, 96, 0)');
            ctx.fillStyle = glow;
            ctx.beginPath();
            ctx.arc(fx, fy, r * 1.8, 0, Math.PI * 2);
            ctx.fill();

            var fg = ctx.createRadialGradient(fx - 2, fy - 2, 1, fx, fy, r);
            fg.addColorStop(0, '#ff6b81');
            fg.addColorStop(0.7, '#e94560');
            fg.addColorStop(1, '#b83040');
            ctx.fillStyle = fg;
            ctx.beginPath();
            ctx.arc(fx, fy, r, 0, Math.PI * 2);
            ctx.fill();

            ctx.fillStyle = 'rgba(255,255,255,0.4)';
            ctx.beginPath();
            ctx.arc(fx - 3, fy - 3, 2.5, 0, Math.PI * 2);
            ctx.fill();
        }

        // 蛇
        snake.forEach(function(seg, i) {
            var sx = seg.x * GRID_SIZE;
            var sy = seg.y * GRID_SIZE;
            var pad = 1.5;
            var cx = sx + GRID_SIZE / 2;
            var cy = sy + GRID_SIZE / 2;
            var br = 6;
            var alpha = 1 - (i / snake.length) * 0.4;

            if (i === 0) {
                ctx.fillStyle = '#4ecca3';
                ctx.shadowColor = 'rgba(78, 204, 163, 0.7)';
                ctx.shadowBlur = 10;
                roundRect(sx + pad, sy + pad, GRID_SIZE - pad * 2, GRID_SIZE - pad * 2, br);
                ctx.fill();
                ctx.shadowColor = 'transparent';
                ctx.shadowBlur = 0;

                var eyeR = 3.5;
                var dirX = direction.x !== 0 ? direction.x : (direction.y !== 0 ? 0 : 1);
                var dirY = direction.y !== 0 ? direction.y : (direction.x !== 0 ? 0 : 1);
                var pX = dirY;
                var pY = dirX;

                // 左眼
                ctx.fillStyle = '#fff';
                ctx.beginPath();
                ctx.arc(cx + dirX * 3 - pX * 3.5, cy + dirY * 3 - pY * 3.5, eyeR, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#1a1a2e';
                ctx.beginPath();
                ctx.arc(cx + dirX * 4 - pX * 3.5, cy + dirY * 4 - pY * 3.5, 2, 0, Math.PI * 2);
                ctx.fill();

                // 右眼
                ctx.fillStyle = '#fff';
                ctx.beginPath();
                ctx.arc(cx + dirX * 3 + pX * 3.5, cy + dirY * 3 + pY * 3.5, eyeR, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#1a1a2e';
                ctx.beginPath();
                ctx.arc(cx + dirX * 4 + pX * 3.5, cy + dirY * 4 + pY * 3.5, 2, 0, Math.PI * 2);
                ctx.fill();
            } else {
                ctx.fillStyle = 'rgba(78, 204, 163, ' + alpha + ')';
                roundRect(sx + pad, sy + pad, GRID_SIZE - pad * 2, GRID_SIZE - pad * 2, br - 1);
                ctx.fill();
            }
        });
    }

    function roundRect(x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
    }

    // 速度档位按钮
    var speedGroup = document.getElementById('speedGroup');
    SPEED_LEVELS.forEach(function(lv, i) {
        var b = document.createElement('button');
        b.className = 'speed-btn' + (i === speedIdx ? ' active' : '');
        b.textContent = lv.label;
        b.addEventListener('click', function() {
            speedIdx = i;
            speedGroup.querySelectorAll('.speed-btn').forEach(function(x, j) {
                x.classList.toggle('active', j === i);
            });
            // 游戏中切换:立即生效
            if (gameState === 'playing' && gameLoopId) {
                clearInterval(gameLoopId);
                gameLoopId = setInterval(function() { gameStep(); draw(); }, SPEED_LEVELS[speedIdx].ms);
            }
        });
        speedGroup.appendChild(b);
    });

    // 事件
    // 返回首页:通知父页面关闭游戏弹窗(若被直接访问则跳转首页)
    document.getElementById('btnBack').addEventListener('click', function() {
        try {
            if (window.parent && window.parent !== window) {
                window.parent.postMessage({ type: 'closeGame' }, '*');
            } else {
                window.location.href = '/';
            }
        } catch (e) {
            window.location.href = '/';
        }
    });

    btnStart.addEventListener('click', startGame);
    btnPause.addEventListener('click', togglePause);

    document.addEventListener('keydown', function(e) {
        if (e.code === 'Space') {
            e.preventDefault();
            if (gameState === 'playing' || gameState === 'paused') togglePause();
            return;
        }
        var km = {
            'ArrowUp': {x:0,y:-1}, 'ArrowDown': {x:0,y:1},
            'ArrowLeft': {x:-1,y:0}, 'ArrowRight': {x:1,y:0},
            'KeyW': {x:0,y:-1}, 'KeyS': {x:0,y:1},
            'KeyA': {x:-1,y:0}, 'KeyD': {x:1,y:0}
        };
        var nd = km[e.code];
        if (!nd || gameState !== 'playing') return;
        e.preventDefault();
        if (nd.x === -direction.x && nd.y === -direction.y && snake.length > 1) return;
        nextDirection = nd;
    });

    document.querySelectorAll('.dpad-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            if (gameState !== 'playing') return;
            var dm = { up:{x:0,y:-1}, down:{x:0,y:1}, left:{x:-1,y:0}, right:{x:1,y:0} };
            var nd = dm[btn.dataset.dir];
            if (!nd) return;
            if (nd.x === -direction.x && nd.y === -direction.y && snake.length > 1) return;
            nextDirection = nd;
        });
        btn.addEventListener('touchstart', function(e) { e.preventDefault(); btn.click(); });
    });

    var tx = 0, ty = 0;
    canvas.addEventListener('touchstart', function(e) {
        if (gameState !== 'playing') return;
        tx = e.touches[0].clientX;
        ty = e.touches[0].clientY;
    }, { passive: true });

    canvas.addEventListener('touchend', function(e) {
        if (gameState !== 'playing') return;
        var dx = e.changedTouches[0].clientX - tx;
        var dy = e.changedTouches[0].clientY - ty;
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 30) return;
        var nd;
        if (Math.abs(dx) > Math.abs(dy)) {
            nd = { x: dx > 0 ? 1 : -1, y: 0 };
        } else {
            nd = { x: 0, y: dy > 0 ? 1 : -1 };
        }
        if (nd.x === -direction.x && nd.y === -direction.y && snake.length > 1) return;
        nextDirection = nd;
    });

    // 初始
    initSnake();
    spawnFood();
    draw();
})();
    </script>
</body>
</html>"""

@app.get("/game")
async def game_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(GAME_HTML)

# ============================================================================
# GPU SSH 直连监控看板页(数据来自 /api/gpu-ssh/poll,无需在 GPU 服务器部署任何服务)
# ============================================================================

GPU_SSH_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU 监控(SSH 直连)</title>
<style>
:root{
  --bg:#0b0f17;--panel:#121826;--panel2:#0e1420;--line:#1f2937;
  --txt:#e5e9f0;--dim:#8b95a7;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;
  --crit:#f87171;--mem:#a78bfa;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,monospace;
  font-size:14px;padding:16px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-bottom:12px}
h1{font-size:18px;font-weight:600}
h1 .badge{font-size:12px;color:var(--acc);border:1px solid var(--acc);
  border-radius:10px;padding:1px 9px;margin-left:8px;vertical-align:2px}
.meta{color:var(--dim);font-size:12.5px}
.meta b{color:var(--txt);font-weight:600}
.controls{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--dim);font-size:12.5px}
select{background:var(--panel);color:var(--txt);border:1px solid var(--line);
  border-radius:8px;padding:3px 8px;font-size:12.5px}
#err{display:none;background:rgba(248,113,113,.12);border:1px solid var(--crit);
  color:var(--crit);border-radius:10px;padding:8px 14px;margin-bottom:12px;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat .k{color:var(--dim);font-size:12px;margin-bottom:5px}
.stat .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .s{color:var(--dim);font-size:11.5px;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;min-width:0}
.card-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.card-head .t{font-size:15px;font-weight:700}
.card-head .t small{color:var(--dim);font-weight:400;margin-left:6px;font-size:12px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;
  background:var(--ok);box-shadow:0 0 6px var(--ok)}
.dot.busy{background:var(--acc);box-shadow:0 0 8px var(--acc)}
.dot.err{background:var(--crit);box-shadow:0 0 8px var(--crit)}
.main{display:flex;gap:16px;align-items:center;margin-bottom:10px}
.ring{position:relative;width:84px;height:84px;flex:none}
.ring svg{transform:rotate(-90deg)}
.ring .val{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring .val b{font-size:17px;font-variant-numeric:tabular-nums}
.ring .val span{font-size:10px;color:var(--dim)}
.kv{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;min-width:0}
.kv div{display:flex;justify-content:space-between;gap:6px;font-size:12.5px;
  border-bottom:1px dashed rgba(255,255,255,.05);padding:2px 0}
.kv span{color:var(--dim);flex:none}
.kv b{font-weight:600;font-variant-numeric:tabular-nums;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:16px;background:var(--panel2);border-radius:8px;overflow:hidden;position:relative;margin:4px 0 2px}
.bar i{display:block;height:100%;border-radius:8px;transition:width .5s;
  background:linear-gradient(90deg,#6366f1,var(--mem))}
.bar .txt{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:11px;color:#fff;text-shadow:0 1px 2px #000}
.spark{width:100%;height:44px;display:block;margin-top:8px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--dim);margin-top:2px}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:5px;vertical-align:3px}
.chart{width:100%;height:128px;display:block;margin-top:10px}
.sysgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px;margin-bottom:14px}
.foot{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:6px;
  flex-wrap:wrap;gap:4px}
.sect{margin:18px 0 10px;display:flex;align-items:center;gap:10px}
.sect h2{font-size:15px;font-weight:600}
.sect .line{flex:1;height:1px;background:var(--line)}
table{width:100%;border-collapse:collapse;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:7px 12px;text-align:left;border-bottom:1px solid var(--line);
  font-size:12.5px;font-variant-numeric:tabular-nums}
th{color:var(--dim);font-weight:500;background:rgba(255,255,255,.02)}
tr:last-child td{border-bottom:none}
.empty{color:var(--dim);text-align:center;padding:14px}
details{margin-top:14px}
summary{cursor:pointer;color:var(--dim);font-size:13px;padding:6px 0}
pre{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:12px;font-size:11.5px;overflow:auto;color:var(--dim);line-height:1.5}
.kmods{display:flex;flex-wrap:wrap;gap:6px}
.kmods span{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:3px 10px;font-size:11.5px;color:var(--acc)}
footer{color:var(--dim);font-size:11.5px;text-align:center;margin-top:20px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
#livedot{animation:pulse 1.6s infinite}
</style>
</head>
<body>
<header>
  <h1>🖥 GPU 监控(SSH 直连)<span class="badge" id="mname">-</span></h1>
  <div class="meta"><span class="dot busy" id="livedot" title="实时刷新中"></span>主机 <b id="host">-</b> · 驱动 <b id="drv">-</b> · 内核 <b id="krn">-</b> ·
    更新于 <b id="ts">-</b></div>
  <div class="controls">
    <label><input type="checkbox" id="auto" checked> 自动刷新</label>
    <select id="rate">
      <option value="2000">2s</option>
      <option value="3000" selected>3s</option>
      <option value="5000">5s</option>
      <option value="10000">10s</option>
    </select>
  </div>
</header>
<div id="err"></div>
<div class="stats" id="stats"></div>
<div class="sect"><h2>💻 CPU / 内存 / 网络</h2><div class="line"></div></div>
<div class="sysgrid" id="sys"></div>
<div class="sect"><h2>🎮 GPU 卡状态</h2><div class="line"></div></div>
<div class="grid" id="cards"></div>
<div class="sect"><h2>GPU 进程</h2><div class="line"></div></div>
<div id="procs"></div>
<details open><summary>驱动 / 内核模块状态</summary>
  <div style="padding:8px 2px;" class="kmods" id="kmods"></div>
</details>
<details><summary>GPU / NIC 拓扑矩阵(mthreads-gmi topo -m)</summary>
  <pre id="topo">加载中…</pre>
</details>
<footer>数据源:SSH 直连主机执行 mthreads-gmi + /proc 系统指标采集 · GPU/CPU/内存/网络实时曲线</footer>
<script>
"use strict";
const $=id=>document.getElementById(id);
const NAME=new URLSearchParams(location.search).get("name")||"";
let HIST=[],timer=null;
function fmt(n,d=0){return Number(n).toLocaleString("zh-CN",
  {minimumFractionDigits:d,maximumFractionDigits:d});}
function el(tag,cls,html){const e=document.createElement(tag);
  if(cls)e.className=cls; if(html!==undefined)e.innerHTML=html; return e;}
function fmtBytes(b){
  if(b==null||isNaN(b))return "-";
  if(b<1024)return fmt(b,0)+" B";
  if(b<1048576)return fmt(b/1024,1)+" KB";
  if(b<1073741824)return fmt(b/1048576,1)+" MB";
  return fmt(b/1073741824,2)+" GB";}
function fmtRate(bps){
  if(bps==null||isNaN(bps))return "-";
  if(bps<1024)return fmt(bps,0)+" B/s";
  if(bps<1048576)return fmt(bps/1024,1)+" KB/s";
  if(bps<1073741824)return fmt(bps/1048576,1)+" MB/s";
  return fmt(bps/1073741824,2)+" GB/s";}
function statusDot(g){
  const d=el("span");
  if(g.temp_c>=85||g.mem_total_mib&&g.mem_used_mib/g.mem_total_mib>0.97){d.className="dot err";}
  else if(g.util_gpu>1||g.mem_used_mib>1024){d.className="dot busy";}
  return d;
}
function tempColor(t){return t>=85?"var(--crit)":t>=70?"var(--warn)":"var(--ok)";}
function memPct(g){return g.mem_total_mib?100*g.mem_used_mib/g.mem_total_mib:0;}
function cpuColor(p){return p>=90?"var(--crit)":p>=70?"var(--warn)":"var(--ok)";}
function ringSVG(pct,color){
  const r=35,c=2*Math.PI*r,off=c*(1-Math.min(pct,100)/100);
  return `<svg width="84" height="84" viewBox="0 0 84 84">
    <circle cx="42" cy="42" r="${r}" fill="none" stroke="#1c2534" stroke-width="8"/>
    <circle cx="42" cy="42" r="${r}" fill="none" stroke="${color}" stroke-width="8"
      stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${off}"
      style="transition:stroke-dashoffset .5s"/></svg>`;
}
function drawSpark(cv){
  const N=HIST.length; if(N<2)return;
  const W=cv.clientWidth||300,H=44,dpr=window.devicePixelRatio||1;
  cv.width=W*dpr;cv.height=H*dpr;
  const g=cv.getContext("2d");g.scale(dpr,dpr);g.clearRect(0,0,W,H);
  const K=Math.min(N,150),S=HIST.slice(N-K);
  const gi=+cv.dataset.gpu;
  const X=i=>i/(K-1)*W;
  const line=(get,ymax,color)=>{
    g.beginPath();
    S.forEach((s,i)=>{const y=H-4-(H-10)*Math.min(get(s),ymax)/ymax;
      i?g.lineTo(X(i),y):g.moveTo(X(i),y);});
    g.strokeStyle=color;g.lineWidth=1.6;g.stroke();};
  g.strokeStyle="rgba(255,255,255,.06)";g.lineWidth=1;
  [0.25,0.5,0.75].forEach(f=>{g.beginPath();g.moveTo(0,H*f);g.lineTo(W,H*f);g.stroke();});
  line(s=>s.p[gi]||0,1000,"rgba(251,191,36,.75)");
  line(s=>s.u[gi]||0,100,"#38bdf8");
}
/* ---------- 多序列曲线图(CPU/内存/网络) ---------- */
function niceMax(v){
  if(v<=0)return 1;
  const p=Math.pow(10,Math.floor(Math.log10(v)));
  for(const m of [1,1.2,1.5,2,2.5,3,4,5,6,8,10]){
    if(m*p>=v)return m*p;
  }
  return 10*p;
}
function drawChart(cv,series,opts){
  opts=opts||{};
  const W=cv.clientWidth||400,H=128,dpr=window.devicePixelRatio||1;
  cv.width=W*dpr;cv.height=H*dpr;
  const g=cv.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);
  const pad={l:44,r:10,t:8,b:16};
  const iw=W-pad.l-pad.r,ih=H-pad.t-pad.b;
  let mx=0;
  series.forEach(s=>s.data.forEach(v=>{if(v!=null&&v>mx)mx=v;}));
  mx=opts.pct?Math.max(mx,100):niceMax(mx);
  const fmtY=opts.pct?(v=>v+"%"):(opts.fmt||fmtRate);
  g.font="10px monospace";g.fillStyle="#8b95a7";
  g.strokeStyle="rgba(255,255,255,.06)";g.lineWidth=1;
  [0,.25,.5,.75,1].forEach(f=>{
    const y=pad.t+ih*(1-f);
    g.beginPath();g.moveTo(pad.l,y);g.lineTo(W-pad.r,y);g.stroke();
    g.textAlign="right";g.fillText(fmtY(mx*f),pad.l-5,y+3);
  });
  const n=series[0]?series[0].data.length:0;
  if(n<2)return;
  const X=i=>pad.l+i/(n-1)*iw;
  const Y=v=>v==null?null:pad.t+ih*(1-Math.min(v,mx)/mx);
  series.forEach(s=>{
    g.beginPath();let started=false;
    s.data.forEach((v,i)=>{
      if(v==null){started=false;return;}
      const y=Y(v);
      if(!started){g.moveTo(X(i),y);started=true;}else g.lineTo(X(i),y);
    });
    g.strokeStyle=s.color;g.lineWidth=1.8;g.stroke();
    const lv=s.data[n-1];
    if(lv!=null){g.beginPath();g.arc(X(n-1),Y(lv),2.6,0,7);g.fillStyle=s.color;g.fill();}
  });
  const t0=HIST[Math.max(HIST.length-n,0)],t1=HIST[HIST.length-1];
  if(t0&&t1){
    g.textAlign="left";g.fillText(new Date(t0.t*1000).toTimeString().slice(0,8),pad.l,H-4);
    g.textAlign="right";g.fillText(new Date(t1.t*1000).toTimeString().slice(0,8),W-pad.r,H-4);
  }
}
function histWin(key,K){
  const n=HIST.length;
  if(!n)return [];
  return HIST.slice(Math.max(0,n-K)).map(s=>s[key]==null?null:s[key]);
}
function netRateSeries(K){
  const n=HIST.length;
  if(n<2)return {rx:[],tx:[]};
  const S=HIST.slice(Math.max(0,n-K));
  const rx=[],tx=[];
  for(let i=0;i<S.length;i++){
    if(i===0||S[i].rx==null||S[i-1].rx==null||S[i].tx==null||S[i-1].tx==null){
      rx.push(null);tx.push(null);continue;}
    const dt=Math.max(S[i].t-S[i-1].t,0.4);
    rx.push(Math.max(0,(S[i].rx-S[i-1].rx)/dt));
    tx.push(Math.max(0,(S[i].tx-S[i-1].tx)/dt));
  }
  return {rx,tx};
}
function lastNetRate(){
  const n=HIST.length;
  if(n<2)return {rx:null,tx:null};
  const a=HIST[n-2],b=HIST[n-1];
  if(a.rx==null||b.rx==null||a.tx==null||b.tx==null)return {rx:null,tx:null};
  const dt=Math.max(b.t-a.t,0.4);
  return {rx:Math.max(0,(b.rx-a.rx)/dt),tx:Math.max(0,(b.tx-a.tx)/dt)};
}
const CHART_N=200; // ~10 分钟曲线窗口
function drawSysCharts(){
  const c1=$("cv-cpu"),c2=$("cv-mem"),c3=$("cv-net");
  if(!c1)return;
  drawChart(c1,[{name:"CPU",color:"#38bdf8",data:histWin("cpu",CHART_N)}],{pct:true});
  drawChart(c2,[{name:"内存",color:"#a78bfa",data:histWin("mem",CHART_N)}],{pct:true});
  const nr=netRateSeries(CHART_N);
  drawChart(c3,[
    {name:"下行",color:"#34d399",data:nr.rx},
    {name:"上行",color:"#fbbf24",data:nr.tx}]);
}
function pingTxt(ms){return ms==null?"不通":fmt(ms,1)+" ms";}
function renderSys(s){
  const sy=s.sys||{};
  const box=$("sys");box.innerHTML="";
  /* CPU 卡 */
  const c=el("div","card");
  c.appendChild(el("div","card-head",`<div class="t">💻 CPU 利用率<small>实时</small></div>
    <span style="font-size:12px;color:var(--dim)">${sy.cpu_pct!=null?fmt(sy.cpu_pct,1)+"%":"-"}</span>`));
  const m1=el("div","main");
  const r1=el("div","ring");
  r1.innerHTML=ringSVG(sy.cpu_pct||0,cpuColor(sy.cpu_pct||0))+
    `<div class="val"><b>${sy.cpu_pct!=null?fmt(sy.cpu_pct,0)+"%":"-"}</b><span>使用率</span></div>`;
  m1.appendChild(r1);
  const ld=sy.load||[null,null,null];
  const kv1=el("div","kv");
  [["负载 1min",ld[0]!=null?fmt(ld[0],2):"-"],
   ["负载 5min",ld[1]!=null?fmt(ld[1],2):"-"],
   ["负载 15min",ld[2]!=null?fmt(ld[2],2):"-"],
   ["采集耗时",sy.collect_s!=null?fmt(sy.collect_s,1)+"s":"-"],
  ].forEach(([k,v])=>kv1.appendChild(el("div",null,`<span>${k}</span><b>${v}</b>`)));
  m1.appendChild(kv1);c.appendChild(m1);
  c.appendChild(el("div","legend",`<span><i style="background:#38bdf8"></i>CPU 使用率 %</span>`));
  const cv1=document.createElement("canvas");cv1.className="chart";cv1.id="cv-cpu";c.appendChild(cv1);
  box.appendChild(c);
  /* 内存卡 */
  const c2=el("div","card");
  const memP=sy.mem_total_kib?100*sy.mem_used_kib/sy.mem_total_kib:0;
  c2.appendChild(el("div","card-head",`<div class="t">🧠 内存<small>系统</small></div>
    <span style="font-size:12px;color:var(--dim)">${sy.mem_total_kib?memP.toFixed(1)+"%":"-"}</span>`));
  const bar=el("div","bar");
  bar.innerHTML=`<i style="width:${memP}%"></i>
    <div class="txt">${fmtBytes((sy.mem_used_kib||0)*1024)} / ${fmtBytes((sy.mem_total_kib||0)*1024)}</div>`;
  c2.appendChild(bar);
  const kv2=el("div","kv");kv2.style.marginTop="8px";
  [["已用",sy.mem_used_kib!=null?fmtBytes(sy.mem_used_kib*1024):"-"],
   ["可用",sy.mem_avail_kib!=null?fmtBytes(sy.mem_avail_kib*1024):"-"],
   ["Swap 已用",sy.swap_used_kib!=null?fmtBytes(sy.swap_used_kib*1024):"-"],
   ["Swap 总量",sy.swap_total_kib!=null?fmtBytes(sy.swap_total_kib*1024):"-"],
  ].forEach(([k,v])=>kv2.appendChild(el("div",null,`<span>${k}</span><b>${v}</b>`)));
  c2.appendChild(kv2);
  c2.appendChild(el("div","legend",`<span><i style="background:#a78bfa"></i>内存使用率 %</span>`));
  const cv2=document.createElement("canvas");cv2.className="chart";cv2.id="cv-mem";c2.appendChild(cv2);
  box.appendChild(c2);
  /* 网络卡 */
  const c3=el("div","card");
  const nr=lastNetRate();
  const inetOk=sy.inet_ms!=null;
  c3.appendChild(el("div","card-head",`<div class="t">🌐 网络<small>实时速率与连通性</small></div>
    <span style="font-size:12px;color:${inetOk?"var(--ok)":"var(--crit)"}">
      <span class="dot ${inetOk?"":"err"}" style="margin-right:4px"></span>外网${inetOk?"连通":"不通"}</span>`));
  const kv3=el("div","kv");
  [["下行速率",fmtRate(nr.rx)],
   ["上行速率",fmtRate(nr.tx)],
   ["外网延迟",pingTxt(sy.inet_ms)],
   ["网关延迟",sy.gw?`${pingTxt(sy.gw_ms)} (${sy.gw})`:pingTxt(sy.gw_ms)],
  ].forEach(([k,v])=>kv3.appendChild(el("div",null,`<span>${k}</span><b>${v}</b>`)));
  c3.appendChild(kv3);
  c3.appendChild(el("div","legend",
    `<span><i style="background:#34d399"></i>下行 RX</span><span><i style="background:#fbbf24"></i>上行 TX</span>`));
  const cv3=document.createElement("canvas");cv3.className="chart";cv3.id="cv-net";c3.appendChild(cv3);
  box.appendChild(c3);
}
function render(s){
  const t=s.totals,sy=s.sys||{};
  $("mname").textContent=NAME;
  $("host").textContent=s.host||"-";$("drv").textContent=s.driver||"-";
  $("krn").textContent=(s.kernel&&s.kernel.uname)||"-";
  $("ts").textContent=new Date(s.ts*1000).toLocaleString("zh-CN");
  const memP=t.mem_total_mib?100*t.mem_used_mib/t.mem_total_mib:0;
  const sysMemP=sy.mem_total_kib?100*sy.mem_used_kib/sy.mem_total_kib:null;
  $("stats").innerHTML="";
  [["总功耗",fmt(t.power_w,0)+" W",s.gpus.length+" 卡"],
   ["平均 GPU 利用率",fmt(t.avg_util,1)+" %",""],
   ["显存占用",fmt(t.mem_used_mib/1024,1)+" / "+fmt(t.mem_total_mib/1024,0)+" GB",memP.toFixed(1)+"%"],
   ["最高温度",fmt(t.max_temp_c,0)+" °C",""],
   ["GPU 进程数",String(s.processes.length),s.processes.length?"":"空闲"],
   ["CPU 利用率",sy.cpu_pct!=null?fmt(sy.cpu_pct,1)+" %":"-",
     sy.load&&sy.load[0]!=null?"负载 "+fmt(sy.load[0],2):""],
   ["内存占用",sysMemP!=null?fmt(sysMemP,1)+" %":"-",
     sy.mem_total_kib?fmtBytes(sy.mem_used_kib*1024)+" / "+fmtBytes(sy.mem_total_kib*1024):""],
   ["外网延迟",sy.inet_ms!=null?fmt(sy.inet_ms,1)+" ms":"不通",
     sy.gw_ms!=null?"网关 "+fmt(sy.gw_ms,1)+" ms":""],
  ].forEach(([k,v,sub])=>{
    const d=el("div","stat");
    d.appendChild(el("div","k",k));d.appendChild(el("div","v",v));
    if(sub)d.appendChild(el("div","s",sub));
    $("stats").appendChild(d);});
  renderSys(s);
  const cards=$("cards");cards.innerHTML="";
  s.gpus.forEach(g=>{
    const c=el("div","card");
    const head=el("div","card-head");
    const tt=el("div","t");tt.appendChild(statusDot(g));
    tt.appendChild(document.createTextNode("GPU "+g.index));
    tt.appendChild(el("small",null,g.name));
    head.appendChild(tt);
    const pl=document.createElement("span");
    pl.style.cssText="font-size:12px;color:var(--dim)";
    pl.textContent=fmt(g.power_w,0)+" / "+fmt(g.power_limit_w,0)+" W";
    head.appendChild(pl);c.appendChild(head);
    const main=el("div","main");
    const ring=el("div","ring");
    ring.innerHTML=ringSVG(g.util_gpu,"#38bdf8")+
      `<div class="val"><b>${fmt(g.util_gpu,0)}%</b><span>利用率</span></div>`;
    main.appendChild(ring);
    const kv=el("div","kv");
    [["显存",fmt(g.mem_used_mib)+" MiB"],
     ["温度",`<span style="color:${tempColor(g.temp_c)}">${fmt(g.temp_c,0)}°C</span>`],
     ["显存带宽",fmt(g.util_mem,0)+" %"],
     ["SM 频率",fmt(g.sm_clock_mhz,0)+" MHz"],
     ["显存频率",fmt(g.mem_clock_mhz,0)+" MHz"],
     ["性能状态",g.perf_state],
    ].forEach(([k,v])=>kv.appendChild(el("div",null,`<span>${k}</span><b>${v}</b>`)));
    main.appendChild(kv);c.appendChild(main);
    const mp=memPct(g);
    const bar=el("div","bar");
    bar.innerHTML=`<i style="width:${mp}%"></i>
      <div class="txt">${fmt(g.mem_used_mib/1024,1)} / ${fmt(g.mem_total_mib/1024,0)} GB(${mp.toFixed(1)}%)</div>`;
    c.appendChild(bar);
    const cv=document.createElement("canvas");
    cv.className="spark";cv.dataset.gpu=g.index;c.appendChild(cv);
    c.appendChild(el("div","foot",
      `<span>ECC ${g.ecc_edc}/${g.ecc_on_die} · BIOS ${g.bios||"-"}</span>`+
      `<span>${g.bus_id||""}${g.slot?" · "+g.slot:""}</span>`+
      `<span>PCIe ${g.pcie_gen||"-"}</span>`));
    cards.appendChild(c);});
  const pc=$("procs");pc.innerHTML="";
  if(!s.processes.length){
    pc.appendChild(el("div","empty","当前没有占用 GPU 的进程"));
  }else{
    const tb=el("table");
    tb.innerHTML="<tr><th>GPU</th><th>PID</th><th>进程</th>"+
      "<th>GPU 利用率</th><th>显存利用率</th><th>显存占用</th></tr>"+
      s.processes.map(p=>`<tr><td>GPU ${p.gpu}</td><td>${p.pid}</td>
        <td title="${p.name}">${p.name}</td>
        <td>${p.gpu_util!=null?p.gpu_util+" %":"-"}</td>
        <td>${p.mem_util!=null?p.mem_util+" %":"-"}</td>
        <td>${p.mem_mib!=null?fmt(p.mem_mib)+" MiB":"-"}</td></tr>`).join("");
    pc.appendChild(tb);}
  const km=$("kmods");km.innerHTML="";
  ((s.kernel&&s.kernel.modules)||[]).forEach(m=>{
    const sp=document.createElement("span");sp.textContent=m;km.appendChild(sp);});
  if(!km.children.length)km.appendChild(el("span",null,"未检测到相关内核模块"));
  $("topo").textContent=s.topo_text||"无拓扑数据";
  requestAnimationFrame(()=>{document.querySelectorAll(".spark").forEach(drawSpark);drawSysCharts();});
}
async function poll(){
  try{
    const r=await fetch(`/api/gpu-ssh/poll?name=${encodeURIComponent(NAME)}`,{cache:"no-store"});
    if(!r.ok)throw new Error("HTTP "+r.status);
    const s=await r.json();
    $("err").style.display="none";
    if(s.ok)render(s);
    else throw new Error(s.error||"数据不可用");
    await loadHist();
  }catch(e){
    const d=$("err");d.style.display="block";
    d.textContent="获取数据失败:"+e.message+"(SSH 连接失败或 mthreads-gmi 异常)";
  }
}
async function loadHist(){
  try{
    const r=await fetch(`/api/gpu-ssh/history?name=${encodeURIComponent(NAME)}`,{cache:"no-store"});
    HIST=await r.json()||[];
  }catch(e){}
}
/* 自调度轮询:上一次完成后才排下一次,SSH 变慢时不会堆积请求 */
async function tick(){
  await poll();
  if($("auto").checked)timer=setTimeout(tick,+$("rate").value);
}
function reschedule(){
  if(timer){clearTimeout(timer);timer=null;}
  if($("auto").checked)timer=setTimeout(tick,300);
}
$("auto").onchange=reschedule;$("rate").onchange=reschedule;
window.addEventListener("resize",()=>{
  document.querySelectorAll(".spark").forEach(drawSpark);drawSysCharts();});
(async()=>{
  await loadHist();await poll();
  if($("auto").checked)timer=setTimeout(tick,+$("rate").value);
})();
</script>
</body>
</html>"""

@app.get("/gpu-ssh-dashboard")
async def gpu_ssh_dashboard(name: str = ""):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(GPU_SSH_PAGE)

# ============================================================================
# 模型编辑部署 ModelStart(/modelstart 页面后端)
#  - SSH 主机管理(复用 paramiko 连接缓存 _ssh_client/_ssh_client 缓存池)
#  - 主机连通探测 / K8s Pod + Docker 容器自动检测
#  - 容器工作目录文件浏览 / 容器内命令执行(屏显输出)
#  - 预设命令(容器创建等)+ 任务编排(多主机多步骤顺序执行,增量日志轮询)
#  - WebSocket 容器终端(docker exec -it / kubectl exec -it,PTY 交互)
# ============================================================================
import shlex as _shlex
from fastapi import WebSocket as _Ws, WebSocketDisconnect as _WsDisc

def _ms_hosts() -> List[Dict[str, Any]]:
    return _load_config().get("modelstart_hosts") or []

def _ms_host(hid: str) -> Optional[Dict[str, Any]]:
    for h in _ms_hosts():
        if h.get("id") == hid:
            return h
    return None

class MsHostItem(BaseModel):
    id: str = ""
    name: str = ""
    host: str = ""
    ssh_port: int = 22
    username: str = "root"
    auth: str = "password"        # password | key
    password: str = ""
    private_key: str = ""         # PEM 私钥(auth=key)
    passphrase: str = ""          # 私钥口令(可空)
    group: str = ""               # 分组名(空 = 未分组)

class MsHostsSave(BaseModel):
    hosts: List[MsHostItem] = []

@app.get("/api/modelstart/hosts")
async def ms_hosts_list():
    return {"hosts": _ms_hosts()}

@app.post("/api/modelstart/hosts")
async def ms_hosts_save(body: MsHostsSave):
    if not (_is_admin() or _has_perm("ssh_hosts")):
        raise HTTPException(403, "SSH 主机池由管理员(或被授予「SSH 主机分组管理」权限的账号)维护,普通子账号仅可使用被分配的主机")
    old = {h.get("id"): h for h in _ms_hosts()}
    clean, seen = [], set()
    for h in body.hosts[:60]:
        host = str(h.host or "").strip()
        if not host or (host, h.ssh_port, h.username) in seen:
            continue
        seen.add((host, h.ssh_port, h.username))
        hid = (h.id or "").strip() or uuid.uuid4().hex[:8]
        prev = old.get(hid) or {}
        # 密码 / 私钥编辑留空 → 沿用旧值
        pwd = h.password or prev.get("password") or ""
        pkey = (h.private_key or "").strip() or prev.get("private_key") or ""
        ppas = h.passphrase or prev.get("passphrase") or ""
        clean.append({
            "id": hid, "name": (h.name or "").strip()[:40] or host,
            "host": host[:100], "ssh_port": int(h.ssh_port or 22),
            "username": (h.username or "root").strip()[:60],
            "auth": "key" if (h.auth == "key" and pkey) else "password",
            "password": pwd[:200], "private_key": pkey[:20000], "passphrase": ppas[:120],
            "group": (h.group or "").strip()[:40],
        })
    cfg = _load_config(); cfg["modelstart_hosts"] = clean; _save_config(cfg)
    return {"ok": True, "hosts": clean}

def _ms_key(h: Dict[str, Any]) -> str:
    return f"ms|{h.get('id')}|{h.get('host')}|{h.get('ssh_port', 22)}|{h.get('username')}"

def _ms_run(h: Dict[str, Any], cmd: str, timeout: int = 30) -> Dict[str, Any]:
    """ModelStart SSH 执行:返回 out/err/rc;连接失效自动重建重试一次。"""
    import paramiko
    key = _ms_key(h)
    for attempt in range(2):
        try:
            c = _ssh_client(key, h, fresh=(attempt == 1))
            _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            try:
                rc = stdout.channel.recv_exit_status()
            except Exception:
                rc = -1
            return {"out": out, "err": err, "rc": rc}
        except paramiko.AuthenticationException:
            raise RuntimeError("SSH 认证失败:用户名或密码错误")
        except Exception as e:
            if attempt == 1:
                raise RuntimeError(f"SSH 执行失败: {type(e).__name__}: {str(e)[:200]}")
            with _SSH_LOCK:
                _SSH_CLIENTS.pop(key, None)

def _ms_run_stream(h: Dict[str, Any], cmd: str, timeout: int = 1800,
                   on_line=None) -> Dict[str, Any]:
    """ModelStart SSH 执行(流式):stdout/stderr 每出一行立即回调 on_line(stream, text),
    供方案运行日志实时反馈;返回值与 _ms_run 一致(out/err/rc)。"""
    import paramiko
    key = _ms_key(h)
    for attempt in range(2):
        bufs = {"out": [], "err": []}
        pend = {"out": "", "err": ""}
        emitted = 0

        def _feed(stream: str, data: str):
            nonlocal emitted
            pend[stream] += data
            bufs[stream].append(data)
            while "\n" in pend[stream]:
                line, pend[stream] = pend[stream].split("\n", 1)
                line = line.rstrip("\r")
                if line.strip() and on_line:
                    emitted += 1
                    on_line(stream, line)

        try:
            c = _ssh_client(key, h, fresh=(attempt == 1))
            _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
            chan = stdout.channel
            chan.settimeout(0.0)                      # 非阻塞:由本函数自行轮询 + 超时
            deadline = time.time() + timeout
            while True:
                got = False
                while chan.recv_ready():
                    got = True
                    _feed("out", chan.recv(65536).decode("utf-8", "replace"))
                while chan.recv_stderr_ready():
                    got = True
                    _feed("err", chan.recv_stderr(65536).decode("utf-8", "replace"))
                # 退出状态已就绪且两路都无缓冲数据 → 结束(exit-status 在 EOF 之后送达,不会有后到的输出)
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    break
                if time.time() > deadline:
                    raise RuntimeError(f"命令执行超过 {timeout}s,已放弃等待")
                if not got:
                    time.sleep(0.12)
            while chan.recv_ready():                  # 收尾兜底:再排空一次缓冲
                _feed("out", chan.recv(65536).decode("utf-8", "replace"))
            while chan.recv_stderr_ready():
                _feed("err", chan.recv_stderr(65536).decode("utf-8", "replace"))
            for stream in ("out", "err"):             # 行尾未换行的残余(如进度条最后一帧)
                tail = pend[stream].rstrip("\r")
                if tail.strip() and on_line:
                    emitted += 1
                    on_line(stream, tail)
            try:
                rc = chan.recv_exit_status()
            except Exception:
                rc = -1
            return {"out": "".join(bufs["out"]), "err": "".join(bufs["err"]), "rc": rc}
        except paramiko.AuthenticationException:
            raise RuntimeError("SSH 认证失败:用户名或密码错误")
        except Exception as e:
            # 已上屏过输出就不能重跑(否则日志重复),直接报错;只有连接失效的空跑才重建连接重试
            if attempt == 1 or emitted:
                raise RuntimeError(f"SSH 执行失败: {type(e).__name__}: {str(e)[:200]}")
            with _SSH_LOCK:
                _SSH_CLIENTS.pop(key, None)

@app.post("/api/modelstart/hosts/{hid}/probe")
async def ms_host_probe(hid: str):
    h = _ms_host(hid)
    if not h:
        raise HTTPException(404, "主机不存在")
    def work():
        r = _ms_run(h, (
            "echo HOST=$(hostname); echo KERNEL=$(uname -sr 2>/dev/null); "
            "echo DOCKER=$(docker --version 2>/dev/null || echo 无); "
            "echo KUBECTL=$(kubectl version --client 2>/dev/null | head -1 || echo 无); "
            "echo GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -4 | tr '\\n' ';')"
        ), 15)
        info: Dict[str, str] = {}
        for line in (r["out"] or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in ("HOST", "KERNEL", "DOCKER", "KUBECTL", "GPU"):
                    info[k] = v.strip()
        raw = (r["out"] + ("\n" + r["err"] if (r["err"] or "").strip() else "")).strip()
        return {"ok": r["rc"] == 0, "info": info, "raw": raw[:2000]}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

# ---------- 容器信息增强:重建 docker run 创建命令 / Pod 创建 YAML ----------
def _ms_mem_str(n: int) -> str:
    """字节数 → docker 可接受的内存字符串(8g / 512m / 原始字节)。"""
    n = int(n or 0)
    if n and n % (1024 ** 3) == 0:
        return f"{n // 1024 ** 3}g"
    if n and n % (1024 ** 2) == 0:
        return f"{n // 1024 ** 2}m"
    return str(n)

def _ms_docker_run_cmd(insp: Dict[str, Any]) -> str:
    """根据 docker inspect 单条结果重建等价的 docker run 创建命令(简化版 runlike)。"""
    cfg = insp.get("Config") or {}
    hc = insp.get("HostConfig") or {}
    q = _shlex.quote
    name = (insp.get("Name") or "").lstrip("/")
    parts = ["docker run -d"]
    if name:
        parts.append("--name " + q(name))
    rp = (hc.get("RestartPolicy") or {}).get("Name") or "no"
    if rp != "no":
        parts.append("--restart " + q(rp))
    if hc.get("AutoRemove"):
        parts.append("--rm")
    net = hc.get("NetworkMode") or ""
    if net and net not in ("default", "bridge"):
        parts.append("--network " + q(net))
    if hc.get("Privileged"):
        parts.append("--privileged")
    ipc = hc.get("IpcMode") or ""
    if ipc and ipc not in ("private", "shareable"):
        parts.append("--ipc " + q(ipc))
    pidm = hc.get("PidMode") or ""
    if pidm:
        parts.append("--pid " + q(pidm))
    gpus = ""
    for dr in hc.get("DeviceRequests") or []:
        caps = (dr.get("Capabilities") or [[]])[0] or []
        if "gpu" in caps:
            dids = [str(d) for d in (dr.get("DeviceIDs") or [])]
            gpus = "all" if "all" in dids else (",".join(dids) if dids else str(dr.get("Count") or ""))
            break
    if gpus:
        parts.append("--gpus " + q(gpus))
    for cp, hbs in sorted((hc.get("PortBindings") or {}).items()):
        for hb in (hbs or []):
            hip = hb.get("HostIp") or ""
            host = (hip + ":" if hip and hip not in ("0.0.0.0", "::") else "") + str(hb.get("HostPort") or "")
            parts.append("-p " + q(f"{host}:{cp}" if host else cp))
    binds = [str(b) for b in (hc.get("Binds") or [])]
    if not binds:
        for m in insp.get("Mounts") or []:
            if m.get("Type") == "bind" and m.get("Source") and m.get("Destination"):
                binds.append(m["Source"] + ":" + m["Destination"] + ("" if m.get("RW", True) else ":ro"))
    parts.extend("-v " + q(b) for b in binds[:60])
    parts.extend("--add-host " + q(str(eh)) for eh in (hc.get("ExtraHosts") or [])[:20])
    parts.extend("-e " + q(e) for e in (cfg.get("Env") or [])[:80]
                 if not e.startswith(("PATH=", "HOSTNAME=")))
    if cfg.get("WorkingDir"):
        parts.append("-w " + q(cfg["WorkingDir"]))
    if cfg.get("User"):
        parts.append("--user " + q(cfg["User"]))
    shm = hc.get("ShmSize") or 0
    if shm and shm != 64 * 1024 * 1024:
        parts.append("--shm-size " + _ms_mem_str(shm))
    if hc.get("Memory"):
        parts.append("--memory " + _ms_mem_str(hc["Memory"]))
    nano = hc.get("NanoCpus") or 0
    if nano:
        parts.append(f"--cpus {nano / 1e9:g}")
    hostname = cfg.get("Hostname") or ""
    if hostname and (insp.get("Id") or "")[:12] != hostname:
        parts.append("--hostname " + q(hostname))
    ep = [str(x) for x in (cfg.get("Entrypoint") or [])]
    cmd = [str(x) for x in (cfg.get("Cmd") or [])]
    if ep:
        parts.append("--entrypoint " + q(ep[0]))
        cmd = ep[1:] + cmd
    parts.append(q(cfg.get("Image") or "IMAGE"))
    parts.extend(q(x) for x in cmd)
    return " ".join(parts)[:4000]

_YAML_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./+=-][A-Za-z0-9_./@+=: -]*$")

def _ms_yaml_scalar(v: Any) -> str:
    """标量 → YAML:仅在可能被误解析(数字/布尔样式/特殊字符)时加引号。"""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~", "none"):
        return json.dumps(s)
    if re.match(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$", s) or re.match(r"^0x[0-9a-fA-F]+$", s):
        return json.dumps(s)
    if (_YAML_PLAIN_RE.match(s) and ": " not in s and " #" not in s and not s.endswith(" ")
            and not s.endswith(":") and not s.startswith(("- ", "? "))):
        return s
    return json.dumps(s, ensure_ascii=False)

def _ms_to_yaml(obj: Any, indent: int = 0) -> str:
    """受限结构的 YAML 输出(dict / list / 标量),标量列表用流式一行。"""
    pad = "  " * indent
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and v:
                out.append(f"{pad}{k}:")
                out.append(_ms_to_yaml(v, indent + 1))
            elif isinstance(v, list) and v:
                if all(not isinstance(x, (dict, list)) for x in v):
                    out.append(f"{pad}{k}: [{', '.join(_ms_yaml_scalar(x) for x in v)}]")
                else:
                    out.append(f"{pad}{k}:")
                    out.append(_ms_to_yaml(v, indent + 1))
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: " + ("{}" if isinstance(v, dict) else "[]"))
            else:
                out.append(f"{pad}{k}: " + _ms_yaml_scalar(v))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                sub = _ms_to_yaml(item, indent + 1).split("\n")
                sub[0] = pad + "- " + sub[0][len(pad) + 2:]
                out.append("\n".join(sub))
            elif isinstance(item, (dict, list)):
                out.append(pad + "- " + ("{}" if isinstance(item, dict) else "[]"))
            else:
                out.append(pad + "- " + _ms_yaml_scalar(item))
    return "\n".join(out)

def _ms_k8s_env_str(e: Dict[str, Any]) -> str:
    """K8s 容器环境变量 → 一行展示(value / secret / configmap / fieldRef)。"""
    n = e.get("name") or ""
    if "value" in e and e.get("value") is not None:
        return f"{n}={e.get('value')}"
    vf = e.get("valueFrom") or {}
    if vf.get("secretKeyRef"):
        return f"{n}=<secret {vf['secretKeyRef'].get('name', '')}/{vf['secretKeyRef'].get('key', '')}>"
    if vf.get("configMapKeyRef"):
        return f"{n}=<configmap {vf['configMapKeyRef'].get('name', '')}/{vf['configMapKeyRef'].get('key', '')}>"
    if vf.get("fieldRef"):
        return f"{n}=<field {vf['fieldRef'].get('fieldPath', '')}>"
    if vf.get("resourceFieldRef"):
        return f"{n}=<resource>"
    return n

def _ms_pod_creation_yaml(md: Dict[str, Any], spec: Dict[str, Any]) -> str:
    """根据 pod spec 生成等价的创建 YAML(简化:省略探针 / 调度等运行时字段)。"""
    def cont(c: Dict[str, Any]) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": c.get("name") or "", "image": c.get("image") or ""}
        if c.get("command"):
            d["command"] = [str(x) for x in c["command"]]
        if c.get("args"):
            d["args"] = [str(x) for x in c["args"]]
        if c.get("ports"):
            d["ports"] = [{"containerPort": p.get("containerPort"), **({"protocol": p["protocol"]} if p.get("protocol") else {})}
                          for p in c["ports"] if p.get("containerPort")]
        if c.get("env"):
            es = []
            for e in c["env"]:
                if not e.get("name"):
                    continue
                if "value" in e:
                    es.append({"name": e["name"], "value": e.get("value")})
                else:
                    vf = e.get("valueFrom") or {}
                    if vf.get("secretKeyRef"):
                        es.append({"name": e["name"], "secretKeyRef": {"name": vf["secretKeyRef"].get("name", ""), "key": vf["secretKeyRef"].get("key", "")}})
                    elif vf.get("configMapKeyRef"):
                        es.append({"name": e["name"], "configMapKeyRef": {"name": vf["configMapKeyRef"].get("name", ""), "key": vf["configMapKeyRef"].get("key", "")}})
                    elif vf.get("fieldRef"):
                        es.append({"name": e["name"], "fieldRef": {"fieldPath": vf["fieldRef"].get("fieldPath", "")}})
            if es:
                d["env"] = es
        if c.get("volumeMounts"):
            ms = []
            for m in c["volumeMounts"]:
                mm = {"name": m.get("name") or "", "mountPath": m.get("mountPath") or ""}
                if m.get("readOnly"):
                    mm["readOnly"] = True
                ms.append(mm)
            if ms:
                d["volumeMounts"] = ms
        rq = {k: v for k, v in ((c.get("resources") or {}).get("requests") or {}).items()}
        lm = {k: v for k, v in ((c.get("resources") or {}).get("limits") or {}).items()}
        if rq or lm:
            d["resources"] = {**({"requests": rq} if rq else {}), **({"limits": lm} if lm else {})}
        return d
    sp: Dict[str, Any] = {"containers": [cont(c) for c in (spec.get("containers") or [])]}
    if spec.get("restartPolicy"):
        sp["restartPolicy"] = spec["restartPolicy"]
    if spec.get("serviceAccountName"):
        sp["serviceAccountName"] = spec["serviceAccountName"]
    ics = [cont(c) for c in (spec.get("initContainers") or [])]
    if ics:
        sp["initContainers"] = ics
    vols = []
    for v in (spec.get("volumes") or []):
        vv: Dict[str, Any] = {"name": v.get("name") or ""}
        if v.get("configMap"):
            vv["configMap"] = {"name": v["configMap"].get("name") or ""}
        elif v.get("secret"):
            vv["secret"] = {"secretName": v["secret"].get("secretName") or ""}
        elif v.get("hostPath"):
            vv["hostPath"] = {"path": v["hostPath"].get("path") or ""}
        elif v.get("persistentVolumeClaim"):
            vv["persistentVolumeClaim"] = {"claimName": v["persistentVolumeClaim"].get("claimName") or ""}
        elif v.get("emptyDir") is not None:
            vv["emptyDir"] = {}
        else:
            continue
        vols.append(vv)
    if vols:
        sp["volumes"] = vols
    meta: Dict[str, Any] = {"name": md.get("name") or "", "namespace": md.get("namespace") or "default"}
    labels = {k: v for k, v in (md.get("labels") or {}).items()
              if k not in ("pod-template-hash", "controller-revision-hash", "pod-template-generation")}
    if labels:
        meta["labels"] = labels
    return _ms_to_yaml({"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": sp})

class MsDetectReq(BaseModel):
    host_id: str

@app.post("/api/modelstart/detect")
async def ms_detect(body: MsDetectReq):
    """检测主机上已存在的 Docker 容器与 K8s Pod(含详细信息与等价创建命令)。"""
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    def work():
        containers, docker_err = [], ""
        r = _ms_run(h, "docker ps -a --format '{{json .}}' 2>&1", 25)
        for line in (r["out"] or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    j = json.loads(line)
                    containers.append({"id": (j.get("ID") or "")[:12], "name": j.get("Names") or "",
                                       "image": j.get("Image") or "", "status": j.get("Status") or "",
                                       "state": j.get("State") or "", "ports": (j.get("Ports") or "")[:300],
                                       "command": (j.get("Command") or "")[:200],
                                       "created_at": j.get("CreatedAt") or "", "running_for": j.get("RunningFor") or ""})
                except Exception:
                    pass
            elif "command not found" in line or "permission denied" in line.lower():
                docker_err = line.strip()[:200]
        # docker inspect 补充详细字段 + 重建 docker run 创建命令
        ids = [c["id"] for c in containers[:200] if c.get("id")]
        if ids:
            try:
                ir = _ms_run(h, "docker inspect " + " ".join(ids), 30)
                iarr = json.loads(ir["out"] or "[]")
            except Exception:
                iarr = []
            iby = {str(it.get("Id"))[:12]: it for it in (iarr if isinstance(iarr, list) else [])
                   if isinstance(it, dict) and it.get("Id")}
            for c in containers:
                it = iby.get(c.get("id") or "")
                if not it:
                    continue
                cfg = it.get("Config") or {}
                hc = it.get("HostConfig") or {}
                stt = it.get("State") or {}
                nset = it.get("NetworkSettings") or {}
                nets = list((nset.get("Networks") or {}).keys())[:10]
                ip = nset.get("IPAddress") or ""
                if not ip and nets:
                    ip = ((nset.get("Networks") or {}).get(nets[0]) or {}).get("IPAddress") or ""
                mounts = []
                for m in (it.get("Mounts") or [])[:40]:
                    dst = m.get("Destination") or ""
                    ro = "" if m.get("RW", True) else " (ro)"
                    if m.get("Type") == "volume":
                        mounts.append(f"[卷] {m.get('Name') or m.get('Source') or ''} → {dst}{ro}")
                    elif m.get("Type") == "tmpfs":
                        mounts.append(f"[tmpfs] → {dst}{ro}")
                    else:
                        mounts.append(f"[bind] {m.get('Source') or ''} → {dst}{ro}")
                port_maps = []
                for cp, hbs in sorted((hc.get("PortBindings") or {}).items()):
                    for hb in (hbs or []):
                        hip = hb.get("HostIp") or ""
                        host = (hip + ":" if hip and hip not in ("0.0.0.0", "::") else "") + str(hb.get("HostPort") or "")
                        port_maps.append(f"{host}->{cp}" if host else cp)
                gpus = ""
                for dr in hc.get("DeviceRequests") or []:
                    caps = (dr.get("Capabilities") or [[]])[0] or []
                    if "gpu" in caps:
                        dids = [str(d) for d in (dr.get("DeviceIDs") or [])]
                        gpus = "all" if "all" in dids else (",".join(dids) if dids else str(dr.get("Count") or ""))
                        break
                c.update({
                    "run_cmd": _ms_docker_run_cmd(it),
                    "entrypoint": [str(x) for x in (cfg.get("Entrypoint") or [])][:8],
                    "cmd": [str(x) for x in (cfg.get("Cmd") or [])][:8],
                    "env": [e for e in (cfg.get("Env") or []) if not e.startswith(("PATH=", "HOSTNAME="))][:60],
                    "mounts": mounts, "networks": nets, "ip": ip,
                    "restart": (hc.get("RestartPolicy") or {}).get("Name") or "no",
                    "auto_remove": bool(hc.get("AutoRemove")),
                    "workdir": cfg.get("WorkingDir") or "", "user": cfg.get("User") or "",
                    "hostname": cfg.get("Hostname") or "",
                    "privileged": bool(hc.get("Privileged")), "gpus": gpus,
                    "shm": _ms_mem_str(hc.get("ShmSize") or 0),
                    "health": ((stt.get("Health") or {}).get("Status") or ""),
                    "started_at": stt.get("StartedAt") or "", "finished_at": stt.get("FinishedAt") or "",
                    "exit_code": stt.get("ExitCode"),
                    "mem_limit": hc.get("Memory") or 0,
                    "cpus": round((hc.get("NanoCpus") or 0) / 1e9, 2) or None,
                    "extra_hosts": [str(x) for x in (hc.get("ExtraHosts") or [])][:20],
                    "port_maps": port_maps[:30],
                    "labels": [f"{k}={v}" for k, v in sorted((cfg.get("Labels") or {}).items())][:30],
                })
        # K8s:一次取全量 JSON,解析出详细信息 + 等价创建 YAML / kubectl run
        pods, k8s_err = [], ""
        k = _ms_run(h, "kubectl get pods -A -o json", 30)
        kout = (k["out"] or "").strip()
        kjson = None
        if kout.startswith("{"):
            try:
                kjson = json.loads(kout)
            except Exception:
                pass
        if not isinstance(kjson, dict):
            klines = [l for l in ((k["err"] or "") + "\n" + kout).splitlines() if l.strip()]
            k8s_err = klines[0][:200] if klines else "kubectl 不可用"
        else:
            for it in (kjson.get("items") or [])[:300]:
                md, spec, st = it.get("metadata") or {}, it.get("spec") or {}, it.get("status") or {}
                cs = st.get("containerStatuses") or []
                st_by = {c.get("name"): c for c in cs}
                owner = ""
                for ref in md.get("ownerReferences") or []:
                    owner = f"{ref.get('kind', '')}/{ref.get('name', '')}"
                    break
                conts = []
                for c in (spec.get("containers") or [])[:20]:
                    cst = st_by.get(c.get("name")) or {}
                    conts.append({
                        "name": c.get("name") or "", "image": c.get("image") or "",
                        "command": [str(x) for x in (c.get("command") or [])][:12],
                        "args": [str(x) for x in (c.get("args") or [])][:12],
                        "ports": [f"{p.get('containerPort')}/{p.get('protocol') or 'TCP'}"
                                  for p in (c.get("ports") or [])[:10] if p.get("containerPort")],
                        "env": [_ms_k8s_env_str(e) for e in (c.get("env") or [])[:40]],
                        "mounts": [f"{m.get('name', '')}:{m.get('mountPath', '')}" + (" (ro)" if m.get("readOnly") else "")
                                   for m in (c.get("volumeMounts") or [])[:30]],
                        "resources": [f"{k2}={v}" for d in ("requests", "limits")
                                      for k2, v in ((c.get("resources") or {}).get(d) or {}).items()][:16],
                        "ready": bool(cst.get("ready")), "restarts": int(cst.get("restartCount") or 0),
                    })
                init = [{"name": c.get("name") or "", "image": c.get("image") or ""}
                        for c in (spec.get("initContainers") or [])[:10]]
                run_cmd = ""
                if not owner and len(spec.get("containers") or []) == 1:
                    c0 = (spec.get("containers") or [])[0]
                    q = _shlex.quote
                    seg = [f"kubectl run {q(md.get('name') or '')}", f"-n {q(md.get('namespace') or 'default')}"]
                    if c0.get("image"):
                        seg.append(f"--image={c0['image']}")
                    if spec.get("restartPolicy") and spec["restartPolicy"] != "Always":
                        seg.append(f"--restart={spec['restartPolicy']}")
                    seg.extend(f"--port={p['containerPort']}" for p in (c0.get("ports") or [])[:5] if p.get("containerPort"))
                    seg.extend(f"--env={e['name']}={e.get('value')}" for e in (c0.get("env") or [])[:20]
                               if e.get("name") and "value" in e)
                    cc = [str(x) for x in (c0.get("command") or [])] + [str(x) for x in (c0.get("args") or [])]
                    if cc:
                        seg.append("--")
                        seg.extend(q(x) for x in cc)
                    run_cmd = " ".join(seg)
                pods.append({
                    "namespace": md.get("namespace") or "default", "name": md.get("name") or "",
                    "ready": f"{sum(1 for c in cs if c.get('ready'))}/{len(cs)}",
                    "phase": st.get("phase") or "", "restarts": sum(int(c.get("restartCount") or 0) for c in cs),
                    "node": spec.get("nodeName") or "", "pod_ip": st.get("podIP") or "",
                    "host_ip": st.get("hostIP") or "", "created": md.get("creationTimestamp") or "",
                    "started": st.get("startTime") or "", "owner": owner,
                    "qos": st.get("qosClass") or "", "service_account": spec.get("serviceAccountName") or "",
                    "images": [c.get("image") or "" for c in (spec.get("containers") or [])][:8],
                    "containers": conts, "init_containers": init,
                    "run_cmd": run_cmd, "create_yaml": _ms_pod_creation_yaml(md, spec)[:20000],
                })
        return {"containers": containers, "docker_error": docker_err,
                "pods": pods, "k8s_error": k8s_err}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

class MsContainerReq(BaseModel):
    host_id: str = ""
    runtime: str = "docker"        # docker | k8s
    container: str = ""
    namespace: str = ""
    path: str = ""
    command: str = ""
    timeout: int = 120

def _ms_wrap_container(runtime: str, container: str, namespace: str, cmd: str) -> str:
    """把命令包装为「在容器内执行」的形式(不带 -it,适合取输出)。"""
    if runtime == "k8s":
        return (f"kubectl exec {_shlex.quote(container)} -n {_shlex.quote(namespace or 'default')} "
                f"-- sh -c {_shlex.quote(cmd)}")
    return f"docker exec {_shlex.quote(container)} sh -c {_shlex.quote(cmd)}"

_LS_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

def _ms_parse_ls(text: str) -> List[Dict[str, Any]]:
    """解析 ls -la 输出(兼容 GNU 三段日期 / ISO 两段日期 / busybox)。"""
    entries = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("total"):
            continue
        head = line.split(None, 5)
        if len(head) < 6 or not re.match(r"^[\-bcdlsp][\-rwxsStT]{9}$", head[0]):
            continue
        perms, owner, size, rest = head[0], head[2], head[4], head[5]
        toks = rest.split(None, 3)
        if toks[0] in _LS_MONTHS and len(toks) >= 4:          # Sep  8 11:08 name
            mtime, name = " ".join(toks[:3]), toks[3]
        else:
            t2 = rest.split(None, 2)                          # 2026-09-08 11:08 name
            if len(t2) < 3:
                continue
            mtime, name = " ".join(t2[:2]), t2[2]
        if name in (".", ".."):
            continue
        entries.append({"name": name, "dir": perms.startswith("d"), "perms": perms,
                        "size": int(size) if size.isdigit() else 0, "owner": owner, "mtime": mtime})
    return entries

@app.post("/api/modelstart/files")
async def ms_files(body: MsContainerReq):
    if not body.container:
        raise HTTPException(400, "未指定容器")
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    cwd = (body.path or "").strip() or "."
    inner = f"cd {_shlex.quote(cwd)} 2>/dev/null || cd /; echo __PWD__$(pwd); ls -la"
    cmd = _ms_wrap_container(body.runtime, body.container, body.namespace, inner)
    def work():
        r = _ms_run(h, cmd, 45)
        out = r["out"] or ""
        m = re.search(r"__PWD__(\S+)", out)
        path = m.group(1) if m else cwd
        entries = _ms_parse_ls(out)
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        raw = out + ("\n[stderr]\n" + r["err"] if (r["err"] or "").strip() else "")
        return {"path": path, "entries": entries, "raw": raw[:8000], "rc": r["rc"]}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

# 常见退出码含义(编排与命令执行失败时给出可读原因,不再"无声终止")
_MS_RC_HINTS = {
    0: "成功", 1: "一般错误(查看 stderr)", 2: "命令用法错误(参数不对)",
    126: "无执行权限或不是可执行文件", 127: "命令不存在(未安装或路径错误)",
    130: "被 Ctrl+C 终止(SIGINT)", 137: "被强制杀死(SIGKILL,常见于 OOM)",
    143: "被终止信号结束(SIGTERM)",
}

def _ms_rc_hint(rc: int, err: str = "") -> str:
    hint = _MS_RC_HINTS.get(rc, "")
    e = (err or "").lower()
    if "no such container" in e:
        hint = "该主机上不存在此容器名(先 docker ps 确认或先创建容器)"
    elif "no such pod" in e or ('pods "' in e and "not found" in e):
        hint = "该主机上不存在此 Pod(检查命名空间与 Pod 名)"
    elif "executable file not found" in e or "command not found" in e:
        hint = "命令不存在:目标主机/容器内未安装该命令或路径错误"
    elif "permission denied" in e:
        hint = "权限不足(考虑 sudo 或更换用户)"
    elif "is the docker daemon running" in e:
        hint = "目标主机 Docker 服务未运行"
    elif "already exists" in e or "conflict" in e:
        hint = "资源已存在 / 命名冲突(如容器重名,重复创建)"
    return hint

@app.post("/api/modelstart/exec")
async def ms_exec(body: MsContainerReq):
    if not body.container:
        raise HTTPException(400, "未指定容器")
    if not (body.command or "").strip():
        raise HTTPException(400, "命令为空")
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    cmd = _ms_wrap_container(body.runtime, body.container, body.namespace, body.command)
    def work():
        t0 = time.time()
        r = _ms_run(h, cmd, min(1800, max(10, body.timeout or 120)))
        dur = round(time.time() - t0, 1)
        return {"cmd": cmd[:3000],
                "out": (r["out"] or "")[:200000],
                "err": (r["err"] or "")[:100000],
                "rc": r["rc"], "duration": dur,
                "hint": _ms_rc_hint(r["rc"], r["err"] or "")}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

# ---- 容器常见运维操作:启动 / 停止 / 重启(Docker)与日志回看(docker / kubectl) ----
_MS_DOCKER_OPS = {"start": "docker start", "stop": "docker stop", "restart": "docker restart"}
_MS_OP_LABEL = {"start": "启动", "stop": "停止", "restart": "重启"}

class MsContainerOpReq(BaseModel):
    host_id: str = ""
    runtime: str = "docker"
    container: str = ""
    namespace: str = ""
    op: str = ""                   # start | stop | restart

@app.post("/api/modelstart/container-op")
async def ms_container_op(body: MsContainerOpReq):
    if body.op not in _MS_DOCKER_OPS:
        raise HTTPException(400, "不支持的操作")
    if body.runtime != "docker":
        raise HTTPException(400, "K8s Pod 由控制器管理,暂不支持直接启停(可用删除 Pod 触发重建)")
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    if not body.container.strip():
        raise HTTPException(400, "未指定容器")
    cmd = f"{_MS_DOCKER_OPS[body.op]} {_shlex.quote(body.container.strip())}"
    def work():
        t0 = time.time()
        r = _ms_run(h, cmd, 120)
        return {"ok": r["rc"] == 0, "op": _MS_OP_LABEL[body.op], "cmd": cmd,
                "out": (r["out"] or "")[:20000], "err": (r["err"] or "")[:10000],
                "rc": r["rc"], "duration": round(time.time() - t0, 1),
                "hint": _ms_rc_hint(r["rc"], r["err"] or "")}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

class MsContainerLogsReq(BaseModel):
    host_id: str = ""
    runtime: str = "docker"
    container: str = ""
    namespace: str = ""
    tail: int = 200                # 取末尾行数(20-2000)

@app.post("/api/modelstart/container-logs")
async def ms_container_logs(body: MsContainerLogsReq):
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    if not body.container.strip():
        raise HTTPException(400, "未指定容器")
    tail = min(max(body.tail or 200, 20), 2000)
    name = _shlex.quote(body.container.strip())
    if body.runtime == "k8s":
        ns = _shlex.quote((body.namespace or "default").strip() or "default")
        cmd = f"kubectl logs {name} -n {ns} --tail={tail} 2>&1"
    else:
        cmd = f"docker logs --tail {tail} {name} 2>&1"
    def work():
        r = _ms_run(h, cmd, 60)
        text = ((r["out"] or "") + ("\n" + r["err"] if (r["err"] or "").strip() else "")).strip()
        return {"ok": r["rc"] == 0, "cmd": cmd, "logs": text[:400000], "rc": r["rc"],
                "hint": _ms_rc_hint(r["rc"], r["err"] or "")}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
# ---- 预设命令 v2:标签 / 语言(shell·python·node…)/ 三种形式(命令 / 脚本 / 上传脚本文件) ----
def _ms_files_dir() -> str:
    """预设上传文件目录(按账号隔离)。"""
    d = os.path.join(_udir(), "modelstart_files")
    os.makedirs(d, exist_ok=True)
    return d

_MS_RUNNERS = {"shell": "sh", "bash": "bash", "python": "python3",
               "node": "node", "perl": "perl", "ruby": "ruby"}

def _ms_presets() -> List[Dict[str, Any]]:
    return _load_config().get("modelstart_presets") or []

class MsPresetItem(BaseModel):
    id: str = ""
    name: str = ""
    kind: str = "host"              # host=主机上执行 | container=容器内执行
    lang: str = "shell"             # shell | bash | python | node | perl | ruby
    form: str = "command"           # command=单条命令 | script=脚本正文 | upload=上传的脚本文件 | yaml=K8s YAML(kubectl apply)
    content: str = ""
    filename: str = ""              # form=upload/yaml:已上传文件名
    tags: List[str] = []

class MsPresetsSave(BaseModel):
    presets: List[MsPresetItem] = []

@app.get("/api/modelstart/presets")
async def ms_presets_list():
    out = []
    for p in _ms_presets():
        item = dict(p)
        if p.get("form") == "upload" and p.get("filename"):
            path = os.path.join(_ms_files_dir(), p["filename"])
            item["file_exists"] = os.path.exists(path)
            item["file_size"] = os.path.getsize(path) if os.path.exists(path) else 0
        out.append(item)
    return {"presets": out}

@app.post("/api/modelstart/presets")
async def ms_presets_save(body: MsPresetsSave):
    clean = []
    for p in body.presets[:200]:
        if not (p.name or "").strip() and not (p.content or "").strip() and not (p.filename or "").strip():
            continue
        form = p.form if p.form in ("command", "script", "upload", "yaml") else "command"
        lang = (p.lang or "shell").lower()
        if lang not in _MS_RUNNERS:
            lang = "shell"
        clean.append({
            "id": (p.id or "").strip() or uuid.uuid4().hex[:8],
            "name": (p.name or "").strip()[:60] or "未命名预设",
            # k8s YAML 固定在主机上执行 kubectl apply,不进容器
            "kind": "container" if (p.kind == "container" and form != "yaml") else "host",
            "lang": lang, "form": form,
            "content": (p.content or "")[:50000],
            "filename": (p.filename or "").strip()[:120],
            "tags": [str(t).strip()[:20] for t in (p.tags or [])[:8] if str(t).strip()],
        })
    cfg = _load_config(); cfg["modelstart_presets"] = clean; _save_config(cfg)
    return {"ok": True, "presets": clean}

@app.post("/api/modelstart/presets/upload")
async def ms_preset_upload(file: UploadFile = File(...)):
    """上传脚本文件(python/shell 等),供 form=upload 的预设执行。"""
    name = os.path.basename(file.filename or "script.txt")[:80]
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB 限制")
    stored = f"{uuid.uuid4().hex[:8]}_{name}"
    with open(os.path.join(_ms_files_dir(), stored), "wb") as f:
        f.write(data)
    return {"ok": True, "filename": stored, "name": name, "size": len(data)}

def _ms_sftp_put(h: Dict[str, Any], local: str, remote: str):
    c = _ssh_client(_ms_key(h), h)
    sftp = c.open_sftp()
    try:
        sftp.put(local, remote)
    finally:
        sftp.close()

def _ms_compose_cmd(preset: Dict[str, Any], kind: str, runtime: str, container: str,
                    namespace: str, h: Dict[str, Any], rid_tag: str, owner: str = "") -> str:
    """把预设编译为远端可执行命令。upload 形式先 SFTP 上传到目标主机 /tmp 再执行;
    yaml 形式(K8s 清单)在目标主机上以 kubectl apply -f 应用(粘贴正文走 stdin,上传文件先 SFTP)。
    owner:预设所属账号(worker 线程无请求上下文,按运行归属解析上传文件目录)。"""
    q = _shlex.quote
    form = preset.get("form") or "command"
    lang = (preset.get("lang") or "shell").lower()
    runner = _MS_RUNNERS.get(lang, "sh")
    if form == "yaml":
        ns = (namespace or "").strip()
        ns_arg = f"-n {q(ns)} " if ns else ""
        fname = preset.get("filename") or ""
        if fname:
            local = os.path.join(_udir(owner or _cur_user()), "modelstart_files", fname)
            if not os.path.exists(local):
                raise RuntimeError("上传的 YAML 文件不存在,请在预设里重新上传")
            remote = f"/tmp/ms_{rid_tag}_{fname}"[:180]
            _ms_sftp_put(h, local, remote)
            return f"kubectl apply {ns_arg}-f {q(remote)}"
        body = (preset.get("content") or "").strip()
        if not body:
            raise RuntimeError("YAML 内容为空")
        eof = "__MS_YEOF__"
        while eof in body:
            eof += "_"
        return f"kubectl apply {ns_arg}-f - <<'{eof}'\n{body}\n{eof}"
    if form == "upload":
        fname = preset.get("filename") or ""
        local = os.path.join(_udir(owner or _cur_user()), "modelstart_files", fname)
        if not fname or not os.path.exists(local):
            raise RuntimeError("上传的脚本文件不存在,请在预设里重新上传")
        remote = f"/tmp/ms_{rid_tag}_{fname}"[:180]
        _ms_sftp_put(h, local, remote)
        if kind == "container":
            if runtime == "k8s":
                return f"kubectl exec -i {q(container)} -n {q(namespace or 'default')} -- {runner} - < {q(remote)}"
            return f"docker exec -i {q(container)} {runner} - < {q(remote)}"
        return f"{runner} {q(remote)}"
    body = (preset.get("content") or "").strip()
    if not body:
        raise RuntimeError("预设内容为空")
    if form == "script":
        eof = "__MS_EOF__"
        while eof in body:
            eof += "_"
        if kind == "container":
            if runtime == "k8s":
                return (f"kubectl exec -i {q(container)} -n {q(namespace or 'default')} -- {runner} - "
                        f"<<'{eof}'\n{body}\n{eof}")
            return f"docker exec -i {q(container)} {runner} - <<'{eof}'\n{body}\n{eof}"
        if lang in ("shell", "bash"):
            return f"{runner} -s <<'{eof}'\n{body}\n{eof}"
        return f"{runner} - <<'{eof}'\n{body}\n{eof}"
    # command 形式
    if kind == "container":
        return _ms_wrap_container(runtime, container, namespace, body)
    return body

class MsRunOnceReq(BaseModel):
    host_id: str
    preset_id: str
    runtime: str = "docker"
    container: str = ""
    namespace: str = ""

@app.post("/api/modelstart/presets/run-once")
async def ms_preset_run_once(body: MsRunOnceReq):
    """预设试运行:在指定主机(或容器)立即执行一次并回传结果。"""
    h = _ms_host(body.host_id)
    if not h:
        raise HTTPException(404, "主机不存在")
    p = next((x for x in _ms_presets() if x.get("id") == body.preset_id), None)
    if not p:
        raise HTTPException(404, "预设不存在")
    kind = "container" if (p.get("kind") == "container" or body.container) else "host"
    if p.get("form") == "yaml":
        kind = "host"               # k8s YAML:在主机上 kubectl apply,与容器无关
    if kind == "container" and not body.container:
        raise HTTPException(400, "容器内执行需要指定容器名")
    def work():
        cmd = _ms_compose_cmd(p, kind, body.runtime, body.container, body.namespace, h, "once")
        t0 = time.time()
        r = _ms_run(h, cmd, timeout=900)
        return {"cmd": cmd[:3000], "out": (r["out"] or "")[:200000], "err": (r["err"] or "")[:100000],
                "rc": r["rc"], "duration": round(time.time() - t0, 1),
                "hint": _ms_rc_hint(r["rc"], r["err"] or "")}
    try:
        return await asyncio.to_thread(work)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

# ---- 任务编排 v2:多主机/分组展开 + 组内并行线程池 + 逐步错误码原因 ----
import concurrent.futures as _cf

def _ms_plans() -> List[Dict[str, Any]]:
    return _load_config().get("modelstart_plans") or []

class MsPlanStep(BaseModel):
    id: str = ""
    kind: str = "host"                 # host=主机上执行 | container=容器内执行
    host_id: str = ""                  # 单主机执行
    host_group: str = ""               # 按分组:展开为组内全部主机(可并行)
    runtime: str = "docker"            # docker | k8s
    container: str = ""
    namespace: str = ""
    preset_id: str = ""
    command: str = ""                  # 未选预设时使用
    continue_on_error: bool = False
    parallel: bool = True              # 分组多主机时并行执行

class MsPlanItem(BaseModel):
    id: str = ""
    name: str = ""
    steps: List[MsPlanStep] = []

class MsPlansSave(BaseModel):
    plans: List[MsPlanItem] = []

@app.get("/api/modelstart/plans")
async def ms_plans_list():
    return {"plans": _ms_plans()}

@app.post("/api/modelstart/plans")
async def ms_plans_save(body: MsPlansSave):
    clean = []
    for pl in body.plans[:50]:
        steps = []
        for st in pl.steps[:100]:
            steps.append({"id": (st.id or "").strip() or uuid.uuid4().hex[:8],
                          "kind": "container" if st.kind == "container" else "host",
                          "host_id": (st.host_id or "").strip(),
                          "host_group": (st.host_group or "").strip()[:40],
                          "runtime": "k8s" if st.runtime == "k8s" else "docker",
                          "container": (st.container or "").strip()[:200],
                          "namespace": (st.namespace or "").strip()[:100],
                          "preset_id": (st.preset_id or "").strip(),
                          "command": (st.command or "")[:20000],
                          "continue_on_error": bool(st.continue_on_error),
                          "parallel": bool(st.parallel)})
        if (pl.name or "").strip() or steps:
            clean.append({"id": (pl.id or "").strip() or uuid.uuid4().hex[:8],
                          "name": (pl.name or "").strip()[:60] or "未命名方案", "steps": steps})
    cfg = _load_config(); cfg["modelstart_plans"] = clean; _save_config(cfg)
    return {"ok": True, "plans": clean}

_MS_RUNS: Dict[str, Dict[str, Any]] = {}
_MS_RUNS_LOCK = threading.Lock()

# 已结束运行的历史存档(每账号独立文件,服务重启后执行历史仍可回看)
def _ms_runs_file() -> str:
    return os.path.join(_udir(), "modelstart_runs.json")

def _ms_history_load() -> List[Dict[str, Any]]:
    try:
        with open(_ms_runs_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _ms_run_persist(run: Dict[str, Any]):
    """运行到达结束态后写入历史文件(去重合并,最多保留 100 条,日志截取末段防膨胀)。"""
    if not run or run.get("status") in (None, "", "running"):
        return
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run["finished"] = finished            # 回写内存记录,列表接口 / 前端耗时计算共用
    snap = {
        "id": run.get("id", ""), "plan_id": run.get("plan_id", ""),
        "plan_name": run.get("plan_name") or (run.get("plan") or {}).get("name") or "未命名方案",
        "status": run["status"], "started": run.get("started", ""),
        "finished": finished,
        "steps": run.get("steps") or [], "seq": run.get("seq", 0),
        "logs": (run.get("logs") or [])[-500:],
    }
    try:
        with _MS_RUNS_LOCK:
            # worker 线程不继承请求上下文:按运行记录的 owner 读写对应账号的历史文件
            runs_file = os.path.join(_udir(run.get("owner") or "admin"), "modelstart_runs.json")
            hist = []
            try:
                with open(runs_file, encoding="utf-8") as f:
                    data = json.load(f)
                hist = data if isinstance(data, list) else []
            except Exception:
                pass
            hist = [r for r in hist if r.get("id") != snap["id"]]
            hist.insert(0, snap)
            with open(runs_file, "w", encoding="utf-8") as f:
                json.dump(hist[:100], f, ensure_ascii=False)
    except Exception:
        pass

def _ms_run_view(run: Dict[str, Any], after: int = 0) -> Dict[str, Any]:
    """单次运行轮询 / 历史详情的统一响应体。"""
    return {"status": run["status"], "started": run.get("started", ""),
            "paused": bool(run.get("paused")) and run.get("status") == "running",
            "steps": [{"name": s["name"], "status": s["status"], "rc": s.get("rc"), "note": s.get("note", "")}
                      for s in run["steps"]],
            "logs": [l for l in run["logs"] if l["i"] > after],
            "last": run.get("seq", 0) - 1}

def _ms_run_log(run: Dict[str, Any], step: int, level: str, text: str):
    with _MS_RUNS_LOCK:
        run["logs"].append({"i": run["seq"], "step": step, "level": level,
                            "t": datetime.now().strftime("%H:%M:%S"), "text": text[:6000]})
        run["seq"] += 1
        if len(run["logs"]) > 2500:                   # 流式输出日志量大:内存只留最近一段,防止长跑膨胀
            del run["logs"][:-2500]

def _ms_step_targets(st: Dict[str, Any]) -> List[Dict[str, Any]]:
    """步骤目标主机:分组名 → 组内全部主机;否则按 host_id 单台。"""
    g = (st.get("host_group") or "").strip()
    if g:
        return [h for h in _ms_hosts() if (h.get("group") or "") == g]
    h = _ms_host(st.get("host_id") or "")
    return [h] if h else []

def _ms_step_label(s: Dict[str, Any]) -> str:
    g = (s.get("host_group") or "").strip()
    if g:
        n = len([h for h in _ms_hosts() if (h.get("group") or "") == g])
        return f"分组「{g}」· {n} 台" + (" · 容器内" if s.get("kind") == "container" else "")
    h = _ms_host(s.get("host_id") or "") or {}
    where = h.get("name") or h.get("host") or "?"
    if s.get("kind") == "container":
        return f"{where} · 容器 {s.get('container') or '?'}"
    return f"{where} · 主机"

def _ms_pause_wait(run: Dict[str, Any]):
    """暂停挂起:阻塞到恢复 / 撤回(仅运行中的运行可暂停)。"""
    while run.get("paused") and run.get("status") == "running":
        time.sleep(0.4)

def _ms_plan_worker(rid: str):
    # worker 线程不继承请求上下文:整线程绑定运行的 owner,
    # 保证步骤中的预设查询 / 上传文件解析等全部按归属账号进行;
    # try/finally:失败提前 return / 取消 / 正常结束,统一落盘执行历史
    run = _MS_RUNS.get(rid) or {}
    _ctx_token = _USER_CTX.set(run.get("owner") or "admin")
    try:
        _ms_plan_worker_inner(rid)
    finally:
        _USER_CTX.reset(_ctx_token)
        _ms_run_persist(_MS_RUNS.get(rid) or {})

def _ms_plan_worker_inner(rid: str):
    run = _MS_RUNS.get(rid) or {}
    steps = (run.get("plan") or {}).get("steps") or []

    def _set(idx: int, **kv):
        run["steps"][idx].update(kv)

    def _log(idx: int, level: str, text: str):
        _ms_run_log(run, idx, level, text)

    def _abort(idx: int, msg: str, cont: bool) -> bool:
        _set(idx, status="fail")
        _log(idx, "err", msg)
        if not cont:
            for j in range(idx + 1, len(steps)):
                _set(j, status="skipped")
            run["status"] = "failed"
            _log(-1, "info", "── 后续步骤已跳过(勾选「失败继续」可忽略) ──")
            return True
        return False

    for idx, st in enumerate(steps):
        _ms_pause_wait(run)                   # 暂停挂起:等恢复 / 撤回后再开下一步
        if run["status"] == "cancelled":
            _set(idx, status="skipped")
            continue
        _set(idx, status="running")
        cont = bool(st.get("continue_on_error"))
        targets = _ms_step_targets(st)
        if not targets:
            g = (st.get("host_group") or "").strip()
            if g:
                if _abort(idx, f"✗ 分组「{g}」内没有主机(先在左侧主机栏分组)", cont):
                    return
            elif _abort(idx, "✗ 主机不存在或已删除", cont):
                return
            continue
        preset = None
        if st.get("preset_id"):
            preset = next((p for p in _ms_presets() if p.get("id") == st["preset_id"]), None)
            if preset is None:
                if _abort(idx, "✗ 所选预设已被删除", cont):
                    return
                continue
        kind = st.get("kind") or "host"
        if preset is not None and preset.get("form") == "yaml":
            kind = "host"           # k8s YAML 预设:固定在主机上 kubectl apply(步骤里误选容器也不受影响)
        if kind == "container" and not (st.get("container") or "").strip():
            if _abort(idx, "✗ 容器步骤未指定容器名", cont):
                return
            continue
        if not preset and not (st.get("command") or "").strip():
            if _abort(idx, "✗ 命令为空(未选预设也未填命令)", cont):
                return
            continue

        def run_on_host(h: Dict[str, Any]):
            tag = h.get("name") or h.get("host") or "?"
            try:
                if preset is not None:
                    cmd = _ms_compose_cmd(preset, kind, st.get("runtime") or "docker",
                                          st.get("container") or "", st.get("namespace") or "",
                                          h, f"{rid[:6]}s{idx}", run.get("owner") or "")
                    if preset.get("form") == "upload":
                        _log(idx, "info", f"[{tag}] ⬆ 已上传脚本到目标主机 /tmp")
                    elif preset.get("form") == "yaml" and preset.get("filename"):
                        _log(idx, "info", f"[{tag}] ⬆ 已上传 YAML 文件到目标主机 /tmp")
                else:
                    cmd = (st.get("command") or "").strip()
                    if kind == "container":
                        cmd = _ms_wrap_container(st.get("runtime") or "docker",
                                                 st.get("container") or "",
                                                 st.get("namespace") or "", cmd)
                _log(idx, "cmd", f"[{tag}] $ {cmd[:1200]}")

                def _on_line(stream: str, text: str):
                    # 流式上屏:每出一行立即进运行日志,前端轮询即可实时看到
                    _log(idx, "err" if stream == "err" else "out",
                         f"[{tag}] [stderr] {text}" if stream == "err" else f"[{tag}] {text}")

                r = _ms_run_stream(h, cmd, timeout=1800, on_line=_on_line)
                ok = r["rc"] == 0
                hint = _ms_rc_hint(r["rc"], r["err"] or "")
                _log(idx, "ok" if ok else "err",
                     f"[{tag}] {'✓' if ok else '✗'} exit={r['rc']}"
                     + (f" · {hint}" if hint and not ok else ""))
                return (ok, r["rc"], hint)
            except Exception as e:
                _log(idx, "err", f"[{tag}] 执行异常:{str(e)[:400]}")
                return (False, -1, str(e)[:200])

        results = []
        if len(targets) == 1 or not st.get("parallel", True):
            for h in targets:
                _ms_pause_wait(run)           # 顺序执行时逐台之间也可暂停
                if run["status"] == "cancelled":
                    break
                results.append(run_on_host(h))
        else:
            with _cf.ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
                futs = [ex.submit(run_on_host, h) for h in targets]
                for f in _cf.as_completed(futs):
                    results.append(f.result())
        if run["status"] == "cancelled":
            _set(idx, status="skipped")
            continue
        oks = sum(1 for r in results if r[0])
        if oks == len(results):
            _set(idx, status="ok", rc=0, note=f"{oks}/{len(results)} 主机成功")
        elif oks == 0:
            rc0, hint0 = results[0][1], results[0][2]
            _set(idx, status="fail", rc=rc0, note="0/{} 主机成功".format(len(results)))
            if not cont:
                for j in range(idx + 1, len(steps)):
                    _set(j, status="skipped")
                run["status"] = "failed"
                why = f"exit={rc0}" + (f" · {hint0}" if hint0 else "")
                _log(-1, "info", f"── 步骤在全部 {len(results)} 台主机失败({why}),方案中止 ──")
                return
        else:
            _set(idx, status="partial", rc=results[0][1], note=f"{oks}/{len(results)} 主机成功")
            _log(idx, "err", f"⚠ 部分成功:{oks}/{len(results)} 台主机执行成功,失败主机见上方日志")
    # 结束态:取消不再被覆盖;有失败 / 部分成功步骤 → partial;全部成功 → done
    if run["status"] != "cancelled":
        sts = [s.get("status") for s in (run.get("steps") or [])]
        run["status"] = "partial" if any(s in ("fail", "partial") for s in sts) else "done"

@app.post("/api/modelstart/plans/{pid}/run")
async def ms_plan_run(pid: str):
    plan = next((pl for pl in _ms_plans() if pl.get("id") == pid), None)
    if not plan:
        raise HTTPException(404, "方案不存在")
    if not plan.get("steps"):
        raise HTTPException(400, "方案没有步骤")
    rid = uuid.uuid4().hex[:12]
    run = {"id": rid, "plan": json.loads(json.dumps(plan)),
           "plan_id": plan.get("id", ""), "plan_name": plan.get("name", ""),
           "owner": _cur_user(), "status": "running", "paused": False,
           "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "steps": [{"name": _ms_step_label(s), "status": "pending", "rc": None, "note": ""}
                     for s in plan["steps"]],
           "logs": [], "seq": 0}
    with _MS_RUNS_LOCK:
        _MS_RUNS[rid] = run
        if len(_MS_RUNS) > 20:                      # 最多保留最近 20 次运行
            for k in list(_MS_RUNS.keys())[:-20]:
                _MS_RUNS.pop(k, None)
    threading.Thread(target=_ms_plan_worker, args=(rid,), daemon=True).start()
    return {"run_id": rid}

def _ms_run_owned(run: Dict[str, Any]) -> bool:
    """运行记录归属:仅本人可查看 / 控制(历史文件按账号分文件,内存记录按 owner 过滤)。"""
    return run.get("owner", "admin") == _cur_user()

@app.get("/api/modelstart/runs/{rid}")
async def ms_run_poll(rid: str, after: int = 0):
    with _MS_RUNS_LOCK:
        run = _MS_RUNS.get(rid)
        if run:
            if not _ms_run_owned(run):
                raise HTTPException(404, "运行记录不存在")
            return _ms_run_view(run, after)
    # 内存没有 → 查执行历史文件(服务重启后仍可回看)
    run = next((r for r in _ms_history_load() if r.get("id") == rid), None)
    if not run:
        raise HTTPException(404, "运行记录不存在(服务可能已重启)")
    return _ms_run_view(run, after)

@app.post("/api/modelstart/runs/{rid}/cancel")
async def ms_run_cancel(rid: str):
    with _MS_RUNS_LOCK:
        run = _MS_RUNS.get(rid)
        if not run or not _ms_run_owned(run):
            raise HTTPException(404, "运行记录不存在")
        if run["status"] == "running":
            run["status"] = "cancelled"
    return {"ok": True}

@app.post("/api/modelstart/runs/{rid}/pause")
async def ms_run_pause(rid: str):
    """暂停:当前正在执行的命令跑完后挂起,等待继续或撤回。"""
    with _MS_RUNS_LOCK:
        run = _MS_RUNS.get(rid)
        if not run or not _ms_run_owned(run):
            raise HTTPException(404, "运行记录不存在")
        ok = run["status"] == "running" and not run.get("paused")
        if ok:
            run["paused"] = True
    if ok:
        _ms_run_log(run, -1, "info", "⏸ 已暂停:当前命令完成后挂起,可「继续执行」或「撤回执行」")
    return {"ok": True}

@app.post("/api/modelstart/runs/{rid}/resume")
async def ms_run_resume(rid: str):
    with _MS_RUNS_LOCK:
        run = _MS_RUNS.get(rid)
        if not run or not _ms_run_owned(run):
            raise HTTPException(404, "运行记录不存在")
        ok = run["status"] == "running" and bool(run.get("paused"))
        if ok:
            run["paused"] = False
    if ok:
        _ms_run_log(run, -1, "info", "▶ 已继续执行")
    return {"ok": True}

@app.get("/api/modelstart/runs")
async def ms_runs_list():
    """执行历史列表:内存运行(含进行中) + 历史文件快照,按开始时间倒序(仅本人)。"""
    me = _cur_user()
    with _MS_RUNS_LOCK:
        mem = [r for r in _MS_RUNS.values() if r.get("owner", "admin") == me]
    seen, merged = set(), []
    for r in mem + _ms_history_load():
        rid = r.get("id")
        if not rid or rid in seen or not r.get("status"):
            continue
        seen.add(rid)
        counts: Dict[str, int] = {}
        for s in (r.get("steps") or []):
            k = s.get("status") or "pending"
            counts[k] = counts.get(k, 0) + 1
        merged.append({"id": rid, "plan_id": r.get("plan_id", ""),
                       "plan_name": r.get("plan_name") or (r.get("plan") or {}).get("name") or "未命名方案",
                       "status": r["status"], "started": r.get("started", ""),
                       "finished": r.get("finished", ""), "steps": r.get("steps") or [],
                       "counts": counts})
    merged.sort(key=lambda x: str(x.get("started") or ""), reverse=True)
    return {"runs": merged[:100]}

@app.delete("/api/modelstart/runs")
async def ms_runs_clear():
    """清空执行历史(当前账号的历史文件 + 已结束的内存记录;进行中的运行保留)。"""
    me = _cur_user()
    with _MS_RUNS_LOCK:
        for k in [k for k, v in _MS_RUNS.items()
                  if v.get("owner", me) == me and v.get("status") != "running"]:
            _MS_RUNS.pop(k, None)
    try:
        with open(_ms_runs_file(), "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
    except Exception:
        pass
    return {"ok": True}

# ---- WebSocket 容器终端(登录容器内做修改,作为页面功能的补充) ----
@app.websocket("/api/modelstart/terminal")
async def ms_terminal(ws: _Ws):
    # WebSocket 不经过 HTTP 鉴权中间件:手工校验会话 cookie + 主机分配
    tok = (ws.cookies or {}).get("auth_token", "")
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(tok)
        if not sess or (time.time() - sess["ts"]) >= _SESSION_TTL:
            sess = None
    if not sess:
        await ws.accept()
        await ws.send_json({"type": "error", "text": "未登录或会话已过期"})
        await ws.close()
        return
    _USER_CTX.set(sess["user"])
    await ws.accept()
    if not _has_perm("modelstart"):           # WebSocket 不走 HTTP 中间件:手工校验模块权限
        await ws.send_json({"type": "error", "text": "没有「模型部署站」功能模块的使用权限,请联系管理员开通"})
        await ws.close()
        return
    hid = ws.query_params.get("host") or ""
    runtime = ws.query_params.get("runtime") or "docker"
    container = ws.query_params.get("container") or ""
    namespace = ws.query_params.get("namespace") or "default"
    h = _ms_host(hid)
    if not h:
        await ws.send_json({"type": "error", "text": "主机不存在或已删除"})
        await ws.close()
        return
    if not container:
        await ws.send_json({"type": "error", "text": "未指定容器"})
        await ws.close()
        return
    chan = None
    try:
        client = _ssh_client(_ms_key(h), h)
        chan = client.invoke_shell(term="xterm-256color", width=118, height=30)
        chan.settimeout(0.5)
        if runtime == "k8s":
            q = _shlex.quote(container), _shlex.quote(namespace)
            enter = (f"kubectl exec -it {q[0]} -n {q[1]} -- bash 2>/dev/null "
                     f"|| kubectl exec -it {q[0]} -n {q[1]} -- sh")
        else:
            enter = (f"docker exec -it {_shlex.quote(container)} bash 2>/dev/null "
                     f"|| docker exec -it {_shlex.quote(container)} sh")
        chan.send(enter + "\n")

        def _recv_some() -> bytes:
            try:
                return chan.recv(4096)
            except socket.timeout:
                return b""
            except Exception:
                return b""

        async def pump_out():
            while True:
                data = await asyncio.to_thread(_recv_some)
                if chan.closed:
                    break
                if data:
                    await ws.send_json({"type": "output", "data": data.decode("utf-8", "replace")})

        async def pump_in():
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "input":
                    chan.send(msg.get("data") or "")
                elif msg.get("type") == "resize":
                    try:
                        chan.resize_pty(width=int(msg.get("cols") or 118),
                                        height=int(msg.get("rows") or 30))
                    except Exception:
                        pass

        t1 = asyncio.create_task(pump_out())
        t2 = asyncio.create_task(pump_in())
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:                      # 回收已完成任务的异常,避免警告
            try:
                t.result()
            except Exception:
                pass
    except _WsDisc:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "text": f"终端错误:{str(e)[:300]}"})
            await ws.close()
        except Exception:
            pass
    finally:
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass

@app.get("/modelstart")
async def modelstart_page():
    """模型部署站:SPA 内 hash 路由页,旧链接重定向。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/#modelstart")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ============================================================================
# Entry
# ============================================================================

def _pick_port(preferred: int = 8765) -> int:
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", preferred))
            return preferred
        except OSError:
            s.bind(("0.0.0.0", 0))
            return s.getsockname()[1]

def _lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None

def _print_qr(text: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.print_ascii(invert=True)
    except Exception:
        pass

if __name__ == "__main__":
    port = _pick_port(8765)
    local_url = f"http://127.0.0.1:{port}"
    lan_ip = _lan_ip()
    lan_url = f"http://{lan_ip}:{port}" if lan_ip else None
    print("=" * 60)
    print("  LLM API Benchmark Tool")
    print(f"  本机访问: {local_url}")
    if lan_url:
        print(f"  局域网访问(手机/其他电脑,同一 Wi-Fi): {lan_url}")
        _print_qr(lan_url)
    print("=" * 60)
    if os.environ.get("BENCH_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
