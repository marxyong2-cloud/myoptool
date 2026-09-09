// main.cpp — ncuprof CLI 入口
// 模式分派：launch-and-attach（默认）/ launch / attach / import / list / query
//
// 主流程（launch-and-attach）：
//   1. 解析选项 → 展开指标
//   2. 创建 Unix socket 监听
//   3. fork/exec 目标（LD_PRELOAD=libncuprof_inj.so）
//   4. accept 目标连接 → 下发 Options
//   5. 循环收 KernelResult → 指标补全 → 规则 → 报告/控制台
//   6. 目标退出 → 收尾（--print-summary、报告关闭）
#include <unistd.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "core/GpuInfo.h"
#include "core/MetricDatabase.h"
#include "core/MiniJson.h"
#include "core/Protocol.h"
#include "core/SectionFile.h"
#include "host/ConsolePrinter.h"
#include "host/IpcChannel.h"
#include "host/MetricEvaluationService.h"
#include "host/Options.h"
#include "host/Report.h"
#include "host/RuleEngine.h"
#include "host/TargetProcess.h"

using namespace ncuprof;

namespace {

std::string selfDir() {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n <= 0) return ".";
    buf[n] = 0;
    std::string p = buf;
    auto s = p.find_last_of('/');
    return s == std::string::npos ? "." : p.substr(0, s);
}

int doList(const Options& opt) {
    auto dirs = std::vector<std::string>{"sections",
                                         "/usr/local/share/ncuprof/sections"};
    if (!opt.sectionDirs.empty()) dirs = opt.sectionDirs;

    std::vector<ProfilerSection> all;
    for (const auto& d : dirs)
        for (auto& s : loadSectionDir(d)) all.push_back(std::move(s));

    if (opt.listSections || (!opt.listSets && !opt.listRules)) {
        printf("Available sections:\n");
        for (const auto& s : all) printf("  %-40s %s\n", s.identifier.c_str(),
                                         s.displayName.c_str());
    }
    if (opt.listSets) {
        printf("Available sets:\n");
        std::vector<std::string> sets;
        for (const auto& s : all)
            for (const auto& st : s.sets)
                if (std::find(sets.begin(), sets.end(), st) == sets.end())
                    sets.push_back(st);
        std::sort(sets.begin(), sets.end());
        for (const auto& st : sets) printf("  %s\n", st.c_str());
    }
    if (opt.listRules) {
        printf("Available rules:\n");
        RuleEngine re(selfDir() + "/python/rules");
        for (const auto& r : re.ruleIdentifiers()) printf("  %s\n", r.c_str());
    }
    return 0;
}

int doQueryMetrics(const Options& opt) {
    SystemInfo sys;
    std::string err;
    GpuDevice dev;
    if (collectGpuInfo(sys, &err) && !sys.gpus.empty()) dev = sys.gpus[0];

    auto infos = MetricDatabase::get().listMetrics(&dev);
    if (opt.csv) printf("Metric Name,Unit,Type\n");
    for (const auto& mi : infos) {
        // --query-metrics-mode base：去重只显示基名
        static std::string lastBase;
        if (opt.queryMetricsMode == "base") {
            auto base = metricBaseName(mi.name);
            if (base == lastBase) continue;
            lastBase = base;
            if (opt.csv) printf("%s,%s,%s\n", base.c_str(), "", "");
            else printf("  %-60s %s\n", base.c_str(),
                        mi.derived ? "(derived)" : "");
            continue;
        }
        if (opt.csv) printf("%s,%s,%s\n", mi.name.c_str(), mi.unit.c_str(),
                            mi.derived ? "derived" : "hw");
        else
            printf("  %-60s %-14s %s\n", mi.name.c_str(), mi.unit.c_str(),
                   mi.derived ? "(derived)" : "");
    }
    printf("\n注: 完整的芯片相关指标表由 CUPTI Host API 在运行时枚举；"
           "此处为内置注册表。\n");
    return 0;
}

int doImport(const Options& opt, ReportStore& store) {
    ReportModel model;
    std::string err;
    if (!store.readAll(model, &err)) {
        fprintf(stderr, "ncuprof: %s\n", err.c_str());
        return 1;
    }
    ConsolePrinter printer(opt);
    if (opt.printSummary) {
        printer.printSummary(model);
    } else {
        for (const auto& k : model.kernels) printer.printKernel(k);
    }
    if (!opt.exportFile.empty()) {
        ReportStore out;
        if (out.openForWrite(opt.exportFile, opt.forceOverwrite, &err)) {
            for (const auto& k : model.kernels) out.addKernel(k, &err);
            out.close();
            printf("==PROF== Report: %s\n", opt.exportFile.c_str());
        } else {
            fprintf(stderr, "ncuprof: %s\n", err.c_str());
            return 1;
        }
    }
    return 0;
}

// 把 IPC KernelResult JSON -> KernelRecord（含指标补全与规则）
KernelRecord kernelFromJson(const MiniJson& j,
                            const MetricEvaluationService& svc,
                            const GpuDevice& dev, RuleEngine& rules) {
    KernelRecord k;
    k.name = j.getString("kernel");
    const MiniJson* g = j.find("grid");
    if (g && g->type == MiniJson::Type::Array && g->arr.size() == 3)
        for (int d = 0; d < 3; ++d) k.gridDim[d] = (int)g->arr[d].num;
    const MiniJson* b = j.find("block");
    if (b && b->type == MiniJson::Type::Array && b->arr.size() == 3)
        for (int d = 0; d < 3; ++d) k.blockDim[d] = (int)b->arr[d].num;

    if (const MiniJson* ms = j.find("metrics");
        ms && ms->type == MiniJson::Type::Object) {
        for (const auto& [name, val] : ms->obj) {
            MetricResult r;
            r.name = name;
            r.value = val.num;
            r.valid = val.type == MiniJson::Type::Number;
            k.metrics[name] = r;
        }
    }
    svc.completeMetrics(dev, k.metrics);
    rules.applyAll(k, nullptr);
    return k;
}

int doProfile(const Options& opt) {
    // ---- 展开指标请求 ----
    auto metrics = opt.expandedMetrics();
    printf("==PROF== Collecting %zu metrics (%s set)\n", metrics.size(),
           opt.sectionSet.c_str());

    // ---- 设备信息（host 侧枚举；target 侧 Attached 会回传校准）----
    SystemInfo sys;
    std::string err;
    if (!collectGpuInfo(sys, &err) || sys.gpus.empty()) {
        fprintf(stderr, "ncuprof: no CUDA device (%s)\n", err.c_str());
        return 1;
    }
    MetricEvaluationService svc(sys);
    RuleEngine rules(selfDir() + "/python/rules");

    // ---- IPC + 目标进程 ----
    std::string sockPath = "/tmp/ncuprof-" + std::to_string(getpid()) + ".sock";
    IpcServer ipc;
    if (!ipc.listen(sockPath, &err)) {
        fprintf(stderr, "ncuprof: %s\n", err.c_str());
        return 1;
    }

    LaunchSpec spec;
    spec.appPath = opt.appPath;
    spec.args = opt.appArgs;
    spec.ipcPath = sockPath;
    spec.injectLibPath = selfDir() + "/libncuprof_inj.so";
    spec.extraEnv = opt.targetEnv;
    spec.trackChildren = opt.targetProcesses == "all";

    TargetProcess target;
    if (!target.start(spec, &err)) {
        fprintf(stderr, "ncuprof: %s\n", err.c_str());
        ipc.closeAll();
        return 1;
    }
    printf("==PROF== Connected to process %d\n", (int)target.pid());

    if (!ipc.acceptTarget(30000, &err)) {
        fprintf(stderr, "==PROF== %s\n", err.c_str());
        target.killTree();
        ipc.closeAll();
        return 1;
    }

    // ---- 下发选项 ----
    MiniJson o = MiniJson::object();
    o.set("kernelName", opt.kernelName);
    o.set("launchCount", (double)opt.launchCount);
    o.set("launchSkip", (double)opt.launchSkip);
    o.set("replayCount", (double)opt.replayCount);
    o.set("cacheControl", opt.cacheControl != "none");
    MiniJson marr = MiniJson::array();
    for (const auto& m : metrics) marr.push(MiniJson::string_(m));
    o.set("metrics", marr);
    if (!ipc.send(ipc::MsgType::Options, o.dump())) {
        fprintf(stderr, "==PROF== failed to send options\n");
        target.killTree();
        ipc.closeAll();
        return 1;
    }

    // ---- 报告输出 ----
    ReportStore store;
    if (!opt.outputFile.empty() &&
        !store.openForWrite(opt.outputFile, opt.forceOverwrite, &err)) {
        fprintf(stderr, "ncuprof: %s\n", err.c_str());
        target.killTree();
        ipc.closeAll();
        return 1;
    }

    // ---- 消息泵 ----
    ConsolePrinter printer(opt);
    ReportModel model;
    model.sessionCmdline = opt.appPath;
    int profiled = 0;
    while (true) {
        ipc::MsgType t;
        std::string payload;
        if (!ipc.recv(t, payload)) break;   // 目标退出/连接断开
        MiniJson j = MiniJson::parse(payload);
        if (t == ipc::MsgType::KernelResult) {
            if (j.has("error")) {
                fprintf(stderr, "==PROF== error profiling \"%s\": %s\n",
                        j.getString("kernel").c_str(),
                        j.getString("error").c_str());
                continue;
            }
            auto k = kernelFromJson(j, svc, sys.gpus[0], rules);
            k.id = (long long)profiled + 1;
            printf("==PROF== Profiling \"%s\" - %d: 100%%\n", k.name.c_str(),
                   profiled);
            if (opt.page != Page::Source && opt.outputFile.empty())
                printer.printKernel(k);
            if (!opt.outputFile.empty()) store.addKernel(k, nullptr);
            model.addKernel(std::move(k));
            ++profiled;
        } else if (t == ipc::MsgType::LogLine) {
            printf("==PROF== [target] %s\n", j.getString("text").c_str());
        }
    }

    int status = target.wait();
    ipc.closeAll();

    printf("==PROF== Disconnected from process\n");
    if (!opt.outputFile.empty()) {
        store.close();
        printf("==PROF== Report: %s\n", opt.outputFile.c_str());
    }
    if (opt.printSummary) printer.printSummary(model);
    printf("==PROF== Profiled %d kernels (target exit status %d)\n", profiled,
           status);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    Options opt;
    std::string err;
    if (!parseOptions(argc, argv, opt, err)) {
        fprintf(stderr, "ncuprof: %s\n\n", err.c_str());
        printUsage();
        return 2;
    }

    ReportStore store;
    switch (opt.mode) {
        case Mode::List:        return doList(opt);
        case Mode::Query:       return doQueryMetrics(opt);
        case Mode::Import:
            if (!store.openForRead(opt.importFile, &err)) {
                fprintf(stderr, "ncuprof: %s\n", err.c_str());
                return 1;
            }
            return doImport(opt, store);
        case Mode::Attach:
            fprintf(stderr,
                    "ncuprof: attach 模式需要目标进程先以 --mode launch 启动"
                    "（当前版本暂未实现 --process-id 挂接，见 docs/Architecture.md）\n");
            return 3;
        default:
            return doProfile(opt);
    }
}
