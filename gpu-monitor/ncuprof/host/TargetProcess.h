// TargetProcess.h — 目标进程启动 + 注入环境（等价 ncu launcher）
// fork/exec 目标应用，设置 LD_PRELOAD=libncuprof_inj.so 与 IPC 环境变量，
// 支持 --target-processes all（跟踪子进程：LD_PRELOAD 自动继承）。
#pragma once
#include <string>
#include <vector>

namespace ncuprof {

struct LaunchSpec {
    std::string appPath;
    std::vector<std::string> args;
    std::string ipcPath;             // Unix socket 路径
    std::string injectLibPath;       // libncuprof_inj.so 绝对路径
    std::vector<std::string> extraEnv;   // --env KEY=VAL
    bool trackChildren = false;      // --target-processes all
};

class TargetProcess {
public:
    // fork + execve；失败返回 false（child 内直接 _exit）
    bool start(const LaunchSpec& spec, std::string* err = nullptr);
    int wait();                       // 阻塞等退出，返回 exit status
    void killTree();                  // SIGKILL 进程组
    pid_t pid() const { return pid_; }

private:
    pid_t pid_ = -1;
};

}  // namespace ncuprof
