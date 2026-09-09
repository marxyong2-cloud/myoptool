// Report.h — 报告模型与存储
//
// ncu 的 .ncu-rep 是 protobuf 私有格式（ProfilerReport.proto + 字符串表）；
// 本实现采用 SQLite（Nsight Systems 验证过的方案）：
//   kernels(id, name, grid_x…, block_x…, device_id, session_id, passes)
//   metrics(kernel_id, name, unit, value)        -- 每 kernel 全量指标
//   rules(kernel_id, section, type, name, text)  -- 规则消息
//   devices(device_id, name, cc, numSMs, …)
//   sessions(id, cmdline, timestamp, tool_version)
// 同 schema 也用于 --import 回读。
#pragma once
#include <map>
#include <string>
#include <vector>

#include "core/MetricDatabase.h"
#include "core/SectionFile.h"

namespace ncuprof {

struct KernelRecord {
    long long id = 0;
    std::string name;
    int gridDim[3] = {1, 1, 1};
    int blockDim[3] = {1, 1, 1};
    int deviceId = 0;
    int passes = 0;
    MetricMap metrics;
    // 规则结果（section -> 消息列表）
    struct RuleMessage {
        std::string section, type, name, text;
        std::vector<std::pair<std::string, double>> focusMetrics;
    };
    std::vector<RuleMessage> ruleMessages;
};

struct ReportModel {
    std::string sessionCmdline;
    std::string toolVersion = "ncuprof 0.1";
    std::vector<KernelRecord> kernels;

    void addKernel(KernelRecord&& k) { kernels.push_back(std::move(k)); }
    size_t totalKernels() const { return kernels.size(); }
};

// 报告读写（无 SQLite 编译时退化为 JSON Lines：每行一个 kernel JSON）
class ReportStore {
public:
    // path 后缀 .json → JSON Lines；否则 SQLite
    bool openForWrite(const std::string& path, bool forceOverwrite, std::string* err);
    bool openForRead(const std::string& path, std::string* err);
    void writeSession(const ReportModel& m, std::string* err);
    void addKernel(const KernelRecord& k, std::string* err);
    bool readAll(ReportModel& m, std::string* err);
    void close();

private:
    bool json_ = false;
    std::string path_;
    void* db_ = nullptr;          // sqlite3*（避免头依赖，void* 传递）
    bool readAllJson(ReportModel& m, std::string* err);
    bool readAllSqlite(ReportModel& m, std::string* err);
};

}  // namespace ncuprof
