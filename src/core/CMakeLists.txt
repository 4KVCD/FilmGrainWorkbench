# fgw_core: the engine, without Qt. Grain parameters and their AFGS1 coding,
# grain synthesis, the display conversion, VVC packets, probing and decoding
# with ffmpeg, the every-frame analysis, VVenC and vvdec scenes, writing MKVs.
add_library(fgw_core STATIC
    src/afgs1.cpp
    src/alignment.cpp
    src/bits.cpp
    src/demux.cpp
    src/display.cpp
    src/frames.cpp
    src/fullscan.cpp
    src/gaussian.cpp
    src/grain_params.cpp
    src/mkv_writer.cpp
    src/mux.cpp
    src/picture.cpp
    src/probe.cpp
    src/rational.cpp
    src/synth.cpp
    src/temp_folder.cpp
    src/text.cpp
    src/timeline.cpp
    src/tools.cpp
    src/tuning.cpp
    src/vvc.cpp
    src/vvc_grain.cpp)
target_include_directories(fgw_core PUBLIC include)
target_link_libraries(fgw_core PUBLIC fgw_platform nlohmann_json PRIVATE fgw_compile_options bcrypt)
# The programs build.py builds into the source tree's bin folder, which a
# build that is not packaged runs (tools.cpp, bundled_program).
target_compile_definitions(fgw_core PRIVATE FGW_SOURCE_DIR="${PROJECT_SOURCE_DIR}")

# libavformat, to read the encode's packets when writing (demux.cpp): FFmpeg's
# LGPL libraries in native/ffmpeg, linked through import libraries made from
# their exports.
set(fgw_ffmpeg ${PROJECT_SOURCE_DIR}/native/ffmpeg)
set(fgw_ffmpeg_imports)
foreach(library avformat-63 avcodec-63 avutil-61)
    set(import ${CMAKE_CURRENT_BINARY_DIR}/ffmpeg/${library}.lib)
    add_custom_command(OUTPUT ${import}
        COMMAND ${CMAKE_AR} /nologo /machine:x64 /def:${fgw_ffmpeg}/lib/${library}.def /name:${library}.dll
                /out:${import}
        DEPENDS ${fgw_ffmpeg}/lib/${library}.def
        VERBATIM)
    list(APPEND fgw_ffmpeg_imports ${import})
endforeach()
add_custom_target(fgw_ffmpeg_imports DEPENDS ${fgw_ffmpeg_imports})
add_dependencies(fgw_core fgw_ffmpeg_imports)
target_include_directories(fgw_core SYSTEM PRIVATE ${fgw_ffmpeg}/include)
target_link_libraries(fgw_core PUBLIC ${fgw_ffmpeg_imports})

# The DLLs a program linking fgw_core needs beside it: fgw_copy_ffmpeg(<target>).
set(FGW_FFMPEG_DLLS ${fgw_ffmpeg}/bin/avformat-63.dll ${fgw_ffmpeg}/bin/avcodec-63.dll
                    ${fgw_ffmpeg}/bin/avutil-61.dll ${fgw_ffmpeg}/bin/swresample-7.dll CACHE INTERNAL "")
function(fgw_copy_ffmpeg target)
    add_custom_command(TARGET ${target} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different ${FGW_FFMPEG_DLLS} $<TARGET_FILE_DIR:${target}>
        VERBATIM)
endfunction()
