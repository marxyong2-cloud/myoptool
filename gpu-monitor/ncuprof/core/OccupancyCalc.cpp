#include "core/OccupancyCalc.h"

#include <algorithm>

namespace ncuprof {

namespace {
// 各架构每 SM 最大驻留块数（CUDA C Programming Guide §Table 15 Technical Specifications）
int maxBlocksPerSM(int ccMajor, int ccMinor) {
    if (ccMajor >= 9) return 32;
    if (ccMajor == 8 && ccMinor >= 6) return 16;   // Ampere GA10x+
    if (ccMajor == 8) return 32;                   // A100
    if (ccMajor == 7) return 32;
    return 16;                                      // Kepler/Maxwell/Pascal/Volta 保守值
}
}  // namespace

OccupancyResult computeOccupancy(const GpuDevice& dev, const KernelLaunchConfig& cfg) {
    OccupancyResult r;
    r.maxBlocksPerSM = maxBlocksPerSM(dev.ccMajor, dev.ccMinor);

    const int threadsPerBlock =
        cfg.blockDimX * cfg.blockDimY * cfg.blockDimZ;
    if (threadsPerBlock <= 0 || dev.warpSize <= 0) return r;

    const int warpsPerBlock = (threadsPerBlock + dev.warpSize - 1) / dev.warpSize;
    const long long blocksPerGrid =
        (long long)cfg.gridDimX * cfg.gridDimY * cfg.gridDimZ;

    // ---- 各维度约束 ----
    // warp 槽位
    r.blockLimitWarps = dev.maxThreadsPerSM / dev.warpSize / warpsPerBlock;
    // 寄存器文件（分配粒度 256 寄存器/线程束调度）
    if (cfg.regsPerThread > 0) {
        int granularity = 8;                        // 寄存器按 8 分配粒度向上取整
        int regs = ((cfg.regsPerThread + granularity - 1) / granularity) * granularity;
        r.blockLimitRegs = dev.regsPerSM / (regs * threadsPerBlock);
    } else {
        r.blockLimitRegs = r.maxBlocksPerSM;        // 无信息：不约束
    }
    // 共享内存（静态 + 动态 optin）
    size_t smem = cfg.staticSmemBytes + cfg.dynamicSmemBytes;
    size_t smemAvail = (size_t)dev.sharedMemPerSM;
    if (smem > 0 && cfg.dynamicSmemBytes > dev.sharedMemPerBlockOptin) {
        return r;                                    // 超出 optin 上限：启动即失败
    }
    r.blockLimitSmem = smem > 0 ? (int)(smemAvail / smem) : r.maxBlocksPerSM;

    r.blockLimitPerSM = std::min({r.blockLimitWarps, r.blockLimitRegs,
                                  r.blockLimitSmem, r.maxBlocksPerSM});
    if (r.blockLimitPerSM < 1) r.blockLimitPerSM = 1;   // 至少驻留 1 块

    const int activeWarpsPerSM = r.blockLimitPerSM * warpsPerBlock;
    const int maxWarpsPerSM = dev.maxThreadsPerSM / dev.warpSize;
    r.theoreticalOccupancyPct =
        100.0 * (double)activeWarpsPerSM / (double)maxWarpsPerSM;
    r.achievedOccupancyPct = r.theoreticalOccupancyPct;

    r.wavesPerSM = (int)(blocksPerGrid / r.blockLimitPerSM + 1);  // 部分波向上取整
    r.valid = true;
    return r;
}

double wavesPerSM(const GpuDevice& dev, const KernelLaunchConfig& cfg) {
    auto r = computeOccupancy(dev, cfg);
    if (!r.valid) return 0.0;
    long long blocksPerGrid =
        (long long)cfg.gridDimX * cfg.gridDimY * cfg.gridDimZ;
    // ncu 语义：完整波数 + 部分波（小数）
    return (double)blocksPerGrid / (double)r.blockLimitPerSM;
}

}  // namespace ncuprof
