# fgw_platform: starting programs and reading what they write (Windows).
add_library(fgw_platform STATIC src/process.cpp src/process_windows.cpp)
target_include_directories(fgw_platform PUBLIC include)
target_link_libraries(fgw_platform PRIVATE fgw_compile_options)
