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

# fgw_walkthrough: drives the real window on real files and reports each
# step's time and the longest the window did not answer. Built beside the app
# to use its Qt DLLs. A developer tool, not a test and not shipped.
if(FGW_BUILD_APP)
    add_executable(fgw_walkthrough tools/walkthrough.cpp)
    target_link_libraries(fgw_walkthrough PRIVATE fgw_app fgw_compile_options)
    set_target_properties(fgw_walkthrough PROPERTIES RUNTIME_OUTPUT_DIRECTORY $<TARGET_FILE_DIR:FilmGrainWorkbench>)
    add_dependencies(fgw_walkthrough FilmGrainWorkbench)
    # It runs offscreen (no window on the screen): Qt's offscreen platform
    # plugin, which the app itself does not ship.
    get_target_property(fgw_qmake Qt6::qmake IMPORTED_LOCATION)
    get_filename_component(fgw_qt_root ${fgw_qmake} DIRECTORY)
    add_custom_command(TARGET fgw_walkthrough POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different ${fgw_qt_root}/../plugins/platforms/qoffscreen.dll
                $<TARGET_FILE_DIR:fgw_walkthrough>/platforms/qoffscreen.dll
        VERBATIM)
endif()
