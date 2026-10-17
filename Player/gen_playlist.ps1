#requires -Version 5.1
# 仅支持 WebDAV 的 HTTP(S) URL；文件夹不递归。输出 UTF-8 .m3u8，供 IINA/VLC 使用。
# 用法：修改配置后运行 .\gen_playlist.ps1。Python 核心保存在本脚本同目录。

$SCRIPT_VERSION = '0.1.0'

# ===== 配置开始 =====
$CORE_URL = 'https://raw.githubusercontent.com/GZhonghui/Tools/refs/heads/main/Player/gen_playlist_core.py'  # gen_playlist_core.py 的 HTTP(S) 直链；留空则使用同目录已有文件
# 先调用环境激活函数，再执行 $PYTHON_BIN；以函数返回后此变量的值为准。
# 填解释器绝对路径时固定使用该解释器；填 python 时按激活后的 PATH 查找。
$PYTHON_BIN = 'python'  # Python 3.10+；也可填写 python.exe 的完整路径（不要附加参数）
# 文件和文件夹 URL 可以混合填写，核心脚本会自动识别，无需额外标记。
# 依据 WebDAV PROPFIND 返回的 resourcetype/collection 属性判断；文件夹只读取当前层。
$WEBDAV_URLS = @(
    'http://127.0.0.1:8081/upload/downloaded_video/'
    # 'https://user:password@example.com/dav/video.mp4'
)
$OUTPUT_DIR = Join-Path $HOME 'Downloads'
$OUTPUT_NAME = 'playlist.m3u8'  # 可以省略 .m3u8
$TIMEOUT_SECONDS = 30
$DOWNLOAD_TIMEOUT_SECONDS = 60

function Activate-PythonEnvironment {
    # 按需自行填写；默认不激活环境，直接使用 PYTHON_BIN。
    # 例如：. 'C:\path\to\venv\Scripts\Activate.ps1'
}
# ===== 配置结束 =====

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Write-Host "gen_playlist.ps1 版本：$SCRIPT_VERSION"
$corePath = Join-Path $PSScriptRoot 'gen_playlist_core.py'
$temporaryPath = $null
$exitCode = 1

try {
    if (-not [string]::IsNullOrWhiteSpace($CORE_URL)) {
        $downloadUri = $null
        if (-not [Uri]::TryCreate($CORE_URL, [UriKind]::Absolute, [ref]$downloadUri) -or
            $downloadUri.Scheme -notin @('http', 'https')) {
            throw 'CORE_URL 必须是 HTTP(S) 直链。'
        }
        $temporaryPath = Join-Path $PSScriptRoot ('.gen_playlist_core.' + [Guid]::NewGuid().ToString('N'))
        # Windows PowerShell 5.1 也使用 TLS 1.2。
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Write-Host '正在更新 Python 核心脚本……'
        Invoke-WebRequest -UseBasicParsing -Uri $downloadUri -OutFile $temporaryPath `
            -TimeoutSec $DOWNLOAD_TIMEOUT_SECONDS -MaximumRedirection 5
    }
    elseif (-not (Test-Path -LiteralPath $corePath -PathType Leaf)) {
        throw '请填写 CORE_URL，或将 gen_playlist_core.py 放到本脚本同目录。'
    }

    # 点调用使函数内对 PYTHON_BIN 等变量的修改也在后续调用中生效。
    . Activate-PythonEnvironment
    if (-not (Get-Command -Name $PYTHON_BIN -CommandType Application -ErrorAction SilentlyContinue)) {
        throw '找不到 PYTHON_BIN 指定的解释器，请修改配置或环境激活函数。'
    }

    # 使用选定的解释器检查下载内容，再原子替换旧文件。
    # 用非空占位符兼容 Windows PowerShell 5.1 传递空字符串参数的行为。
    $candidate = if ($temporaryPath) { $temporaryPath } else { '-' }
    $validate = @'
import os, sys
from pathlib import Path
if sys.version_info < (3, 10):
    raise SystemExit('需要 Python 3.10 或更新版本。')
if sys.argv[1] != '-':
    source = Path(sys.argv[1])
    data = source.read_bytes()
    if not data.strip():
        raise SystemExit('下载的核心脚本为空。')
    compile(data, 'gen_playlist_core.py', 'exec')
    os.replace(source, sys.argv[2])
'@
    & $PYTHON_BIN -c $validate $candidate $corePath
    if ($LASTEXITCODE -ne 0) { throw 'Python 检查或更新失败，未运行核心脚本。' }

    $coreArgs = @($corePath, '--output-dir', $OUTPUT_DIR, '--name', $OUTPUT_NAME,
                  '--timeout', [string]$TIMEOUT_SECONDS, '--') + @($WEBDAV_URLS)
    & $PYTHON_BIN @coreArgs
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Error ('运行失败：' + $_.Exception.Message) -ErrorAction Continue
}
finally {
    if ($temporaryPath -and (Test-Path -LiteralPath $temporaryPath)) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}
exit $exitCode
