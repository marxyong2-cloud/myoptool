// MetricDatabase.h — 指标注册表与派生公式
//
// ncu 的指标求值由 libnvperf_host.so（PerfWorks 私有指标数据库）完成。
// 本实现采用双层策略：
//   1) 直接采集层：PerfWorks 命名的 HW 指标（sm__…, l1tex__…, gpu__…）原样
//      交给 CUPTI Profiler Host API（cupti_profiler_host.h）做 名字→计数→值
//      的求值 —— CUPTI 公开版覆盖同名指标，无需私有公式。
//   2) 派生层：MetricDatabase 内置公开公式（launch__*、occupancy、SOL 汇总
//      等），由已采指标 + 设备属性计算，等价于 ncu 的静态指标与派生指标。
#pragma once
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "core/GpuInfo.h"

namespace ncuprof {

// 一次 kernel 剖析的指标结果（名字 -> 值，可含多实例）
struct MetricResult {
    std::string name;
    double value = 0.0;
    std::string unit;
    std::vector<double> instances;   // ShowInstances 时每实例一值
    bool valid = false;
    std::string invalidReason;       // "not collected on this chip" 等
};

using MetricMap = std::map<std::string, MetricResult>;

class MetricDatabase {
public:
    using DerivedFn = std::function<MetricResult(const MetricMap&, const GpuDevice&)>;

    static MetricDatabase& get();    // 进程级单例

    // 注册派生指标：collectFor 为其依赖的直接采集指标名
    void addDerived(const std::string& name, const std::string& unit,
                    std::vector<std::string> collectFor, DerivedFn fn);

    // 对全部注册派生指标求值（结果写回 metrics，供报告/规则使用）
    void evaluateDerived(MetricMap& metrics, const GpuDevice& dev) const;

    // 判断指标名是否可直接交 CUPTI 采集（PerfWorks HW 指标）
    static bool isCollectible(const std::string& fullMetricName);
    // 派生指标依赖表：{"occupancy": ["sm__maximum_warps_per_active_cycle_pct", …]}
    std::vector<std::string> dependenciesOf(const std::string& fullMetricName) const;

    // --query-metrics 输出
    struct MetricInfo { std::string name, unit, description; bool derived; };
    std::vector<MetricInfo> listMetrics(const GpuDevice* dev) const;

private:
    struct Entry {
        std::string unit;
        std::vector<std::string> deps;
        DerivedFn fn;
    };
    MetricDatabase() { registerBuiltins(); }
    std::map<std::string, Entry> derived_;
    void registerBuiltins();
};

// breakdown:<throughput> 展开：返回该吞吐指标的构成子指标名
// （例：gpu__compute_memory_throughput → {dram__throughput…, lts__throughput…, l1tex__throughput…}）
std::vector<std::string> expandBreakdown(const std::string& throughputMetric,
                                         const GpuDevice& dev);

}  // namespace ncuprof
