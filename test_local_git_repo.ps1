param(
    [string]$Python = "D:\Downloads\miniconda\envs\handeye\python.exe"
)

$ErrorActionPreference = "Stop"

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$TestDir = Join-Path $env:TEMP "behavior_segment_git_test_$Stamp"

Write-Host "Source: $SourceDir"
Write-Host "Test repo: $TestDir"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found in PATH"
}

if (-not (Test-Path $Python)) {
    throw "Python not found: $Python"
}

New-Item -ItemType Directory -Force -Path $TestDir | Out-Null

$ExcludeDirs = @(".git", "__pycache__", ".vis_test")
$ExcludeFiles = @("*.mp4", "*.part", "*.pyc")

Get-ChildItem -Path $SourceDir -Force | ForEach-Object {
    if ($_.PSIsContainer) {
        if ($ExcludeDirs -contains $_.Name) {
            return
        }
        Copy-Item -Path $_.FullName -Destination $TestDir -Recurse -Force
    } else {
        foreach ($Pattern in $ExcludeFiles) {
            if ($_.Name -like $Pattern) {
                return
            }
        }
        Copy-Item -Path $_.FullName -Destination $TestDir -Force
    }
}

Set-Location $TestDir

@"
__pycache__/
*.pyc
.vis_test/
*.part
*.mp4
"@ | Set-Content -Path ".gitignore" -Encoding UTF8

Write-Host "`n== Python import check =="
& $Python -B -c "import sys; sys.path.insert(0, '.'); import video_annotation_v12, vis_segment; print('import ok')"

Write-Host "`n== Git init/check =="
git init
git branch -M main
git add .
git status --short

$OldName = git config user.name
$OldEmail = git config user.email
if (-not $OldName) {
    git config user.name "Behavior Segment Test"
}
if (-not $OldEmail) {
    git config user.email "behavior-segment-test@example.com"
}

git commit -m "Test behavior segment standalone repo"

Write-Host "`nLocal standalone git repo test succeeded."
Write-Host "Temporary repo kept at:"
Write-Host "  $TestDir"
Write-Host "`nTo remove it later:"
Write-Host "  Remove-Item -Recurse -Force `"$TestDir`""
