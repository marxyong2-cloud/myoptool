// Protocol.h — host <-> target IPC 消息定义
//
// ncu 前端与注入库间走 protobuf（NsysServerProto 等）+ 私有 RPC；
// 本实现采用等价的显式序列化协议：长度前缀(uint32 BE) + 消息类型(uint16)
// + JSON 载荷（计数数据等大二进制走 base64 字段）。简单、可调试、可扩展。
#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace ncuprof::ipc {

enum class MsgType : uint16_t {
    // target -> host
    Attached = 1,        // {pid, cmdline, driverVersion, devices:[...]}
    Progress = 2,        // {stage:"init|collect|replay", pct:0..100}
    KernelBegin = 3,     // {name, demangled, grid:[x,y,z], block:[x,y,z], stream,
                         //  context, deviceId, correlationId, passCount}
    KernelResult = 4,    // {correlationId, metrics:{name:value}, countersB64,
                         //  durationNs, launches:[{startNs,endNs}]}
    KernelSkip = 5,      // {name, reason:"filtered"}
    LogLine = 6,         // {level, text}
    ExitReport = 7,      // {totalProfiled, totalSkipped}
    // host -> target
    Options = 100,       // 下发全部剖析选项（见 host/Options.h）
    Detach = 101,        // 结束会话
    Abort = 102,         // 终止目标进程
};

constexpr uint32_t kMaxMessageBytes = 512u * 1024 * 1024;

// 帧编码：4B 长度(BE) + 2B 类型(BE) + 载荷
inline bool encodeFrame(MsgType t, const std::string& jsonPayload,
                        std::string& outFrame) {
    uint32_t len = (uint32_t)(6 + jsonPayload.size());
    outFrame.clear();
    outFrame.reserve(len);
    outFrame.push_back((char)(len >> 24));
    outFrame.push_back((char)(len >> 16));
    outFrame.push_back((char)(len >> 8));
    outFrame.push_back((char)len);
    outFrame.push_back((char)((uint16_t)t >> 8));
    outFrame.push_back((char)(uint16_t)t);
    outFrame.append(jsonPayload);
    return true;
}

// 从缓冲区解析一帧。consumed=帧总字节数；不完整帧返回 false 且 consumed=0。
inline bool decodeFrame(const char* data, size_t len, MsgType& t,
                        std::string& jsonPayload, size_t& consumed) {
    consumed = 0;
    if (len < 6) return false;
    uint32_t frameLen = ((uint32_t)(unsigned char)data[0] << 24) |
                        ((uint32_t)(unsigned char)data[1] << 16) |
                        ((uint32_t)(unsigned char)data[2] << 8) |
                        (uint32_t)(unsigned char)data[3];
    if (frameLen < 6 || frameLen > kMaxMessageBytes) return false;
    if (len < frameLen) return false;   // 半包，等待更多数据
    t = (MsgType)(((unsigned char)data[4] << 8) | (unsigned char)data[5]);
    jsonPayload.assign(data + 6, frameLen - 6);
    consumed = frameLen;
    return true;
}

// 环境变量：host 在 fork 前设置，target 注入库读取
constexpr const char* kEnvIpcPath = "NCUPROF_IPC_PATH";       // unix socket 路径
constexpr const char* kEnvInject  = "NCUPROF_INJECT";          // =1 时注入库生效
constexpr const char* kEnvTargetProcesses = "NCUPROF_TARGET_PROCESSES";

}  // namespace ncuprof::ipc
