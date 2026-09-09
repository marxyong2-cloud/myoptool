// ConsolePrinter.h — 控制台渲染（--page details/raw、--csv、--print-summary）
// 输出样式对齐 ncu：
//   "  kernelName"
//   "    Section Name"
//   "      Metric Label           unit   value"
//   以及规则消息块 "    [X] RuleName: text"
#pragma once
#include <string>
#include <vector>

#include "host/Options.h"
#include "host/Report.h"

namespace ncuprof {

class ConsolePrinter {
public:
    explicit ConsolePrinter(const Options& opt) : opt_(opt) {}

    void printKernel(const KernelRecord& k) const;
    void printSummary(const ReportModel& m) const;   // --print-summary

    // ncu 数量级自动缩放：字节/秒 -> K/M/G；时间 -> us/ms
    static std::string scaleValue(double v, const std::string& unit,
                                  bool autoScale);

private:
    const Options& opt_;
    void printSection(const KernelRecord& k, const ProfilerSection& s) const;
    void printRawKernel(const KernelRecord& k) const;
    void printRuleMessages(const KernelRecord& k) const;
    static const char* msgTypeTag(const std::string& t);
};

}  // namespace ncuprof
