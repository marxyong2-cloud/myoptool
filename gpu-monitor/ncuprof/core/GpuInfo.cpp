#include "core/GpuInfo.h"

#include <cuda.h>

#include <cstring>

namespace ncuprof {

#define CU_CHECK(expr, ctx)                                                    \
    do {                                                                       \
        CUresult _r = (expr);                                                  \
        if (_r != CUDA_SUCCESS) {                                              \
            const char* _s = nullptr;                                          \
            cuGetErrorString(_r, &_s);                                         \
            if (err) *err = std::string(ctx) + ": " + (_s ? _s : "unknown");   \
            return false;                                                      \
        }                                                                      \
    } while (0)

bool collectGpuInfo(SystemInfo& out, std::string* err) {
    CU_CHECK(cuInit(0), "cuInit");

    int driverVer = 0, devCount = 0;
    CU_CHECK(cuDriverGetVersion(&driverVer), "cuDriverGetVersion");
    out.driverVersion = std::to_string(driverVer / 1000) + "." +
                        std::to_string(driverVer % 1000 / 10);
    CU_CHECK(cuDeviceGetCount(&devCount), "cuDeviceGetCount");

    for (int i = 0; i < devCount; ++i) {
        CUdevice d;
        if (cuDeviceGet(&d, i) != CUDA_SUCCESS) continue;
        GpuDevice g;
        g.deviceIndex = i;

        char buf[256] = {0};
        cuDeviceGetName(buf, sizeof buf - 1, d);
        g.name = buf;

        cuDeviceGetAttribute(&g.ccMajor, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR, d);
        cuDeviceGetAttribute(&g.ccMinor, CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR, d);
        cuDeviceGetAttribute(&g.numSMs, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT, d);
        cuDeviceGetAttribute(&g.numCoresPerSM, CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_MULTIPROCESSOR, d);
        cuDeviceGetAttribute(&g.regsPerSM, CU_DEVICE_ATTRIBUTE_MAX_REGISTERS_PER_MULTIPROCESSOR, d);
        cuDeviceGetAttribute(&g.sharedMemPerBlockOptin,
                             CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_BLOCK_OPTIN, d);
        cuDeviceGetAttribute(&g.sharedMemPerSM,
                             CU_DEVICE_ATTRIBUTE_MAX_SHARED_MEMORY_PER_MULTIPROCESSOR, d);
        cuDeviceGetAttribute(&g.maxThreadsPerSM,
                             CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_MULTIPROCESSOR, d);
        cuDeviceGetAttribute(&g.maxThreadsPerBlock, CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_BLOCK, d);
        cuDeviceGetAttribute(&g.clockRateKHz, CU_DEVICE_ATTRIBUTE_CLOCK_RATE, d);
        cuDeviceGetAttribute(&g.memClockRateKHz, CU_DEVICE_ATTRIBUTE_MEMORY_CLOCK_RATE, d);
        cuDeviceGetAttribute(&g.memBusWidth, CU_DEVICE_ATTRIBUTE_GLOBAL_MEMORY_BUS_WIDTH, d);
        cuDeviceGetAttribute(&g.l2CacheBytes, CU_DEVICE_ATTRIBUTE_L2_CACHE_SIZE, d);
        cuDeviceGetAttribute(&g.asyncEngineCount, CU_DEVICE_ATTRIBUTE_ASYNC_ENGINE_COUNT, d);
        cuDeviceGetAttribute(&g.pciDomain, CU_DEVICE_ATTRIBUTE_PCI_DOMAIN_ID, d);
        cuDeviceGetAttribute(&g.pciBus, CU_DEVICE_ATTRIBUTE_PCI_BUS_ID, d);
        cuDeviceGetAttribute(&g.pciDevice, CU_DEVICE_ATTRIBUTE_PCI_DEVICE_ID, d);

        int total = 0;
        cuDeviceGetAttribute(&total, CU_DEVICE_ATTRIBUTE_TOTAL_CONSTANT_MEMORY, d);
        g.totalMemBytes = 0;
        size_t mem = 0;
        if (cuDeviceTotalMem(&mem, d) == CUDA_SUCCESS) g.totalMemBytes = (long long)mem;

        // UUID
        CUuuid uuid;
        if (cuDeviceGetUuid(&uuid, d) == CUDA_SUCCESS) {
            static const char* hex = "0123456789abcdef";
            for (int b = 0; b < 16; ++b) {
                g.uuid.push_back(hex[(unsigned char)uuid.bytes[b] >> 4]);
                g.uuid.push_back(hex[(unsigned char)uuid.bytes[b] & 0xf]);
            }
        }

        // CUPTI Profiler 的芯片名（ggxxx / ghxxx…）：从 PCI 设备号推导，
        // 精确名由 CuptiMetricCollector 用 cuptiProfilerGetSupportedChips 校正。
        g.chipName = "unknown";
        out.gpus.push_back(g);
    }
    return !out.gpus.empty();
}

bool deviceAttributeValue(const GpuDevice& dev, const std::string& attr,
                          double& v, std::string& unit) {
    auto set = [&](double x, const char* u) { v = x; unit = u; return true; };
    if (attr == "device__attribute_compute_capability_major") return set(dev.ccMajor, "");
    if (attr == "device__attribute_compute_capability_minor") return set(dev.ccMinor, "");
    if (attr == "device__attribute_multiprocessor_count") return set(dev.numSMs, "SM");
    if (attr == "device__attribute_max_registers_per_multiprocessor")
        return set(dev.regsPerSM, "register");
    if (attr == "device__attribute_max_shared_memory_per_block")
        return set(dev.sharedMemPerBlockOptin, "byte");
    if (attr == "device__attribute_max_shared_memory_per_multiprocessor")
        return set(dev.sharedMemPerSM, "byte");
    if (attr == "device__attribute_max_threads_per_block")
        return set(dev.maxThreadsPerBlock, "thread");
    if (attr == "device__attribute_warp_size") return set(dev.warpSize, "thread/warp");
    if (attr == "device__attribute_l2_cache_size") return set(dev.l2CacheBytes, "byte");
    if (attr == "device__attribute_total_memory") return set((double)dev.totalMemBytes, "byte");
    if (attr == "device__attribute_memory_bus_width") return set(dev.memBusWidth, "byte");
    if (attr == "device__attribute_clock_rate") return set(dev.clockRateKHz, "Kcycle/second");
    if (attr == "device__attribute_memory_clock_rate") return set(dev.memClockRateKHz, "Kcycle/second");
    if (attr == "device__attribute_name") { v = 0; unit = dev.name; return true; }
    return false;
}

}  // namespace ncuprof
