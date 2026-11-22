# Builds the vvencapp that fga-inject runs, from a VVenC source tree as it is
# on disk (uncommitted changes included), plus vvenc-fga4-local-master.patch
# (this folder): --fga 4 --fgastacked 1, the three modes' analyses without
# coding, with --fga 2's and 3's sets fitted to the grain a decoded encode lost.
# The patch is the commit 403fc076 on the 4KVCD local master 066f71fb.
#
# The tree is only read: it is mirrored into a staging folder, the patch is
# applied there, and the files whose contents changed are copied into the
# folder that is built, so a rebuild after an analyzer change compiles only
# what changed. Builds with every core at normal priority.
#
#   vvc\build_fga_tool_vvencapp.ps1 -VvencTree C:\w\fgc [-Out <exe>]
#
# Needs git, CMake and Visual Studio 2022 (or its Build Tools).
param(
    [Parameter(Mandatory)] [string]$VvencTree,
    [string]$Out = "",
    [string]$WorkDir = "",
    [string]$Patch = (Join-Path $PSScriptRoot "vvenc-fga4-local-master.patch")
)
# Not $ErrorActionPreference = "Stop": Windows PowerShell turns any line a program
# writes to stderr (CMake's warnings) into an error. Exit codes are checked instead.
function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

$tree = (Resolve-Path $VvencTree -ErrorAction Stop).Path.TrimEnd('\')
$name = Split-Path $tree -Leaf
$root = Join-Path $env:LOCALAPPDATA "fgw-fga-tool"
if (-not $WorkDir) { $WorkDir = Join-Path $root "build-$name" }
if (-not $Out) { $Out = Join-Path $root "vvencapp-$name.exe" }
$staging = Join-Path $WorkDir "staging"
$src = Join-Path $WorkDir "src"
$build = Join-Path $WorkDir "build"
New-Item -ItemType Directory -Force $staging, $src, (Split-Path $Out) | Out-Null

# 1. The tree as it is, without its git data and build output (excluded by full
#    path: robocopy's bare names would match source\Lib too). Exit codes below 8
#    are robocopy's successes.
robocopy $tree $staging /MIR /XD "$tree\.git" "$tree\build" "$tree\bin" "$tree\lib" /XF "$tree\.git" `
    /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying $tree failed (robocopy exit code $LASTEXITCODE)" }
if (-not (Test-Path (Join-Path $staging "source\Lib\vvenc"))) { throw "$tree is not a VVenC source tree" }

# 2. The patch. The tree's files may have CRLF line ends where the patch has LF:
#    --ignore-whitespace matches them (lines it adds keep the patch's LF, which
#    the compiler does not mind). Run outside any repository, so git applies
#    to the files as they are.
Push-Location $staging
try {
    git apply --ignore-whitespace $Patch; Check "applying $(Split-Path $Patch -Leaf) to a copy of $tree"
} finally {
    Pop-Location
}

# 3. Into the built folder, only the files whose contents differ; files gone
#    from the tree go from there too (VVenC writes its programs into the
#    source's bin and lib folders: those stay).
$copied = 0
$kept = @{}
foreach ($file in Get-ChildItem $staging -Recurse -File) {
    $relative = $file.FullName.Substring($staging.Length + 1)
    $kept[$relative] = $true
    $target = Join-Path $src $relative
    # Unchanged: same size and time (robocopy and Copy-Item keep a file's time), or, for the files the patch
    # rewrote in staging this run, the same contents.
    $built = Get-Item $target -ErrorAction SilentlyContinue
    if ($built -and $built.Length -eq $file.Length -and ($built.LastWriteTimeUtc -eq $file.LastWriteTimeUtc -or
        (Get-FileHash $target -Algorithm MD5).Hash -eq (Get-FileHash $file.FullName -Algorithm MD5).Hash)) {
        continue
    }
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    Copy-Item $file.FullName $target -Force
    $copied++
}
$removed = 0
foreach ($file in Get-ChildItem $src -Recurse -File) {
    $relative = $file.FullName.Substring($src.Length + 1)
    if ($relative -match '^(bin|lib)\\' -or $kept.ContainsKey($relative)) { continue }
    Remove-Item $file.FullName -Force
    $removed++
}
Write-Host "$copied files changed, $removed removed since the last build"

# 4. The build, on every core (configured once: the build re-runs CMake itself when a CMakeLists changes);
#    /nr:false: no MSBuild nodes left running after it.
if (-not (Test-Path (Join-Path $build "CMakeCache.txt"))) {
    cmake -S $src -B $build -G "Visual Studio 17 2022" -A x64 2>&1 | Out-Null
    Check "CMake configure"
}
cmake --build $build --config Release --target vvencapp --parallel -- /m /nr:false /v:minimal
Check "build of vvencapp"
Copy-Item (Join-Path $src "bin\release-static\vvencapp.exe") $Out -Force -ErrorAction Stop
Write-Host "vvencapp for fga-inject: $Out"
