#include "core/MetricDatabase.h"

#include <cmath>
#include <cstring>

#include "core/MetricName.h"
#include "core/OccupancyCalc.h"

namespace ncuprof {

MetricDatabase& MetricDatabase::get() {
    static MetricDatabase db;
    return db;
}

// 直接可采集指标：PerfWorks 单元前缀（逆向自 ncu 文档 §2.3.4 Units）
static const char* kCollectiblePrefixes[] = {
    "sm__",       "smsp__",    "l1tex__",   "lts__",    "ltc__",
    "dram__",     "fbpa__",    "gpc__",     "gpu__",    "gr__",
    "sys__",      "tpc__",     "idc__",     "icc__",    "gcc__",
    "imc__",      "pm__",      "nvltx__",   "nvlrx__",  "mcc__",
    "lrc__",      "gxc__",     "xcomp__",   "fe__",
};

static const char* kStaticPrefixes[] = {
    "launch__", "device__attribute_", "kernel__", "session__", "value__",
};

bool MetricDatabase::isCollectible(const std::string& full) {
    MetricName m;
    if (!parseMetricName(full, m)) return false;
    if (!m.prefix.empty()) return false;                  // regex:/group:/breakdown: 需展开
    for (auto* p : kStaticPrefixes)
        if (m.base.compare(0, strlen(p), p) == 0) return false;   // 静态/派生
    for (auto* p : kCollectiblePrefixes)
        if (m.base.compare(0, strlen(p), p) == 0) return true;
    return false;
}

std::vector<std::string> MetricDatabase::dependenciesOf(const std::string& full) const {
    MetricName m;
    if (!parseMetricName(full, m)) return {};
    auto it = derived_.find(m.base);
    if (it == derived_.end()) return {};
    return it->second.deps;
}

void MetricDatabase::addDerived(const std::string& name, const std::string& unit,
                                std::vector<std::string> collectFor, DerivedFn fn) {
    derived_[name] = Entry{unit, std::move(collectFor), std::move(fn)};
}

void MetricDatabase::evaluateDerived(MetricMap& metrics, const GpuDevice& dev) const {
    // 多轮求值以支持派生指标之间的依赖（occupancy 链）
    for (int round = 0; round < 3; ++round) {
        bool changed = false;
        for (const auto& [name, e] : derived_) {
            auto it = metrics.find(name);
            if (it != metrics.end() && it->second.valid) continue;
            MetricResult r = e.fn(metrics, dev);
            if (r.valid) {
                r.name = name;
                r.unit = e.unit;
                metrics[name] = r;
                changed = true;
            }
        }
        if (!changed) break;
    }
}

std::vector<MetricDatabase::MetricInfo> MetricDatabase::listMetrics(
    const GpuDevice* dev) const {
    std::vector<MetricInfo> out;
    // 直接采集层（节选高频指标；完整列表在运行时经 CUPTI Host API 枚举）
    static const char* hw[] = {
        "sm__throughput.avg.pct_of_peak_sustained_elapsed",
        "gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed",
        "gpu__dram_throughput.avg.pct_of_peak_sustained_elapsed",
        "l1tex__throughput.avg.pct_of_peak_sustained_active",
        "lts__throughput.avg.pct_of_peak_sustained_elapsed",
        "gpc__cycles_elapsed.max",
        "gpc__cycles_elapsed.avg.per_second",
        "gpu__time_duration.sum",
        "sm__cycles_active.avg",
        "sm__inst_executed.avg.per_cycle_active",
        "sm__inst_executed_pipe_fma.avg.pct_of_peak_sustained_active",
        "smsp__average_warps_issue_stalled_long_scoreboard_per_issue_active.ratio",
        "sm__warps_active.avg.pct_of_peak_sustained_active",
        "l1tex__data_bank_conflicts_pipe_lsu.sum",
        "l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_ld.ratio",
        "lts__t_sector_hit_rate.pct",
        "dram__bytes.sum",
        "dram__bytes_read.sum",
        "dram__bytes_write.sum",
        "sm__sass_thread_inst_executed_op_shared_ld.sum",
    };
    for (auto* n : hw) out.push_back({n, "", "PerfWorks hardware counter metric", false});

    for (const auto& [name, e] : derived_)
        out.push_back({name, e.unit, "derived metric", true});

    if (dev) {
        static const char* attrs[] = {
            "device__attribute_compute_capability_major",
            "device__attribute_compute_capability_minor",
            "device__attribute_multiprocessor_count",
            "device__attribute_max_registers_per_multiprocessor",
            "device__attribute_max_shared_memory_per_block",
            "device__attribute_max_threads_per_block",
            "device__attribute_l2_cache_size",
            "device__attribute_total_memory",
        };
        for (auto* a : attrs) out.push_back({a, "", "device attribute", true});
    }
    return out;
}

// ---------------- 内置派生指标 ----------------
namespace {

// 注意命名不可为 get()：registerBuiltins() 的 lambda 内无限定查找
// 会先命中类静态成员 MetricDatabase::get()（单例访问器）。
double metricValue(const MetricMap& m, const char* name, double dflt = 0.0) {
    auto it = m.find(name);
    return it != m.end() && it->second.valid ? it->second.value : dflt;
}

MetricResult ok(double v) {
    MetricResult r;
    r.value = v;
    r.valid = true;
    return r;
}

KernelLaunchConfig launchConfigFrom(const MetricMap& m) {
    KernelLaunchConfig c;
    c.blockDimX = (int)metricValue(m, "launch__block_size");
    c.gridDimX = (int)metricValue(m, "launch__grid_size");
    c.regsPerThread = (int)metricValue(m, "launch__registers_per_thread");
    c.staticSmemBytes = (size_t)metricValue(m, "launch__shared_mem_per_block_static");
    c.dynamicSmemBytes = (size_t)metricValue(m, "launch__shared_mem_per_block_dynamic");
    return c;
}

}  // namespace

std::vector<std::string> expandBreakdown(const std::string& throughput,
                                         const GpuDevice& dev) {
    // 吞吐分解（ncu breakdown: 前缀）：内存吞吐 → DRAM/L2/L1 构成
    if (throughput.find("compute_memory") != std::string::npos) {
        std::vector<std::string> r = {
            "gpu__dram_throughput.avg.pct_of_peak_sustained_elapsed",
            "lts__throughput.avg.pct_of_peak_sustained_elapsed",
            "l1tex__throughput.avg.pct_of_peak_sustained_active",
        };
        if (dev.ccMajor >= 9)   // Hopper+: 一致性/原子单位加入分解
            r.push_back("sm__mio_pq_read_cycles_active.avg.pct_of_peak_sustained_elapsed");
        return r;
    }
    if (throughput.find("sm__throughput") == 0) {
        return {
            "sm__inst_executed.avg.pct_of_peak_sustained_elapsed",
            "sm__issue_active.avg.pct_of_peak_sustained_elapsed",
            "sm__pipe_alu_cycles_active.avg.pct_of_peak_sustained_active",
            "sm__pipe_fma_cycles_active.avg.pct_of_peak_sustained_active",
            "sm__pipe_fp64_cycles_active.avg.pct_of_peak_sustained_active",
            "sm__pipe_shared_cycles_active.avg.pct_of_peak_sustained_active",
            "sm__mio_pq_write_cycles_active.avg.pct_of_peak_sustained_active",
        };
    }
    return {throughput};   // 未识别：原样采集
}

void MetricDatabase::registerBuiltins() {
    // ===== occupancy 链（等价 ncu LaunchStats + Occupancy sections）=====
    addDerived(
        "launch__occupancy_limit_blocks", "block",
        {"launch__grid_size", "launch__block_size", "launch__registers_per_thread",
         "launch__shared_mem_per_block_static", "launch__shared_mem_per_block_dynamic"},
        [this](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            auto r = computeOccupancy(d, launchConfigFrom(m));
            if (!r.valid) return MetricResult{};
            return ok(r.blockLimitPerSM);
        });

    addDerived(
        "launch__occupancy_limit_registers", "block",
        {"launch__block_size", "launch__registers_per_thread"},
        [](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            auto r = computeOccupancy(d, launchConfigFrom(m));
            return r.valid ? ok(r.blockLimitRegs) : MetricResult{};
        });

    addDerived(
        "launch__occupancy_limit_shared_mem", "block",
        {"launch__shared_mem_per_block_static", "launch__shared_mem_per_block_dynamic"},
        [](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            auto r = computeOccupancy(d, launchConfigFrom(m));
            return r.valid ? ok(r.blockLimitSmem) : MetricResult{};
        });

    addDerived(
        "launch__occupancy_limit_warps", "block",
        {"launch__block_size"},
        [](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            auto r = computeOccupancy(d, launchConfigFrom(m));
            return r.valid ? ok(r.blockLimitWarps) : MetricResult{};
        });

    addDerived(
        "launch__occupancy_pct", "percent",
        {"launch__block_size", "launch__registers_per_thread",
         "launch__shared_mem_per_block_static"},
        [](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            auto r = computeOccupancy(d, launchConfigFrom(m));
            return r.valid ? ok(r.theoreticalOccupancyPct) : MetricResult{};
        });

    addDerived(
        "launch__waves_per_multiprocessor", "wave",
        {"launch__grid_size", "launch__block_size", "launch__registers_per_thread",
         "launch__shared_mem_per_block_static", "launch__shared_mem_per_block_dynamic"},
        [](const MetricMap& m, const GpuDevice& d) -> MetricResult {
            return ok(wavesPerSM(d, launchConfigFrom(m)));
        });

    // ===== 每 second 换算（反向自 ncu 子指标语义：X.sum / gpu__time_duration.sum）=====
    addDerived(
        "dram__bytes.sum.per_second", "byte/second",
        {"dram__bytes.sum", "gpu__time_duration.sum"},
        [](const MetricMap& m, const GpuDevice&) -> MetricResult {
            double durNs = metricValue(m, "gpu__time_duration.sum", -1);
            if (durNs <= 0) return MetricResult{};
            return ok(metricValue(m, "dram__bytes.sum") / (durNs * 1e-9));
        });

    // ===== 规则用归一化权重（SOLBottleneck 规则的输入）=====
    addDerived(
        "value__compute_throughput_normalized", "ratio",
        {"sm__throughput.avg.pct_of_peak_sustained_elapsed"},
        [](const MetricMap& m, const GpuDevice&) -> MetricResult {
            return ok(metricValue(m, "sm__throughput.avg.pct_of_peak_sustained_elapsed") / 100.0);
        });
    addDerived(
        "value__memory_throughput_normalized", "ratio",
        {"gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed"},
        [](const MetricMap& m, const GpuDevice&) -> MetricResult {
            return ok(metricValue(m, "gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed") / 100.0);
        });
}

}  // namespace ncuprof
