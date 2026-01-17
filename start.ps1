# Auto-detect GPU and start LogsCrawler with appropriate configuration

Write-Host "🔍 Detecting GPU..." -ForegroundColor Cyan

$GPU_TYPE = "cpu"

# Check for NVIDIA GPU
try {
    $nvidia = nvidia-smi 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ NVIDIA GPU detected" -ForegroundColor Green
        $GPU_TYPE = "nvidia"
    }
} catch {}

# Check for AMD GPU (Windows)
if ($GPU_TYPE -eq "cpu") {
    $amdGpu = Get-WmiObject Win32_VideoController | Where-Object { $_.Name -match "AMD|Radeon" }
    if ($amdGpu) {
        Write-Host "✅ AMD GPU detected (Note: ROCm support requires Linux)" -ForegroundColor Yellow
        # ROCm doesn't work well on Windows, fall back to CPU
    }
}

if ($GPU_TYPE -eq "cpu") {
    Write-Host "ℹ️  No supported GPU detected, using CPU mode" -ForegroundColor Gray
}

# Start with appropriate compose files
switch ($GPU_TYPE) {
    "nvidia" {
        Write-Host "🚀 Starting with NVIDIA GPU support..." -ForegroundColor Green
        docker-compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
    }
    default {
        Write-Host "🚀 Starting in CPU mode..." -ForegroundColor Gray
        docker-compose up -d
    }
}

Write-Host ""
Write-Host "⏳ Waiting for services to start..." -ForegroundColor Cyan
Start-Sleep -Seconds 10

# Pull the AI model
Write-Host "📥 Pulling AI model (phi3:mini)..." -ForegroundColor Cyan
docker exec logscrawler-ollama ollama pull phi3:mini

Write-Host ""
Write-Host "✨ LogsCrawler is ready!" -ForegroundColor Green
Write-Host "   Dashboard: http://localhost:5000" -ForegroundColor White
Write-Host "   OpenSearch: http://localhost:9200" -ForegroundColor White
Write-Host "   Ollama: http://localhost:11434" -ForegroundColor White
