#include "core/SectionFile.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <dirent.h>
#include <sys/stat.h>

namespace ncuprof {

// ===================== textproto 解析器 =====================
namespace {

struct Lexer {
    const std::string& src;
    size_t pos = 0;
    std::string err;

    explicit Lexer(const std::string& s) : src(s) {}

    void skipWs() {
        while (pos < src.size()) {
            char c = src[pos];
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') { ++pos; continue; }
            if (c == '#') {                       // 行注释（部分 ncu 文件使用）
                while (pos < src.size() && src[pos] != '\n') ++pos;
                continue;
            }
            break;
        }
    }
    bool eof() { skipWs(); return pos >= src.size(); }
    char peek() { skipWs(); return pos < src.size() ? src[pos] : '\0'; }
    bool consume(char c) {
        if (peek() != c) return false;
        ++pos;
        return true;
    }
    bool fail(const std::string& m) {
        if (err.empty())
            err = m + " at offset " + std::to_string(pos);
        return false;
    }

    // 标识符: [A-Za-z0-9_\-]+（字段名/枚举/整数；':' 是字段分隔符，不含在内）
    std::string ident() {
        skipWs();
        size_t start = pos;
        while (pos < src.size()) {
            char c = src[pos];
            if (isalnum((unsigned char)c) || c == '_' || c == '-') { ++pos; continue; }
            break;
        }
        return src.substr(start, pos - start);
    }
};

// 解析一个值: 字符串 / 标识符 / 整数 / 浮点（官方 section 有 Multiplier: 0.5）
bool parseValue(Lexer& L, TextValue& v) {
    char c = L.peek();
    if (c == '"') {
        ++L.pos;
        std::string s;
        while (L.pos < L.src.size() && L.src[L.pos] != '"') {
            char ch = L.src[L.pos++];
            if (ch == '\\' && L.pos < L.src.size()) {   // \" \\ \n 转义
                char e = L.src[L.pos++];
                switch (e) {
                    case 'n': s.push_back('\n'); break;
                    case 't': s.push_back('\t'); break;
                    case 'r': s.push_back('\r'); break;
                    default:  s.push_back(e);   break;
                }
            } else s.push_back(ch);
        }
        if (L.pos >= L.src.size()) return L.fail("unterminated string");
        ++L.pos;  // 收尾引号
        v.kind = TextValue::kStr;
        v.str = s;
        return true;
    }
    // 数字（含浮点/十六进制）：官方 section 的 CtrDomains.Ctrs 用 0x 位掩码
    if (isdigit((unsigned char)c) || c == '-' || c == '.') {
        size_t start = L.pos;
        bool isFloat = false;
        if (c == '0' && L.pos + 1 < L.src.size() &&
            (L.src[L.pos + 1] == 'x' || L.src[L.pos + 1] == 'X')) {
            L.pos += 2;
            while (L.pos < L.src.size() && isxdigit((unsigned char)L.src[L.pos]))
                ++L.pos;
            std::string tok = L.src.substr(start, L.pos - start);
            v.kind = TextValue::kInt;
            v.integer = (int64_t)strtoull(tok.c_str(), nullptr, 16);
            v.ident = tok;
            return true;
        }
        while (L.pos < L.src.size()) {
            char d = L.src[L.pos];
            if (isdigit((unsigned char)d)) { ++L.pos; continue; }
            if (d == '.' || d == 'e' || d == 'E') { isFloat = true; ++L.pos; continue; }
            if ((d == '+' || d == '-') && L.pos > start &&
                (L.src[L.pos - 1] == 'e' || L.src[L.pos - 1] == 'E')) { ++L.pos; continue; }
            break;
        }
        std::string tok = L.src.substr(start, L.pos - start);
        v.kind = TextValue::kInt;
        v.integer = isFloat ? (int64_t)strtod(tok.c_str(), nullptr)
                            : strtoll(tok.c_str(), nullptr, 10);
        // 浮点按整数存储（Multiplier 0.5 -> 0），原值保留在 ident 里以便取 double
        v.ident = tok;
        return true;
    }
    std::string id = L.ident();
    if (id.empty()) return L.fail("expected value");
    v.kind = TextValue::kIdent;
    v.ident = id;
    return true;
}

// 解析消息体 { field… }（已消费 '{'）
bool parseMessageBody(Lexer& L, TextMessage& msg);

// 解析字段: name ':' value  或  name '{' … '}' 或  name value（protobuf 容忍）
bool parseField(Lexer& L, TextMessage& msg) {
    std::string name = L.ident();
    if (name.empty()) return L.fail("expected field name");
    char c = L.peek();

    if (c == ':') {
        ++L.pos;
        if (L.peek() == '{') {          // name: { … }（textproto 允许）
            ++L.pos;
            auto m = std::make_shared<TextMessage>();
            m->name = name;
            if (!parseMessageBody(L, *m)) return false;
            TextMessage::Field f;
            f.name = name; f.isMessage = true; f.message = m;
            msg.fields.push_back(std::move(f));
            return true;
        }
        TextValue v;
        if (!parseValue(L, v)) return false;
        TextMessage::Field f;
        f.name = name; f.value = v;
        msg.fields.push_back(std::move(f));
        return true;
    }
    if (c == '{') {                     // name { … }
        ++L.pos;
        auto m = std::make_shared<TextMessage>();
        m->name = name;
        if (!parseMessageBody(L, *m)) return false;
        TextMessage::Field f;
        f.name = name; f.isMessage = true; f.message = m;
        msg.fields.push_back(std::move(f));
        return true;
    }
    return L.fail("expected ':' or '{' after field '" + name + "'");
}

bool parseMessageBody(Lexer& L, TextMessage& msg) {
    while (true) {
        if (L.eof()) return L.fail("unexpected EOF in message body");
        if (L.consume('}')) return true;
        // 容忍字段值后的尾随逗号（官方 section 文件存在：Groups: "x",）
        if (L.consume(',')) continue;
        if (!parseField(L, msg)) return false;
    }
}

}  // namespace

bool parseTextProto(const std::string& text, TextMessage& out, std::string* err) {
    Lexer L(text);
    while (!L.eof()) {
        if (L.consume('}')) return err ? (*err = "stray '}'", false) : false;
        if (!parseField(L, out)) {
            if (err) *err = L.err;
            return false;
        }
    }
    return true;
}

const TextMessage::Field* TextMessage::find(const std::string& n) const {
    for (const auto& f : fields)
        if (f.name == n) return &f;
    return nullptr;
}
std::vector<const TextMessage::Field*> TextMessage::findAll(const std::string& n) const {
    std::vector<const Field*> r;
    for (const auto& f : fields)
        if (f.name == n) r.push_back(&f);
    return r;
}
std::string TextMessage::str(const std::string& n, const std::string& dflt) const {
    auto* f = find(n);
    return f ? f->value.asString() : dflt;
}
int64_t TextMessage::integer(const std::string& n, int64_t dflt) const {
    auto* f = find(n);
    return f ? f->value.asInt() : dflt;
}

// ===================== section 模型 =====================

bool SectionArchRange::match(int ccMajor, int ccMinor) const {
    if (minMajor && (ccMajor < minMajor ||
        (ccMajor == minMajor && ccMinor < minMinor)))
        return false;
    if (maxMajor && (ccMajor > maxMajor ||
        (ccMajor == maxMajor && ccMinor > maxMinor)))
        return false;
    return true;
}

bool SectionMetricFilter::match(int ccMajor, int ccMinor) const {
    if (ranges.empty()) return true;
    for (const auto& r : ranges)
        if (r.match(ccMajor, ccMinor)) return true;
    return false;
}

namespace {

// "CC_86" / "CC_75" -> (8,6)/(7,5)
bool parseArch(const std::string& s, int& maj, int& min) {
    if (s.size() < 6 || s.compare(0, 3, "CC_") != 0) return false;
    auto dot = s.find('_');          // CC_86 无第二个 '_'
    std::string v = s.substr(3);
    if (v.empty()) return false;
    maj = atoi(std::string(1, v[0]).c_str());
    min = v.size() > 1 ? atoi(std::string(1, v[1]).c_str()) : 0;
    return maj > 0;
}

// 从 Metric 消息构造 SectionMetric（含 Filter/Multiplier/Groups）
SectionMetric parseMetric(const TextMessage& m) {
    SectionMetric sm;
    sm.name = m.str("Name");
    sm.label = m.str("Label");
    sm.unit = m.str("Unit");
    sm.showInstances = m.integer("ShowInstances", 0) != 0;
    if (auto* mult = m.find("Multiplier"))
        sm.multiplier = mult->value.asDouble();
    for (auto* g : m.findAll("Groups"))
        sm.groups.push_back(g->value.asString());
    // Filter { Items { MinArch MaxArch } Items { … } }：全部区间收集（OR 语义）
    for (auto* items : m.findAll("Filter")) {
        if (!items->isMessage) continue;
        SectionMetricFilter f;
        for (auto* it : items->message->findAll("Items")) {
            if (!it->isMessage) continue;
            SectionArchRange r;
            int maj, min;
            if (parseArch(it->message->str("MinArch"), maj, min)) {
                r.minMajor = maj; r.minMinor = min;
            }
            if (parseArch(it->message->str("MaxArch"), maj, min)) {
                r.maxMajor = maj; r.maxMinor = min;
            }
            f.ranges.push_back(r);
        }
        if (!f.ranges.empty()) sm.filter = f;
    }
    return sm;
}

std::shared_ptr<SectionBodyItem> parseBodyItem(const TextMessage::Field& f) {
    if (!f.isMessage) return nullptr;
    auto item = std::make_shared<SectionBodyItem>();
    item->raw = f.message;

    // 官方格式：Body/HorizontalContainer 的 Items{…} 本身即一个 SectionBodyItem，
    // 载荷（Table/BarChart/HorizontalContainer）是其内嵌字段；同时兼容直接
    // 传入载荷字段本身。
    const TextMessage::Field* p = &f;
    if (p->name != "Table" && p->name != "BarChart" &&
        p->name != "HorizontalContainer") {
        p = nullptr;
        for (const auto& sub : f.message->fields) {
            if (sub.isMessage && (sub.name == "Table" || sub.name == "BarChart" ||
                                  sub.name == "HorizontalContainer")) {
                p = &sub;
                break;
            }
        }
        if (!p) return item;   // 未知载荷：kOther + raw 兜底
    }
    const TextMessage& m = *p->message;

    if (p->name == "Table") {
        item->kind = SectionBodyItem::kTable;
        item->label = m.str("Label");
        for (auto* mf : m.findAll("Metrics")) {
            if (!mf->isMessage) continue;
            auto* inner = mf->message->find("Metrics");  // Metrics { Metrics {…} }
            if (inner && inner->isMessage)
                item->tableMetrics.push_back(parseMetric(*inner->message));
            else
                item->tableMetrics.push_back(parseMetric(*mf->message));
        }
    } else if (p->name == "BarChart") {
        item->kind = SectionBodyItem::kBarChart;
        item->label = m.str("Label");
        for (auto* mf : m.findAll("Metrics")) {
            if (!mf->isMessage) continue;
            auto* inner = mf->message->find("Metrics");
            const TextMessage* mm = inner && inner->isMessage ? inner->message.get()
                                                              : mf->message.get();
            SectionBarChartMetric b;
            b.name = mm->str("Name");
            b.label = mm->str("Label");
            item->barMetrics.push_back(b);
        }
    } else {   // HorizontalContainer
        item->kind = SectionBodyItem::kHorizontalContainer;
        item->label = m.str("Label");
        for (auto* it : m.findAll("Items")) {
            auto child = parseBodyItem(*it);
            if (child) item->children.push_back(child);
        }
    }
    return item;
}

}  // namespace

bool ProfilerSection::loadFromString(const std::string& text, std::string* err) {
    TextMessage root;
    if (!parseTextProto(text, root, err)) return false;

    identifier = root.str("Identifier");
    displayName = root.str("DisplayName");
    description = root.str("Description");
    order = (int)root.integer("Order", 0);
    if (identifier.empty()) {
        if (err) *err = "section missing Identifier";
        return false;
    }

    for (auto* f : root.findAll("Sets")) {
        if (f->isMessage) {
            std::string id = f->message->str("Identifier");
            if (!id.empty()) sets.push_back(id);
        }
    }

    if (auto* h = root.find("Header"); h && h->isMessage) {
        for (auto* mf : h->message->findAll("Metrics")) {
            if (mf->isMessage) headerMetrics.push_back(parseMetric(*mf->message));
        }
    }

    for (auto* top = root.find("Metrics"); top && top->isMessage;) {
        for (auto* mf : top->message->findAll("Metrics")) {
            if (mf->isMessage) metrics.push_back(parseMetric(*mf->message));
        }
        break;
    }

    if (auto* sm = root.find("SourceMetrics"); sm && sm->isMessage) {
        for (auto* mf : sm->message->findAll("Metrics")) {
            if (mf->isMessage) sourceMetrics.push_back(parseMetric(*mf->message));
        }
    }

    if (auto* md = root.find("MetricDefinitions"); md && md->isMessage) {
        for (auto* d : md->message->findAll("MetricDefinitions")) {
            if (!d->isMessage) continue;
            SectionMetricDef def;
            def.name = d->message->str("Name");
            def.expression = d->message->str("Expression");
            metricDefs.push_back(def);
        }
    }

    for (auto* b : root.findAll("Body")) {
        if (!b->isMessage) continue;
        for (auto* it : b->message->findAll("Items")) {
            auto item = parseBodyItem(*it);
            if (item) body.push_back(std::move(*item));
        }
    }
    return true;
}

bool ProfilerSection::loadFromFile(const std::string& path, std::string* err) {
    FILE* fp = fopen(path.c_str(), "rb");
    if (!fp) {
        if (err) *err = "cannot open " + path;
        return false;
    }
    std::string text;
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, fp)) > 0) text.append(buf, n);
    fclose(fp);
    if (!loadFromString(text, err)) {
        if (err) *err = path + ": " + *err;
        return false;
    }
    return true;
}

std::vector<ProfilerSection> loadSectionDir(const std::string& dir,
                                            std::vector<std::string>* errs) {
    std::vector<ProfilerSection> out;
    DIR* d = opendir(dir.c_str());
    if (!d) {
        if (errs) errs->push_back("cannot open section dir " + dir);
        return out;
    }
    struct dirent* e;
    std::vector<std::string> names;
    while ((e = readdir(d)) != nullptr) {
        std::string n = e->d_name;
        if (n.size() > 8 && n.compare(n.size() - 8, 8, ".section") == 0)
            names.push_back(n);
    }
    closedir(d);
    std::sort(names.begin(), names.end());
    for (const auto& n : names) {
        ProfilerSection s;
        std::string err;
        if (s.loadFromFile(dir + "/" + n, &err)) {
            out.push_back(std::move(s));
        } else if (errs) {
            errs->push_back(err);
        }
    }
    // 按 Order 排序（ncu 语义：小者先显示）
    std::stable_sort(out.begin(), out.end(),
                     [](const ProfilerSection& a, const ProfilerSection& b) {
                         return a.order < b.order;
                     });
    return out;
}

}  // namespace ncuprof
