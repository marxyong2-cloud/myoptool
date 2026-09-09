// RuleEngine.h — Python 规则宿主（等价 ncu 的 NvRules：规则为 Python 模块，
// 实现 get_identifier/get_name/get_description/get_section_identifier/apply(handle)）。
// 宿主把 KernelRecord 暴露为 NvRules 风格对象树，规则产生的消息回填
// KernelRecord::ruleMessages。
#pragma once
#include <string>
#include <vector>

#include "host/Report.h"

namespace ncuprof {

class RuleEngine {
public:
    // rulesDir: python/rules 目录。加载全部 *.py（跳过 _ 前缀）
    explicit RuleEngine(std::string rulesDir);

    // 对一个 kernel 运行全部规则（Python 不可用时静默跳过并返回 false）
    bool applyAll(KernelRecord& k, std::string* err = nullptr);

    // --list-rules
    std::vector<std::string> ruleIdentifiers() const;

private:
    std::string rulesDir_;
    bool pythonOk_ = false;
    void* pyCtx_ = nullptr;   // RuleHost 的不透明句柄（实现文件内转换）
};

}  // namespace ncuprof
