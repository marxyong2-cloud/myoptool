// SectionFile.h — 解析 ncu 官方 .section 文件（protobuf text 格式的子集）
// schema 逆向自 extras/FileFormat/ProfilerSection.proto；支持:
//   Identifier / DisplayName / Description / Order / Sets{Identifier} /
//   Header{Metrics{Name,Label,Filter{Items{MinArch,MaxArch}}}} /
//   Metrics{Metrics{…}} / MetricDefinitions{MetricDefinitions{Name,Expression}} /
//   Body{DisplayName, Items{Table{…}|BarChart{…}|HorizontalContainer{…}}}
//   SourceMetrics{…}
// 值类型: string("…") / 标识符 / 整数 / 枚举名，均可带 Field { … } 嵌套。
#pragma once
#include <cstdint>
#include <cstdlib>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace ncuprof {

// ---------- 通用 textproto 值 ----------
struct TextValue {
    enum Kind { kIdent, kStr, kInt } kind = kIdent;
    std::string ident;     // 枚举/bool/标识符原文；数字时保留原始 token（可含小数）
    std::string str;       // 引号字符串（解码后）
    int64_t integer = 0;   // 浮点输入被截断为整数部分；浮点值请用 asDouble()

    int64_t asInt() const { return kind == kInt ? integer : (ident == "true" ? 1 : 0); }
    double asDouble() const {
        if (kind == kInt) return strtod(ident.c_str(), nullptr);
        if (kind == kIdent) return ident == "true" ? 1.0 : 0.0;
        return 0.0;
    }
    bool isTrue() const {
        return (kind == kInt && integer != 0) || (kind == kIdent && ident == "true");
    }
    std::string asString() const { return kind == kStr ? str : ident; }
};

struct TextMessage {
    std::string name;
    // 顺序保留的字段（textproto 字段可重复出现，按出现序输出）
    struct Field {
        std::string name;
        bool isMessage = false;
        TextValue value;                          // 标量字段
        std::shared_ptr<TextMessage> message;     // 消息字段
    };
    std::vector<Field> fields;

    const Field* find(const std::string& n) const;
    std::vector<const Field*> findAll(const std::string& n) const;
    // 便捷取值：找不到返回 dflt
    std::string str(const std::string& n, const std::string& dflt = "") const;
    int64_t integer(const std::string& n, int64_t dflt = 0) const;
};

// textproto 全量解析器（.section / 未来 .chart / 自定义规则配置均可用）
bool parseTextProto(const std::string& text, TextMessage& out, std::string* err = nullptr);

// ---------- 强类型 section 模型 ----------
struct SectionArchRange {
    int minMajor = 0, minMinor = 0;   // 0 = 无下限
    int maxMajor = 0, maxMinor = 0;   // 0 = 无上限
    bool match(int ccMajor, int ccMinor) const;
};

// Filter { Items { MinArch MaxArch } Items { … } }：区间列表，任一命中即通过
struct SectionMetricFilter {
    std::vector<SectionArchRange> ranges;   // 空 = 所有架构
    bool match(int ccMajor, int ccMinor) const;
};

struct SectionMetric {
    std::string name;
    std::string label;
    std::string unit;
    SectionMetricFilter filter;         // 空 = 所有架构
    bool showInstances = false;
    double multiplier = 1.0;            // PmSampling 采样值乘数
    std::vector<std::string> groups;    // 所属 metric group（SourceView 组织用）
};

struct SectionMetricDef {               // MetricDefinitions: 派生指标
    std::string name;
    std::string expression;             // "A + B" 形式（A/B 为已收集指标名）
    SectionMetricFilter filter;
};

struct SectionBarChartMetric { std::string label, name; };

struct SectionBodyItem {
    enum Kind { kTable, kBarChart, kHorizontalContainer, kOther } kind = kOther;
    std::string label;
    std::vector<SectionMetric> tableMetrics;            // Table / BarChart 均复用
    std::vector<SectionBarChartMetric> barMetrics;
    std::vector<std::shared_ptr<SectionBodyItem>> children;  // HorizontalContainer
    std::shared_ptr<TextMessage> raw;                   // 原始消息（兜底/扩展）
};

struct ProfilerSection {
    std::string identifier;
    std::string displayName;
    std::string description;
    int order = 0;
    std::vector<std::string> sets;               // basic/detailed/full…
    std::vector<SectionMetric> headerMetrics;
    std::vector<SectionMetric> metrics;          // Metrics{Metrics{…}}
    std::vector<SectionMetric> sourceMetrics;    // SourceMetrics{…}
    std::vector<SectionMetricDef> metricDefs;    // 派生指标
    std::vector<SectionBodyItem> body;           // Body{…}（可有多个）

    bool loadFromFile(const std::string& path, std::string* err = nullptr);
    bool loadFromString(const std::string& text, std::string* err = nullptr);
};

// 目录扫描：加载 <dir>/*.section（ncu 搜索规则：sections/ 目录，忽略 .ncu-ignore）
std::vector<ProfilerSection> loadSectionDir(const std::string& dir,
                                             std::vector<std::string>* errs = nullptr);

}  // namespace ncuprof
