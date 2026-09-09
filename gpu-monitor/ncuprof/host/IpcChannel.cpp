#include "host/IpcChannel.h"

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <cstring>

namespace ncuprof {

bool IpcServer::listen(const std::string& path, std::string* err) {
    path_ = path;
    ::unlink(path.c_str());
    listenFd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listenFd_ < 0) {
        if (err) *err = "socket() failed";
        return false;
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path.c_str(), sizeof addr.sun_path - 1);
    if (::bind(listenFd_, (sockaddr*)&addr, sizeof addr) != 0) {
        if (err) *err = "bind(" + path + ") failed";
        return false;
    }
    if (::listen(listenFd_, 1) != 0) {
        if (err) *err = "listen() failed";
        return false;
    }
    return true;
}

bool IpcServer::acceptTarget(int timeoutMs, std::string* err) {
    if (listenFd_ < 0) {
        if (err) *err = "not listening";
        return false;
    }
    pollfd p{listenFd_, POLLIN, 0};
    int r = ::poll(&p, 1, timeoutMs);
    if (r <= 0) {
        if (err) *err = "target did not connect within timeout";
        return false;
    }
    connFd_ = ::accept(listenFd_, nullptr, nullptr);
    if (connFd_ < 0) {
        if (err) *err = "accept() failed";
        return false;
    }
    return true;
}

bool IpcServer::send(ipc::MsgType t, const std::string& jsonPayload) {
    if (connFd_ < 0) return false;
    std::string frame;
    ipc::encodeFrame(t, jsonPayload, frame);
    size_t sent = 0;
    while (sent < frame.size()) {
        ssize_t n = ::send(connFd_, frame.data() + sent, frame.size() - sent, MSG_NOSIGNAL);
        if (n <= 0) return false;
        sent += (size_t)n;
    }
    return true;
}

bool IpcServer::recv(ipc::MsgType& t, std::string& jsonPayload) {
    if (connFd_ < 0) return false;
    while (true) {
        ipc::MsgType tt;
        std::string payload;
        size_t consumed;
        if (ipc::decodeFrame(rxBuf_.data(), rxBuf_.size(), tt, payload, consumed)) {
            t = tt;
            jsonPayload = std::move(payload);
            rxBuf_.erase(0, consumed);
            return true;
        }
        char chunk[65536];
        ssize_t n = ::recv(connFd_, chunk, sizeof chunk, 0);
        if (n <= 0) return false;
        rxBuf_.append(chunk, (size_t)n);
        if (rxBuf_.size() > ipc::kMaxMessageBytes) return false;
    }
}

void IpcServer::closeAll() {
    if (connFd_ >= 0) ::close(connFd_);
    if (listenFd_ >= 0) ::close(listenFd_);
    connFd_ = listenFd_ = -1;
    if (!path_.empty()) ::unlink(path_.c_str());
}

}  // namespace ncuprof
