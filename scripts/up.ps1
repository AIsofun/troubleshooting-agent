#!/usr/bin/env pwsh
<#
.SYNOPSIS
    运维助手 Agent — 一键管理脚本

.DESCRIPTION
    快速拉起/停止 Agent 服务。

.PARAMETER Obs
    同时启动可观测性栈（Prometheus + Loki + Grafana）

.PARAMETER Build
    拉起前先构建镜像（代码有更新时使用）

.PARAMETER Pull
    拉起前先拉取最新基础镜像

.PARAMETER Down
    停止并移除容器（保留数据卷）

.PARAMETER DownVolumes
    停止并移除容器和所有数据卷（⚠️ 数据会丢失）

.PARAMETER Logs
    跟踪 agent-api 日志输出

.PARAMETER Status
    显示容器状态

.EXAMPLE
    # 只启动核心服务
    .\scripts\up.ps1

    # 启动核心 + 可观测性栈
    .\scripts\up.ps1 -Obs

    # 代码更新后重新构建并启动
    .\scripts\up.ps1 -Build -Obs

    # 停止所有服务
    .\scripts\up.ps1 -Down
#>

param(
    [switch]$Obs,
    [switch]$Build,
    [switch]$Pull,
    [switch]$Down,
    [switch]$DownVolumes,
    [switch]$Logs,
    [switch]$Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── 切换到项目根目录 ──────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
Push-Location $ProjectRoot

try {

    # ── Profile 列表 ──────────────────────────────
    $profiles = @("core")
    if ($Obs) { $profiles += "obs" }
    $profileArgs = ($profiles | ForEach-Object { "--profile", $_ }) -join " "

    function Invoke-Compose {
        param([string[]]$Args)
        $cmd = "docker compose " + (($profiles | ForEach-Object { "--profile $_" }) -join " ") + " " + ($Args -join " ")
        Write-Host "[run] $cmd" -ForegroundColor DarkGray
        Invoke-Expression $cmd
        if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
    }

    # ── .env 检查 ────────────────────────────────
    if (-not (Test-Path ".env")) {
        Write-Warning ".env not found. Copying from .env.example..."
        Copy-Item ".env.example" ".env"
        Write-Host "Please review .env and set POSTGRES_PASSWORD before going to production." -ForegroundColor Yellow
    }

    # ── 执行动作 ─────────────────────────────────
    if ($DownVolumes) {
        Write-Warning "This will DELETE all data volumes. Are you sure? (y/N)"
        $confirm = Read-Host
        if ($confirm -ne 'y') { Write-Host "Aborted."; exit 0 }
        Invoke-Compose @("down", "--volumes", "--remove-orphans")
        Write-Host "All containers and volumes removed." -ForegroundColor Red
        exit 0
    }

    if ($Down) {
        Invoke-Compose @("down", "--remove-orphans")
        Write-Host "Services stopped." -ForegroundColor Yellow
        exit 0
    }

    if ($Status) {
        docker compose ps
        exit 0
    }

    if ($Logs) {
        docker compose logs -f agent-api
        exit 0
    }

    if ($Pull) {
        Write-Host "Pulling latest base images..." -ForegroundColor Cyan
        Invoke-Compose @("pull")
    }

    if ($Build) {
        Write-Host "Building agent-api image..." -ForegroundColor Cyan
        docker compose build --no-cache agent-api
        if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    }

    # ── 拉起服务 ──────────────────────────────────
    Write-Host "Starting services (profiles: $($profiles -join ', '))..." -ForegroundColor Cyan
    Invoke-Compose @("up", "-d", "--remove-orphans")

    # ── 等待 healthcheck ──────────────────────────
    Write-Host "Waiting for agent-api to become healthy..." -ForegroundColor Cyan
    $maxWait = 60
    $waited = 0
    $healthy = $false
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 3
        $waited += 3
        $state = docker inspect --format "{{.State.Health.Status}}" agent-api 2>$null
        if ($state -eq "healthy") {
            $healthy = $true
            break
        }
        Write-Host "  ($waited s) status: $state" -ForegroundColor DarkGray
    }

    # ── 输出访问地址 ──────────────────────────────
    Write-Host ""
    if ($healthy) {
        Write-Host "✅ Agent API:   http://localhost:8000" -ForegroundColor Green
    } else {
        Write-Host "⚠️  agent-api may still be starting. Check: docker compose logs agent-api" -ForegroundColor Yellow
        Write-Host "   Agent API:   http://localhost:8000" -ForegroundColor Cyan
    }
    if ($Obs) {
        Write-Host "📊 Grafana:     http://localhost:3000  (admin / $env:GRAFANA_PASSWORD)" -ForegroundColor Cyan
        Write-Host "🔍 Prometheus:  http://localhost:9090" -ForegroundColor Cyan
        Write-Host "📋 Loki:        http://localhost:3100" -ForegroundColor Cyan
    }
    Write-Host ""
    Write-Host "To stop:    .\scripts\up.ps1 -Down"
    Write-Host "To view logs: .\scripts\up.ps1 -Logs"

} finally {
    Pop-Location
}
