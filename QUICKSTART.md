# Quick Start Guide

Get LogsCrawler up and running in 5 minutes!

## Step 1: Start the Services

**For GPU (RTX 5080, etc.):**
```bash
docker-compose up -d
```

**For CPU-only:**
```bash
docker-compose -f docker-compose.cpu.yml up -d
```

## Step 2: Pull an LLM Model

Wait for Ollama to start (about 10-30 seconds), then pull a model:

```bash
# Small and fast (recommended for testing)
docker exec -it logscrawler-ollama ollama pull llama3.2

# Or larger and more capable (requires more GPU memory)
docker exec -it logscrawler-ollama ollama pull llama3.1:8b
```

## Step 3: Verify Everything is Running

```bash
# Check containers
docker ps

# Check logs
docker logs logscrawler
docker logs logscrawler-ollama

# Verify GPU is detected (if using GPU)
docker exec -it logscrawler-ollama nvidia-smi
```

## Step 4: Access the Dashboard

Open your browser:
```
http://localhost:8000
```

## Step 5: Test It Out

1. **View Containers**: Click "Containers" in the sidebar
2. **Watch Live Logs**: Click "Live Logs" to see real-time streaming
3. **Ask AI**: Click "AI Assistant" and try:
   - "Are there any errors in the recent logs?"
   - "What containers are running?"
   - "Summarize the recent activity"

## Troubleshooting

### Ollama not responding?
```bash
# Check if it's running
docker ps | grep ollama

# Check logs
docker logs logscrawler-ollama

# Restart if needed
docker-compose restart ollama
```

### No containers showing?
- Make sure you have some Docker containers running
- Check Docker socket permissions (Linux/Mac)
- Verify Docker Desktop is running (Windows)

### GPU not working?
- Verify NVIDIA drivers: `nvidia-smi`
- Check NVIDIA Container Toolkit is installed
- Try CPU mode: `docker-compose -f docker-compose.cpu.yml up -d`

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Configure environment variables in `.env` (optional)
- Try different LLM models for better results
- Explore the API at `http://localhost:8000/docs`

Enjoy monitoring your Docker logs! 🚀
