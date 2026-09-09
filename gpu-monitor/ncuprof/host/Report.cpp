#include "host/Report.h"

#include <ctime>
#include <fstream>
#include <sstream>

#include "core/MiniJson.h"

#ifdef NCUPROF_HAVE_SQLITE
#include <sqlite3.h>
#endif

namespace ncuprof {

// ---------------- JSON Lines 后端（始终可用） ----------------
// 每行: {"kernel":…, "grid":[…], "block":[…], "metrics":{…},
//        "rules":[{…}]}
static std::string kernelToJson(const KernelRecord& k) {
    std::ostringstream o;
    o << "{\"kernel\":\"" << k.name << "\"";
    o << ",\"grid\":[" << k.gridDim[0] << "," << k.gridDim[1] << "," << k.gridDim[2] << "]";
    o << ",\"block\":[" << k.blockDim[0] << "," << k.blockDim[1] << "," << k.blockDim[2] << "]";
    o << ",\"deviceId\":" << k.deviceId << ",\"passes\":" << k.passes;
    o << ",\"metrics\":{";
    bool first = true;
    for (const auto& [name, r] : k.metrics) {
        if (!r.valid) continue;
        if (!first) o << ",";
        first = false;
        o << "\"" << name << "\":" << r.value;
    }
    o << "}";
    if (!k.ruleMessages.empty()) {
        o << ",\"rules\":[";
        bool f2 = true;
        for (const auto& rm : k.ruleMessages) {
            if (!f2) o << ",";
            f2 = false;
            o << "{\"section\":\"" << rm.section << "\",\"type\":\"" << rm.type
              << "\",\"name\":\"" << rm.name << "\",\"text\":\"" << rm.text << "\"}";
        }
        o << "]";
    }
    o << "}";
    return o.str();
}

bool ReportStore::openForWrite(const std::string& path, bool forceOverwrite,
                               std::string* err) {
    path_ = path;
    json_ = path.size() > 5 && path.compare(path.size() - 5, 5, ".json") == 0;
#ifdef NCUPROF_HAVE_SQLITE
    if (!json_) {
        if (!forceOverwrite) {
            std::ifstream probe(path);
            if (probe.good()) {
                if (err) *err = path + " exists (use -f to overwrite)";
                return false;
            }
        }
        if (sqlite3_open(path.c_str(), (sqlite3**)&db_) != SQLITE_OK) {
            if (err) *err = "cannot open sqlite db";
            db_ = nullptr;
            return false;
        }
        char* e = nullptr;
        sqlite3_exec((sqlite3*)db_,
            "CREATE TABLE IF NOT EXISTS sessions("
            " id INTEGER PRIMARY KEY, cmdline TEXT, timestamp TEXT, tool_version TEXT);"
            "CREATE TABLE IF NOT EXISTS kernels("
            " id INTEGER PRIMARY KEY, name TEXT, grid_x INT, grid_y INT, grid_z INT,"
            " block_x INT, block_y INT, block_z INT, device_id INT, passes INT,"
            " session_id INT);"
            "CREATE TABLE IF NOT EXISTS metrics("
            " kernel_id INT, name TEXT, unit TEXT, value REAL);"
            "CREATE TABLE IF NOT EXISTS rules("
            " kernel_id INT, section TEXT, type TEXT, name TEXT, text TEXT);",
            nullptr, nullptr, &e);
        if (e) {
            if (err) *err = e;
            sqlite3_free(e);
            return false;
        }
        return true;
    }
#else
    if (!json_) {
        // 无 SQLite：强制使用 .json 后缀
        json_ = true;
        if (path_.find('.') == std::string::npos) path_ += ".json";
    }
#endif
    if (!forceOverwrite) {
        std::ifstream probe(path_);
        if (probe.good()) {
            if (err) *err = path_ + " exists (use -f to overwrite)";
            return false;
        }
    }
    std::ofstream f(path_, std::ios::trunc);
    return f.good();
}

void ReportStore::writeSession(const ReportModel& m, std::string* err) {
    (void)m; (void)err;   // sessions 表在首个 kernel 写入时建（JSON 后端无会话行）
}

void ReportStore::addKernel(const KernelRecord& k, std::string* err) {
    (void)err;
#ifdef NCUPROF_HAVE_SQLITE
    if (!json_ && db_) {
        sqlite3* db = (sqlite3*)db_;
        sqlite3_stmt* st = nullptr;
        sqlite3_prepare_v2(db,
            "INSERT INTO kernels(name,grid_x,grid_y,grid_z,block_x,block_y,block_z,"
            "device_id,passes,session_id) VALUES(?,?,?,?,?,?,?,?,?,1)",
            -1, &st, nullptr);
        sqlite3_bind_text(st, 1, k.name.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int(st, 2, k.gridDim[0]);
        sqlite3_bind_int(st, 3, k.gridDim[1]);
        sqlite3_bind_int(st, 4, k.gridDim[2]);
        sqlite3_bind_int(st, 5, k.blockDim[0]);
        sqlite3_bind_int(st, 6, k.blockDim[1]);
        sqlite3_bind_int(st, 7, k.blockDim[2]);
        sqlite3_bind_int(st, 8, k.deviceId);
        sqlite3_bind_int(st, 9, k.passes);
        sqlite3_step(st);
        sqlite3_finalize(st);
        long long kid = sqlite3_last_insert_rowid(db);

        sqlite3_prepare_v2(db,
            "INSERT INTO metrics(kernel_id,name,unit,value) VALUES(?,?,?,?)",
            -1, &st, nullptr);
        for (const auto& [name, r] : k.metrics) {
            if (!r.valid) continue;
            sqlite3_reset(st);
            sqlite3_bind_int64(st, 1, kid);
            sqlite3_bind_text(st, 2, name.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(st, 3, r.unit.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_double(st, 4, r.value);
            sqlite3_step(st);
        }
        sqlite3_finalize(st);
        return;
    }
#endif
    std::ofstream f(path_, std::ios::app);
    f << kernelToJson(k) << "\n";
}

bool ReportStore::openForRead(const std::string& path, std::string* err) {
    path_ = path;
    json_ = path.size() > 5 && path.compare(path.size() - 5, 5, ".json") == 0;
    if (json_) return true;
#ifdef NCUPROF_HAVE_SQLITE
    if (sqlite3_open_v2(path.c_str(), (sqlite3**)&db_, SQLITE_OPEN_READONLY,
                        nullptr) == SQLITE_OK)
        return true;
#endif
    if (err) *err = "cannot open report " + path;
    return false;
}

bool ReportStore::readAll(ReportModel& m, std::string* err) {
    if (json_) return readAllJson(m, err);
#ifdef NCUPROF_HAVE_SQLITE
    return readAllSqlite(m, err);
#else
    if (err) *err = "built without sqlite; use .json reports";
    return false;
#endif
}

bool ReportStore::readAllJson(ReportModel& m, std::string* err) {
    std::ifstream f(path_);
    if (!f.good()) {
        if (err) *err = "cannot open " + path_;
        return false;
    }
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        MiniJson j = MiniJson::parse(line);
        KernelRecord k;
        k.name = j.getString("kernel");
        const MiniJson* g = j.find("grid");
        if (g && g->type == MiniJson::Type::Array && g->arr.size() == 3)
            for (int d = 0; d < 3; ++d) k.gridDim[d] = (int)g->arr[d].num;
        const MiniJson* b = j.find("block");
        if (b && b->type == MiniJson::Type::Array && b->arr.size() == 3)
            for (int d = 0; d < 3; ++d) k.blockDim[d] = (int)b->arr[d].num;
        k.deviceId = (int)j.getNumber("deviceId", 0);
        k.passes = (int)j.getNumber("passes", 0);
        if (const MiniJson* ms = j.find("metrics");
            ms && ms->type == MiniJson::Type::Object) {
            for (const auto& [name, val] : ms->obj) {
                MetricResult r;
                r.name = name;
                r.value = val.num;
                r.valid = val.type == MiniJson::Type::Number;
                k.metrics[name] = r;
            }
        }
        m.addKernel(std::move(k));
    }
    return true;
}

#ifdef NCUPROF_HAVE_SQLITE
bool ReportStore::readAllSqlite(ReportModel& m, std::string* err) {
    sqlite3* db = (sqlite3*)db_;
    sqlite3_stmt* st = nullptr;
    if (sqlite3_prepare_v2(db,
            "SELECT id,name,grid_x,grid_y,grid_z,block_x,block_y,block_z,"
            "device_id,passes FROM kernels ORDER BY id",
            -1, &st, nullptr) != SQLITE_OK) {
        if (err) *err = "query kernels failed";
        return false;
    }
    std::vector<KernelRecord> recs;
    while (sqlite3_step(st) == SQLITE_ROW) {
        KernelRecord k;
        k.id = sqlite3_column_int64(st, 0);
        const unsigned char* n = sqlite3_column_text(st, 1);
        if (n) k.name = (const char*)n;
        k.gridDim[0] = sqlite3_column_int(st, 2);
        k.gridDim[1] = sqlite3_column_int(st, 3);
        k.gridDim[2] = sqlite3_column_int(st, 4);
        k.blockDim[0] = sqlite3_column_int(st, 5);
        k.blockDim[1] = sqlite3_column_int(st, 6);
        k.blockDim[2] = sqlite3_column_int(st, 7);
        k.deviceId = sqlite3_column_int(st, 8);
        k.passes = sqlite3_column_int(st, 9);
        recs.push_back(std::move(k));
    }
    sqlite3_finalize(st);

    for (auto& k : recs) {
        if (sqlite3_prepare_v2(db,
                "SELECT name,unit,value FROM metrics WHERE kernel_id=?",
                -1, &st, nullptr) == SQLITE_OK) {
            sqlite3_bind_int64(st, 1, k.id);
            while (sqlite3_step(st) == SQLITE_ROW) {
                MetricResult r;
                const unsigned char* n = sqlite3_column_text(st, 0);
                const unsigned char* u = sqlite3_column_text(st, 1);
                if (n) r.name = (const char*)n;
                if (u) r.unit = (const char*)u;
                r.value = sqlite3_column_double(st, 2);
                r.valid = true;
                k.metrics[r.name] = r;
            }
            sqlite3_finalize(st);
        }
        m.addKernel(std::move(k));
    }
    return true;
}
#endif

void ReportStore::close() {
#ifdef NCUPROF_HAVE_SQLITE
    if (db_) sqlite3_close((sqlite3*)db_);
#endif
    db_ = nullptr;
}

}  // namespace ncuprof
