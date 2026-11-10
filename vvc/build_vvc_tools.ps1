# Builds vvencapp and vvdecapp for the VVC grain analyzer and copies them to ..\bin.
#
# VVenC: commit 1eb07fe0, master of the 4KVCD fork: fraunhoferhhi/vvenc master
#        a455d51e (--fga 1 to 3, pull requests 764 and 765) with the 4KVCD pull
#        requests not merged yet, among them the film grain analysis fixes (every
#        shot analysed inside the shot, no grain on clipped highlights, black bars
#        or clipped blacks; for --fga 2 and 3 no coarse grain on fast motion, more
#        grain on Blu-ray sources whose grain repeats, grain on bright areas with no
#        flat blocks, --fga 3's filter coefficients in [-128, 127] so FFmpeg decodes
#        the streams, and the fit of each picture's grain to the grain its coding
#        lost); plus vvenc-fga-analysis-only.patch (this folder): --fga 4, the three
#        modes' analyses without coding, for adding the grain to an encode made
#        elsewhere. --fga 4 codes nothing, so its grain is the analyzers' own, not
#        fitted to any coding's loss. -VvencSource takes another clone holding them.
# vvdec: fraunhoferhhi/vvdec master (with its film grain fixes, pull requests 414
#        and 449), plus pull request 450 (film grain for 4:2:2, 4:4:4 and 4:0:0;
#        4:2:0 unchanged), not merged yet.
# Needs git, CMake and Visual Studio 2022 (or its Build Tools).
param(
    [string]$WorkDir = (Join-Path $env:LOCALAPPDATA "afgs1wb-vvc-build"),
    [string]$VvencSource = "https://github.com/4KVCD/vvenc.git",
    [string]$VvencBranch = "master",
    [string]$VvencRef = "1eb07fe0",    # the fork's master (2026-11-15)
    [string]$VvdecRef = "6f6bb29",     # vvdec master (2026-10-20)
    [string]$VvdecPatch = "428097930", # pull request 450's own commit (its first is 449, in master)
    [string]$VvencLocalPatch = "vvenc-fga-analysis-only.patch"
)
# Not $ErrorActionPreference = "Stop": Windows PowerShell turns any line a program
# writes to stderr (CMake's warnings) into an error. Exit codes are checked instead.
$bin = Join-Path $PSScriptRoot "..\bin"
New-Item -ItemType Directory -Force $WorkDir, $bin | Out-Null

function Check($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

# Checks out `ref` (from `fetch` of `url`), applies the commits of `patches`
# (from `patch_fetch`) and the patch files `local_patches` (in this folder) to
# the working tree without committing, and builds `target`.
function Build($name, $url, $fetch, $ref, $target, $patch_fetch = $null, $patches = @(), $local_patches = @()) {
    $src = Join-Path $WorkDir $name
    if (-not (Test-Path $src)) { git clone -q $url $src; Check "git clone $url" }
    git -C $src fetch -q $url $fetch; Check "git fetch $fetch from $url"
    if ($patch_fetch) { git -C $src fetch -q $url $patch_fetch; Check "git fetch $patch_fetch from $url" }
    git -C $src checkout -q -f $ref; Check "git checkout $ref"  # -f: drops patches applied by an earlier run
    foreach ($patch in $patches) {
        git -C $src cherry-pick --no-commit $patch; Check "applying $patch"
    }
    foreach ($patch in $local_patches) {
        git -C $src apply (Join-Path $PSScriptRoot $patch); Check "applying $patch"
    }
    # A build folder per ref and patches: reusing one across refs mixed objects
    # built against old headers into the new program (a vvencapp that corrupted
    # its heap on every encode).
    $names = @($ref) + $patches + ($local_patches | ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_) })
    $build = Join-Path $src ("build-" + ($names -join "-"))
    cmake -S $src -B $build -G "Visual Studio 17 2022" -A x64 2>&1 | Out-Null
    Check "CMake configure of $name"
    cmake --build $build --config Release --target $target -- /m /v:minimal
    Check "build of $target"
    Copy-Item "$src\bin\release-static\$target.exe" $bin -Force -ErrorAction Stop
}

Build "vvenc" $VvencSource $VvencBranch $VvencRef "vvencapp" $null @() @($VvencLocalPatch)
Build "vvdec" "https://github.com/fraunhoferhhi/vvdec.git" "master" $VvdecRef "vvdecapp" "pull/450/head" @($VvdecPatch)
Write-Host "vvencapp.exe and vvdecapp.exe are in $bin"
