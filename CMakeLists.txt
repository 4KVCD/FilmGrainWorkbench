# Film Grain Workbench: the engine (fgw_core), the processes it runs
# (fgw_platform), the window (FilmGrainWorkbench) and their tests.
#
#   cmake --preset windows -DCMAKE_PREFIX_PATH=<Qt>\6.12.0\msvc2022_64
#   cmake --build --preset windows-release
#   ctest --preset windows-release
cmake_minimum_required(VERSION 3.25)
project(FilmGrainWorkbench VERSION 1.0.0 LANGUAGES CXX)

if(NOT WIN32 OR NOT MSVC)
    message(FATAL_ERROR "Film Grain Workbench is built on Windows with MSVC (Visual Studio 2022): "
                        "cmake --preset windows")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(FGW_BUILD_TESTS "Build the tests" ON)
option(FGW_BUILD_APP "Build the window (needs Qt 6)" ON)

# The options every target of ours is compiled with. /utf-8: the sources are
# UTF-8 (the "×", "–" and "σ" the app shows). /arch:AVX2 (x64 processors from
# 2013 on): wider vectors for the pixel loops, and the fused multiply-add the
# HDR display conversion rounds with (display.cpp) as one instruction. MSVC
# fuses only where the code asks (std::fma), never on its own.
add_library(fgw_compile_options INTERFACE)
target_compile_options(fgw_compile_options INTERFACE /W4 /WX /permissive- /utf-8 /MP /arch:AVX2)
target_compile_definitions(fgw_compile_options INTERFACE NOMINMAX WIN32_LEAN_AND_MEAN)

# nlohmann/json, vendored (third_party/README.md). SYSTEM: its warnings are
# not ours to fix.
add_library(nlohmann_json INTERFACE)
target_include_directories(nlohmann_json SYSTEM INTERFACE third_party/nlohmann/include)

add_subdirectory(src/platform)
add_subdirectory(src/core)
if(FGW_BUILD_APP)
    add_subdirectory(src/app)
endif()

if(FGW_BUILD_TESTS)
    enable_testing()
    # GoogleTest, vendored (third_party/README.md), built with the same C
    # run-time library as our code (MSVC's /MD).
    set(GOOGLETEST_VERSION 1.18.0)
    set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
    add_subdirectory(third_party/googletest EXCLUDE_FROM_ALL SYSTEM)
    add_subdirectory(tests)
endif()
