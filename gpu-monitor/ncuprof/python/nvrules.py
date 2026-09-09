# nvrules.py — NvRules 兼容层（规则开发接口）
#
# ncu 的规则通过 C++ NvRules 模块访问指标（见 RuleTemplates/*.py）；
# 本实现由 host 的 RuleEngine 把 kernel 指标打包为 handle dict 传入，
# 规则通过本模块的 API 读取指标并输出消息。写法与官方规则一致：
#
#   import nvrules
#   def get_identifier(): return "SOLBottleneck"
#   def apply(handle):
#       ctx = nvrules.get_context(handle)
#       action = ctx.range_by_idx(0).action_by_idx(0)
#       sm = action.metric_by_name("sm__throughput.avg.pct_of_peak_sustained_elapsed")
#       fe = ctx.frontend()
#       fe.message(nvrules.MsgType.OK, "text", "Name")
#
# 兼容子集：get_context / MsgType / IFrontend(Severity, message, focus_metric,
# send_dict_to_children) / IContext(range_by_idx, frontend) / IRange(action_by_idx)
# / IAction(metric_by_name, workload_type) / IMetric(name, value, unit, valid)

class MsgType:
    NONE = 0
    OK = 1
    OPTIMIZATION = 2
    WARNING = 3
    ERROR = 4


_MSG_TAGS = {MsgType.OK: "OK", MsgType.OPTIMIZATION: "OPTIMIZATION",
             MsgType.WARNING: "WARNING", MsgType.ERROR: "ERROR"}

_MSG_NAMES = {v: k for k, v in _MSG_TAGS.items()}


class IMetric:
    """单个指标：name/value/unit/valid + value() 访问器"""

    def __init__(self, name, value, unit="", valid=True):
        self._name = name
        self._value = value
        self._unit = unit
        self._valid = valid

    def name(self):
        return self._name

    def value(self):
        return self._value

    def unit(self):
        return self._unit

    def valid(self):
        return self._valid


class IAction:
    """一次 kernel 剖析动作（指标集合载体）"""

    def __init__(self, handle):
        self._handle = handle

    def metric_by_name(self, name):
        metrics = self._handle.get("metrics", {})
        if name not in metrics:
            return IMetric(name, 0.0, "", False)
        return IMetric(name, metrics[name], "", True)

    def workload_type(self):
        return "kernel"

    def value(self, name):  # 便捷访问
        return self.metric_by_name(name).value()


class IRange:
    def __init__(self, handle):
        self._handle = handle

    def action_by_idx(self, idx):
        return IAction(self._handle)


class IFrontend:
    """输出接口：消息 / focus metric / 向子规则传值"""

    Severity_SEVERITY_DEFAULT = 0
    Severity_SEVERITY_LOW = 1
    Severity_SEVERITY_HIGH = 2

    def __init__(self, handle):
        self._handle = handle
        self._child_dict = {}

    def message(self, msg_type, text, name=""):
        """返回 msg_id；消息追加到 handle["messages"]（host 回读）"""
        messages = self._handle.setdefault("messages", [])
        tag = _MSG_TAGS.get(msg_type, "OK")
        messages.append({
            "section": self._handle.get("current_section", ""),
            "type": tag,
            "name": name,
            "text": text,
        })
        return len(messages) - 1

    def focus_metric(self, msg_id, metric_name, value, severity, detail=""):
        messages = self._handle.get("messages", [])
        if 0 <= msg_id < len(messages):
            messages[msg_id].setdefault("focus_metrics", []).append(
                {"name": metric_name, "value": value,
                 "severity": severity, "detail": detail})

    def send_dict_to_children(self, d):
        self._child_dict.update(d)


class IContext:
    def __init__(self, handle):
        self._handle = handle

    def frontend(self):
        return IFrontend(self._handle)

    def range_by_idx(self, idx):
        return IRange(self._handle)


def get_context(handle):
    """规则入口：handle -> IContext"""
    return IContext(handle)


# ---------------- 请求指标辅助（等价 RequestedMetricsParser）----------------

class MetricRequest:
    def __init__(self, metric_name, key=None, required=True, default_value=None):
        self.metric_name = metric_name
        self.key = key or metric_name
        self.required = required
        self.default_value = default_value


def parse_requested_metrics(handle, requests):
    """按 MetricRequest 列表取指标；缺失的必选指标返回 None 触发规则跳过。
    返回 {key: IMetric|None}。"""
    action = IContext(handle).range_by_idx(0).action_by_idx(0)
    out = {}
    for r in requests:
        m = action.metric_by_name(r.metric_name)
        if not m.valid() and r.required:
            out[r.key] = None
        elif not m.valid() and not r.required:
            out[r.key] = IMetric(r.metric_name,
                                 r.default_value if r.default_value is not None else 0.0,
                                 "", False)
        else:
            out[r.key] = m
    return out
