// MetricName.h — PerfWorks 风格指标名解析
// 命名规范（逆向自 ncu ProfilingGuide §2.3.2 Metrics Structure）:
//   <前缀:>unit__(subunit?)_(pipestage?)_quantity_(qualifiers?).rollup.submetric…
// 前缀: "" (直接求值) | "regex:" | "group:" | "breakdown:[depth:]"
#pragma once
#include <string>
#include <vector>

namespace ncuprof {

struct MetricName {
    std::string prefix;      // "", "regex", "group", "breakdown"
    std::string pattern;     // regex/group 使用的模式或分解目标名
    int breakdownDepth = 1;  // breakdown:[depth:]metric
    std::string base;        // sm__throughput
    std::string rollup;      // avg / sum / min / max（计数类必有）
    std::vector<std::string> submetrics;  // pct_of_peak_sustained_elapsed …
    std::string fullName;    // 规范化后的完整名

    bool hasSubmetric(const char* s) const;
};

// 解析指标名。解析失败返回 false（仍填充 fullName 便于报错）。
bool parseMetricName(const std::string& name, MetricName& out);

// "sm__throughput.avg.pct_of_peak_sustained_elapsed" -> "sm__throughput"
// （用于查询指标基名的属性，如依赖的硬件计数器集合）
std::string metricBaseName(const std::string& full);

// 指标单位类型（决定控制台数量级缩放，ncu --units）
enum class MetricUnit {
    Unknown, Cycle, Second, Byte, BytePerSecond, Instruction, Thread,
    Percent, Ratio, Warp, Block, Kbyte, Address, Frequency, Count
};
MetricUnit unitFromString(const std::string& s);
const char* unitToString(MetricUnit u);

}  // namespace ncuprof
