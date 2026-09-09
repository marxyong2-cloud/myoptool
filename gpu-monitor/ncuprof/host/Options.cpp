#include "host/Options.h"

#include <algorithm>
#include <cstring>

#include "core/MetricDatabase.h"
#include "core/SectionFile.h"

namespace ncuprof {

void printUsage() { printUsageImpl(); }

namespace {

void printUsageImpl() {
    puts(
        "Usage:\n"
        "  ncuprof [options] [application] [application-arguments]\n"
        "\n"
        "Modes:\n"
        "  (default) launch-and-attach   Launch application and profile it\n"
        "  --mode launch                 Launch suspended, wait for attach\n"
        "  --mode attach                 Attach to a launched target\n"
        "  --import <file>               Import an existing report\n"
        "\n"
        "Profile options:\n"
        "  -k, --kernel-name <name>      Filter kernels by name (prefix 'regex:')\n"
        "  -c, --launch-count <N>        Stop after N kernel launches\n"
        "  -s, --launch-skip <N>         Skip the first N kernel launches\n"
        "      --kernel-id <id>          Filter by context:stream:name:invocation\n"
        "      --section <id>            Collect specified section only\n"
        "      --section-dir <path>      Extra section file search directory\n"
        "      --set <set>               Section set (basic|detailed|full)\n"
        "      --metrics <m1,m2,...>     Collect individual metrics\n"
        "      --replay-mode <mode>      kernel|application\n"
        "      --replay-count <N>        Max kernel replay passes per kernel\n"
        "      --cache-control <mode>    all|none\n"
        "      --clock-control <mode>    base|none\n"
        "      --nvtx                    Enable NVTX filtering\n"
        "      --nvtx-include <expr>     Profile kernels inside NVTX ranges\n"
        "      --nvtx-exclude <expr>     Exclude kernels inside NVTX ranges\n"
        "\n"
        "Output options:\n"
        "  -o, --output <file>           Write report to file\n"
        "  -f, --force                   Overwrite existing file\n"
        "      --page <page>             details|raw|source\n"
        "      --csv                     Comma separated output\n"
        "      --print-summary per-kernel\n"
        "      --details-all             Show all section metrics\n"
        "      --units auto|base\n"
        "\n"
        "List/query options:\n"
        "      --list-sets / --list-sections / --list-rules\n"
        "      --query-metrics [--chip <chip>] [--query-metrics-mode base|suffix]\n"
        "\n"
        "Other options:\n"
        "      --target-processes all|application-only\n"
        "      --env KEY=VAL             Set environment for target\n"
        "  -h, --help                    Print this help");
}

struct Arg {
    int argc; char** argv; int& i; std::string& err;
    std::string v;   // 当前选项的值

    // 匹配 "--name value" / "--name=value" / 短选项 "-x value"
    bool take(const char* longName, char shortName = 0) {
        std::string a = argv[i];
        std::string ln = longName;
        if (a == ln) return nextValue();
        if (a.compare(0, ln.size() + 1, ln + "=") == 0) {
            v = a.substr(ln.size() + 1);
            return true;
        }
        if (shortName && a.size() >= 2 && a[0] == '-' && a[1] == shortName &&
            a.find('=') != std::string::npos && a[2] == '=') {
            v = a.substr(3);
            return true;
        }
        if (shortName && a.size() == 2 && a[0] == '-' && a[1] == shortName)
            return nextValue();
        return false;
    }
    bool nextValue() {
        if (i + 1 >= argc) {
            err = std::string("missing value for ") + argv[i];
            return false;
        }
        v = argv[++i];
        return true;
    }
    void splitComma(std::vector<std::string>& out) {
        size_t start = 0, comma;
        while ((comma = v.find(',', start)) != std::string::npos) {
            if (comma > start) out.push_back(v.substr(start, comma - start));
            start = comma + 1;
        }
        if (start < v.size()) out.push_back(v.substr(start));
    }
};

std::vector<std::string> defaultSectionDirs() {
    // 搜索顺序（逆向自 ncu 文档）：当前目录 ./sections、安装前缀 share/
    std::vector<std::string> dirs{"sections",
                                  "/usr/local/share/ncuprof/sections"};
    if (const char* home = getenv("HOME"))
        dirs.push_back(std::string(home) + "/.config/ncuprof/sections");
    return dirs;
}

}  // namespace

// 展开验证：目标应用必须存在（list/query/import 模式除外）
static bool finalizeOptions(Options& o, std::string& err) {
    if (o.mode == Mode::List || o.mode == Mode::Query || o.mode == Mode::Import)
        return true;

    if (o.appPath.empty()) {
        err = "no application specified (use: ncuprof [options] <app> [args])";
        return false;
    }
    return true;
}

std::vector<std::string> Options::expandedMetrics() const {
    std::vector<std::string> out;
    auto addUnique = [&](const std::string& m) {
        for (auto& x : out) if (x == m) return;
        out.push_back(m);
    };

    if (!metrics.empty()) {
        for (const auto& m : metrics) addUnique(m);
    } else {
        // sections / set -> 指标
        auto& db = MetricDatabase::get();
        auto dirs = sectionDirs.empty() ? defaultSectionDirs() : sectionDirs;
        std::vector<ProfilerSection> all;
        for (const auto& d : dirs) {
            for (auto& s : loadSectionDir(d)) all.push_back(std::move(s));
        }
        for (const auto& s : all) {
            bool want = !sections.empty()
                            ? std::find(sections.begin(), sections.end(),
                                        s.identifier) != sections.end()
                            : std::find(s.sets.begin(), s.sets.end(),
                                        sectionSet) != s.sets.end();
            if (!want) continue;
            for (const auto& m : s.headerMetrics) addUnique(m.name);
            for (const auto& m : s.metrics) addUnique(m.name);
        }
    }

    // 追加派生指标依赖（launch__* 等）
    auto& db = MetricDatabase::get();
    std::vector<std::string> withDeps;
    for (const auto& m : out) {
        withDeps.push_back(m);
        for (const auto& d : db.dependenciesOf(m)) {
            if (std::find(withDeps.begin(), withDeps.end(), d) == withDeps.end())
                withDeps.push_back(d);
        }
    }
    return withDeps;
}

bool parseOptions(int argc, char** argv, Options& o, std::string& err) {
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--") {
            for (int j = i + 1; j < argc; ++j) o.appArgs.push_back(argv[j]);
            if (!o.appArgs.empty()) {
                o.appPath = o.appArgs.front();
                o.appArgs.erase(o.appArgs.begin());
            }
            return finalizeOptions(o, err);
        }

        Arg arg{argc, argv, i, err};
        if (a == "-h" || a == "--help") { printUsage(); o.mode = Mode::List; return true; }
        if (arg.take("--kernel-name", 'k')) { o.kernelName = arg.v; continue; }
        if (arg.take("--launch-count", 'c')) { o.launchCount = atoi(arg.v.c_str()); continue; }
        if (arg.take("--launch-skip", 's')) { o.launchSkip = atoi(arg.v.c_str()); continue; }
        if (arg.take("--kernel-id")) { o.kernelId = arg.v; continue; }
        if (arg.take("--kernel-name-base")) { o.kernelNameBase = arg.v; continue; }
        if (arg.take("--section")) { o.sections.push_back(arg.v); continue; }
        if (arg.take("--section-dir")) { o.sectionDirs.push_back(arg.v); continue; }
        if (arg.take("--set")) { o.sectionSet = arg.v; o.sections.clear(); continue; }
        if (arg.take("--metrics")) { arg.splitComma(o.metrics); continue; }
        if (arg.take("--replay-mode")) {
            o.replayMode = arg.v == "application" ? ReplayMode::Application
                                                  : ReplayMode::Kernel;
            continue;
        }
        if (arg.take("--replay-count")) { o.replayCount = atoi(arg.v.c_str()); continue; }
        if (arg.take("--cache-control")) { o.cacheControl = arg.v; continue; }
        if (arg.take("--clock-control")) { o.clockControl = arg.v; continue; }
        if (a == "--nvtx") { o.nvtxEnabled = true; continue; }
        if (arg.take("--nvtx-include")) { o.nvtxEnabled = true; o.nvtxInclude.push_back(arg.v); continue; }
        if (arg.take("--nvtx-exclude")) { o.nvtxEnabled = true; o.nvtxExclude.push_back(arg.v); continue; }
        if (arg.take("--output", 'o')) { o.outputFile = arg.v; continue; }
        if (arg.take("--import")) { o.mode = Mode::Import; o.importFile = arg.v; continue; }
        if (arg.take("--export")) { o.exportFile = arg.v; continue; }
        if (a == "-f" || a == "--force") { o.forceOverwrite = true; continue; }
        if (arg.take("--page")) {
            if (arg.v == "raw") o.page = Page::Raw;
            else if (arg.v == "source") o.page = Page::Source;
            else o.page = Page::Details;
            continue;
        }
        if (a == "--csv") { o.csv = true; continue; }
        if (arg.take("--print-summary")) { o.printSummary = arg.v == "per-kernel"; continue; }
        if (a == "--details-all") { o.detailsAll = true; continue; }
        if (arg.take("--units")) { o.units = arg.v; continue; }
        if (a == "--list-sets") { o.listSets = true; o.mode = Mode::List; continue; }
        if (a == "--list-sections") { o.listSections = true; o.mode = Mode::List; continue; }
        if (a == "--list-rules") { o.listRules = true; o.mode = Mode::List; continue; }
        if (a == "--query-metrics") { o.queryMetrics = true; o.mode = Mode::Query; continue; }
        if (arg.take("--chip")) { o.queryChip = arg.v; continue; }
        if (arg.take("--query-metrics-mode")) { o.queryMetricsMode = arg.v; continue; }
        if (arg.take("--target-processes")) { o.targetProcesses = arg.v; continue; }
        if (arg.take("--env")) { o.targetEnv.push_back(arg.v); continue; }
        if (arg.take("--mode")) {
            if (arg.v == "launch") o.mode = Mode::Launch;
            else if (arg.v == "attach") o.mode = Mode::Attach;
            else o.mode = Mode::LaunchAndAttach;
            continue;
        }
        err = "unknown option: " + a;
        return false;
    }
    return finalizeOptions(o, err);
}

}  // namespace ncuprof
