import fastapi
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from app.configs import settings
from app.apis import api_router
import logging
import asyncio
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    PROXY_PORT = 12345

    logger.info(f"Starting socat proxy on port {PROXY_PORT}")
    command = f"socat TCP-LISTEN:{PROXY_PORT},fork TCP:localhost:{settings.port}"
    process = await asyncio.create_subprocess_shell(command, stdout=sys.stdout, stderr=sys.stderr)

    try:
        yield
    finally:
        process.terminate()

    yield

server_app = fastapi.FastAPI(lifespan=lifespan)

server_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

server_app.include_router(api_router)
server_app.mount("/", StaticFiles(directory="public"), name="static")

@server_app.get("/health")
async def healthcheck():
    return {"status": "ok", "message": "Yo, I am alive"}

def main():
    uvicorn.run(server_app, host=settings.host, port=settings.port)

if __name__ == '__main__':
    main()
