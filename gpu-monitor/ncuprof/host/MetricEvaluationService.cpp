#include "host/MetricEvaluationService.h"

#include <algorithm>

namespace ncuprof {

void MetricEvaluationService::completeMetrics(
    const GpuDevice& dev,
    std::map<std::string, MetricResult>& metrics) const {
    // ---- 1. device__attribute_* 静态指标（--page raw 显示用）----
    static const char* attrs[] = {
        "device__attribute_compute_capability_major",
        "device__attribute_compute_capability_minor",
        "device__attribute_multiprocessor_count",
        "device__attribute_max_registers_per_multiprocessor",
        "device__attribute_max_shared_memory_per_block",
        "device__attribute_max_shared_memory_per_multiprocessor",
        "device__attribute_max_threads_per_block",
        "device__attribute_warp_size",
        "device__attribute_l2_cache_size",
        "device__attribute_total_memory",
        "device__attribute_memory_bus_width",
        "device__attribute_clock_rate",
        "device__attribute_memory_clock_rate",
    };
    for (auto* a : attrs) {
        double v;
        std::string unit;
        if (deviceAttributeValue(dev, a, v, unit)) {
            MetricResult r;
            r.name = a;
            r.value = v;
            r.unit = unit;
            r.valid = true;
            metrics[a] = r;
        }
    }

    // ---- 2. 派生指标（occupancy / waves / per_second …）----
    MetricDatabase::get().evaluateDerived(metrics, dev);

    // ---- 3. breakdown: 展开 ----
    // breakdown:<throughput> -> 展开成 N 个子指标，并生成 "breakdown:<…>"
    // 汇总指标（值为构成中的最大值，ncu 语义：throughput = max(构成)）
    std::vector<std::string> breakdowns;
    for (const auto& [name, r] : metrics) {
        if (name.compare(0, 10, "breakdown:") == 0)
            breakdowns.push_back(name);
    }
    for (const auto& b : breakdowns) {
        std::string target = b.substr(10);
        auto parts = expandBreakdown(target, dev);
        double maxPct = 0;
        bool any = false;
        for (const auto& p : parts) {
            auto it = metrics.find(p);
            if (it != metrics.end() && it->second.valid) {
                maxPct = std::max(maxPct, it->second.value);
                any = true;
            }
        }
        if (any) {
            MetricResult r;
            r.name = b;
            r.value = maxPct;
            r.unit = "percent";
            r.valid = true;
            metrics[b] = r;
        }
    }
}

}  // namespace ncuprof
