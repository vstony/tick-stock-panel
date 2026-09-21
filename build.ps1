#Requires -Version 5.1
<#
.SYNOPSIS
    tick-stock-panel 一键脚本:在线更新 / 条件编译 / 本地运行。

.DESCRIPTION
    菜单式入口, 默认第 0 项 = 一键全流程(在线更新 → 条件编译 → 本地运行)。
    位置参数可直接写菜单编号: `\build.ps1 0` 等价于在菜单里按 0(不打印菜单)。

    - 在线更新: git fetch + fast-forward pull; 更新前把「已跟踪文件的本地改动」用
      git stash 暂存, 更新后自动恢复。未跟踪文件、data/、node_modules/ 等忽略项不受影响。
    - 条件编译: 后端 extras(legacy-cpu / backtest / desktop)、前端生产构建(tsc + vite
      build)、可选 PyInstaller 桌面客户端打包, 并在打包后校验 Polars 运行时完整性。
    - 本地运行: 开发模式(前后端热更新, 复用 dev.ps1)或生产模式(单端口, 后端托管
      frontend/dist)。

    远端分工(固定, 每次启动自动校对): origin = 上游仓库 shy3130(只用于 fetch/pull),
    vstony = 自己的 fork(只用于 push)。在线更新永远从上游拉, 推送变更永远推到 fork,
    并把 remote.pushDefault 设为 vstony —— 裸 `git push` 也不会误推到上游。

.PARAMETER Task
    菜单编号(位置参数, 推荐):
        0=一键全流程  1=在线更新  2=条件编译  3=本地运行  4=同步依赖
        5=运行测试    6=清理产物  7=状态检查  8=推送变更到 fork  q=退出(不执行)
    也可写任务名: menu(默认, 交互菜单) | all | update | push | build | run | deps | test | status | clean。
    编号 2/3 会直接执行该步动作(条件编译 / 本地运行, 用 -BackendExtras / -RunMode 控制),
    不会像菜单那样再弹子菜单; 需要交互改配置时直接运行 `\build.ps1`(无参数)

.PARAMETER BackendExtras
    后端可选依赖 extras(注意是 extra 名, 不是包名), 空格/逗号分隔,
    如 'legacy-cpu' / 'legacy-cpu backtest'。
    可用值以 backend/pyproject.toml 的 [project.optional-dependencies] 为准:
    legacy-cpu(旧 CPU 兼容内核) / backtest(回测引擎, 含 vectorbt) / desktop(桌面壳, 含 pywebview) / dev(测试工具)。
    传已知包名会自动纠正(如 vectorbt → backtest); 其余未知值直接报错并列出可用项。

.PARAMETER RunMode
    dev(默认, 前后端双端口热更新) | prod(单端口, 需先编译前端)。

.PARAMETER PackageDesktop
    编译阶段额外执行 PyInstaller 桌面客户端打包(较慢, 产物在 backend/dist/)。

.PARAMETER NoFrontendBuild
    跳过前端生产构建(pnpm build)。

.PARAMETER SkipDeps
    跳过依赖同步(uv sync / pnpm install)。

.PARAMETER SkipUpdate
    一键全流程中跳过在线更新。

.PARAMETER SkipRun
    一键全流程中编译完成后不启动服务。

.PARAMETER Yes
    跳过所有交互确认(自动化用)。

.PARAMETER TestPath
    仅测试任务生效, 传给 pytest 的路径/参数, 空格分隔多个, 如
    'tests/test_market_phase.py -k phase'; 留空则跑全部测试。

.EXAMPLE
    .\build.ps1
    菜单模式, 选 0 执行一键全流程。

.EXAMPLE
    .\build.ps1 0
    位置参数直达菜单第 0 项: 一键全流程(更新 → 编译 → 运行), 不打印菜单。

.EXAMPLE
    .\build.ps1 2 -BackendExtras 'legacy-cpu backtest' -PackageDesktop -Yes
    位置参数直达第 2 项(条件编译), 并指定 extras 与桌面打包。

.EXAMPLE
    .\build.ps1 3 -RunMode prod
    位置参数直达第 3 项(本地运行), 生产模式单端口启动。

.EXAMPLE
    .\build.ps1 -Task all -Yes
    等价写法(任务名): 非交互一键全流程。

.EXAMPLE
    .\build.ps1 -Task build -BackendExtras 'legacy-cpu backtest' -PackageDesktop -Yes
    条件编译: 含旧 CPU 兼容内核与回测引擎, 并打桌面客户端包。

.EXAMPLE
    .\build.ps1 -Task run -RunMode prod
    生产模式: 单端口启动, 前端由后端托管(frontend/dist)。

.EXAMPLE
    .\build.ps1 8
    把本地已提交的变更推送到自己的 fork (vstony); 上游 shy3130 不受影响。

.NOTES
    执行策略受限时先运行:
        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

    本文件必须保存为 UTF-8(带 BOM): Windows PowerShell 5.1 读无 BOM 的 .ps1 会按系统
    ANSI(中文 Windows = GBK)解码, 中文提示会乱码, 个别字节还可能破坏语法。
#>
[CmdletBinding()]
param(
    # 位置参数: 菜单编号(0-7/q) 或 任务名(menu/all/update/build/run/deps/test/status/clean)
    [ValidateSet('menu', 'all', 'update', 'push', 'build', 'run', 'deps', 'test', 'status', 'clean',
        '0', '1', '2', '3', '4', '5', '6', '7', '8', 'q')]
    [Parameter(Position = 0)]
    [string]$Task = 'menu',

    [string]$BackendExtras = '',

    [ValidateSet('dev', 'prod')]
    [string]$RunMode = 'dev',

    [switch]$PackageDesktop,

    [switch]$NoFrontendBuild,

    [switch]$SkipDeps,

    [switch]$SkipUpdate,

    [switch]$SkipRun,

    [switch]$Yes,

    [string]$TestPath = ''
)

# 不要设成 Stop: 外部命令(git/uv/pnpm)的 stderr 会被 PowerShell 转成 ErrorRecord,
# 全局 Stop 会让正常的告警输出变成终止性错误。退出码一律显式检查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'

# ============================== 路径 ==============================
$Root         = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir   = Join-Path $Root 'backend'
$FrontendDir  = Join-Path $Root 'frontend'
$EnvFile      = Join-Path $Root '.env'
$EnvExample   = Join-Path $Root '.env.example'
$VersionFile  = Join-Path $Root 'VERSION'
$DevScript    = Join-Path $Root 'dev.ps1'
$FrontendDist = Join-Path $FrontendDir 'dist'
$DesktopDist  = Join-Path $BackendDir 'dist'

# ============================== 远端仓库分工 ==============================
# 固定「从上游拉、往自己的 fork 推」, 不依赖每个人本地的 remote 配置:
#   origin → 上游仓库(只 fetch/pull)    vstony → 自己的 fork(只 push)
# 启动时校对这两个 remote 的 URL 与 remote.pushDefault(见 Ensure-Remotes),
# 避免手滑把本地改动推到上游仓库。
$UpstreamRemote = 'origin'
$UpstreamUrl    = 'https://github.com/shy3130/tick-stock-panel.git'
$ForkRemote     = 'vstony'
$ForkUrl        = 'https://github.com/vstony/tick-stock-panel.git'

# 编译配置(菜单 2 可改)
$script:Cfg = [ordered]@{
    BackendExtras     = $BackendExtras.Trim()
    SkipFrontendBuild = [bool]$NoFrontendBuild
    PackageDesktop    = [bool]$PackageDesktop
}

$script:Failed = $false
$script:GuardedOk = $true
$script:ExitMenu = $false

# 强制 UTF-8 控制台, 避免子进程(uv/pnpm/git)中文输出乱码
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
} catch {}

# ============================== 输出 ==============================
function Write-Step([string]$m) { Write-Host ''; Write-Host "[tsp] == $m ==" -ForegroundColor Cyan }
function Write-Info([string]$m) { Write-Host "[tsp] $m" -ForegroundColor DarkGray }
function Write-Ok([string]$m) { Write-Host "[tsp] $m" -ForegroundColor Green }
function Write-Warn([string]$m) { Write-Host "[tsp] $m" -ForegroundColor Yellow }
function Write-Err([string]$m) { Write-Host "[tsp] $m" -ForegroundColor Red }

function Stop-Fatal([string]$m) { throw [System.InvalidOperationException]::new($m) }

# 任务包装: 失败只中断当前任务并回到菜单, 不整个脚本退出
function Invoke-Guarded {
    param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][scriptblock]$Body)
    try {
        & $Body
        $script:GuardedOk = $true
    } catch {
        Write-Err "$Title 失败: $($_.Exception.Message)"
        Write-Info '已跳过该步骤后续动作, 可查看上方日志后重试。'
        $script:GuardedOk = $false
        $script:Failed = $true
    }
}

function Confirm-Action([string]$Message) {
    if ($Yes) { Write-Info "已自动确认: $Message (-Yes)"; return $true }
    # 输入被重定向(管道 / CI / 非交互宿主)时 Read-Host 会直接返回空串, 而空串在本函数里
    # 等价于「回车 = 同意」—— 于是一句 `echo n | .\build.ps1 8` 会静默把提交推到远端。
    # 这种环境下一律按「否」处理(宁可什么都不做), 确实要在自动化里执行请显式加 -Yes。
    if ([Console]::IsInputRedirected) {
        Write-Warn "当前输入不是交互式终端, 已按「否」处理: $Message"
        Write-Info '如需在自动化中执行, 请显式加 -Yes'
        return $false
    }
    $answer = Read-Host "$Message [Y/n]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $true }
    return (@('y', 'yes', '是') -contains $answer.Trim().ToLower())
}

function Require-Command {
    param([Parameter(Mandatory)][string]$Name, [string]$Hint = '')
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    $msg = "未找到命令 $Name"
    if ($Hint) { $msg += "。安装方式: $Hint" }
    Stop-Fatal $msg
}

# 统一的外部命令入口: 输出逐行透传到控制台(保留实时性), 退出码非 0 即失败
function Run-External {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Command,
        [string[]]$Arguments = @(),
        [string]$WorkDir = $Root
    )
    Write-Info "→ $Label"
    Push-Location $WorkDir
    try {
        & $Command @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { Stop-Fatal "$Label 失败(退出码 $code)" }
}

# ============================== .env 读取 ==============================
# 只读启动器自有键, 绝不把 .env 当 PowerShell 代码执行
function Read-DotEnvValue($Path, $Name) {
    if (-not (Test-Path $Path)) { return $null }
    $escaped = [Regex]::Escape($Name)
    foreach ($line in Get-Content $Path) {
        if ($line -match "^\s*$escaped\s*=\s*(.*?)\s*$") {
            $value = $Matches[1].Trim()
            $value = ($value -replace '\s+#.*$', '').Trim()
            if ($value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

function Get-DotEnvKeys($Path) {
    if (-not (Test-Path $Path)) { return @() }
    $keys = @()
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') { $keys += $Matches[1] }
    }
    return $keys
}

function Get-Settings {
    $host_ = Read-DotEnvValue $EnvFile 'HOST'
    $port_ = Read-DotEnvValue $EnvFile 'PORT'
    if (-not $host_) { $host_ = '0.0.0.0' }
    if (-not $port_) { $port_ = '3018' }
    $bind = $host_
    $display = if ($bind -in @('0.0.0.0', '::')) { 'localhost' } else { $bind }
    $frontPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { '3011' }
    return [ordered]@{
        Bind         = $bind
        DisplayHost  = $display
        BackendPort  = [int]$port_
        FrontendPort = [int]$frontPort
        LogLevel     = (Read-DotEnvValue $EnvFile 'LOG_LEVEL')
    }
}

function Ensure-EnvFile {
    if (Test-Path $EnvFile) { return }
    if (-not (Test-Path $EnvExample)) { Write-Warn '.env 与 .env.example 都不存在, 跳过(部分功能可能不可用)'; return }
    Copy-Item $EnvExample $EnvFile
    Write-Ok '已从 .env.example 生成 .env(密钥留空 = 免费/关闭模式, 可稍后在面板设置页填写)'
}

# ============================== 状态采集 ==============================
# 相对某个 ref 的「领先 / 落后」提交数, 返回 @(领先, 落后); 取不到时返回 @(0, 0)
function Get-AheadBehind([string]$Ref) {
    $counts = & git -C $Root rev-list --left-right --count "HEAD...$Ref" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $counts) { return @(0, 0) }
    $parts = @($counts -split '\s+' | Where-Object { $_ })
    if ($parts.Count -lt 2) { return @(0, 0) }
    return @([int]$parts[0], [int]$parts[1])
}

function Get-GitState {
    $state = [ordered]@{
        IsRepo = (Test-Path (Join-Path $Root '.git'))
        Branch = '-'
        Upstream = ''      # 上游引用(origin/<分支>): 落后多少 = 有多少更新可拉
        Ahead = 0
        Behind = 0
        Fork = ''          # fork 引用(vstony/<分支>): 领先多少 = 有多少提交待推送
        ForkAhead = 0
        ForkBehind = 0
        Tracked = 0
        Untracked = 0
    }
    if (-not $state.IsRepo) { return $state }

    $branch = & git -C $Root rev-parse --abbrev-ref HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $branch) { $state.Branch = $branch.Trim() }

    # 按「远端名/同名分支」直接比对, 不依赖分支的 upstream 跟踪配置:
    # fork 工作流里跟踪常被改成自己的 fork, 那样就看不出离上游还差多少。
    if ($state.Branch -ne '-') {
        $up = "$UpstreamRemote/$($state.Branch)"
        if (& git -C $Root rev-parse --verify --quiet "refs/remotes/$up" 2>$null) {
            $state.Upstream = $up
            $pair = Get-AheadBehind $up
            $state.Ahead = $pair[0]
            $state.Behind = $pair[1]
        }
        $fork = "$ForkRemote/$($state.Branch)"
        if (& git -C $Root rev-parse --verify --quiet "refs/remotes/$fork" 2>$null) {
            $state.Fork = $fork
            $pair = Get-AheadBehind $fork
            $state.ForkAhead = $pair[0]
            $state.ForkBehind = $pair[1]
        }
    }

    $porcelain = & git -C $Root status --porcelain 2>$null
    foreach ($line in @($porcelain)) {
        if (-not $line) { continue }
        if ($line.StartsWith('??')) { $state.Untracked++ } else { $state.Tracked++ }
    }
    return $state
}

function Get-ToolVersion([string]$Command, [string[]]$Arguments) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { return '未安装' }
    try {
        $out = & $Command @Arguments 2>&1 | Select-Object -First 1
        if ($null -eq $out) { return '未知' }
        return ([string]$out).Trim()
    } catch {
        return '未知'
    }
}

function Get-LockFingerprint {
    $files = @(
        (Join-Path $BackendDir 'uv.lock'),
        (Join-Path $BackendDir 'pyproject.toml'),
        (Join-Path $FrontendDir 'pnpm-lock.yaml'),
        (Join-Path $FrontendDir 'package.json')
    )
    $parts = @()
    foreach ($f in $files) {
        if (Test-Path $f) { $parts += (Get-FileHash $f -Algorithm SHA256).Hash } else { $parts += 'missing' }
    }
    return ($parts -join '-')
}

function Get-ListeningProcessIds([int]$Port) {
    # 老版本 Windows / 精简系统可能没有 NetTCPIP 模块, 此时直接返回空(不影响其它功能)
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return @() }
    $conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) { return @() }
    return @($conns.OwningProcess | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

# ============================== 任务: 状态 ==============================
function Show-Status {
    Write-Step '状态检查'
    $s = Get-GitState
    $cfg = Get-Settings

    Write-Host ("  仓库目录   : {0}" -f $Root)
    if ($s.IsRepo) {
        $track = "领先 {0} / 落后 {1}" -f $s.Ahead, $s.Behind
        if (-not $s.Upstream) { $track = '无本地记录(先执行在线更新)' }
        Write-Host ("  分支       : {0}{1} ({2})" -f $s.Branch, $(if ($s.Upstream) { " → $($s.Upstream)" } else { '' }), $track)
        $pushTrack = if (-not $s.Fork) { '无本地记录(下次拉取后可见)' }
        elseif ($s.ForkBehind -gt 0) { "远端领先 $($s.ForkBehind) 个提交(需先同步)" }
        elseif ($s.ForkAhead -gt 0) { "待推送 $($s.ForkAhead) 个提交" }
        else { '已同步' }
        Write-Host ("  拉取来源   : {0} → {1}" -f $UpstreamRemote, $UpstreamUrl)
        Write-Host ("  推送目标   : {0} → {1} ({2})" -f $ForkRemote, $ForkUrl, $pushTrack)
        Write-Host ("  本地改动   : 已跟踪 {0} 项, 未跟踪 {1} 项" -f $s.Tracked, $s.Untracked)
    } else {
        Write-Host '  分支       : 非 Git 仓库(在线更新不可用)'
    }

    $ver = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { '未知' }
    $feVer = '未知'
    $fePkg = Join-Path $FrontendDir 'package.json'
    if (Test-Path $fePkg) { $feVer = (Get-Content $fePkg -Raw | ConvertFrom-Json).version }
    Write-Host ("  版本       : VERSION {0} / frontend {1}" -f $ver, $feVer)

    Write-Host '  工具链     :'
    Write-Host ("    git    {0}" -f (Get-ToolVersion 'git' @('--version')))
    Write-Host ("    uv     {0}" -f (Get-ToolVersion 'uv' @('--version')))
    Write-Host ("    node   {0}" -f (Get-ToolVersion 'node' @('-v')))
    Write-Host ("    pnpm   {0}" -f (Get-ToolVersion 'pnpm' @('-v')))
    Write-Host ("    python {0}" -f (Get-ToolVersion 'python' @('--version')))

    $venv = Test-Path (Join-Path $BackendDir '.venv')
    $modules = Test-Path (Join-Path $FrontendDir 'node_modules')
    Write-Host ("  依赖       : backend/.venv {0} | frontend/node_modules {1}" -f `
        $(if ($venv) { '已就绪' } else { '缺失' }), $(if ($modules) { '已就绪' } else { '缺失' }))

    # 产物分两类: 前端 dist 是生产模式/桌面包的必需产物, 桌面包(backend/dist)是可选产物。
    # 默认不打包时它不存在属正常, 所以两者分开措辞 —— 别让「没打包」看起来像「故障」。
    $distOk = Test-Path (Join-Path $FrontendDist 'index.html')
    Write-Host ("  产物       : 前端 frontend/dist {0}" -f `
        $(if ($distOk) { '已构建' } else { '未构建(dev 模式不需要; prod 模式与桌面打包需要先编译)' }))
    $deskOk = Test-Path (Join-Path $DesktopDist 'TickFlowStockPanel')
    if ($deskOk) {
        Write-Host '              桌面客户端 已打包 (backend/dist/TickFlowStockPanel)'
    } elseif ($script:Cfg.PackageDesktop) {
        Write-Host '              桌面客户端 未打包 — 本次已开启打包, 编译阶段会生成' -ForegroundColor Yellow
    } else {
        Write-Host '              桌面客户端 未打包(可选产物; 需要时加 -PackageDesktop 或用菜单 2)' -ForegroundColor DarkGray
    }

    if (Test-Path $EnvFile) {
        $tf = if (Read-DotEnvValue $EnvFile 'TICKFLOW_API_KEY') { '已设置' } else { '未设置' }
        $ai = if (Read-DotEnvValue $EnvFile 'AI_API_KEY') { '已设置' } else { '未设置' }
        Write-Host ("  配置       : .env 存在 (TICKFLOW_API_KEY {0}, AI_API_KEY {1})" -f $tf, $ai)
    } else {
        Write-Host '  配置       : .env 缺失(首次运行会自动从 .env.example 生成)'
    }

    $bpids = Get-ListeningProcessIds $cfg.BackendPort
    $fpids = Get-ListeningProcessIds $cfg.FrontendPort
    Write-Host ("  端口       : {0} {1} | {2} {3}" -f `
        $cfg.BackendPort, $(if ($bpids.Count) { "被 PID $($bpids -join ',') 占用" } else { '空闲' }), `
        $cfg.FrontendPort, $(if ($fpids.Count) { "被 PID $($fpids -join ',') 占用" } else { '空闲' }))
    Write-Host ("  运行地址   : 开发 后端 http://{0}:{1} 前端 http://{0}:{2}" -f $cfg.DisplayHost, $cfg.BackendPort, $cfg.FrontendPort)
    Write-Host ("               生产 单端口 http://{0}:{1}" -f $cfg.DisplayHost, $cfg.BackendPort)
    Write-Host '  数据       : data/ 已在 .gitignore 中, 更新与清理都不会触碰'
}

# ============================== 远端分工 ==============================
# 幂等校对远端: origin 必须指向上游(shy3130), vstony 必须指向自己的 fork,
# 且裸 `git push` 默认推 fork。各人本地 clone 的 remote 名/URL/推送方式各不相同,
# 与其依赖口头约定, 不如每次启动都纠正一遍(非本仓库或无 git 时静默跳过)。
function Ensure-Remotes {
    if (-not (Test-Path (Join-Path $Root '.git'))) { return }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return }

    $targets = @(
        [pscustomobject]@{ Name = $UpstreamRemote; Url = $UpstreamUrl; Role = '上游(只拉取)' },
        [pscustomobject]@{ Name = $ForkRemote; Url = $ForkUrl; Role = 'fork(只推送)' }
    )
    foreach ($t in $targets) {
        $current = (& git -C $Root remote get-url $t.Name 2>$null)
        if (-not $current) {
            & git -C $Root remote add $t.Name $t.Url 2>&1 | ForEach-Object { Write-Host "    $_" }
            if ($LASTEXITCODE -eq 0) { Write-Ok "已添加远端 $($t.Name) [$($t.Role)] → $($t.Url)" }
            continue
        }
        if ($current.Trim() -ne $t.Url) {
            Write-Warn "远端 $($t.Name) 原指向 $($current.Trim()), 已纠正为 $($t.Url)"
            & git -C $Root remote set-url $t.Name $t.Url 2>&1 | ForEach-Object { Write-Host "    $_" }
        }
        # 独立的 pushurl 会让「拉取来源」与「推送目标」对不上, 统一清掉改用 remote.pushDefault
        if (& git -C $Root config --get "remote.$($t.Name).pushurl" 2>$null) {
            & git -C $Root config --unset "remote.$($t.Name).pushurl" 2>&1 | Out-Null
            Write-Info "已移除 $($t.Name) 的独立 pushurl(推送目标统一由 remote.pushDefault 决定)"
        }
    }

    if ((& git -C $Root config --get remote.pushDefault 2>$null) -ne $ForkRemote) {
        & git -C $Root config remote.pushDefault $ForkRemote 2>&1 | Out-Null
        Write-Info "git push 默认目标已设为 $ForkRemote"
    }
}

# ============================== 任务: 在线更新 ==============================
function Get-ShortSha {
    $sha = & git -C $Root rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $sha) { return $sha.Trim() }
    return '?'
}

function Restore-Stash([string]$StashSha, [string]$StashMessage) {
    Write-Info '恢复本地改动 (git stash pop)'
    Push-Location $Root
    try {
        & git stash pop 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    } finally { Pop-Location }

    if ($code -eq 0) {
        Write-Ok '本地改动已恢复'
        return
    }
    Write-Warn 'git stash pop 出现冲突, 暂存未被丢弃(可用 git status 查看冲突文件)'
    Write-Info  "手动恢复: git stash list  →  git checkout --theirs/--ours <file>  →  git stash drop"
    Write-Info  "保留的暂存: $StashSha  ($StashMessage)"
}

function Update-Repo {
    Write-Step "在线更新: 从上游 $UpstreamRemote 拉取($UpstreamUrl)"
    if (-not (Test-Path (Join-Path $Root '.git'))) {
        Write-Warn '当前目录不是 Git 仓库, 跳过在线更新'
        return
    }
    Require-Command 'git'
    Ensure-Remotes

    $s = Get-GitState
    if ($s.Branch -eq '-') {
        Write-Warn '当前处于游离 HEAD 状态, 跳过 pull'
        return
    }
    $upstreamRef = "$UpstreamRemote/$($s.Branch)"

    Write-Info "拉取远端 (git fetch --prune $UpstreamRemote)"
    & git -C $Root fetch --prune $UpstreamRemote 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Stop-Fatal "git fetch $UpstreamRemote 失败(检查网络或代理设置)" }
    # fork 状态只用于提示「有多少提交待推送」, 拉取失败不影响本次更新
    & git -C $Root fetch --prune $ForkRemote 2>&1 | Out-Null

    if (-not (& git -C $Root rev-parse --verify --quiet "refs/remotes/$upstreamRef" 2>$null)) {
        Write-Warn "上游没有分支 $($s.Branch)($upstreamRef), 跳过 pull"
        return
    }

    $s = Get-GitState
    if ($s.Behind -eq 0) {
        Write-Ok "已是最新版本 (HEAD $((Get-ShortSha)))"
        if ($s.Ahead -gt 0) { Write-Info "本地领先上游 $($s.Ahead) 个提交" }
        Show-PushHint $s
        Show-EnvKeyDiff
        return
    }
    Write-Info "上游有 $($s.Behind) 个新提交, 本地领先 $($s.Ahead) 个提交"

    $lockBefore = Get-LockFingerprint
    $verBefore = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { '' }

    # ---- 1. 保护本地改动: 只暂存已跟踪文件, 不碰未跟踪文件与忽略项(data/、node_modules/) ----
    $stashSha = $null
    $stashMessage = ''
    $stashed = $false
    if ($s.Tracked -gt 0) {
        Write-Warn "检测到 $($s.Tracked) 个已跟踪文件存在本地改动"
        if (Confirm-Action '更新前用 git stash 暂存这些改动, 更新完成后自动恢复?') {
            $stashMessage = "tsp-auto-update-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
            $prevSha = & git -C $Root rev-parse --verify refs/stash 2>$null
            & git -C $Root stash push -m $stashMessage 2>&1 | ForEach-Object { Write-Host "    $_" }
            if ($LASTEXITCODE -ne 0) { Stop-Fatal 'git stash 失败, 已放弃更新(本地改动未被改动)' }
            $nowSha = & git -C $Root rev-parse --verify refs/stash 2>$null
            # 用 SHA 变化确认 stash 真的创建成功, 避免误操作到历史暂存
            if ($nowSha -and $nowSha -ne $prevSha) {
                $stashed = $true
                $stashSha = $nowSha.Trim()
                Write-Ok "本地改动已暂存: $stashMessage"
            } else {
                Stop-Fatal 'git stash 未产生新暂存, 已放弃更新(请手动检查 git status)'
            }
        } else {
            Write-Warn '已取消更新(本地改动未暂存, 直接 pull 可能失败)'
            return
        }
    } else {
        Write-Info '无已跟踪文件改动, 无需暂存'
    }

    # ---- 2. 合并上游: 优先 fast-forward, 失败时按情况处理 ----
    # 用 `git merge --ff-only <上游引用>` 而非 `git pull`: pull 依赖分支的 upstream 跟踪,
    # 而 fork 工作流里这条跟踪常被改成自己的 fork。先捕获再逐行输出, 便于判断失败原因。
    Write-Info "合并上游 (git merge --ff-only $upstreamRef)"
    $pullOut = & git -C $Root merge --ff-only $upstreamRef 2>&1
    $pullCode = $LASTEXITCODE
    $pullText = ($pullOut | Out-String)
    $pullOut | ForEach-Object { Write-Host "    $_" }

    if ($pullCode -ne 0) {
        $untrackedBlocked = $pullText -match 'untracked working tree files would be overwritten'

        if ($untrackedBlocked) {
            Write-Warn '上游新增/修改的文件与本地未跟踪文件同名, 无法直接合并'
            if (Confirm-Action '把未跟踪文件一并暂存(git stash -u)后重试?') {
                $msg2 = "$stashMessage-untracked"
                & git -C $Root stash push -u -m $msg2 2>&1 | ForEach-Object { Write-Host "    $_" }
                if ($LASTEXITCODE -ne 0) { Stop-Fatal 'git stash -u 失败' }
                $stashed = $true
                if (-not $stashMessage) { $stashMessage = $msg2 }
                Write-Info "重试合并 (git merge --ff-only $upstreamRef)"
                $pullOut = & git -C $Root merge --ff-only $upstreamRef 2>&1
                $pullCode = $LASTEXITCODE
                $pullOut | ForEach-Object { Write-Host "    $_" }
            }
        } elseif ($s.Ahead -gt 0) {
            Write-Warn "本地有 $($s.Ahead) 个本地提交, fast-forward 不可用"
            if (Confirm-Action "改为 git rebase $upstreamRef 变基到上游?(有冲突会中断并保留现场)") {
                & git -C $Root rebase $upstreamRef 2>&1 | ForEach-Object { Write-Host "    $_" }
                $pullCode = $LASTEXITCODE
                if ($pullCode -ne 0) {
                    Write-Warn 'rebase 中断(可能有冲突)'
                    Write-Info '解决冲突后执行: git rebase --continue(放弃: git rebase --abort)'
                    if ($stashed) { Restore-Stash $stashSha $stashMessage }
                    Stop-Fatal 'rebase 未完成, 更新终止'
                }
            }
        }
    }

    if ($pullCode -ne 0) {
        if ($stashed) { Restore-Stash $stashSha $stashMessage }
        Stop-Fatal 'git pull 失败, 代码未更新(本地改动已恢复到更新前状态)'
    }

    # ---- 3. 恢复本地改动 ----
    if ($stashed) { Restore-Stash $stashSha $stashMessage }

    # ---- 4. 更新结果摘要 ----
    $verAfter = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { '' }
    Write-Ok "更新完成: $verBefore → $verAfter (HEAD $((Get-ShortSha)))"
    if ($lockBefore -ne (Get-LockFingerprint)) {
        Write-Warn '依赖锁文件已随上游更新, 编译阶段会重新同步依赖(uv sync / pnpm install)'
    }
    Show-PushHint (Get-GitState)
    Show-EnvKeyDiff
}

# 提示「本地比自己的 fork 领先多少」。只提示不自动推送 —— 推送是对外可见的显式动作,
# 需要时用菜单 8 / .\build.ps1 8 / -Task push。
function Show-PushHint($State) {
    if (-not $State.Fork) {
        Write-Info "推送目标 $ForkRemote($ForkUrl); 需要推送时执行 .\build.ps1 8"
        return
    }
    if ($State.ForkAhead -gt 0) {
        Write-Info "本地领先 $($State.Fork) $($State.ForkAhead) 个提交, 需要推送时执行 .\build.ps1 8 (-Task push)"
    } elseif ($State.ForkBehind -gt 0) {
        Write-Warn "$($State.Fork) 有 $($State.ForkBehind) 个本地没有的提交, 推送前需要先合并"
    } else {
        Write-Ok "$($State.Fork) 已同步"
    }
}

# ============================== 任务: 推送变更 ==============================
# 只把已提交的内容推到自己的 fork; 不做强推 —— 远端有本地没有的提交时直接报错，
# 让用户先在线更新(菜单 1), 避免一不小心覆盖远端。
function Push-Changes {
    Write-Step "推送变更: 推送到 $ForkRemote($ForkUrl)"
    if (-not (Test-Path (Join-Path $Root '.git'))) {
        Write-Warn '当前目录不是 Git 仓库, 跳过推送'
        return
    }
    Require-Command 'git'
    Ensure-Remotes

    $s = Get-GitState
    if ($s.Branch -eq '-') { Write-Warn '当前处于游离 HEAD 状态, 跳过推送'; return }
    $branch = $s.Branch

    Write-Info "拉取远端 (git fetch --prune $ForkRemote)"
    & git -C $Root fetch --prune $ForkRemote 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Stop-Fatal "git fetch $ForkRemote 失败(检查网络或代理设置)" }

    $s = Get-GitState
    if ($s.Fork -and $s.ForkBehind -gt 0) {
        Stop-Fatal "$($s.Fork) 有 $($s.ForkBehind) 个本地没有的提交, 先在线更新(菜单 1)再推送, 否则会被拒绝或覆盖远端"
    }
    if ($s.Fork -and $s.ForkAhead -eq 0) {
        Write-Ok "$($s.Fork) 已是最新 (HEAD $((Get-ShortSha)))"
        return
    }
    if ($s.Tracked -gt 0) {
        Write-Warn "$($s.Tracked) 个已跟踪文件的未提交改动不会被推送(需要先 git commit)"
    }

    Write-Info '待推送的提交(最多显示 10 条):'
    if ($s.Fork) {
        & git -C $Root log --oneline --no-decorate --max-count=10 "$($s.Fork)..HEAD" 2>$null | ForEach-Object { Write-Host "    $_" }
    } else {
        & git -C $Root log --oneline --no-decorate --max-count=10 2>$null | ForEach-Object { Write-Host "    $_" }
        Write-Info "$ForkRemote 上还没有分支 $branch, 本次推送会新建"
    }

    if (-not (Confirm-Action "推送到 $ForkRemote/$branch ?")) { Write-Info '已取消推送'; return }

    & git -C $Root push $ForkRemote $branch 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) { Stop-Fatal "git push $ForkRemote 失败(认证失败 / 网络异常 / 远端有本地没有的提交都会导致失败)" }
    Write-Ok "推送完成: HEAD $((Get-ShortSha)) → $ForkRemote/$branch"
}

# 上游 .env.example 新增了配置项时给出提示(不自动改用户的 .env)
function Show-EnvKeyDiff {
    if (-not (Test-Path $EnvFile)) { return }
    $exampleKeys = Get-DotEnvKeys $EnvExample
    $envKeys = Get-DotEnvKeys $EnvFile
    $missing = @($exampleKeys | Where-Object { $envKeys -notcontains $_ })
    if ($missing.Count -gt 0) {
        Write-Warn (".env.example 中这些配置项你的 .env 还没有: {0}" -f ($missing -join ', '))
        Write-Info '按需手动追加到 .env, 未追加时使用代码内默认值。'
    }
}

# ============================== 任务: 依赖同步 ==============================
# 从 backend/pyproject.toml 读 [project.optional-dependencies] 的真实 extra 名。
# 用途: 校验用户输入 —— 把包名(如 vectorbt)当 extra 名传下去, uv 会报
# "Extra `vectorbt` is not defined" 这种底层错误, 提前拦下并给出可用列表。
function Get-AvailableExtras {
    $pyproject = Join-Path $BackendDir 'pyproject.toml'
    if (-not (Test-Path $pyproject)) { return @() }
    $keys = @()
    $inSection = $false
    foreach ($line in Get-Content $pyproject) {
        if ($line -match '^\s*\[') {
            $inSection = ($line -match '^\s*\[project\.optional-dependencies\]')
            continue
        }
        if ($inSection -and $line -match '^\s*([A-Za-z0-9_.\-]+)\s*=\s*\[') { $keys += $Matches[1].ToLower() }
    }
    return $keys
}

# 规范化 + 校验 extras。只在「包名与 extra 一一对应」时才自动纠正
# (vectorbt↔backtest), 有歧义的输入一律报错并列出可用项, 不瞎猜。
function Resolve-BackendExtras([string]$Raw) {
    $items = @($Raw -split '[\s,]+' | Where-Object { $_ })
    if ($items.Count -eq 0) { return '' }

    $available = @(Get-AvailableExtras)
    if ($available.Count -eq 0) {
        Write-Warn '未能从 backend/pyproject.toml 读到 extras 列表, 跳过校验'
        return (($items | ForEach-Object { $_.Trim().ToLower() }) -join ' ')
    }

    $aliases = @{
        'vectorbt' = 'backtest'   # backtest extra 里只装 vectorbt
        'pywebview' = 'desktop'   # desktop extra 里只装 pywebview
        'webview' = 'desktop'
        'rtcompat' = 'legacy-cpu' # legacy-cpu 装的是 polars[rtcompat]
    }
    $resolved = @()
    foreach ($item in $items) {
        $key = $item.Trim().ToLower()
        if ($available -contains $key) { $resolved += $key; continue }
        if ($aliases.ContainsKey($key) -and ($available -contains $aliases[$key])) {
            Write-Warn "'$key' 是包名, 已按 extra 名 '$($aliases[$key])' 处理"
            $resolved += $aliases[$key]
            continue
        }
        Stop-Fatal ("未知 extra '$item'。可用 extras: {0}。注意这里要填 extra 名而不是包名(回测引擎的 extra 叫 backtest, vectorbt 是它内部的依赖)。" -f ($available -join ', '))
    }
    return (($resolved | Select-Object -Unique) -join ' ')
}

function Sync-Deps {
    param([switch]$Force)

    Write-Step '同步依赖'
    Require-Command 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Require-Command 'pnpm' 'npm i -g pnpm'

    # 菜单改配置 / 外部改 pyproject 后都可能失效, 这里再兜一次校验
    $script:Cfg.BackendExtras = Resolve-BackendExtras $script:Cfg.BackendExtras
    $extras = ($script:Cfg.BackendExtras -split '\s+' | Where-Object { $_ }) -join ' '
    $venv = Join-Path $BackendDir '.venv'
    $extrasMarker = Join-Path $venv '.tsp-extras'
    $markerValue = if (Test-Path $extrasMarker) { (Get-Content $extrasMarker -Raw).Trim() } else { '<none>' }
    $currentValue = if ($extras) { $extras } else { '<none>' }

    $needBackend = $Force -or (-not (Test-Path $venv)) -or ($markerValue -ne $currentValue)
    if ($needBackend) {
        # 注意: 子命令必须放最前(uv sync <flags>), "uv sync --frozen sync" 会被当成多余参数
        $syncArgs = @('sync', '--frozen')
        foreach ($e in ($extras -split ' ' | Where-Object { $_ })) { $syncArgs += @('--extra', $e) }
        $label = if ($extras) { "后端依赖 (uv sync --extra $extras)" } else { '后端依赖 (uv sync)' }

        Push-Location $BackendDir
        try {
            Write-Info "→ $label"
            # 边流式输出边留底: 需要区分「extras 写错」与「锁文件不一致」, 只有后者该退回非 frozen 重试
            $syncLog = New-Object System.Collections.Generic.List[string]
            & uv @syncArgs 2>&1 | ForEach-Object { $syncLog.Add([string]$_) ; Write-Host "    $_" }
            $code = $LASTEXITCODE
            if ($code -ne 0 -and (($syncLog -join "`n") -match 'is not defined in the project')) {
                Stop-Fatal ("uv sync 不接受这些 extras: [{0}]。可用 extras: {1}" -f $extras, ((Get-AvailableExtras) -join ', '))
            }
            if ($code -ne 0) {
                Write-Warn 'uv sync --frozen 失败(锁文件与 pyproject 可能不一致), 退回非 frozen 重试'
                $retryArgs = @($syncArgs | Where-Object { $_ -ne '--frozen' })
                & uv @retryArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
                $code = $LASTEXITCODE
            }
        } finally { Pop-Location }
        if ($code -ne 0) { Stop-Fatal '后端依赖同步失败' }

        if (-not (Test-Path $venv)) { New-Item -ItemType Directory -Force -Path $venv | Out-Null }
        Set-Content -Path $extrasMarker -Value $currentValue -Encoding ascii
        Write-Ok "后端依赖就绪 (extras: $currentValue)"
    } else {
        Write-Info "后端依赖已就绪, 跳过 (extras: $currentValue)"
    }

    $nodeModules = Join-Path $FrontendDir 'node_modules'
    $lockFile = Join-Path $FrontendDir 'pnpm-lock.yaml'
    $lockMarker = Join-Path $nodeModules '.tsp-lock-hash'
    $lockHash = if (Test-Path $lockFile) { (Get-FileHash $lockFile -Algorithm SHA256).Hash } else { 'missing' }
    $markerHash = if (Test-Path $lockMarker) { (Get-Content $lockMarker -Raw).Trim() } else { '<none>' }

    $needFrontend = $Force -or (-not (Test-Path $nodeModules)) -or ($markerHash -ne $lockHash)
    if ($needFrontend) {
        Run-External -Label '前端依赖 (pnpm install)' -Command 'pnpm' -Arguments @('install', '--frozen-lockfile') -WorkDir $FrontendDir
        if (-not (Test-Path $nodeModules)) { Stop-Fatal '前端依赖目录未生成: frontend/node_modules' }
        Set-Content -Path $lockMarker -Value $lockHash -Encoding ascii
        Write-Ok '前端依赖就绪'
    } else {
        Write-Info '前端依赖已就绪, 跳过'
    }
}

# ============================== 任务: 条件编译 ==============================
function Build-Frontend {
    if ($script:Cfg.SkipFrontendBuild) { Write-Info '按配置跳过前端生产构建'; return }
    Write-Step '编译前端 (tsc -b && vite build)'
    Run-External -Label 'pnpm build' -Command 'pnpm' -Arguments @('build') -WorkDir $FrontendDir
    $index = Join-Path $FrontendDist 'index.html'
    if (-not (Test-Path $index)) { Stop-Fatal "前端构建产物缺失: $index" }
    Write-Ok '前端构建完成 → frontend/dist'
}

function Package-Desktop {
    Write-Step '打包桌面客户端 (PyInstaller onedir)'
    Require-Command 'uv'
    if (-not (Test-Path (Join-Path $FrontendDist 'index.html'))) {
        Write-Warn '未检测到 frontend/dist, 桌面客户端将缺少前端资源'
        Write-Info '先执行前端生产构建(菜单 2 → 执行编译, 或 .\build.ps1 -Task build)'
    }

    Push-Location $BackendDir
    try {
        Write-Info '→ 安装 PyInstaller(装入 backend 的 uv 环境)'
        & uv pip install pyinstaller 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { Stop-Fatal 'PyInstaller 安装失败' }

        $icon = Join-Path $Root 'packaging\icon.ico'
        if (-not (Test-Path $icon)) {
            Write-Info '→ 生成应用图标 (packaging/generate_icon.py)'
            & uv run python ../packaging/generate_icon.py 2>&1 | ForEach-Object { Write-Host "    $_" }
            if ($LASTEXITCODE -ne 0) { Write-Warn '图标生成失败, 继续打包(PyInstaller 会退回默认图标)' }
        }

        Write-Info '→ pyinstaller packaging/tickflow.spec'
        & uv run pyinstaller ../packaging/tickflow.spec --noconfirm 2>&1 | ForEach-Object { Write-Host "    $_" }
        if ($LASTEXITCODE -ne 0) { Stop-Fatal 'PyInstaller 打包失败(详见 backend/build/*.warn)' }
    } finally { Pop-Location }

    $outDir = Join-Path $DesktopDist 'TickFlowStockPanel'
    if (-not (Test-Path $outDir)) { Stop-Fatal "未找到打包产物目录: $outDir" }
    Write-Ok "桌面客户端打包完成 → backend/dist/TickFlowStockPanel"

    # legacy-cpu 构建必须同时带上 AVX2 与兼容内核, 否则老 CPU 上启动即崩(release.yml 同款校验)
    if ($script:Cfg.BackendExtras -match 'legacy-cpu') {
        $runtimeRoot = Join-Path $outDir '_internal'
        $required = @('_polars_runtime_32\_polars_runtime.pyd', '_polars_runtime_compat\_polars_runtime.pyd')
        $missing = @($required | Where-Object { -not (Test-Path (Join-Path $runtimeRoot $_)) })
        if ($missing.Count -gt 0) { Stop-Fatal "打包产物缺少 Polars 运行时: $($missing -join ', ')" }
        Write-Ok 'Polars 运行时(含 legacy-cpu 兼容内核)校验通过'
    }
}

function Start-Build {
    # 直接执行时先把生效的配置打出来, 避免「以为改了 extras/打包开关」的误会
    Write-Info ("编译配置: extras=[{0}] 前端构建={1} 桌面打包={2}" -f `
        $(if ($script:Cfg.BackendExtras) { $script:Cfg.BackendExtras } else { '无' }), `
        $(if ($script:Cfg.SkipFrontendBuild) { '跳过' } else { '开' }), `
        $(if ($script:Cfg.PackageDesktop) { '开' } else { '关' }))
    if ($SkipDeps) { Write-Info '按参数跳过依赖同步' } else { Sync-Deps }
    Build-Frontend
    if ($script:Cfg.PackageDesktop) { Package-Desktop }
    Write-Ok '编译完成'
}

# ============================== 任务: 测试 ==============================
function Run-Tests {
    Write-Step '运行后端测试 (pytest)'
    Require-Command 'uv' 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    # pytest/ruff/mypy 在可选 extras 的 dev 组里(pyproject 没有 dependency-groups),
    # 所以必须显式 --extra dev, 否则直接跑会报 "No module named pytest"(见 docs/plugin-development.md)。
    # 同时带上当前配置的 extras, 避免 uv 同步时把已安装的兼容内核卸掉。
    $pytestArgs = @('run')
    $extras = @($script:Cfg.BackendExtras -split '\s+' | Where-Object { $_ })
    foreach ($e in @($extras + @('dev') | Select-Object -Unique)) { $pytestArgs += @('--extra', $e) }
    $pytestArgs += @('python', '-m', 'pytest', '-q')
    # TestPath 支持空格分隔多个路径/关键字, 必须拆成独立参数, 否则 pytest 会当成一个不存在的文件
    if ($TestPath) { $pytestArgs += @($TestPath -split '\s+' | Where-Object { $_ }) }

    $env:PYTHONUNBUFFERED = '1'
    Run-External -Label ("pytest {0}" -f $(if ($TestPath) { $TestPath } else { '(全部)' })) `
        -Command 'uv' -Arguments $pytestArgs -WorkDir $BackendDir
    Write-Ok '测试通过'
}

# ============================== 任务: 清理 ==============================
function Clear-Artifacts {
    Write-Step '清理构建产物'
    $targets = @(
        $FrontendDist,
        (Join-Path $FrontendDir '.vite'),
        (Join-Path $BackendDir 'build'),
        (Join-Path $BackendDir 'dist'),
        (Join-Path $BackendDir '.pytest_cache'),
        (Join-Path $BackendDir '.ruff_cache'),
        (Join-Path $BackendDir '.mypy_cache'),
        (Join-Path $Root 'packaging\Output')
    ) + @(Get-ChildItem -Path $FrontendDir -Filter '*.tsbuildinfo' -File -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName)
    $existing = @($targets | Where-Object { Test-Path $_ })
    $pycache = @(Get-ChildItem -Path $BackendDir -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' })

    if ($existing.Count -eq 0 -and $pycache.Count -eq 0) { Write-Ok '没有可清理的产物'; return }
    foreach ($t in $existing) { Write-Info "将删除: $t" }
    Write-Info ("将删除: __pycache__ 目录 {0} 个" -f $pycache.Count)
    Write-Info 'data/、backend/.venv、frontend/node_modules 不会被删除'

    if (-not (Confirm-Action '确认清理以上构建产物?')) { Write-Info '已取消'; return }

    foreach ($t in $existing) {
        Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $t) { Write-Warn "删除失败(可能被占用): $t" } else { Write-Ok "已删除: $t" }
    }
    foreach ($d in $pycache) {
        Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Ok '清理完成(依赖与 data/ 保留)'
}

# ============================== 任务: 本地运行 ==============================
# 释放端口: 复用 dev.ps1 的处理口径(kill 整个进程树, 兼容僵尸 socket)
function Stop-PortListener([string]$Name, [int]$Port) {
    $pids = Get-ListeningProcessIds $Port
    if ($pids.Count -eq 0) { return }

    $alive = @($pids | Where-Object {
        try { [System.Diagnostics.Process]::GetProcessById($_) | Out-Null; $true } catch { $false }
    })
    if ($alive.Count -eq 0) {
        Write-Warn "端口 $Port ($Name) 存在僵尸 socket(进程已退出), 直接启动"
        return
    }

    Write-Warn "端口 $Port ($Name) 被占用, 结束进程树: $($alive -join ', ')"
    foreach ($p in $alive) {
        $null = & cmd /c "taskkill /F /T /PID $p 2>nul"
        try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
    }
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 300
        if ((Get-ListeningProcessIds $Port).Count -eq 0) { Write-Ok "端口 $Port 已释放"; return }
    }
    Write-Warn "端口 $Port 仍被占用, 启动可能失败"
}

function Show-UrlBanner([string]$ModeText, [string[]]$Lines) {
    Write-Host ''
    Write-Host '+----------------------------------------------+' -ForegroundColor Blue
    Write-Host ("|  tickflow-stock-panel  {0}" -f $ModeText.PadRight(20)) -ForegroundColor Blue
    Write-Host '|                                              ' -ForegroundColor Blue
    foreach ($l in $Lines) {
        Write-Host ("|  {0}" -f $l.PadRight(44)) -ForegroundColor Blue
    }
    Write-Host '|                                              ' -ForegroundColor Blue
    Write-Host '|  Ctrl-C 停止服务                             ' -ForegroundColor Blue
    Write-Host '+----------------------------------------------+' -ForegroundColor Blue
    Write-Host ''
}

function Start-Local {
    if ($RunMode -eq 'prod') { Start-Prod } else { Start-Dev }
}

# 开发模式: 直接复用 dev.ps1(端口释放 / 依赖安装 / 双进程输出聚合都已验证过)
function Start-Dev {
    Write-Step '本地运行: 开发模式(前后端热更新)'
    if (-not (Test-Path $DevScript)) { Stop-Fatal "缺少 $DevScript" }

    $extras = $script:Cfg.BackendExtras.Trim()
    if ($extras) { $env:BACKEND_EXTRAS = $extras; Write-Info "BACKEND_EXTRAS = $extras (仅本次进程生效)" }
    elseif ($env:BACKEND_EXTRAS) { Write-Info "BACKEND_EXTRAS = $($env:BACKEND_EXTRAS) (来自环境变量)" }

    $cfg = Get-Settings
    Show-UrlBanner 'dev' @(
        ("backend   http://{0}:{1}" -f $cfg.DisplayHost, $cfg.BackendPort),
        ("frontend  http://{0}:{1}" -f $cfg.DisplayHost, $cfg.FrontendPort)
    )
    Write-Info '交给 dev.ps1 启动(依赖检查 / 端口释放 / 双进程日志聚合)'
    & $DevScript
    # dev.ps1 收尾时会把 taskkill 等原生命令的退出码留在 $LASTEXITCODE(常见 128),
    # 正常 Ctrl-C 或单个进程退出都会走到这里, 因此只提示、不判失败;
    # 真正的启动错误(缺依赖等)dev.ps1 自己已经打印在上方。
    $code = $LASTEXITCODE
    if ($code -and $code -ne 0) {
        Write-Warn "dev.ps1 退出码 $code(收尾或被外部终止时属正常, 启动失败原因见上方 [dev] 日志)"
    } else {
        Write-Ok '开发模式已停止'
    }
}

# 生产模式: 前端先构建成静态产物, 由后端单端口托管(frontend/dist)
function Start-Prod {
    Write-Step '本地运行: 生产模式(单端口托管 frontend/dist)'
    $cfg = Get-Settings

    if (-not (Test-Path (Join-Path $FrontendDist 'index.html'))) {
        Write-Warn '未找到 frontend/dist/index.html'
        if (-not (Confirm-Action '现在执行前端生产构建?')) { Write-Info '已取消'; return }
        if ($SkipDeps) { Write-Info '按参数跳过依赖同步' } else { Sync-Deps }
        Build-Frontend
    }

    $python = Join-Path $BackendDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { Stop-Fatal '未找到 backend/.venv, 请先执行依赖同步(菜单 4)' }

    Stop-PortListener 'backend' $cfg.BackendPort

    $uvicornArgs = @('-m', 'uvicorn', 'app.main:app')
    if (Test-Path $EnvFile) { $uvicornArgs += @('--env-file', $EnvFile) }
    $levels = @('critical', 'error', 'warning', 'info', 'debug', 'trace')
    if ($cfg.LogLevel -and ($levels -contains $cfg.LogLevel.ToLower())) { $uvicornArgs += @('--log-level', $cfg.LogLevel.ToLower()) }
    $uvicornArgs += @('--host', $cfg.Bind, '--port', [string]$cfg.BackendPort)

    Show-UrlBanner 'prod' @("http://$($cfg.DisplayHost):$($cfg.BackendPort)")
    $env:PYTHONUNBUFFERED = '1'
    Push-Location $BackendDir
    try {
        & $python @uvicornArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code -and $code -ne 0) { Stop-Fatal "后端退出码 $code (端口占用? 详见上方日志)" }
    Write-Ok '服务已停止'
}

# ============================== 一键全流程 ==============================
function Invoke-FullFlow {
    Write-Step '一键全流程: 在线更新 → 条件编译 → 本地运行'
    Ensure-EnvFile
    Show-Status
    $lockBefore = Get-LockFingerprint

    if ($SkipUpdate) {
        Write-Info '按参数跳过在线更新'
    } else {
        Invoke-Guarded '在线更新' { Update-Repo }
    }

    $depsDirty = ($lockBefore -ne (Get-LockFingerprint))
    if ($SkipDeps) {
        Write-Info '按参数跳过依赖同步'
    } else {
        Invoke-Guarded '同步依赖' { Sync-Deps -Force:$depsDirty }
        if (-not $script:GuardedOk) {
            Write-Err '依赖同步失败, 终止后续编译与运行'
            return
        }
    }

    Invoke-Guarded '编译前端' { Build-Frontend }
    if (-not $script:GuardedOk) {
        Write-Err '前端编译失败, 终止本地运行(修掉编译错误后重跑即可)'
        return
    }
    if ($script:Cfg.PackageDesktop) { Invoke-Guarded '桌面客户端打包' { Package-Desktop } }

    if ($SkipRun) { Write-Ok '一键全流程: 编译完成(按参数不启动服务)'; return }
    Invoke-Guarded '本地运行' { Start-Local }
}

# ============================== 菜单 ==============================
function Show-Banner {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Blue
    Write-Host '  tick-stock-panel 一键脚本   更新 / 编译 / 运行' -ForegroundColor Blue
    Write-Host '============================================================' -ForegroundColor Blue

    $s = Get-GitState
    if ($s.IsRepo) {
        $track = "领先上游 $($s.Ahead) / 落后上游 $($s.Behind)"
        if (-not $s.Upstream) { $track = '无上游引用' }
        Write-Host ("  分支 {0}  |  VERSION {1}  |  本地改动 {2}  |  {3}" -f `
            $s.Branch, $(if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { '?' }),
            $s.Tracked, $track) -ForegroundColor DarkGray
        $pushHint = if (-not $s.Fork) { '未拉取 fork 状态' }
        elseif ($s.ForkAhead -gt 0) { "待推送 $($s.ForkAhead)" }
        else { 'fork 已同步' }
        Write-Host ("  远端: {0}=拉取上游 / {1}=推送变更  |  {2}" -f $UpstreamRemote, $ForkRemote, $pushHint) -ForegroundColor DarkGray
    } else {
        Write-Host '  非 Git 仓库模式' -ForegroundColor DarkGray
    }
    Write-Host ("  编译配置: extras=[{0}] 前端构建={1} 桌面打包={2}" -f `
        $(if ($script:Cfg.BackendExtras) { $script:Cfg.BackendExtras } else { '无' }), `
        $(if ($script:Cfg.SkipFrontendBuild) { '跳过' } else { '开' }), `
        $(if ($script:Cfg.PackageDesktop) { '开' } else { '关' })) -ForegroundColor DarkGray
    Write-Host '------------------------------------------------------------'
}

function Show-MenuOnce {
    Show-Banner
    Write-Host '  0) 一键全流程    在线更新 → 条件编译 → 本地运行'
    Write-Host '  1) 在线更新      拉取上游代码(本地改动自动 stash 暂存/恢复)'
    Write-Host '  2) 条件编译      后端 extras / 前端构建 / 桌面打包'
    Write-Host '  3) 本地运行      开发模式(热更新) / 生产模式(单端口)'
    Write-Host '  4) 同步依赖      uv sync + pnpm install'
    Write-Host '  5) 运行测试      后端 pytest'
    Write-Host '  6) 清理产物      frontend/dist, backend/dist, __pycache__'
    Write-Host '  7) 状态检查      工具链 / 依赖 / 产物 / 端口'
    Write-Host '  8) 推送变更      推送本地提交到自己的 fork (vstony)'
    Write-Host '  Q) 退出'
    Write-Host '------------------------------------------------------------'
    Write-Host '  提示: 也可 .\build.ps1 <编号> 直达某一步(例: .\build.ps1 0)' -ForegroundColor DarkGray

    $choice = (Read-Host '请选择 [0-8/Q]').Trim().ToLower()
    switch ($choice) {
        '0' { Invoke-FullFlow }
        '1' { Invoke-Guarded '在线更新' { Update-Repo } }
        '2' { Edit-BuildConfig }
        '3' { Select-RunMode; Invoke-Guarded '本地运行' { Start-Local } }
        '4' { Invoke-Guarded '同步依赖' { Sync-Deps } }
        '5' { Invoke-Guarded '运行测试' { Run-Tests } }
        '6' { Clear-Artifacts }
        '7' { Show-Status }
        '8' { Invoke-Guarded '推送变更' { Push-Changes } }
        'q' { $script:ExitMenu = $true }
        '' { }
        default { Write-Warn "无效选项: $choice" }
    }
}

function Edit-BuildConfig {
    while ($true) {
        Write-Host ''
        Write-Host '------------ 条件编译配置 ------------' -ForegroundColor Blue
        Write-Host ("  1) 后端额外依赖   当前: [{0}]" -f $(if ($script:Cfg.BackendExtras) { $script:Cfg.BackendExtras } else { '无' }))
        Write-Host ("  2) 前端生产构建   当前: {0}" -f $(if ($script:Cfg.SkipFrontendBuild) { '跳过' } else { '开(tsc + vite build)' }))
        Write-Host ("  3) 桌面客户端打包 当前: {0}" -f $(if ($script:Cfg.PackageDesktop) { '开(PyInstaller onedir)' } else { '关' }))
        Write-Host '  4) 执行编译'
        Write-Host '  0) 返回'
        Write-Host ("  可用 extras(取自 pyproject.toml): {0}" -f ((Get-AvailableExtras) -join ', '))
        Write-Host '  说明: 要填 extra 名而不是包名 —— backtest=回测引擎(内部装 vectorbt), legacy-cpu=旧 CPU 兼容内核(polars[rtcompat]), desktop=桌面壳(pywebview)'

        $choice = (Read-Host '请选择 [1-4/0]').Trim()
        switch ($choice) {
            '1' {
                $rawExtras = (Read-Host '  输入 extras(空格或逗号分隔, 直接回车=清空)').Trim()
                if (-not $rawExtras) {
                    $script:Cfg.BackendExtras = ''
                    Write-Ok '后端额外依赖已设为 [无]'
                } else {
                    try {
                        # 校验不通过就保留原配置, 不把错误值带进后续编译
                        $script:Cfg.BackendExtras = Resolve-BackendExtras $rawExtras
                        Write-Ok ("后端额外依赖已设为 [{0}]" -f $script:Cfg.BackendExtras)
                    } catch {
                        Write-Err $_.Exception.Message
                        Write-Info '已保留原配置, 未改动'
                    }
                }
            }
            '2' {
                $script:Cfg.SkipFrontendBuild = -not $script:Cfg.SkipFrontendBuild
                Write-Ok ("前端生产构建: {0}" -f $(if ($script:Cfg.SkipFrontendBuild) { '跳过' } else { '开' }))
            }
            '3' {
                $script:Cfg.PackageDesktop = -not $script:Cfg.PackageDesktop
                Write-Ok ("桌面客户端打包: {0}" -f $(if ($script:Cfg.PackageDesktop) { '开' } else { '关' }))
            }
            '4' { Invoke-Guarded '条件编译' { Start-Build } }
            '0' { return }
            default { Write-Warn "无效选项: $choice" }
        }
    }
}

function Select-RunMode {
    Write-Host ''
    Write-Host '------------ 运行模式 ------------' -ForegroundColor Blue
    Write-Host '  1) 开发模式  前端 3011 热更新 + 后端 3018 热重载(默认)'
    Write-Host '  2) 生产模式  仅后端单端口 3018, 托管 frontend/dist(需先编译前端)'
    $choice = (Read-Host '请选择 [1/2]').Trim()
    if ($choice -eq '2') { $script:RunMode = 'prod' } else { $script:RunMode = 'dev' }
    Write-Ok ("运行模式: {0}" -f $script:RunMode)
}

function Invoke-Menu {
    # 非交互输入下 Read-Host 恒返回空串, 菜单会变成无限循环刷屏, 直接退出并提示用位置参数
    if ([Console]::IsInputRedirected) {
        Write-Warn '当前输入不是交互式终端, 菜单不可用'
        Write-Info '请用位置参数直达某一步: .\build.ps1 0(一键全流程) / 1(更新) / 7(状态) / 8(推送) 等'
        return
    }
    Write-Info '菜单模式: 0 = 一键全流程(更新 → 编译 → 运行); Q = 退出'
    while (-not $script:ExitMenu) { Show-MenuOnce }
}

# 位置参数/任务名 → 内部动作名。编号语义与菜单一致, 但 2/3 直接执行该步动作:
#   .\build.ps1 0  ≡  菜单里按 0(一键全流程), 不打印菜单也不二次询问。
function Resolve-TaskAction([string]$Raw) {
    $key = ''
    if ($Raw) { $key = $Raw.Trim().ToLower() }
    switch ($key) {
        '0'     { return 'all' }
        '1'     { return 'update' }
        '2'     { return 'build' }
        '3'     { return 'run' }
        '4'     { return 'deps' }
        '5'     { return 'test' }
        '6'     { return 'clean' }
        '7'     { return 'status' }
        '8'     { return 'push' }
        'q'     { return 'quit' }
        ''      { return 'menu' }
        default { return $key }
    }
}

# ============================== 入口 ==============================
Ensure-EnvFile
Ensure-Remotes

$action = Resolve-TaskAction $Task

# 参数里的 extras 提前校验: 别名自动纠正, 未知值立即报错(不等到编译到一半才炸)
try {
    $script:Cfg.BackendExtras = Resolve-BackendExtras $script:Cfg.BackendExtras
} catch {
    Write-Err $_.Exception.Message
    exit 1
}

if ($action -eq 'quit') {
    Write-Info '未执行任何操作(菜单编号 q = 退出); 需要菜单请直接运行 .\build.ps1'
} elseif ($action -eq 'menu') {
    Invoke-Menu
    Write-Info '已退出菜单'
} else {
    switch ($action) {
        'all'    { Invoke-Guarded '一键全流程' { Invoke-FullFlow } }
        'update' { Invoke-Guarded '在线更新' { Update-Repo } }
        'push'   { Invoke-Guarded '推送变更' { Push-Changes } }
        'build'  { Invoke-Guarded '条件编译' { Start-Build } }
        'run'    { Invoke-Guarded '本地运行' { Start-Local } }
        'deps'   { Invoke-Guarded '同步依赖' { Sync-Deps } }
        'test'   { Invoke-Guarded '运行测试' { Run-Tests } }
        'status' { Show-Status }
        'clean'  { Clear-Artifacts }
        default  { Write-Err "未知操作: $action"; $script:Failed = $true }
    }
}

if ($script:Failed) { exit 1 }
