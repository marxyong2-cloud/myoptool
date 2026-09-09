#include "host/ConsolePrinter.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <map>

#include "core/SectionFile.h"

namespace ncuprof {

std::string ConsolePrinter::scaleValue(double v, const std::string& unit,
                                       bool autoScale) {
    char buf[64];
    if (!autoScale || !std::isfinite(v) || v == 0) {
        snprintf(buf, sizeof buf, "%.6g", v);
        return buf;
    }
    double a = std::fabs(v);
    if (unit == "byte" || unit == "byte/second" || unit == "Kbyte") {
        static const char* pfx[] = {"", "K", "M", "G", "T", "P"};
        int p = 0;
        double s = v;
        while (a >= 1000 && p < 5) { a /= 1000; s /= 1000; ++p; }
        snprintf(buf, sizeof buf, "%.2f", s);
        return std::string(buf) + pfx[p];
    }
    if (unit == "second") {
        static const char* pfx[] = {"n", "u", "m", ""};
        int p = 0;
        double s = v * 1e9;   // 入口默认 ns（与 CUPTI 一致）
        a = std::fabs(s);
        while (a >= 1000 && p < 3) { a /= 1000; s /= 1000; ++p; }
        snprintf(buf, sizeof buf, "%.2f", s);
        return std::string(buf) + pfx[p];
    }
    if (unit == "cycle/second" || unit == "Kcycle/second") {
        static const char* pfx[] = {"", "K", "M", "G"};
        int p = 0;
        double s = unit == "Kcycle/second" ? v * 1000 : v;
        a = std::fabs(s);
        while (a >= 1000 && p < 3) { a /= 1000; s /= 1000; ++p; }
        snprintf(buf, sizeof buf, "%.2f", s);
        return std::string(buf) + pfx[p];
    }
    snprintf(buf, sizeof buf, "%.6g", v);
    return buf;
}

const char* ConsolePrinter::msgTypeTag(const std::string& t) {
    if (t == "OPTIMIZATION") return "[!]";
    if (t == "WARNING") return "[W]";
    if (t == "ERROR") return "[E]";
    return "[I]";   // OK
}

void ConsolePrinter::printKernel(const KernelRecord& k) const {
    if (opt_.page == Page::Raw) {
        printRawKernel(k);
        return;
    }
    // ---- details 页 ----
    printf("%s\n", k.name.c_str());
    printf("  Grid: (%d,%d,%d)  Block: (%d,%d,%d)  Passes: %d\n",
           k.gridDim[0], k.gridDim[1], k.gridDim[2],
           k.blockDim[0], k.blockDim[1], k.blockDim[2], k.passes);

    static std::vector<ProfilerSection> sections = [] {
        std::vector<ProfilerSection> all;
        all = loadSectionDir("sections");
        auto more = loadSectionDir("/usr/local/share/ncuprof/sections");
        for (auto& s : more) all.push_back(std::move(s));
        return all;
    }();

    for (const auto& s : sections) {
        bool want = opt_.sections.empty() || opt_.metrics.empty()
                        ? std::find(s.sets.begin(), s.sets.end(), opt_.sectionSet) != s.sets.end()
                        : std::find(opt_.sections.begin(), opt_.sections.end(),
                                    s.identifier) != opt_.sections.end();
        if (!want) continue;
        printSection(k, s);
    }
    printRuleMessages(k);
    printf("\n");
}

void ConsolePrinter::printSection(const KernelRecord& k,
                                  const ProfilerSection& s) const {
    printf("  %s\n", s.displayName.c_str());
    auto printMetric = [&](const SectionMetric& m) {
        auto it = k.metrics.find(m.name);
        if (it == k.metrics.end() || !it->second.valid) {
            if (opt_.csv) return;
            printf("    %-40s %10s %12s\n",
                   (m.label.empty() ? m.name : m.label).c_str(),
                   m.unit.c_str(), "n/a");
            return;
        }
        const MetricResult& r = it->second;
        std::string unit = r.unit.empty() ? m.unit : r.unit;
        if (opt_.csv) {
            printf("%s,%s,%s,%.10g\n", k.name.c_str(), m.name.c_str(),
                   unit.c_str(), r.value);
        } else {
            printf("    %-40s %10s %12s\n",
                   (m.label.empty() ? m.name : m.label).c_str(),
                   unit.c_str(),
                   scaleValue(r.value, unit, opt_.units == "auto").c_str());
        }
    };
    for (const auto& m : s.headerMetrics) printMetric(m);
    if (opt_.detailsAll)
        for (const auto& m : s.metrics) printMetric(m);
}

void ConsolePrinter::printRawKernel(const KernelRecord& k) const {
    if (opt_.csv) {
        printf("Kernel,Name,Unit,Value\n");
        for (const auto& [name, r] : k.metrics) {
            if (!r.valid) continue;
            printf("%s,%s,%s,%.10g\n", k.name.c_str(), name.c_str(),
                   r.unit.c_str(), r.value);
        }
        return;
    }
    printf("%s\n", k.name.c_str());
    for (const auto& [name, r] : k.metrics) {
        if (!r.valid) continue;
        printf("    %-60s %10s %16s\n", name.c_str(), r.unit.c_str(),
               scaleValue(r.value, r.unit, opt_.units == "auto").c_str());
    }
    printf("\n");
}

void ConsolePrinter::printRuleMessages(const KernelRecord& k) const {
    for (const auto& rm : k.ruleMessages) {
        printf("    %s %s: %s\n", msgTypeTag(rm.type), rm.name.c_str(),
               rm.text.c_str());
        for (const auto& [mn, mv] : rm.focusMetrics)
            printf("        focus: %s = %.6g\n", mn.c_str(), mv);
    }
}

void ConsolePrinter::printSummary(const ReportModel& m) const {
    // --print-summary per-kernel：每 kernel 一行（name, 次数, 平均时长/SOL…）
    struct Agg {
        int count = 0;
        double durSum = 0, smSum = 0, memSum = 0;
    };
    std::map<std::string, Agg> agg;
    for (const auto& k : m.kernels) {
        auto& a = agg[k.name];
        ++a.count;
        auto it = k.metrics.find("gpu__time_duration.sum");
        if (it != k.metrics.end() && it->second.valid) a.durSum += it->second.value;
        it = k.metrics.find("sm__throughput.avg.pct_of_peak_sustained_elapsed");
        if (it != k.metrics.end() && it->second.valid) a.smSum += it->second.value;
        it = k.metrics.find("gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed");
        if (it != k.metrics.end() && it->second.valid) a.memSum += it->second.value;
    }
    if (opt_.csv)
        printf("Kernel,Count,AvgDurationNs,AvgSmThroughputPct,AvgMemThroughputPct\n");
    for (const auto& [name, a] : agg) {
        if (opt_.csv)
            printf("%s,%d,%.6g,%.4f,%.4f\n", name.c_str(), a.count,
                   a.durSum / a.count, a.smSum / a.count, a.memSum / a.count);
        else
            printf("%-50s n:%-4d dur:%-12.6g SM%%:%-8.2f Mem%%:%-8.2f\n",
                   name.c_str(), a.count, a.durSum / a.count,
                   a.smSum / a.count, a.memSum / a.count);
    }
}

}  // namespace ncuprof
