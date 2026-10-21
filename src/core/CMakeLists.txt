# fgw_core: the engine, without Qt. Grain parameters and their AFGS1 coding,
# grain synthesis, the display conversion, VVC packets, Matroska writing.
add_library(fgw_core STATIC
    src/afgs1.cpp
    src/bits.cpp
    src/display.cpp
    src/gaussian.cpp
    src/grain_params.cpp
    src/mkv_writer.cpp
    src/picture.cpp
    src/synth.cpp
    src/tuning.cpp
    src/vvc.cpp)
target_include_directories(fgw_core PUBLIC include)
target_link_libraries(fgw_core PUBLIC fgw_platform nlohmann_json PRIVATE fgw_compile_options)
