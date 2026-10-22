# Builds vvencapp and vvdecapp for the VVC grain analyzer and copies them to ..\bin.
#
# VVenC: fraunhoferhhi/vvenc pull request 765 (film grain analysis --fga 2 and 3),
#        which contains pull request 764 (--fga 1, the VTM JVET-AN0237 port), with
#        the film grain SEI written only where its parameters change and at random
#        access points, and the parameters switching exactly at scene cuts.
# vvdec: fraunhoferhhi/vvdec master (with its film grain fixes, pull requests 414
#        and 449), plus pull request 450 (film grain for 4:2:2, 4:4:4 and 4:0:0;
#        4:2:0 unchanged), not merged yet.
# Needs git, CMake and Visual Studio 2022 (or its Build Tools).
param(
    [string]$WorkDir = (Join-Path $env:LOCALAPPDATA "afgs1wb-vvc-build"),
    [string]$VvencRef = "a9fc370fe",   # head of pull request 765 (2026-10-21)
    [string]$VvdecRef = "6f6bb29",     # vvdec master (2026-10-20)
    [string]$VvdecPatch = "428097930"  # pull request 450's own commit (its first is 449, in master)
)
# Not $ErrorActionPreference = "Stop": Windows PowerShell turns any line a program
# writes to stderr (CMake's warnings) into an error. Exit codes are checked instead.
$bin = Join-Path $PSScriptRoot "..\bin"
New-Item -ItemType Directory -Force $WorkDir, $bin | Out-Null

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

# Checks out `ref` (from `fetch`), applies the commits of `patches` (from
# `patch_fetch`) to the working tree without committing, and builds `target`.
function Build($name, $url, $fetch, $ref, $target, $patch_fetch = $null, $patches = @()) {
    $src = Join-Path $WorkDir $name
    if (-not (Test-Path $src)) { git clone -q $url $src; Check "git clone $url" }
    git -C $src fetch -q origin $fetch; Check "git fetch $fetch"
    if ($patch_fetch) { git -C $src fetch -q origin $patch_fetch; Check "git fetch $patch_fetch" }
    git -C $src checkout -q -f $ref; Check "git checkout $ref"  # -f: drops patches applied by an earlier run
    foreach ($patch in $patches) {
        git -C $src cherry-pick --no-commit $patch; Check "applying $patch"
    }
    # A build folder per ref and patches: reusing one across refs mixed objects
    # built against old headers into the new program (a vvencapp that corrupted
    # its heap on every encode).
    $build = Join-Path $src ("build-" + (@($ref) + $patches -join "-"))
    cmake -S $src -B $build -G "Visual Studio 17 2022" -A x64 2>&1 | Out-Null
    Check "CMake configure of $name"
    cmake --build $build --config Release --target $target -- /m /v:minimal
    Check "build of $target"
    Copy-Item "$src\bin\release-static\$target.exe" $bin -Force -ErrorAction Stop
}

Build "vvenc" "https://github.com/fraunhoferhhi/vvenc.git" "pull/765/head" $VvencRef "vvencapp"
Build "vvdec" "https://github.com/fraunhoferhhi/vvdec.git" "master" $VvdecRef "vvdecapp" "pull/450/head" @($VvdecPatch)
Write-Host "vvencapp.exe and vvdecapp.exe are in $bin"
