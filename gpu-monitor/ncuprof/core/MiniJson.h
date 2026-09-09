// MiniJson.h — 受限 JSON 子集（仅 IPC 载荷使用；不追求完备，追求零依赖）
// 支持：object/array/string/number/bool/null；读写我们自己的消息格式。
#pragma once
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace ncuprof {

class MiniJson {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Type type = Type::Null;
    bool b = false;
    double num = 0;
    std::string str;
    std::vector<MiniJson> arr;
    std::vector<std::pair<std::string, MiniJson>> obj;

    static MiniJson object() { MiniJson j; j.type = Type::Object; return j; }
    static MiniJson array()  { MiniJson j; j.type = Type::Array;  return j; }

    // ---- 构建 ----
    void set(const std::string& k, MiniJson v) {
        type = Type::Object;
        for (auto& [key, val] : obj)
            if (key == k) { val = std::move(v); return; }
        obj.emplace_back(k, std::move(v));
    }
    void set(const std::string& k, const char* v) { set(k, MiniJson::string_(v)); }
    void set(const std::string& k, const std::string& v) { set(k, MiniJson::string_(v)); }
    void set(const std::string& k, double v) {
        MiniJson j; j.type = Type::Number; j.num = v; set(k, std::move(j));
    }
    void set(const std::string& k, int v) { set(k, (double)v); }
    void set(const std::string& k, bool v) {
        MiniJson j; j.type = Type::Bool; j.b = v; set(k, std::move(j));
    }
    void set(const std::string& k, std::initializer_list<double> vs) {
        MiniJson a = array();
        for (double v : vs) { MiniJson j; j.type = Type::Number; j.num = v; a.push(std::move(j)); }
        set(k, std::move(a));
    }
    void push(MiniJson v) { type = Type::Array; arr.push_back(std::move(v)); }

    // ---- 读取 ----
    const MiniJson* find(const std::string& k) const {
        if (type != Type::Object) return nullptr;
        for (auto& [key, val] : obj)
            if (key == k) return &val;
        return nullptr;
    }
    bool has(const std::string& k) const { return find(k) != nullptr; }
    std::string getString(const std::string& k, const std::string& dflt = "") const {
        auto* v = find(k);
        return v && v->type == Type::String ? v->str : dflt;
    }
    double getNumber(const std::string& k, double dflt = 0) const {
        auto* v = find(k);
        return v && v->type == Type::Number ? v->num : dflt;
    }
    bool getBool(const std::string& k, bool dflt = false) const {
        auto* v = find(k);
        return v && v->type == Type::Bool ? v->b : dflt;
    }
    const MiniJson& getArray(const std::string& k) const {
        static MiniJson empty;
        auto* v = find(k);
        return v && v->type == Type::Array ? *v : empty;
    }
    const std::vector<MiniJson>& items() const { return arr; }
    std::string asString() const { return type == Type::String ? str : ""; }

    // ---- 序列化 ----
    std::string dump() const {
        std::string out;
        dumpTo(out);
        return out;
    }
    void dumpTo(std::string& out) const {
        char buf[64];
        switch (type) {
            case Type::Null:    out += "null"; break;
            case Type::Bool:    out += b ? "true" : "false"; break;
            case Type::Number:
                if (std::floor(num) == num && std::fabs(num) < 1e15)
                    snprintf(buf, sizeof buf, "%lld", (long long)num);
                else
                    snprintf(buf, sizeof buf, "%.17g", num);
                out += buf;
                break;
            case Type::String:  dumpString(out); break;
            case Type::Array: {
                out += '[';
                for (size_t i = 0; i < arr.size(); ++i) {
                    if (i) out += ',';
                    arr[i].dumpTo(out);
                }
                out += ']';
                break;
            }
            case Type::Object: {
                out += '{';
                for (size_t i = 0; i < obj.size(); ++i) {
                    if (i) out += ',';
                    MiniJson k = string_(obj[i].first);
                    k.dumpString(out);
                    out += ':';
                    obj[i].second.dumpTo(out);
                }
                out += '}';
                break;
            }
        }
    }

    // ---- 解析 ----
    static MiniJson parse(const std::string& s) {
        size_t pos = 0;
        MiniJson j = parseValue(s, pos);
        return j;
    }

private:
    static MiniJson string_(const std::string& v) {
        MiniJson j; j.type = Type::String; j.str = v; return j;
    }
    void dumpString(std::string& out) const {
        out += '"';
        for (char c : str) {
            switch (c) {
                case '"':  out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n";  break;
                case '\r': out += "\\r";  break;
                case '\t': out += "\\t";  break;
                default:
                    if ((unsigned char)c < 0x20) {
                        char b[8];
                        snprintf(b, sizeof b, "\\u%04x", c);
                        out += b;
                    } else out += c;
            }
        }
        out += '"';
    }
    static void skipWs(const std::string& s, size_t& p) {
        while (p < s.size() && (s[p] == ' ' || s[p] == '\t' || s[p] == '\n' || s[p] == '\r'))
            ++p;
    }
    static MiniJson parseValue(const std::string& s, size_t& p) {
        skipWs(s, p);
        MiniJson j;
        if (p >= s.size()) return j;
        char c = s[p];
        if (c == '{') {
            ++p;
            j.type = Type::Object;
            skipWs(s, p);
            if (p < s.size() && s[p] == '}') { ++p; return j; }
            while (p < s.size()) {
                skipWs(s, p);
                std::string key = parseString(s, p);
                skipWs(s, p);
                if (p < s.size() && s[p] == ':') ++p;
                j.obj.emplace_back(std::move(key), parseValue(s, p));
                skipWs(s, p);
                if (p < s.size() && s[p] == ',') { ++p; continue; }
                break;
            }
            if (p < s.size() && s[p] == '}') ++p;
            return j;
        }
        if (c == '[') {
            ++p;
            j.type = Type::Array;
            skipWs(s, p);
            if (p < s.size() && s[p] == ']') { ++p; return j; }
            while (p < s.size()) {
                j.arr.push_back(parseValue(s, p));
                skipWs(s, p);
                if (p < s.size() && s[p] == ',') { ++p; continue; }
                break;
            }
            if (p < s.size() && s[p] == ']') ++p;
            return j;
        }
        if (c == '"') {
            j.type = Type::String;
            j.str = parseString(s, p);
            return j;
        }
        if (s.compare(p, 4, "true") == 0) {
            j.type = Type::Bool; j.b = true; p += 4; return j;
        }
        if (s.compare(p, 5, "false") == 0) {
            j.type = Type::Bool; j.b = false; p += 5; return j;
        }
        if (s.compare(p, 4, "null") == 0) { p += 4; return j; }
        // number
        {
            char* end = nullptr;
            double v = strtod(s.c_str() + p, &end);
            if (end != s.c_str() + p) {
                j.type = Type::Number;
                j.num = v;
                p = (size_t)(end - s.c_str());
            }
        }
        return j;
    }
    static std::string parseString(const std::string& s, size_t& p) {
        std::string out;
        if (p >= s.size() || s[p] != '"') return out;
        ++p;
        while (p < s.size() && s[p] != '"') {
            char c = s[p++];
            if (c == '\\' && p < s.size()) {
                char e = s[p++];
                switch (e) {
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case 'r': out += '\r'; break;
                    case 'u': {
                        if (p + 4 <= s.size()) {
                            unsigned cp = (unsigned)strtoul(s.substr(p, 4).c_str(), nullptr, 16);
                            p += 4;
                            // UTF-8 编码（BMP 内）
                            if (cp < 0x80) out += (char)cp;
                            else if (cp < 0x800) {
                                out += (char)(0xC0 | (cp >> 6));
                                out += (char)(0x80 | (cp & 0x3F));
                            } else {
                                out += (char)(0xE0 | (cp >> 12));
                                out += (char)(0x80 | ((cp >> 6) & 0x3F));
                                out += (char)(0x80 | (cp & 0x3F));
                            }
                        }
                        break;
                    }
                    default: out += e; break;
                }
            } else out += c;
        }
        if (p < s.size()) ++p;   // 收尾引号
        return out;
    }
};

}  // namespace ncuprof
