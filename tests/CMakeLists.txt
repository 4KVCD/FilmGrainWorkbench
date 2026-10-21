# The tests: fgw_core against the Python app's own results (reference/),
# with GoogleTest.
add_executable(fgw_core_tests
    core/reference.cpp
    core/grain_test.cpp
    core/picture_test.cpp
    core/engine_test.cpp)
target_compile_definitions(fgw_core_tests PRIVATE FGW_REFERENCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/reference")
target_link_libraries(fgw_core_tests PRIVATE fgw_core fgw_platform nlohmann_json gtest gtest_main bcrypt
                                             fgw_compile_options)
fgw_copy_ffmpeg(fgw_core_tests)

include(GoogleTest)
gtest_discover_tests(fgw_core_tests DISCOVERY_MODE PRE_TEST)

# fgw_check: runs one engine function on real files and prints its result as
# JSON, for comparing with the Python app (tools/compare_with_python.py). A
# developer tool, not a test and not shipped.
add_executable(fgw_check tools/fgw_check.cpp)
target_link_libraries(fgw_check PRIVATE fgw_core nlohmann_json fgw_compile_options)
fgw_copy_ffmpeg(fgw_check)
