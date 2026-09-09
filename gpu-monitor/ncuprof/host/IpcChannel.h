// IpcChannel.h/.cpp — host 侧 IPC：监听 Unix Socket、收发协议帧
#pragma once
#include <functional>
#include <string>

#include "core/Protocol.h"

namespace ncuprof {

class IpcServer {
public:
    // path: socket 路径（临时目录下生成）；listen 后 accept 单个目标连接
    bool listen(const std::string& path, std::string* err = nullptr);
    bool acceptTarget(int timeoutMs, std::string* err = nullptr);
    bool send(ipc::MsgType t, const std::string& jsonPayload);
    // 阻塞收一帧（无数据立即返回 false）
    bool recv(ipc::MsgType& t, std::string& jsonPayload);
    void closeAll();

    int targetPid() const { return targetPid_; }

private:
    int listenFd_ = -1;
    int connFd_ = -1;
    std::string path_;
    std::string rxBuf_;
    int targetPid_ = -1;
};

}  // namespace ncuprof
