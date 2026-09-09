#include "core/MetricName.h"

#include <cctype>

namespace ncuprof {
namespace {

// 把 "a.b.c" 按顶层 '.' 切分（指标名内不存在转义点）
std::vector<std::string> splitDots(const std::string& s) {
    std::vector<std::string> parts;
    std::string cur;
    for (char c : s) {
        if (c == '.') { parts.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    parts.push_back(cur);
    return parts;
}

bool isRollup(const std::string& s) {
    return s == "sum" || s == "avg" || s == "min" || s == "max";
}

}  // namespace

bool MetricName::hasSubmetric(const char* s) const {
    for (const auto& m : submetrics)
        if (m == s) return true;
    return false;
}

bool parseMetricName(const std::string& name, MetricName& out) {
    out = MetricName{};
    out.fullName = name;

    std::string rest = name;

    // ---- 前缀 ----
    auto colon = rest.find(':');
    if (colon != std::string::npos) {
        std::string p = rest.substr(0, colon);
        if (p == "regex" || p == "group") {
            out.prefix = p;
            out.pattern = rest.substr(colon + 1);
            return true;  // 模式类指标：求值阶段再展开
        }
        if (p == "breakdown") {
            out.prefix = "breakdown";
            std::string arg = rest.substr(colon + 1);
            // breakdown:[depth:]metric
            auto firstDot = arg.find('.');
            std::string head = firstDot == std::string::npos ? arg : arg.substr(0, firstDot);
            if (!head.empty() && std::isdigit((unsigned char)head[0])) {
                out.breakdownDepth = atoi(head.c_str());
                out.pattern = arg.substr(firstDot + 1);
            } else {
                out.pattern = arg;
            }
            return true;
        }
        return false;  // 未知前缀
    }

    // ---- base.rollup.submetric... ----
    auto parts = splitDots(rest);
    out.base = parts[0];
    if (parts.size() >= 2 && isRollup(parts[1])) {
        out.rollup = parts[1];
        for (size_t i = 2; i < parts.size(); ++i) out.submetrics.push_back(parts[i]);
    } else {
        // launch__*/device__attribute_* 等非 PerfWorks 命名没有 rollup，
        // parts[1:] 全部视作名字的一部分（含多级限定符）。
        for (size_t i = 1; i < parts.size(); ++i)
            out.base += "." + parts[i];
    }

    // 基本校验：unit__quantity 至少含 "__"（launch__/device__/gpu__ 除外也满足）
    if (out.base.empty()) return false;
    return true;
}

std::string metricBaseName(const std::string& full) {
    MetricName m;
    if (!parseMetricName(full, m) || !m.prefix.empty()) return full;
    return m.base;
}

MetricUnit unitFromString(const std::string& s) {
    if (s == "cycle" || s == "cycles") return MetricUnit::Cycle;
    if (s == "second" || s == "usecond" || s == "nsecond" || s == "msecond") return MetricUnit::Second;
    if (s == "byte" || s == "Kbyte" || s == "Mbyte" || s == "Gbyte") return MetricUnit::Byte;
    if (s == "byte/second") return MetricUnit::BytePerSecond;
    if (s == "inst") return MetricUnit::Instruction;
    if (s == "thread") return MetricUnit::Thread;
    if (s == "percent") return MetricUnit::Percent;
    if (s == "ratio") return MetricUnit::Ratio;
    if (s == "warp") return MetricUnit::Warp;
    if (s == "block") return MetricUnit::Block;
    if (s == "address") return MetricUnit::Address;
    if (s == "cycle/second") return MetricUnit::Frequency;
    return MetricUnit::Unknown;
}

const char* unitToString(MetricUnit u) {
    switch (u) {
        case MetricUnit::Cycle: return "cycle";
        case MetricUnit::Second: return "second";
        case MetricUnit::Byte: return "byte";
        case MetricUnit::BytePerSecond: return "byte/second";
        case MetricUnit::Instruction: return "inst";
        case MetricUnit::Thread: return "thread";
        case MetricUnit::Percent: return "percent";
        case MetricUnit::Ratio: return "ratio";
        case MetricUnit::Warp: return "warp";
        case MetricUnit::Block: return "block";
        case MetricUnit::Kbyte: return "Kbyte";
        case MetricUnit::Address: return "address";
        case MetricUnit::Frequency: return "cycle/second";
        default: return "";
    }
}

}  // namespace ncuprof
