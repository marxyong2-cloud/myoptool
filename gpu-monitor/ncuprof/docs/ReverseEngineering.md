# 逆向分析报告：nsight_compute-linux-x86_64-2026.2.1.6.run

> 分析日期：2026-09-02
> 样本：`nsight_compute-linux-x86_64-2026.2.1.6.run`（501,995,370 字节）
> 分析方式：静态解包 + 载荷结构分析 + 包内可读源码/配置/文档提取（未对闭源 ELF 做反编译）

## 1. 外层封装

| 属性 | 值 |
|---|---|
| 格式 | Makeself 2.2.0 自解压脚本 |
| shell 头 | 498 行（11,986 字节） |
| 载荷 | gzip 压缩 tar，501,983,384 字节，起始偏移 11,986 |
| 安装器 | `install-linux.pl`（Perl） |
| CRC/MD5 | 3410595719 / db9c61cc…（makeself 头内） |

`install-linux.pl` 仅做三件事：显示 EULA → 复制 `pkg/*` 到目标目录（默认
`/usr/local/NVIDIA-Nsight-Compute-2026.2`）→ 可选创建符号链接与 `.desktop`。
无编译、无系统服务、无内核模块。

## 2. 包内布局（载荷解压后 `pkg/`）

```
pkg/
├── ncu                        # CLI 启动脚本（选择 target/linux-* 目录）
├── ncu-ui                     # GUI 启动脚本
├── install-linux.pl 在外层
├── target/                    # ===== 目标机（被剖析进程）侧组件 =====
│   ├── linux-desktop-glibc_2_11_3-x64/
│   │   ├── ncu                        # CLI 主程序（ELF）
│   │   ├── libnvperf_target.so        # 目标侧性能计数收集（PerfWorks target）
│   │   ├── libcuda-injection.so       # CUDA API 注入库
│   │   ├── libInterceptorInjectionTarget.so
│   │   ├── libTreeLauncherTargetInjection.so
│   │   ├── libTreeLauncherTargetUpdatePreloadInjection.so
│   │   ├── libTreeLauncherPlaceholder.so
│   │   ├── TreeLauncherTargetLdPreloadHelper   # LD_PRELOAD 引导器
│   │   ├── TreeLauncherSubreaper               # 子进程收割（PR_SET_CHILD_SUBREAPER）
│   │   └── nv-system-info-collector   # 系统/GPU 信息采集器
│   ├── linux-desktop-t210-a64/        # 同上（ARM64 Jetson）
│   └── linux-desktop-glibc_2_11_3-x86/# 同上（32 位）
├── host/                      # ===== 宿主机（前端）侧组件 =====
│   ├── target-linux-x64/              # CLI 侧运行库
│   │   ├── launcher                   # 进程启动器
│   │   ├── libnvperf_host.so          # 指标求值引擎（PerfWorks host，指标数据库+公式）
│   │   ├── libcupti.so.10.2 … 13.3    # 捆绑全版本 CUPTI
│   │   ├── libToolsInjection*.so      # CUDA/NCCL/MPI/Python 等 API 注入库
│   │   └── nsys 相关（系统级剖析集成）
│   └── linux-desktop-glibc_2_11_3-x64/# Qt6 GUI（ncu-ui.bin）+ 插件
├── sections/                  # ===== 剖析 section 定义（可读，textproto）=====
│   ├── SpeedOfLight.section / Occupancy.section / LaunchStats.section …
│   └── *.py（规则逻辑：AchievedOccupancy、CPIStall、SpeedOfLight…）
├── extras/
│   ├── FileFormat/*.proto     # .ncu-rep 报告格式 protobuf 定义（可读）
│   ├── python/ncu_report.py   # SWIG 生成的报告 Python API
│   ├── python/ncu_occupancy.py# occupancy 计算器 Python API
│   ├── Api/nvComputeInj.h     # 注入库公开 API（nvInjBeginProfiling/End）
│   ├── RuleTemplates/         # 规则/section 开发模板
│   └── samples/               # CUDA 示例 + .ncu-rep 样例
├── docs/                      # 全套离线文档（ProfilingGuide/CLI/CustomizationGuide/NvRulesAPI）
└── EULA.txt
```

捆绑 Python 3.12 运行时、Qt6、boost 等第三方库占载荷大部分体积。

## 3. 运行机制（从包内文档与可读源码还原）

### 3.1 进程模型

```
用户 → ncu（前端, CLI/GUI）
         │ fork/exec + 环境注入（LD_PRELOAD=TreeLauncherTargetLdPreloadHelper…）
         ▼
       目标应用进程
         │ 注入库拦截 CUDA Driver API（cuInit/cuLaunchKernel…）
         │ 命中过滤条件的 kernel → 暂停应用 → 启动收集
         ▼
       CUPTI（Profiler/Activity/Callback API）→ GPU 硬件性能计数器
         │ 结果（protobuf 消息）回传前端
         ▼
       ncu：libnvperf_host.so 求值指标 → sections 渲染 → Python 规则（NvRules）
         → .ncu-rep（protobuf + 字符串表）/ 控制台 / CSV
```

`nvComputeInj.h` 证实了 attach 模式协议：应用自行 `dlopen` 注入库后调用
`nvInjBeginProfiling()`，随后用 `ncu --process-id <pid>` 挂接；
`nvInjEndProfiling()` 通知前端分离退出。

### 3.2 Replay（多 pass 收集）

硬件计数器每 pass 可采数目有限，指标集被拆成多个 pass，每个 pass 重放一次
kernel（Kernel Replay）或整个应用（Application Replay），或按区间重放
（Range Replay / Application Range Replay）。Kernel Replay 的关键是：
第一 pass 保存 kernel 可访问的全部 GPU 显存 → 识别被写子集 → 每个 pass 前
恢复被写区域，保证确定性。单 pass 可完成时不做保存/恢复。配套
Clock Control（锁频 base/none）与 Cache Control（pass 间 flush L2）。

### 3.3 指标体系（PerfWorks 命名）

```
unit__(subunit?)_(pipestage?)_quantity_(qualifiers?) . rollup . submetric
例：sm__throughput.avg.pct_of_peak_sustained_elapsed
```

- rollup：`.sum/.avg/.min/.max`（跨 HW unit 实例聚合）
- submetric：`.per_second/.per_cycle_active/.per_cycle_elapsed/
  .pct_of_peak_sustained_active/.pct_of_peak_sustained_elapsed` 等
- 特殊前缀：`regex:`、`group:`、`breakdown:<throughput>`（吞吐分解）
- launch 静态指标：`launch__*`（waves_per_multiprocessor 等）
- 设备属性：`device__attribute_*`

### 3.4 Section / Set / Rule 体系

- `.section` 文件为 protobuf text 格式（schema 见 `extras/FileFormat/ProfilerSection.proto`）：
  `Identifier/DisplayName/Description/Order/Sets{}/Header{Metrics{}}/Body{Items{…}}/MetricDefinitions{}`，
  指标支持 `Filter{Items{MinArch/MaxArch}}` 按 compute capability 过滤、
  `CollectionFilter{CollectionScopes}` 按收集时机（Launch/Runtime）过滤。
- 预置 set：`default/basic/detailed/full/roofline…`
- 规则为 Python 模块，实现 `get_identifier/get_name/get_description/
  get_section_identifier/apply(handle)`，通过 NvRules API 取指标、发消息
  （MsgType OK/OPTIMIZATION/WARNING）、挂 focus metric、向子规则传权重
  （`send_dict_to_children`）。

### 3.5 报告格式（.ncu-rep）

Protobuf 序列化（`ProfilerReport.proto`、`ProfilerSection.proto`、
`ProfilerStringTable.proto`、`RuleResults.proto`、`Nvtx.proto` 等），
字符串走字符串表；Python 侧由 SWIG 包装的 `_ncu_report.so` 暴露
`IContext→IRange→IAction→IMetric/ISourceInfo` 对象树。

## 4. 与本仓库实现的映射

| ncu 组件 | 本实现对应 |
|---|---|
| `ncu` CLI / 选项系统 | `host/main.cpp`、`host/Options.*` |
| `launcher` + `TreeLauncher*`（注入引导） | `host/TargetProcess.*`、`target/injection.cpp`（LD_PRELOAD） |
| `libcuda-injection.so`（API 拦截） | `target/injection.cpp`（dlsym 环绕 cuLaunchKernel 等） |
| `libnvperf_target.so`（目标侧收集） | `target/CuptiMetricCollector.*`（CUPTI Profiler Target API） |
| kernel replay（显存 save/restore） | `target/ReplayEngine.*` |
| `libnvperf_host.so`（指标求值） | `core/MetricDatabase.*` + CUPTI Profiler Host API |
| `sections/*.section` + 解析 | `sections/*.section`（同格式）+ `core/SectionFile.*` |
| NvRules（规则引擎） | `python/nvrules.py` + `host/RuleEngine.*` |
| `.ncu-rep`（protobuf 报告） | `host/Report.*`（SQLite，参照 Nsight Systems 报告方案） |
| `ncu_report.py` Python API | `python/report_api.py` |
| `ncu_occupancy.py` | `core/OccupancyCalc.*` |
| `nv-system-info-collector` | `core/GpuInfo.*` |

## 5. 许可提示

原包受 NVIDIA EULA 约束，闭源 ELF 未做反编译；本报告全部来自解包后的
明文资源（安装脚本、section 文本、proto、Python、HTML 文档）与公开文档。
本仓库源码为功能等价的独立实现（clean-room），不含 NVIDIA 代码。
