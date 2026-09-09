#include "host/TargetProcess.h"

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>

namespace ncuprof {

bool TargetProcess::start(const LaunchSpec& spec, std::string* err) {
    std::vector<char*> argv;
    argv.push_back(const_cast<char*>(spec.appPath.c_str()));
    for (const auto& a : spec.args) argv.push_back(const_cast<char*>(a.c_str()));
    argv.push_back(nullptr);

    std::vector<std::string> envStorage;
    auto addEnv = [&](const std::string& kv) { envStorage.push_back(kv); };
    addEnv("NCUPROF_IPC_PATH=" + spec.ipcPath);
    addEnv("NCUPROF_INJECT=1");
    if (spec.trackChildren)
        addEnv(std::string("NCUPROF_TARGET_PROCESSES=all"));

    // 继承当前 environ + 追加
    std::vector<char*> envp;
    for (char** e = environ; *e; ++e) {
        // 同名变量由我们覆盖：跳过旧值
        if (strncmp(*e, "NCUPROF_", 8) == 0) continue;
        envp.push_back(*e);
    }
    // LD_PRELOAD 前置（保留既有 preload）
    std::string preload = "LD_PRELOAD=" + spec.injectLibPath;
    for (char** e = environ; *e; ++e) {
        if (strncmp(*e, "LD_PRELOAD=", 11) == 0) {
            preload += ":" + std::string(*e + 11);
            break;
        }
    }
    addEnv(preload);
    for (const auto& kv : spec.extraEnv) addEnv(kv);
    for (const auto& kv : envStorage) envp.push_back(const_cast<char*>(kv.c_str()));
    envp.push_back(nullptr);

    pid_ = fork();
    if (pid_ < 0) {
        if (err) *err = "fork() failed";
        return false;
    }
    if (pid_ == 0) {
        // 子进程：新进程组（便于整树终止），脱离父的控制终端影响
        setpgid(0, 0);
        execve(spec.appPath.c_str(), argv.data(), envp.data());
        // execve 失败才会到这里
        _exit(127);
    }
    return true;
}

int TargetProcess::wait() {
    if (pid_ < 0) return -1;
    int status = 0;
    while (waitpid(pid_, &status, 0) != pid_) {
        if (errno == EINTR) continue;
        return -1;
    }
    pid_ = -1;
    return status;
}

void TargetProcess::killTree() {
    if (pid_ < 0) return;
    kill(-pid_, SIGKILL);   // 进程组
    kill(pid_, SIGKILL);
    waitpid(pid_, nullptr, 0);
    pid_ = -1;
}

}  // namespace ncuprof
