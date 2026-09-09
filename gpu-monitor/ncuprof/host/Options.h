// Options.h — 命令行选项模型（对齐 ncu CLI 的核心选项子集）
// 选项语义逆向自包内 docs/NsightComputeCli 文档。
#pragma once
#include <string>
#include <vector>

namespace ncuprof {

enum class Mode { LaunchAndAttach, Launch, Attach, Import, List, Query };
enum class Page { Details, Raw, Source };
enum class ReplayMode { Kernel, Application };

struct Options {
    Mode mode = Mode::LaunchAndAttach;
    Page page = Page::Details;
    bool csv = false;
    bool printSummary = false;       // --print-summary per-kernel
    bool detailsAll = false;         // --details-all

    // 过滤
    std::string kernelName;          // -k <name|regex:...>
    std::string kernelNameBase = "function";   // function|demangled
    int launchCount = -1;            // -c
    int launchSkip = 0;              // -s
    std::string kernelId;            // --kernel-id ctx:stream:name:inv

    // 收集
    std::vector<std::string> sections;      // --section（标识符）
    std::string sectionSet = "basic";       // --set
    std::vector<std::string> metrics;       // --metrics
    std::vector<std::string> sectionDirs;   // --section-dir
    ReplayMode replayMode = ReplayMode::Kernel;
    int replayCount = 16;                   // --replay-count
    std::string cacheControl = "all";       // all|none
    std::string clockControl = "none";      // base|none
    bool nvtxEnabled = false;               // --nvtx
    std::vector<std::string> nvtxInclude, nvtxExclude;
    bool importSource = false;              // -lineinfo（源码页需要，未来）

    // 输出
    std::string outputFile;         // -o（空=仅控制台）
    std::string importFile;         // --import
    std::string exportFile;         // --export
    bool forceOverwrite = false;    // -f
    std::string units = "auto";     // auto|base

    // 查询/列表
    bool listSets = false, listSections = false, listRules = false;
    bool queryMetrics = false;
    std::string queryChip;          // --chip gh102
    std::string queryMetricsMode;   // base|suffix

    // 目标进程
    std::string targetProcesses = "application-only";   // all|application-only
    std::vector<std::string> targetEnv;   // --env KEY=VAL

    // 目标应用（'--' 之后）
    std::string appPath;
    std::vector<std::string> appArgs;

    // 派生：展开 sections/sets 后的完整指标请求（含 launch__* 静态指标）
    std::vector<std::string> expandedMetrics() const;
};

// 解析 argv（含 "--" 分隔目标应用）；失败返回 false 并写 err
bool parseOptions(int argc, char** argv, Options& out, std::string& err);

// usage 文本（ncu 风格）
void printUsage();

}  // namespace ncuprof
