# Third-party code

Vendored so that a build needs no download; copied from VideoMetricsLab's
C++ port (`cpp/third_party`), where each was downloaded from its project and
checked against the SHA-256 it publishes.

| Library | Version | Used for | Licence |
| --- | --- | --- | --- |
| [GoogleTest](https://github.com/google/googletest) | 1.18.0 | The tests (not shipped) | BSD 3-Clause (`googletest/LICENSE`) |
| [nlohmann/json](https://github.com/nlohmann/json) | 3.12.0 | Reading ffprobe's output and the saved analyses; the tests' reference files | MIT (`nlohmann/LICENSE.MIT`) |

Only what a build needs is kept: of GoogleTest, the `googletest` folder's
`CMakeLists.txt`, `cmake`, `include` and `src`; of nlohmann/json, the single
header and its licence.
