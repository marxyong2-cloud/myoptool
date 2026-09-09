# ncuprof 架构设计

开源 CUDA kernel 性能剖析器 —— 功能对标 NVIDIA Nsight Compute (ncu) 的核心链路。

## 1. 总体结构

```
┌──────────────────────────── 宿主机 (host) ────────────────────────────┐
│  ncuprof (CLI)                                                          │
│  ├── Options            命令行解析（对齐 ncu 选项）                       │
│  ├── TargetProcess      fork/exec 目标 + LD_PRELOAD 注入环境            │
│  ├── IpcChannel/Protocol  与目标进程的 Unix Socket 协议                 │
│  ├── SectionFile        解析 .section（官方 textproto 格式兼容）         │
│  ├── MetricDatabase     指标注册表 + 派生公式（occupancy/SOL…）          │
│  ├── RuleEngine         Python 规则宿主（NvRules 兼容 API）              │
│  ├── Report             SQLite 报告（--import/--export 可再处理）        │
│  └── ConsolePrinter     details/raw 页面 + CSV                          │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ IPC (长度前缀二进制消息)
┌───────────────────────────────▼─────────── 目标机 (target, 注入库) ─────┐
│  libncuprof_inj.so（LD_PRELOAD 加载）                                    │
│  ├── injection.cpp      符号拦截 cuInit/cuLaunchKernel/cuMemcpy…         │
│  ├── Session            过滤器(kernel 名/数量/NVTX) + 会话状态机          │
│  ├── ReplayEngine       kernel replay：显存 save/restore、L2 flush、锁频  │
│  └── CuptiMetricCollector CUPTI Profiler Target API：pass 循环收集计数    │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ CUPTI → 驱动 → GPU 硬件性能计数器 (PMC)
```

## 2. 关键流程：一次 kernel 剖析

```
host                          target(注入库)                    GPU
 │ fork+exec+LD_PRELOAD ──────▶│
 │◀──────── ATTACHED ─────────│ (agent 线程连接回 host)
 │  OPTIONS(sections,metrics,  │
 │          filters, replay…) │
 │                             │ cuLaunchKernel 拦截
 │                             │── 匹配过滤条件? ──否──▶ 直接放行
 │                             │ 是:
 │                             │ 1) 活动记录订阅(kernel 时长)
 │                             │ 2) 显存快照(仅多 pass 时)
 │  ◀── KERNEL_BEGIN(name,grid)
 │                             │ 3) 对每个 pass p:
 │                             │    cuptiProfilerSetConfig(pass p)
 │                             │    cuptiProfilerBeginPass()
 │                             │    重放 kernel ──────────────▶ 执行
 │                             │    cuptiProfilerEndPass()
 │                             │    恢复被写显存 / flush L2
 │                             │ 4) FlushCounterData → CounterDataImage
 │  ◀── KERNEL_RESULT(counters,activity)
 │  指标求值(MetricDatabase) → 5) 规则(SOLBottleneck…)
 │  报告/控制台输出
```

## 3. 设计决策

| 决策 | 理由（源自逆向发现） |
|---|---|
| 注入方式采用 LD_PRELOAD + `dlsym(RTLD_NEXT)` | 与 ncu 的 `TreeLauncherTargetLdPreloadHelper` 一致，无需 ptrace |
| 计数收集走 CUPTI Profiler Target API（`cupti_profiler_target.h` 参数结构体风格） | ncu 的 `libnvperf_target.so` 即该路径的私有版本；CUPTI 自 CUDA 11 起公开 |
| 指标求值：优先 CUPTI Profiler Host API（按官方指标名），内置公式覆盖派生指标 | `libnvperf_host.so` 的私有指标数据库不可再分发；CUPTI 公开版覆盖同名指标 |
| kernel replay：首 pass 前快照全部可达显存，pass 间恢复被写区域 | 文档 2.2.3 节明确描述该算法；被写区域由 pass 1 的 `CUpti_ActivityKernel3` 内存依赖信息 + 后续对比得到（本实现保守恢复全部快照） |
| 报告用 SQLite | ncu-rep 为 protobuf（私有 schema 膨胀）；Nsight Systems 已验证 SQLite 报告可行且可查询 |
| section 文件直接兼容官方 `.section` textproto | 格式公开（`ProfilerSection.proto`），生态已有大量现成 section 文件 |
| 规则用 Python（NvRules 兼容层） | ncu 的规则同为 Python；模板见 `extras/RuleTemplates` |

## 4. 目录

```
ncuprof/
├── CMakeLists.txt
├── core/      双侧共享：SectionFile、MetricName、MetricDatabase、OccupancyCalc、GpuInfo、Protocol
├── host/      CLI 前端
├── target/    注入库
├── python/    NvRules 兼容层 + 内置规则 + 报告 Python API
├── sections/  内置 section 文件（官方格式）
├── samples/   被剖析示例程序
├── tests/     解析器单测
└── docs/      本文档 + 逆向报告
```

## 5. 依赖

- CUDA Toolkit（cuda driver API 头 + CUPTI 头；运行时由目标应用自带）
- C++17、CMake ≥ 3.16
- Python 3（规则引擎，可选编译 `ncuprof_EMBED_PYTHON`）
- SQLite3（报告）
