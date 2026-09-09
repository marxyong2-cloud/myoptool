// GpuInfo.h — 设备属性采集（等价 nv-system-info-collector 的核心输出 +
// ncu 报告里的 device__attribute_* 指标来源）
#pragma once
#include <string>
#include <vector>

namespace ncuprof {

struct GpuDevice {
    int deviceIndex = 0;
    std::string name;                 // "NVIDIA GeForce RTX 4090"
    std::string chipName;             // "gh102" 风格（CUPTI Profiler 需要）
    int ccMajor = 0, ccMinor = 0;     // compute capability
    int numSMs = 0;                   // 多处理器数
    int numCoresPerSM = 0;
    int regsPerSM = 0;                // 每 SM 寄存器文件（32bit）
    int sharedMemPerBlockOptin = 0;   // 动态共享内存上限（字节）
    int sharedMemPerSM = 0;
    int warpSize = 32;
    int maxThreadsPerSM = 0;
    int maxThreadsPerBlock = 0;
    int clockRateKHz = 0;             // 额定频率
    int memClockRateKHz = 0;
    long long totalMemBytes = 0;
    int memBusWidth = 0;
    int l2CacheBytes = 0;
    int asyncEngineCount = 0;
    int pciDomain = 0, pciBus = 0, pciDevice = 0;
    std::string uuid;
    std::string driverVersion;
    std::string runtimeVersion;

    // 峰值带宽（字节/秒），用于 SOL/roofline
    double dramBandwidthPeak() const {
        return (double)memClockRateKHz * 1000.0 * (memBusWidth / 8) * 2.0;  // DDR
    }
    double peakFlopsF32() const {
        return (double)numSMs * numCoresPerSM * 2.0 * (double)clockRateKHz * 1000.0;
    }
};

struct SystemInfo {
    std::string osName, kernelVersion, cpuModel, hostname;
    std::vector<GpuDevice> gpus;
};

// 用 CUDA Driver API 枚举（注入库内/宿主均可调用；返回 false = 无驱动）
bool collectGpuInfo(SystemInfo& out, std::string* err = nullptr);

// device__attribute_* 静态指标 → 值（ncu 的 --page raw 列出这些）
// 返回 false 表示属性名未知
bool deviceAttributeValue(const GpuDevice& dev, const std::string& attr,
                          double& outValue, std::string& outUnit);

}  // namespace ncuprof
