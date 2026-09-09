// OccupancyCalc.h — occupancy 计算器（等价 ncu 的 ncu_occupancy.py / LaunchStats 规则）
//
// 理论 occupancy：每个 SM 能同时驻留的 warp 数上限，由
//   线程数/块、每块寄存器、每块静态+动态共享内存、块数上限
// 四个维度取最小值决定。
#pragma once
#include "core/GpuInfo.h"

namespace ncuprof {

struct KernelLaunchConfig {
    int blockDimX = 1, blockDimY = 1, blockDimZ = 1;
    int gridDimX = 1, gridDimY = 1, gridDimZ = 1;
    int regsPerThread = 0;       // 0 = 未编译进 fatbin 信息（回退保守值）
    size_t staticSmemBytes = 0;
    size_t dynamicSmemBytes = 0;
    size_t maxDynamicSmemBytes = 0;  // cuFuncSetAttribute 的 optin 值
};

struct OccupancyResult {
    double theoreticalOccupancyPct = 0.0;   // 每 SM 活跃 warp / 最大 warp
    double achievedOccupancyPct = 0.0;      // 需要 sm__warps_active 指标，未知时 = theoretical
    int blockLimitPerSM = 0;                // 各维度约束的最小块数
    int blockLimitWarps = 0, blockLimitRegs = 0, blockLimitSmem = 0;
    int maxBlocksPerSM = 0;                 // 硬上限（通常 32，Ampere+ 为 16/32）
    int wavesPerSM = 0;                     // launch__waves_per_multiprocessor
    bool valid = false;
};

OccupancyResult computeOccupancy(const GpuDevice& dev, const KernelLaunchConfig& cfg);

// launch__waves_per_multiprocessor（ncu LaunchStats section 的关键指标）
double wavesPerSM(const GpuDevice& dev, const KernelLaunchConfig& cfg);

}  // namespace ncuprof
