#include "host/RuleEngine.h"

#include <dirent.h>

#include <algorithm>
#include <cstring>

#ifdef NCUPROF_HAVE_PYTHON
#include <Python.h>
#endif

namespace ncuprof {

namespace {
std::vector<std::string> listPyFiles(const std::string& dir) {
    std::vector<std::string> out;
    DIR* d = opendir(dir.c_str());
    if (!d) return out;
    struct dirent* e;
    while ((e = readdir(d)) != nullptr) {
        std::string n = e->d_name;
        if (n.size() > 3 && n.compare(n.size() - 3, 3, ".py") == 0 && n[0] != '_')
            out.push_back(n.substr(0, n.size() - 3));
    }
    closedir(d);
    std::sort(out.begin(), out.end());
    return out;
}
}  // namespace

RuleEngine::RuleEngine(std::string rulesDir)
    : rulesDir_(std::move(rulesDir)) {
#ifdef NCUPROF_HAVE_PYTHON
    if (!Py_IsInitialized()) {
        Py_InitializeEx(0);   // 不接管信号
        pythonOk_ = true;
    } else {
        pythonOk_ = true;
    }
#endif
}

std::vector<std::string> RuleEngine::ruleIdentifiers() const {
    return listPyFiles(rulesDir_);
}

bool RuleEngine::applyAll(KernelRecord& k, std::string* err) {
#ifdef NCUPROF_HAVE_PYTHON
    if (!pythonOk_) return false;

    // 准备桥接模块搜索路径：python/（nvrules.py 兼容层） + python/rules
    PyObject* sysPath = PySys_GetObject("path");
    std::string rulesParent = rulesDir_.substr(0, rulesDir_.find_last_of('/'));
    PyObject* d1 = PyUnicode_FromString(rulesParent.c_str());
    PyList_Insert(sysPath, 0, d1);
    Py_DECREF(d1);
    PyObject* d2 = PyUnicode_FromString(rulesDir_.c_str());
    PyList_Insert(sysPath, 0, d2);
    Py_DECREF(d2);

    bool anyRan = false;
    for (const auto& modName : listPyFiles(rulesDir_)) {
        PyObject* mod = PyImport_ImportModule(modName.c_str());
        if (!mod) {
            PyErr_Clear();
            continue;
        }
        // 构造 handle：{"metrics": {name: value…}, "kernel": name,
        //              "grid": [..], "block": [..], "messages": []}
        PyObject* handle = PyDict_New();
        PyObject* metrics = PyDict_New();
        for (const auto& [name, r] : k.metrics) {
            if (!r.valid) continue;
            PyObject* v = PyFloat_FromDouble(r.value);
            PyDict_SetItemString(metrics, name.c_str(), v);
            Py_DECREF(v);
        }
        PyDict_SetItemString(handle, "metrics", metrics);
        Py_DECREF(metrics);
        PyObject* kn = PyUnicode_FromString(k.name.c_str());
        PyDict_SetItemString(handle, "kernel", kn);
        Py_DECREF(kn);
        PyDict_SetItemString(handle, "messages", PyList_New(0));

        PyObject* applyFn = PyObject_GetAttrString(mod, "apply");
        if (applyFn && PyCallable_Check(applyFn)) {
            PyObject* args = PyTuple_Pack(1, handle);
            PyObject* res = PyObject_CallObject(applyFn, args);
            Py_DECREF(args);
            if (res) {
                Py_DECREF(res);
                anyRan = true;
            } else {
                PyErr_Clear();   // 单条规则失败不影响其他规则
            }
        }
        Py_XDECREF(applyFn);

        // 回读 messages 列表 -> KernelRecord
        PyObject* msgs = PyDict_GetItemString(handle, "messages");
        if (msgs && PyList_Check(msgs)) {
            for (Py_ssize_t i = 0; i < PyList_Size(msgs); ++i) {
                PyObject* m = PyList_GetItem(msgs, i);   // 借引用
                KernelRecord::RuleMessage rm;
                auto getStr = [&](const char* key) -> std::string {
                    PyObject* v = PyDict_GetItemString(m, key);
                    if (!v || !PyUnicode_Check(v)) return "";
                    return PyUnicode_AsUTF8(v);
                };
                rm.section = getStr("section");
                rm.type = getStr("type");
                rm.name = getStr("name");
                rm.text = getStr("text");
                k.ruleMessages.push_back(std::move(rm));
            }
        }
        Py_DECREF(handle);
        Py_DECREF(mod);
    }
    return anyRan;
#else
    (void)k;
    if (err) *err = "built without Python support (ncuprof_EMBED_PYTHON)";
    return false;
#endif
}

}  // namespace ncuprof
