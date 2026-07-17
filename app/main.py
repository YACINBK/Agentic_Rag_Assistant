from fastapi import FastAPI

from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="Whitecape Knowledge Assistant", version="0.1.0")


@app.on_event("startup")
async def on_startup():
    logger.info("application_started")


@app.get("/health")
async def health():
    return {"status": "ok"}
