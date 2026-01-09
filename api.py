"""
CAT Tiles API - FastAPI Tile Server for Posit Connect Deployment
Serves Cloud-Optimized GeoTIFF tiles from Google Cloud Storage

This is a standalone tile server designed to be deployed to Posit Connect,
allowing the CAT frontend to be hosted on GitHub Pages as a static site.
"""

import os
import logging
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import unquote, urlencode
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from titiler.core.factory import TilerFactory
import warnings

# =============================================================================
# GDAL CONFIGURATION - MUST BE FIRST, BEFORE ANY RASTERIO/TITILER IMPORTS!
# =============================================================================
# Configure GDAL/Rasterio for Google Cloud Storage access
os.environ['GDAL_DISABLE_READDIR_ON_OPEN'] = 'EMPTY_DIR'
os.environ['CPL_VSIL_CURL_ALLOWED_EXTENSIONS'] = '.tif,.tiff'
os.environ['GDAL_HTTP_TIMEOUT'] = '300'
os.environ['GDAL_HTTP_CONNECTTIMEOUT'] = '60'

# Enable anonymous/public access to GCS buckets
# This allows accessing public buckets without authentication
os.environ['GS_NO_SIGN_REQUEST'] = 'YES'

# =============================================================================
# Configuration
# =============================================================================

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Posit Connect root path (adjust if needed)
root_path = os.getenv("FASTAPI_ROOT_PATH", "/cat-tiles")

# Check if GCS credentials are available
gcs_creds = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
if gcs_creds:
    logger.info(f"✅ GCS credentials found: {gcs_creds}")
    logger.info("📌 Will use authenticated access for GCS (credentials take precedence)")
else:
    logger.info("ℹ️ No GOOGLE_APPLICATION_CREDENTIALS set")
    logger.info(f"📌 Using anonymous/public access for GCS buckets (GS_NO_SIGN_REQUEST={os.environ.get('GS_NO_SIGN_REQUEST')})")

# Warning suppression for TileMatrix
SUPPRESS_TILEMATRIX_WARNINGS = True
if SUPPRESS_TILEMATRIX_WARNINGS:
    warnings.filterwarnings(
        'ignore',
        message='TileMatrix not found for level.*',
        category=UserWarning,
        module='morecantile.models'
    )

# CORS Configuration - Allow GitHub Pages to access this API
CORS_ORIGINS = [
    "https://michaelakridge-noaa.github.io",  # Your GitHub Pages domain
    "http://localhost:8000",                   # Local development
    "http://localhost:5500",                   # Live Server
    "http://127.0.0.1:8000",                   # Local development
    "*"                                         # Allow all (remove in production)
]

# =============================================================================
# Lifespan Event Handler (Modern FastAPI pattern)
# =============================================================================

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    # Startup
    logger.info("=" * 80)
    logger.info("🚀 CAT Tiles API Starting Up")
    logger.info("=" * 80)
    logger.info(f"📍 Root Path: {root_path}")
    logger.info(f"🌐 CORS Origins: {CORS_ORIGINS}")
    logger.info(f"☁️ GCS Public Access: {os.environ.get('GS_NO_SIGN_REQUEST')}")
    logger.info(f"📝 Docs available at: {root_path}/docs")
    logger.info("=" * 80)
    
    yield
    
    # Shutdown (if needed)
    logger.info("🛑 CAT Tiles API Shutting Down")

# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="🪸 Coral Annotation Tool (CAT) Tiles API",
    description="""![CAT Logo](./images/icon.png)
    
    **Cloud-Optimized GeoTIFF Tile Server for CAT (Coral Annotation Tool)**
    
    This API provides tile serving capabilities for COG files stored in Google Cloud Storage.
    
    ## Features:
    - 🗺️ Dynamic tile generation from COG files
    - ☁️ Google Cloud Storage support (public buckets)
    - 🎯 TiTiler integration for optimal performance
    - 🔍 Metadata and statistics endpoints
    
    ## Endpoints:
    - `/tiles/{z}/{x}/{y}` - Get map tiles
    - `/WebMercatorQuad/tiles/{z}/{x}/{y}` - Get map tiles (TMS)
    - `/info` - Get raster metadata
    - `/WebMercatorQuad/tilejson.json` - Get TileJSON with WGS84 bounds
    - `/statistics` - Get raster statistics
    - `/preview` - Get preview image
    
    ## Usage:
    Pass a GCS URL in the `url` parameter:
    - `gs://bucket-name/path/to/file.tif`
    - `/vsigs/bucket-name/path/to/file.tif`
    
    ## Example:
    ```
    GET /info?url=gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/StRS_Sites_Products/orthomosaic_cog/2025_GUA-2838_mos_cog.tif
    ```
    
    ---
    
    **Contact:** Michael.Akridge@noaa.gov  
    **GitHub:** https://github.com/MichaelAkridge-NOAA/cat
    """,
    version="1.0.0",
    root_path=root_path,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# =============================================================================
# Mount Static Files (Images)
# =============================================================================

# Get the directory where api.py is located
BASE_DIR = Path(__file__).parent

# Mount images directory
images_dir = BASE_DIR / "images"
if images_dir.exists():
    app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")
    logger.info(f"📁 Mounted images directory: {images_dir}")

# =============================================================================
# CORS Middleware
# =============================================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# Middleware for Path Handling
# =============================================================================

@app.middleware("http")
async def log_and_handle_gcs_paths(request: Request, call_next):
    """
    Middleware to log requests and handle GCS paths.
    Supports gs://, /vsigs/, and http(s):// URLs.
    """
    if request.url.path.startswith(('/tiles/', '/info', '/bounds', '/statistics', '/preview')):
        url_param = request.query_params.get('url')
        if url_param:
            decoded_url = unquote(url_param)
            
            logger.info(f"🔍 TiTiler request: {request.url.path}")
            logger.info(f"   Original URL param: {url_param}")
            logger.info(f"   Decoded URL: {decoded_url}")
            
            # Check for special paths
            is_url = decoded_url.startswith(('http://', 'https://'))
            is_gcs = decoded_url.startswith('gs://')
            is_vsigs = decoded_url.startswith('/vsigs/')
            
            logger.info(f"   Flags: url={is_url}, gcs={is_gcs}, vsigs={is_vsigs}")
            logger.info(f"   ✅ Path passed through to TiTiler")
    
    response = await call_next(request)
    return response

# =============================================================================
# Custom Path Dependency for TiTiler
# =============================================================================

def GCSPathParams(url: str = Query(..., description="Dataset URL (gs://, /vsigs/, or http(s)://)")):
    """
    Custom path dependency that allows GDAL virtual file systems like /vsigs/ and gs://
    This bypasses TiTiler's default path validation.
    """
    logger.info(f"🔧 GCSPathParams received: {url}")
    return url

# =============================================================================
# TiTiler Factory
# =============================================================================

# Create TilerFactory with custom path dependency for GCS support
cog = TilerFactory(path_dependency=GCSPathParams)

# Register all COG endpoints automatically
# This creates routes like:
# - /tiles/{z}/{x}/{y}@{scale}x
# - /info
# - /statistics
# - /bounds
# - /preview
# - /WebMercatorQuad/tiles/{z}/{x}/{y}
app.include_router(cog.router, tags=["Cloud Optimized GeoTIFF"])

# =============================================================================
# Health Check & Info Endpoints
# =============================================================================

@app.get("/", tags=["Health"], response_class=HTMLResponse)
async def root():
    """Root endpoint - API information"""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>CAT Tiles API</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 900px;
                margin: 50px auto;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
            }}
            .container {{
                background: white;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            }}
            h1 {{
                color: #667eea;
                margin: 0 0 10px 0;
            }}
            .subtitle {{
                color: #666;
                margin-bottom: 30px;
                font-size: 18px;
            }}
            .status {{
                display: inline-block;
                background: #28a745;
                color: white;
                padding: 6px 12px;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 600;
                margin-bottom: 20px;
            }}
            .section {{
                margin: 30px 0;
            }}
            .section h2 {{
                color: #333;
                font-size: 20px;
                margin-bottom: 15px;
                border-bottom: 2px solid #667eea;
                padding-bottom: 8px;
            }}
            .endpoint {{
                background: #f8f9fa;
                padding: 12px;
                border-left: 4px solid #667eea;
                margin: 10px 0;
                border-radius: 4px;
            }}
            .endpoint code {{
                color: #764ba2;
                font-weight: 600;
            }}
            a {{
                color: #667eea;
                text-decoration: none;
                font-weight: 600;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            .button {{
                display: inline-block;
                background: #667eea;
                color: white;
                padding: 12px 24px;
                border-radius: 6px;
                text-decoration: none;
                font-weight: 600;
                margin: 10px 10px 10px 0;
                transition: background 0.3s;
            }}
            .button:hover {{
                background: #5568d3;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div style="text-align: center; margin-bottom: 20px;">
                <img src="{root_path}/images/icon.png" alt="CAT Logo" style="width: 100px; height: 100px;">
            </div>
            <h1 style="text-align: center;">🪸 CAT Tiles API</h1>
            <div class="subtitle" style="text-align: center;">Cloud-Optimized GeoTIFF Tile Server</div>
            <div style="text-align: center;">
                <div class="status">✓ Operational</div>
            </div>
            
            <div class="section">
                <h2>Quick Links</h2>
                <a href="{root_path}/docs" class="button">📚 API Documentation</a>
                <a href="{root_path}/health" class="button">🏥 Health Check</a>
            </div>
            
            <div class="section">
                <h2>📡 Key Endpoints</h2>
                <div class="endpoint">
                    <strong>Get Tile Info:</strong><br>
                    <code>GET {root_path}/info?url=gs://bucket/file.tif</code>
                </div>
                <div class="endpoint">
                    <strong>Get Map Tiles:</strong><br>
                    <code>GET {root_path}/tiles/{{z}}/{{x}}/{{y}}?url=gs://bucket/file.tif</code>
                </div>
                <div class="endpoint">
                    <strong>Get TileJSON (with bounds):</strong><br>
                    <code>GET {root_path}/WebMercatorQuad/tilejson.json?url=gs://bucket/file.tif</code>
                </div>
                <div class="endpoint">
                    <strong>Get Statistics:</strong><br>
                    <code>GET {root_path}/statistics?url=gs://bucket/file.tif</code>
                </div>
            </div>
            
            <div class="section">
                <h2>🌊 Example Usage</h2>
                <div class="endpoint">
                    <strong>NOAA PIFSC Orthomosaic:</strong><br>
                    <code>gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/StRS_Sites_Products/orthomosaic_cog/2025_GUA-2838_mos_cog.tif</code><br><br>
                    <a href="{root_path}/info?url=gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/StRS_Sites_Products/orthomosaic_cog/2025_GUA-2838_mos_cog.tif">View Info →</a>
                </div>
            </div>
            
            <div class="section">
                <h2>ℹ️ About</h2>
                <p>This API serves Cloud-Optimized GeoTIFF (COG) tiles from Google Cloud Storage public buckets using TiTiler.</p>
                <p><strong>Features:</strong> GCS support, WGS84 bounds, statistics, preview images, metadata</p>
                <p><strong>Contact:</strong> Michael.Akridge@noaa.gov</p>
            </div>
        </div>
    </body>
    </html>
    """

@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "message": "CAT Tiles API is operational",
        "gcs_enabled": os.environ.get('GS_NO_SIGN_REQUEST') == 'YES',
        "gdal_config": {
            "GDAL_DISABLE_READDIR_ON_OPEN": os.environ.get('GDAL_DISABLE_READDIR_ON_OPEN'),
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": os.environ.get('CPL_VSIL_CURL_ALLOWED_EXTENSIONS'),
            "GS_NO_SIGN_REQUEST": os.environ.get('GS_NO_SIGN_REQUEST')
        }
    }

# =============================================================================
# Example Test Endpoint (Optional - for debugging)
# =============================================================================

@app.get("/test-gcs", tags=["Testing"])
async def test_gcs_access(
    bucket: str = "nmfs_odp_pifsc", 
    file_path: str = "PIFSC/ESD/ARP/StRS_Sites_Products/orthomosaic_cog/2025_GUA-2838_mos_cog.tif"
):
    """
    Test endpoint to verify GCS access is working.
    
    Default example uses a real NOAA PIFSC orthomosaic COG file.
    
    Example: /test-gcs
    Example: /test-gcs?bucket=your-bucket&file_path=path/to/file.tif
    """
    import rasterio
    
    test_url = f"gs://{bucket}/{file_path}"
    
    try:
        with rasterio.open(test_url) as src:
            return {
                "status": "success",
                "url": test_url,
                "driver": src.driver,
                "width": src.width,
                "height": src.height,
                "crs": str(src.crs),
                "bounds": src.bounds
            }
    except Exception as e:
        logger.error(f"❌ Failed to access GCS file: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to access GCS file: {str(e)}"
        )

# =============================================================================
# Main (for local testing only)
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("🧪 Running in local development mode")
    logger.info("🌐 Access API at: http://localhost:8000")
    logger.info("📚 Access docs at: http://localhost:8000/docs")
    
    # Use import string for reload to work properly
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )