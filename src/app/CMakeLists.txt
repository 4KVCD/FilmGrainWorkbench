# FilmGrainWorkbench: the window, with Qt 6 Widgets. Built into
# build/cmake/src/app/<config> with Qt's DLLs (windeployqt) and FFmpeg's beside it.
find_package(Qt6 6.5 REQUIRED COMPONENTS Widgets)
qt_standard_project_setup()

add_library(fgw_app STATIC
    av1_frames.cpp
    av1_tab.cpp
    av1_write.cpp
    dialogs.cpp
    frame_pane.cpp
    job.cpp
    plots.cpp
    preview.cpp
    settings.cpp
    theme.cpp
    vvc_scene.cpp
    vvc_tab.cpp
    workbench.cpp
    resources/app.qrc)
target_include_directories(fgw_app PUBLIC .)
target_link_libraries(fgw_app PUBLIC fgw_core Qt6::Widgets PRIVATE fgw_compile_options)
target_compile_definitions(fgw_app PUBLIC FGW_VERSION="${PROJECT_VERSION}")

# The program: main.cpp, its icon and version (app.rc).
configure_file(resources/app.rc.in ${CMAKE_CURRENT_BINARY_DIR}/app.rc @ONLY)
qt_add_executable(FilmGrainWorkbench WIN32 main.cpp ${CMAKE_CURRENT_BINARY_DIR}/app.rc)
target_link_libraries(FilmGrainWorkbench PRIVATE fgw_app fgw_compile_options)
fgw_copy_ffmpeg(FilmGrainWorkbench)

# Qt's DLLs and plugins beside the program, so it runs from the build folder.
get_target_property(fgw_qmake Qt6::qmake IMPORTED_LOCATION)
get_filename_component(fgw_qt_bin ${fgw_qmake} DIRECTORY)
add_custom_command(TARGET FilmGrainWorkbench POST_BUILD
    COMMAND ${fgw_qt_bin}/windeployqt.exe --no-translations --no-system-d3d-compiler --no-opengl-sw
            --no-compiler-runtime --no-system-dxc-compiler --no-network --verbose 0 $<TARGET_FILE:FilmGrainWorkbench>
    VERBATIM)
