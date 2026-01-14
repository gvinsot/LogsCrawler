"""Main FastAPI application entry point."""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path

from app.config import settings
from app.api.routes import router as api_router
from app.api.websocket import router as ws_router
from app.api.remote_routes import router as remote_router
from app.services.docker_service import docker_service
from app.services.ai_service import ai_service
from app.services.remote_systems_service import remote_systems_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


# Background task for periodic log scanning
async def periodic_log_scan():
    """Periodically scan logs for issues."""
    while True:
        try:
            await asyncio.sleep(settings.analysis_interval)
            
            # Quick scan for issues
            if docker_service.is_connected():
                logs = docker_service.get_all_logs(tail=50)
                if logs:
                    await ai_service.quick_issue_check(logs)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# Background task for log ingestion (RAG)
async def periodic_log_ingestion():
    """Periodically ingest logs for RAG."""
    from app.services.log_processor import log_processor
    
    while True:
        try:
            await asyncio.sleep(30)  # Ingest every 30 seconds
            
            if docker_service.is_connected():
                # Ingest recent logs from all running containers
                containers = docker_service.get_containers(all_containers=False)
                for container in containers:
                    try:
                        logs = docker_service.get_logs(
                            container_id=container.id,
                            tail=50,
                        )
                        for log in logs:
                            await log_processor.ingest_log(
                                container_id=log.container_id,
                                container_name=log.container_name,
                                message=log.message,
                                timestamp=log.timestamp,
                            )
                    except Exception as e:
                        logger.debug(f"Error ingesting logs from {container.name}: {e}")
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in log ingestion: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    print(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    
    # Check Docker connection
    if docker_service.is_connected():
        print("✅ Docker daemon connected")
        try:
            containers = docker_service.get_containers()
            print(f"   Found {len(containers)} containers")
        except Exception as e:
            print(f"   ⚠️  Warning: Could not list containers: {e}")
    else:
        print("⚠️  Docker daemon not connected")
        # Try to get more details
        try:
            docker_service.client.ping()
        except Exception as e:
            print(f"   Error: {e}")
            print("   Make sure:")
            print("   - Docker is running")
            print("   - Docker socket is mounted: /var/run/docker.sock")
            print("   - Socket permissions are correct")
    
    # Check AI service
    if await ai_service.check_connection():
        print(f"✅ AI service connected (Ollama)")
        models = await ai_service.get_available_models()
        print(f"   Available models: {', '.join(models[:5]) or 'None found'}")
    else:
        print("⚠️  AI service not connected (Ollama)")
        print(f"   Make sure Ollama is running at {settings.ollama_host}")
    
    # Initialize RAG components
    ingestion_task = None
    if settings.rag_enabled:
        print("🔧 Initializing RAG system...")
        
        # Connect to MongoDB
        from app.services.storage_service import storage_service
        if await storage_service.connect():
            print("✅ MongoDB connected")
        else:
            print("⚠️  MongoDB not connected - RAG statistics disabled")
        
        # Initialize vector service (usearch)
        from app.services.vector_service import vector_service
        stats = vector_service.get_stats()
        print(f"✅ Vector index ready ({stats['total_vectors']} vectors)")
        
        # Initialize AI service RAG
        if await ai_service.initialize_rag():
            print("✅ RAG system initialized")
        
        # Pull embedding model if not available
        if await ai_service.check_connection():
            try:
                async with __import__('httpx').AsyncClient() as client:
                    response = await client.post(
                        f"{settings.ollama_host}/api/pull",
                        json={"name": settings.embedding_model, "stream": False},
                        timeout=300.0
                    )
                    if response.status_code == 200:
                        print(f"✅ Embedding model ready ({settings.embedding_model})")
            except Exception as e:
                print(f"⚠️  Could not pull embedding model: {e}")
        
        # Initialize remote systems service with MongoDB
        await remote_systems_service.initialize(storage_service._db)
        print(f"✅ Remote systems service initialized ({len(remote_systems_service.get_all_systems())} systems)")
        
        # Start background log ingestion
        ingestion_task = asyncio.create_task(periodic_log_ingestion())
        print("✅ Background log ingestion started")
    else:
        # Initialize remote systems without persistence
        await remote_systems_service.initialize(None)
    
    # Start background task for issue scanning
    scan_task = asyncio.create_task(periodic_log_scan())
    
    yield
    
    # Shutdown
    scan_task.cancel()
    if ingestion_task:
        ingestion_task.cancel()
    
    try:
        await scan_task
    except asyncio.CancelledError:
        pass
    
    if ingestion_task:
        try:
            await ingestion_task
        except asyncio.CancelledError:
            pass
    
    # Close MongoDB connection
    if settings.rag_enabled:
        from app.services.storage_service import storage_service
        await storage_service.close()
    
    # Close remote SSH connections
    await remote_systems_service.close_all_connections()
    
    print("👋 Shutting down LogsCrawler")


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Docker Log Monitoring with AI-Powered Issue Detection",
    version=settings.app_version,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router)
app.include_router(ws_router)
app.include_router(remote_router)

# Static files
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    
    # Fallback if static files not found
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>LogsCrawler</title></head>
    <body>
        <h1>LogsCrawler</h1>
        <p>Static files not found. Please ensure the static directory exists.</p>
        <p><a href="/docs">API Documentation</a></p>
    </body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
