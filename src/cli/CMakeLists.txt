# fga-inject: VVenC's film grain analysis rerun on an existing VVC encode,
# its FGC messages replaced, written as MKV (fga_inject.cpp). No Qt.
add_executable(fga-inject fga_inject.cpp)
target_link_libraries(fga-inject PRIVATE fgw_core fgw_compile_options)
fgw_copy_ffmpeg(fga-inject)
