# Builds vvencapp and vvdecapp for the VVC grain analyzer and copies them to ..\bin.
#
# VVenC: fraunhoferhhi/vvenc pull request 765 (film grain analysis --fga 2 and 3),
#        which contains pull request 764 (--fga 1, the VTM JVET-AN0237 port).
# vvdec: fraunhoferhhi/vvdec master; its film grain synthesis handles both FGC models.
# Needs git, CMake and Visual Studio 2022 (or its Build Tools).
param(
    [string]$WorkDir = (Join-Path $env:LOCALAPPDATA "afgs1wb-vvc-build"),
    [string]$VvencRef = "6b212fd",     # head of pull request 765
    [string]$VvdecRef = "6f6bb29"      # vvdec master when this was written
)
# Not $ErrorActionPreference = "Stop": Windows PowerShell turns any line a program
# writes to stderr (CMake's warnings) into an error. Exit codes are checked instead.
$bin = Join-Path $PSScriptRoot "..\bin"
New-Item -ItemType Directory -Force $WorkDir, $bin | Out-Null

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

function Build($name, $url, $fetch, $ref, $target) {
    $src = Join-Path $WorkDir $name
    if (-not (Test-Path $src)) { git clone -q $url $src; Check "git clone $url" }
    git -C $src fetch -q origin $fetch; Check "git fetch $fetch"
    git -C $src checkout -q $ref; Check "git checkout $ref"
    cmake -S $src -B "$src\build-msvc" -G "Visual Studio 17 2022" -A x64 2>&1 | Out-Null
    Check "CMake configure of $name"
    cmake --build "$src\build-msvc" --config Release --target $target -- /m /v:minimal
    Check "build of $target"
    Copy-Item "$src\bin\release-static\$target.exe" $bin -Force -ErrorAction Stop
}

Build "vvenc" "https://github.com/fraunhoferhhi/vvenc.git" "pull/765/head" $VvencRef "vvencapp"
Build "vvdec" "https://github.com/fraunhoferhhi/vvdec.git" "master" $VvdecRef "vvdecapp"
Write-Host "vvencapp.exe and vvdecapp.exe are in $bin"
