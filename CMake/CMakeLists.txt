cmake_minimum_required(VERSION 3.10)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED True)

set(CMAKE_BUILD_TYPE Release)

project(reEngine)

set(sourDir ${PROJECT_SOURCE_DIR}/Source)
set(inclDir ${PROJECT_SOURCE_DIR}/Include)
set(suppDir ${PROJECT_SOURCE_DIR}/Support)

set(sourceFiles )
set(sourceFiles ${sourceFiles};${sourDir}/FFT/FFT.cpp)

add_library(FFT STATIC ${sourceFiles})

target_include_directories(FFT PRIVATE ${suppDir}/Include;${inclDir}/FFT)

target_compile_definitions(FFT PRIVATE NO_MIN_MAX;_CRT_SECURE_NO_WARNINGS)

set_target_properties(FFT PROPERTIES ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/Lib)

set(sourceFiles )
set(sourceFiles ${sourceFiles};${sourDir}/reEngine/Main.cpp)
set(sourceFiles ${sourceFiles};${sourDir}/reEngine/Support.cpp)

add_executable(reEngine ${sourceFiles})

target_link_directories(reEngine PRIVATE ${suppDir}/Lib;${CMAKE_BINARY_DIR}/Lib)

target_link_libraries(reEngine PRIVATE FFT)
target_link_libraries(reEngine PRIVATE glfw3)
target_link_libraries(reEngine PRIVATE assimp)

target_include_directories(reEngine PRIVATE ${suppDir}/Include;${inclDir})

target_compile_definitions(reEngine PRIVATE NO_MIN_MAX;_CRT_SECURE_NO_WARNINGS)

set_target_properties(reEngine PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/Bin)
