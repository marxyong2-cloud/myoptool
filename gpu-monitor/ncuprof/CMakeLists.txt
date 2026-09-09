cmake_minimum_required(VERSION 3.16)
project(ncuprof LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# CUDA 工具链可选：缺失时退化为逻辑层构建（test_parsers + Python 测试），
# host/target 的 CUPTI 采集链路仍需 CUDA 11+ 工具链（见 README 验证状态）。
find_package(CUDAToolkit QUIET)          # CUPTI 头 + cuda 驱动 API 头
if(CUDAToolkit_FOUND)
  message(STATUS "CUDAToolkit ${CUDAToolkit_VERSION} found: full build")
else()
  message(STATUS "CUDAToolkit not found: logic-layer-only build "
                 "(test_parsers + Python rules/grammar tests)")
endif()
find_package(Python3 COMPONENTS Interpreter Development QUIET)
find_package(SQLite3 QUIET)

add_compile_options(-Wall -Wextra)

# ---------------- 测试 ----------------
enable_testing()
# 解析器/occupancy/指标库单测：纯逻辑层，无 CUDA 依赖
add_executable(test_parsers tests/test_parsers.cpp
  core/SectionFile.cpp core/MetricName.cpp core/MetricDatabase.cpp core/OccupancyCalc.cpp)
target_include_directories(test_parsers PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME parsers COMMAND test_parsers)

# Python 规则引擎 / section 文法测试（纯 Python，无需 GPU）
if(Python3_FOUND)
  add_test(NAME rules   COMMAND ${Python3_EXECUTABLE}
           ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_rules.py)
  add_test(NAME section_grammar COMMAND ${Python3_EXECUTABLE}
           ${CMAKE_CURRENT_SOURCE_DIR}/tests/verify_section_grammar.py
           ${CMAKE_CURRENT_SOURCE_DIR}/sections)
endif()

if(CUDAToolkit_FOUND)

# ---------------- 双侧共享核心库 ----------------
add_library(ncuprof_core STATIC
  core/SectionFile.cpp
  core/MetricName.cpp
  core/MetricDatabase.cpp
  core/OccupancyCalc.cpp
  core/GpuInfo.cpp
)
target_include_directories(ncuprof_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(ncuprof_core PUBLIC CUDA::cuda_driver)

# ---------------- 目标侧注入库 ----------------
add_library(ncuprof_inj SHARED
  target/injection.cpp
  target/Session.cpp
  target/ReplayEngine.cpp
  target/CuptiMetricCollector.cpp
)
target_link_libraries(ncuprof_inj PRIVATE ncuprof_core CUDA::cupti ${CMAKE_DL_LIBS} pthread)
set_target_properties(ncuprof_inj PROPERTIES PREFIX "")   # lib 名: libncuprof_inj.so

# ---------------- 宿主前端 ----------------
add_executable(ncuprof
  host/main.cpp
  host/Options.cpp
  host/TargetProcess.cpp
  host/IpcChannel.cpp
  host/Report.cpp
  host/ConsolePrinter.cpp
  host/RuleEngine.cpp
  host/MetricEvaluationService.cpp
)
target_link_libraries(ncuprof PRIVATE ncuprof_core CUDA::cupti ${CMAKE_DL_LIBS} pthread)
if(SQLite3_FOUND)
  target_link_libraries(ncuprof PRIVATE SQLite::SQLite3)
  target_compile_definitions(ncuprof PRIVATE NCUPROF_HAVE_SQLITE)
else()
  message(WARNING "SQLite3 not found: -o 报告输出退化为 JSON (--format json)")
endif()
if(Python3_FOUND)
  target_link_libraries(ncuprof PRIVATE Python3::Python)
  target_compile_definitions(ncuprof PRIVATE NCUPROF_HAVE_PYTHON)
else()
  message(WARNING "Python3 dev not found: 规则引擎禁用")
endif()

install(TARGETS ncuprof ncuprof_inj RUNTIME DESTINATION bin LIBRARY DESTINATION lib)
install(DIRECTORY sections/ DESTINATION share/ncuprof/sections)
install(DIRECTORY python/ DESTINATION share/ncuprof/python)

endif()   # CUDAToolkit_FOUND
