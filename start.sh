#!/bin/bash
# Auto-detect GPU and start LogsCrawler with appropriate configuration

set -e

echo "🔍 Detecting GPU..."

# Check for NVIDIA GPU
if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    echo "✅ NVIDIA GPU detected"
    GPU_TYPE="nvidia"
# Check for AMD ROCm GPU
elif command -v rocm-smi &> /dev/null && rocm-smi &> /dev/null; then
    echo "✅ AMD ROCm GPU detected"
    GPU_TYPE="rocm"
else
    echo "ℹ️  No GPU detected, using CPU mode"
    GPU_TYPE="cpu"
fi

# Start with appropriate compose files
case $GPU_TYPE in
    nvidia)
        echo "🚀 Starting with NVIDIA GPU support..."
        docker-compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
        ;;
    rocm)
        echo "🚀 Starting with AMD ROCm GPU support..."
        docker-compose -f docker-compose.yml -f docker-compose.rocm.yml up -d
        ;;
    *)
        echo "🚀 Starting in CPU mode..."
        docker-compose up -d
        ;;
esac

echo ""
echo "⏳ Waiting for services to start..."
sleep 5

# Pull the AI model
echo "📥 Pulling AI model (phi3:mini)..."
docker exec logscrawler-ollama ollama pull phi3:mini || echo "⚠️  Could not pull model, will retry on first use"

echo ""
echo "✨ LogsCrawler is ready!"
echo "   Dashboard: http://localhost:5000"
echo "   OpenSearch: http://localhost:9200"
echo "   Ollama: http://localhost:11434"
