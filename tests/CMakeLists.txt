# The tests: fgw_core against the Python app's own results (reference/),
# with GoogleTest.
add_executable(fgw_core_tests
    core/reference.cpp
    core/grain_test.cpp
    core/picture_test.cpp)
target_compile_definitions(fgw_core_tests PRIVATE FGW_REFERENCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/reference")
target_link_libraries(fgw_core_tests PRIVATE fgw_core fgw_platform nlohmann_json gtest gtest_main bcrypt
                                             fgw_compile_options)

include(GoogleTest)
gtest_discover_tests(fgw_core_tests DISCOVERY_MODE PRE_TEST)
