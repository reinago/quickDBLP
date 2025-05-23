cmake_minimum_required(VERSION 3.7...3.23)

# vcpkg
include(FetchContent)
mark_as_advanced(FORCE
  FETCHCONTENT_BASE_DIR
  FETCHCONTENT_FULLY_DISCONNECTED
  FETCHCONTENT_QUIET
  FETCHCONTENT_UPDATES_DISCONNECTED)

# Require git for download
find_package(Git REQUIRED)

FetchContent_Declare(vcpkg-download
  GIT_REPOSITORY https://github.com/microsoft/vcpkg.git
  GIT_TAG 2025.04.09 # Update default-registry baseline in vcpkg-configuration.json when changing!
  GIT_SHALLOW TRUE)
FetchContent_GetProperties(vcpkg-download)
if (NOT vcpkg-download_POPULATED)
  message(STATUS "Fetch vcpkg ...")
  FetchContent_Populate(vcpkg-download)
  mark_as_advanced(FORCE
    FETCHCONTENT_SOURCE_DIR_VCPKG-DOWNLOAD
    FETCHCONTENT_UPDATES_DISCONNECTED_VCPKG-DOWNLOAD)
endif ()

# option(X_VCPKG_APPLOCAL_DEPS_INSTALL "" ON) # Does currently not not work as expected, see comment on manual install below!
#set(VCPKG_OVERLAY_PORTS "${mm_empty_ports_dir}${CMAKE_CURRENT_LIST_DIR}/cmake/vcpkg_ports")
#set(VCPKG_OVERLAY_TRIPLETS "${CMAKE_CURRENT_LIST_DIR}/cmake/vcpkg_triplets") # We are using triplets with VCPKG_DISABLE_COMPILER_TRACKING set (on Windows).
set(VCPKG_BOOTSTRAP_OPTIONS "-disableMetrics") # Disable Telemetry
set(VCPKG_INSTALL_OPTIONS "--clean-after-build" "--no-print-usage") # Build dirs get quite large and are usually only needed for debugging new ports.
set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_BINARY_DIR}/_deps/vcpkg-download-src/scripts/buildsystems/vcpkg.cmake" CACHE STRING "Vcpkg toolchain file")

# hackedyhack
#set(VCPKG_TARGET_TRIPLET x64-windows-static)

project(ponder_dblp VERSION 0.1.0 DESCRIPTION "DBLP preprocessor")
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_VERBOSE_MAKEFILE ON)

# find_package(zlib CONFIG REQUIRED)
find_package(boost_iostreams CONFIG REQUIRED)


# file(GLOB_RECURSE DBLP_SRC_FILES RELATIVE "${CMAKE_CURRENT_SOURCE_DIR}" "*.cpp")

add_executable(ponder_dblp "${CMAKE_CURRENT_SOURCE_DIR}/ponder_dblp.cpp")

target_include_directories(ponder_dblp PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
# target_link_directories(starsurvey PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/lib")
target_link_libraries(ponder_dblp PRIVATE Boost::iostreams)
# add_custom_command(
#         TARGET starsurvey POST_BUILD
#         COMMAND ${CMAKE_COMMAND} -E copy_directory
#                 ${CMAKE_CURRENT_SOURCE_DIR}/res
#                 ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/res)

# Install vcpkg libraries
# X_VCPKG_APPLOCAL_DEPS_INSTALL currently does only work for windows, in addition only actually used dll's are
# installed. This is a problem for libraries that load dll's dynamically on runtime (.i.e. ospray).
# Therefore, we just copy all vcpkg libraries to our install dir, until vcpkg may has a better option in future.
if (WIN32)
  install(DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/vcpkg_installed/x64-windows/$<$<CONFIG:Debug>:debug/>bin/
    DESTINATION "${CMAKE_INSTALL_BINDIR}"
    FILES_MATCHING PATTERN "*.dll" PATTERN "*.pdb")
else ()
  install(DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/vcpkg_installed/x64-linux/$<$<CONFIG:Debug>:debug/>lib/
    DESTINATION "${CMAKE_INSTALL_LIBDIR}"
    FILES_MATCHING PATTERN "*.so*")
endif ()

# add_custom_command(
#         TARGET Lactea POST_BUILD
#         COMMAND ${CMAKE_COMMAND} -E copy_directory
#                 ${CMAKE_CURRENT_SOURCE_DIR}/res
#                 ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/res)
