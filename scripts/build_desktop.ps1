param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$Diagnostic
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot $Python
$BuildName = if ($Diagnostic) { "FeedbackInsightHubDiagnostic" } else { "FeedbackInsightHub" }
$WindowMode = if ($Diagnostic) { "--console" } else { "--windowed" }
$PipelineSourceFiles = @(
    "feedback\analysis_adapters.py",
    "feedback\analysis_input.py",
    "feedback\analysis_worker.py",
    "feedback\background_analysis.py",
    "feedback\local_service.py",
    "feedback\models.py",
    "feedback\text_pipeline.py",
    "feedback\ai_snapshot_service.py",
    "feedback\ai_stage_service.py",
    "feedback\ai_statistics_service.py",
    "feedback\ai_text_service.py",
    "feedback\ai_synthesis_service.py"
)
$PipelineDataArgs = @()
foreach ($SourceFile in $PipelineSourceFiles) {
    $SourcePath = Join-Path $ProjectRoot $SourceFile
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "找不到分析管線來源：$SourcePath"
    }
    $PipelineDataArgs += "--add-data"
    $PipelineDataArgs += "$SourcePath;feedback"
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "找不到專案 Python：$PythonPath"
}

& $PythonPath -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "尚未安裝 PyInstaller。請先在已確認的專案環境安裝桌面打包依賴。"
}

Push-Location $ProjectRoot
try {
    & $PythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        $WindowMode `
        --name $BuildName `
        --paths $ProjectRoot `
        --specpath (Join-Path $ProjectRoot "build") `
        --collect-data feedback `
        --collect-all dearpygui `
        @PipelineDataArgs `
        --hidden-import config.settings `
        --hidden-import accounts `
        --hidden-import accounts.apps `
        --hidden-import accounts.models `
        --hidden-import feedback `
        --hidden-import feedback.apps `
        --hidden-import feedback.models `
        --hidden-import desktop_app.app `
        --hidden-import feedback.analysis_adapters `
        --hidden-import feedback.background_analysis `
        --hidden-import feedback.analysis_jobs `
        --hidden-import feedback.analysis_worker `
        --hidden-import feedback.ai_worker `
        --hidden-import feedback.ai_stage_service `
        "desktop_app\__main__.py"
    if ($LASTEXITCODE -ne 0) {
        throw "EXE 打包失敗。"
    }
}
finally {
    Pop-Location
}

Write-Host "完成：dist\$BuildName\$BuildName.exe"
