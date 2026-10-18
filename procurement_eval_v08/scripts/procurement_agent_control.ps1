param(
    [ValidateSet('Start', 'Stop', 'Configure')]
    [string]$Action = 'Start',
    [string]$ConfigPath = '',
    [string]$StatePath = '',
    [switch]$NoBrowser,
    [ValidateRange(1, 600)]
    [int]$AgentStartupTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LocalDataRoot = Join-Path $env:LOCALAPPDATA 'ProcurementAgent'
if (-not $ConfigPath) { $ConfigPath = Join-Path $LocalDataRoot 'model-config.json' }
if (-not $StatePath) { $StatePath = Join-Path $LocalDataRoot 'runtime-state.json' }
$BackendUrl = 'http://127.0.0.1:8765'
$AgentUrl = 'http://127.0.0.1:8780'
$LogDirectory = Join-Path $ProjectRoot 'runtime_data\logs'

function Write-LauncherMessage([string]$Message, [string]$Color) {
    Write-Host "[采购Agent] $Message" -ForegroundColor $Color
    # 即使 Python 尚未输出或预检查失败，也保留启动器诊断；不记录模型密钥。
    try {
        if (-not (Test-Path -LiteralPath $LogDirectory)) {
            New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
        }
        Add-Content -LiteralPath (Join-Path $LogDirectory 'launcher.log') -Encoding UTF8 `
            -Value ("{0:o} {1}" -f (Get-Date), $Message)
    } catch { Write-Host '[采购Agent] 无法写入启动日志，请检查目录权限。' -ForegroundColor Yellow }
}
function Write-Info([string]$Message) { Write-LauncherMessage $Message 'Cyan' }
function Write-Ok([string]$Message) { Write-LauncherMessage $Message 'Green' }
function Write-Warn([string]$Message) { Write-LauncherMessage $Message 'Yellow' }
function Fail([string]$Message) {
    Write-LauncherMessage $Message 'Red'
    exit 1
}

function Ensure-ParentDirectory([string]$Path) {
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

function Resolve-Python311 {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) {
        Fail '未找到 Python Launcher（py.exe）。请先安装 Python 3.11，并勾选安装 py launcher。'
    }
    $output = & $launcher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        Fail '未找到 Python 3.11。请安装 Python 3.11 后重新启动。'
    }
    $python = ($output | Select-Object -Last 1).Trim()
    if (-not (Test-Path -LiteralPath $python)) {
        Fail "Python 3.11 路径无效：$python"
    }
    return $python
}

function Test-Dependency([string]$PythonExe) {
    & $PythonExe -c "import httpx, openai; assert tuple(map(int, openai.__version__.split('.')[:1])) >= (1,)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "缺少 Agent 依赖。请在项目目录运行：py -3.11 -m pip install -r requirements.txt"
    }
}

function Get-PlainText([Security.SecureString]$SecureValue) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Configure-Model {
    # 避免继承其他 PowerShell 版本的模块搜索路径后误加载不兼容的安全模块。
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    Write-Info '首次配置会把 API Key 用 Windows 当前用户 DPAPI 加密，保存到本机 LOCALAPPDATA，不写入项目。'
    Write-Host '千问示例 Base URL：https://dashscope.aliyuncs.com/compatible-mode/v1'
    Write-Host 'DeepSeek 示例 Base URL：https://api.deepseek.com'
    $baseUrl = (Read-Host '请输入模型 Base URL').Trim()
    $modelName = (Read-Host '请输入模型名称（例如 qwen-plus 或 deepseek-chat）').Trim()
    $secureKey = Read-Host '请输入模型 API Key（输入内容不会显示）' -AsSecureString
    if (-not $baseUrl -or -not $modelName -or $secureKey.Length -eq 0) {
        Fail 'Base URL、模型名称和 API Key 都不能为空。'
    }
    try {
        $uri = [Uri]$baseUrl
        if ($uri.Scheme -notin @('http', 'https') -or -not $uri.Host) { throw 'invalid' }
    } catch {
        Fail '模型 Base URL 格式无效。'
    }
    Ensure-ParentDirectory $ConfigPath
    $config = [ordered]@{
        BaseUrl = $baseUrl.TrimEnd('/')
        ModelName = $modelName
        ProtectedApiKey = ConvertFrom-SecureString $secureKey
        SavedAt = (Get-Date).ToUniversalTime().ToString('o')
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
    Write-Ok "模型配置已安全保存：$ConfigPath"
    Write-Host '以后直接双击“启动采购Agent.bat”即可。'
}

function Import-ModelConfig {
    if ($env:AGENT_MODEL_API_KEY -and $env:AGENT_MODEL_BASE_URL -and $env:AGENT_MODEL_NAME) {
        Write-Info '使用当前进程已有的模型环境变量。'
        return
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Fail '尚未配置模型。请先双击项目根目录的“配置采购Agent模型.bat”。'
    }
    try {
        Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
        $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
        if (-not $config.BaseUrl -or -not $config.ModelName -or -not $config.ProtectedApiKey) {
            throw 'missing fields'
        }
        $secureKey = ConvertTo-SecureString $config.ProtectedApiKey
        $plainKey = Get-PlainText $secureKey
        if (-not $plainKey) { throw 'empty key' }
        $env:AGENT_MODEL_API_KEY = $plainKey
        $env:AGENT_MODEL_BASE_URL = [string]$config.BaseUrl
        $env:AGENT_MODEL_NAME = [string]$config.ModelName
        $plainKey = $null
    } catch {
        Fail "无法加载本机模型配置（$($_.Exception.GetType().Name)）。请检查 Windows 安全模块或重新运行配置脚本；不会覆盖已有配置。"
    }
}

function Test-BackendReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$BackendUrl/requests" -Headers @{ Authorization = 'Bearer demo-agent' } -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Test-AgentReady {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$AgentUrl/api/session" -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Test-PortOpen([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(600) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Wait-Ready([scriptblock]$Probe, [string]$ServiceName, [int]$Seconds = 30, $ChildProcess = $null) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $nextProgress = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if ($ChildProcess) {
            $ChildProcess.Refresh()
            if ($ChildProcess.HasExited) {
                Write-Warn "$ServiceName 进程提前退出（退出码 $($ChildProcess.ExitCode)），请查看 $LogDirectory 中的 out/err 日志。"
                return $false
            }
        }
        if (& $Probe) { return $true }
        if ((Get-Date) -ge $nextProgress) {
            Write-Info "$ServiceName 仍在初始化（包括检索模型预热），启动日志：$LogDirectory。"
            $nextProgress = (Get-Date).AddSeconds(10)
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warn "$ServiceName 未在 $Seconds 秒内就绪，请查看 $LogDirectory。"
    return $false
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) { return $null }
    try { return Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json }
    catch {
        Write-Warn '启动状态文件损坏，将忽略；不会据此关闭任何进程。'
        return $null
    }
}

function Save-State($BackendPid, $AgentPid, [string]$PythonExe) {
    Ensure-ParentDirectory $StatePath
    $backendProcess = if ($BackendPid) { Get-Process -Id ([int]$BackendPid) -ErrorAction SilentlyContinue } else { $null }
    $agentProcess = if ($AgentPid) { Get-Process -Id ([int]$AgentPid) -ErrorAction SilentlyContinue } else { $null }
    [ordered]@{
        BackendPid = $BackendPid
        BackendStartedAt = if ($backendProcess) { $backendProcess.StartTime.ToUniversalTime().ToString('o') } else { $null }
        AgentPid = $AgentPid
        AgentStartedAt = if ($agentProcess) { $agentProcess.StartTime.ToUniversalTime().ToString('o') } else { $null }
        PythonExe = $PythonExe
        ProjectRoot = $ProjectRoot
        SavedAt = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Stop-OwnedProcess($PidValue, $ExpectedStartedAt, [string]$ExpectedScript, [string]$Label, [string]$PythonExe) {
    if (-not $PidValue) {
        Write-Warn "$Label 不是由本启动器启动，未执行关闭。"
        return
    }
    $process = Get-Process -Id ([int]$PidValue) -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Info "$Label 已经停止。"
        return
    }

    # 优先核对完整命令行；受限 Windows 环境无法读取 WMI 时，退回核对状态文件记录的 Python 路径。
    $commandProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PidValue" -ErrorAction SilentlyContinue
    $command = if ($commandProcess) { [string]$commandProcess.CommandLine } else { '' }
    $expectedPath = Join-Path $ProjectRoot $ExpectedScript
    $commandMatches = $command -and ($command.Contains($expectedPath) -or $command.Contains($ExpectedScript))
    $pythonMatches = $PythonExe -and $process.Path -and (
        [string]::Equals([IO.Path]::GetFullPath($process.Path), [IO.Path]::GetFullPath($PythonExe), [StringComparison]::OrdinalIgnoreCase)
    )
    $startMatches = $false
    if ($ExpectedStartedAt) {
        try {
            $expectedStart = [DateTimeOffset]::Parse([string]$ExpectedStartedAt).UtcDateTime
            $startMatches = [Math]::Abs(($process.StartTime.ToUniversalTime() - $expectedStart).TotalMilliseconds) -lt 10
        } catch { $startMatches = $false }
    }
    if (-not $commandMatches -and -not ($pythonMatches -and $startMatches)) {
        Write-Warn "$Label 的 PID 已被其他程序占用，为避免误杀未关闭该进程。"
        return
    }
    Stop-Process -Id ([int]$PidValue) -Force
    Write-Ok "$Label 已关闭。"
}

function Stop-Services {
    $state = Read-State
    if (-not $state) {
        Write-Warn '未找到由一键启动器记录的后台进程，不会按端口强制结束未知程序。'
        if ((Test-AgentReady) -or (Test-BackendReady)) {
            Write-Warn '检测到服务仍在运行；请关闭原启动窗口，或确认其来源后手动结束。'
        }
        return
    }
    Stop-OwnedProcess $state.AgentPid $state.AgentStartedAt 'agent_server.py' '采购 Agent' $state.PythonExe
    Stop-OwnedProcess $state.BackendPid $state.BackendStartedAt 'backend.py' '采购模拟器' $state.PythonExe
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 700
    if ((Test-AgentReady) -or (Test-BackendReady)) {
        Write-Warn '仍检测到服务端口存活，可能存在并非由本启动器创建的实例。'
    } else {
        Write-Ok '采购 Agent 与模拟器均已停止。'
    }
}

function Start-Services {
    foreach ($required in @('backend.py', 'agent_server.py', 'agent.html', 'demo_test.sqlite', 'requirements.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $required))) {
            Fail "缺少必要文件：$required"
        }
    }
    $python = Resolve-Python311
    Test-Dependency $python
    Import-ModelConfig
    if (-not (Test-Path -LiteralPath $LogDirectory)) {
        New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    }
    $state = Read-State
    $backendPid = if ($state) { $state.BackendPid } else { $null }
    $agentPid = if ($state) { $state.AgentPid } else { $null }

    if (Test-BackendReady) {
        Write-Info '采购模拟器已运行，不重复启动。'
    } else {
        if (Test-PortOpen 8765) {
            Fail '端口 8765 已被其他程序占用，且不是可识别的采购模拟器。'
        }
        Write-Info '正在启动采购模拟器（保留现有 demo_test.sqlite）…'
        $process = Start-Process -FilePath $python -ArgumentList @('-u', 'backend.py', 'demo_test.sqlite') `
            -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $LogDirectory 'backend.out.log') `
            -RedirectStandardError (Join-Path $LogDirectory 'backend.err.log')
        $backendPid = $process.Id
        $startedAt = $process.StartTime.ToUniversalTime().ToString('o')
        Save-State $backendPid $agentPid $python
        if (-not (Wait-Ready ${function:Test-BackendReady} '采购模拟器' -ChildProcess $process)) {
            Stop-OwnedProcess $backendPid $startedAt 'backend.py' '采购模拟器' $python
            Save-State $null $agentPid $python
            Fail '采购模拟器启动失败。'
        }
        Write-Ok '采购模拟器已就绪。'
    }

    if (Test-AgentReady) {
        Write-Info '采购 Agent 已运行，不重复启动。'
    } else {
        if (Test-PortOpen 8780) {
            Fail '端口 8780 已被其他程序占用，且不是可识别的采购 Agent。'
        }
        Write-Info '正在启动采购 Agent…'
        $process = Start-Process -FilePath $python -ArgumentList @('-u', 'agent_server.py') `
            -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $LogDirectory 'agent.out.log') `
            -RedirectStandardError (Join-Path $LogDirectory 'agent.err.log')
        $agentPid = $process.Id
        $startedAt = $process.StartTime.ToUniversalTime().ToString('o')
        # 预热前就保存归属，启动失败时仍能安全关闭本启动器创建的进程。
        Save-State $backendPid $agentPid $python
        if (-not (Wait-Ready ${function:Test-AgentReady} '采购 Agent' -Seconds $AgentStartupTimeoutSeconds -ChildProcess $process)) {
            Stop-OwnedProcess $agentPid $startedAt 'agent_server.py' '采购 Agent' $python
            Save-State $backendPid $null $python
            Fail '采购 Agent 启动失败。'
        }
        Write-Ok '采购 Agent 已就绪。'
    }

    Save-State $backendPid $agentPid $python
    if (-not $NoBrowser) {
        Write-Info '正在打开 Agent 和采购模拟器页面…'
        try { Start-Process $AgentUrl } catch { Write-Warn "无法自动打开 Agent，请手动访问 $AgentUrl。" }
        try { Start-Process $BackendUrl } catch { Write-Warn "无法自动打开模拟器，请手动访问 $BackendUrl。" }
    }
    Write-Ok "启动完成：$AgentUrl"
}

# 允许测试加载函数而不实际启动服务。
if ($MyInvocation.InvocationName -eq '.') { return }

try {
switch ($Action) {
    'Configure' { Configure-Model }
    'Stop' { Stop-Services }
    'Start' { Start-Services }
}
} catch { Fail "启动器执行失败：$($_.Exception.Message)；日志目录：$LogDirectory" }
