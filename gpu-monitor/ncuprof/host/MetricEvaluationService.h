// MetricEvaluationService.h — host 侧指标后处理
// 把 target 回传的原始指标（HW 计数 + launch__* 静态值）补全为完整指标集：
//   1. 派生指标求值（MetricDatabase：occupancy 链 / per_second / 归一化权重）
//   2. device__attribute_* 静态指标注入
//   3. breakdown: 展开（吞吐构成表）
#pragma once
#include "core/GpuInfo.h"
#include "core/MetricDatabase.h"

namespace ncuprof {

class MetricEvaluationService {
public:
    // devices: Attached 消息 + 本机枚举得到的设备表（deviceIndex -> GpuDevice）
    explicit MetricEvaluationService(SystemInfo sys) : sys_(std::move(sys)) {}

    // 就地补全 KernelRecord::metrics
    void completeMetrics(const GpuDevice& dev,
                         std::map<std::string, MetricResult>& metrics) const;

    const SystemInfo& system() const { return sys_; }

private:
    SystemInfo sys_;
};

}  // namespace ncuprof
