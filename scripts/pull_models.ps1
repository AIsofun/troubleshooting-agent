#!/usr/bin/env pwsh
<#
.SYNOPSIS
    预拉取离线运行所需的 Ollama 模型。
    在有网络的机器上运行，然后将 ollama_data 卷打包迁移到离线机器。

.DESCRIPTION
    拉取：
      - LLM 模型（qwen2.5:14b 或 config.yaml 中指定的模型）
      - Embedding 模型（bge-m3）

    拉取完成后，ollama 的模型数据存储在 Docker 卷 ops-agent_ollama_data 中。
    迁移方法：
      docker run --rm -v ops-agent_ollama_data:/data -v $(pwd):/backup \
        alpine tar czf /backup/ollama_models.tar.gz -C /data .
    在目标机器解包：
      docker run --rm -v ops-agent_ollama_data:/data -v $(pwd):/backup \
        alpine tar xzf /backup/ollama_models.tar.gz -C /data
#>

param(
    [string]$LLMModel = "qwen2.5:14b",
    [string]$EmbedModel = "bge-m3"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
Push-Location $ProjectRoot

try {
    # Ensure ollama container is running
    $running = docker compose --profile core ps --format json ollama 2>$null | ConvertFrom-Json
    if (-not $running -or $running.State -ne "running") {
        Write-Host "Starting ollama service..." -ForegroundColor Cyan
        docker compose --profile core up -d ollama
        Start-Sleep -Seconds 5
    }

    Write-Host "Pulling LLM model: $LLMModel" -ForegroundColor Cyan
    docker exec ollama ollama pull $LLMModel

    Write-Host "Pulling embedding model: $EmbedModel" -ForegroundColor Cyan
    docker exec ollama ollama pull $EmbedModel

    Write-Host ""
    Write-Host "✅ Models pulled successfully." -ForegroundColor Green
    Write-Host "To export for offline deployment:"
    Write-Host '  docker run --rm -v ops-agent_ollama_data:/data -v ${PWD}:/backup alpine tar czf /backup/ollama_models.tar.gz -C /data .'
} finally {
    Pop-Location
}
