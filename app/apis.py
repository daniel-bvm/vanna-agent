from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from app.oai_models import ChatCompletionRequest, APIStatus, ResponseMessage, ChatCompletionAdditionalParameters, ChatTemplate, ChatCompletionStreamResponse
from app.db_models import (
    PostgresAuth, 
    MySQLAuth, 
    MSSQLAuth, 
    SnowflakeAuth, 
    BigQueryAuth, 
    OracleAuth, 
    OtherDBAuth,
    DBAuthorization,
    DatabaseAuth,
    validate_db_auth
)
import logging
from pydantic import BaseModel
from typing import Any, Optional, AsyncGenerator
from app.vanna_session import get_singleton_session
import asyncio
import time
from app.configs import settings

class EventSignalHandler():
    def __init__(self):
        self.event_signals: dict[str, asyncio.Event] = {}

    def register_event_signal(self, event_id: str) -> asyncio.Event:
        event: asyncio.Event = asyncio.Event()
        logger.info(f"Request {event_id} - Registering event signal")
        self.event_signals[event_id] = event
        return event

    def emit_event_signal(self, event_id: str):

        if event_id in self.event_signals:
            logger.info(f"Request {event_id} - Emitting event signal")
            self.event_signals[event_id].set()
        else:
            logger.warning(f"Request {event_id} - Event signal not found")

    def unregister_event_signal(self, event_id: str):
        logger.info(f"Request {event_id} - Unregistering event signal")
        del self.event_signals[event_id]

event_handler = EventSignalHandler()

logger = logging.getLogger(__name__)
api_router = APIRouter()

@api_router.post("/prompt")
async def chat_completions(request: ChatCompletionRequest, original_request: Request):
    orig_body: dict = await original_request.json()
    request_id = orig_body.get("id", request.request_id)
    stream = request.stream 
    event = event_handler.register_event_signal(request_id)

    try:
        additional_parameters: Optional[ChatCompletionAdditionalParameters] = (
            ChatCompletionAdditionalParameters.model_validate(orig_body) 
            if orig_body.get("chat_template_kwargs")
            else ChatCompletionAdditionalParameters(chat_template_kwargs=ChatTemplate(enable_thinking=True))
        )
    except Exception as e:
        logger.error(f"Invalid additional parameters: {e}")
        additional_parameters = ChatCompletionAdditionalParameters(chat_template_kwargs=ChatTemplate(enable_thinking=True))
        
    session = get_singleton_session()
    enqueued = time.time()
    generator = session.handle_request(request, event, additional_parameters)
    ttft, tps, n_tokens = 0, 0, 0

    if stream:
        async def to_bytes(gen: AsyncGenerator) -> AsyncGenerator[bytes, None]:
            nonlocal ttft, tps, n_tokens

            try:
                async for chunk in gen:
                    current_time = time.time()

                    n_tokens += 1
                    ttft = min(ttft, current_time - enqueued)
                    tps = n_tokens / (current_time - enqueued)

                    if isinstance(chunk, ChatCompletionStreamResponse):
                        data = chunk.model_dump_json()
                        yield "data: " + data + "\n\n"

                logger.info(f"Request {request_id} - TTFT: {ttft:.2f}s, TPS: {tps:.2f} tokens/s")

            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(to_bytes(generator), media_type="text/event-stream")
    
    else:
        async for chunk in generator:
            current_time = time.time()

            n_tokens += 1
            ttft = min(ttft, current_time - enqueued)
            tps = n_tokens / (current_time - enqueued)

        logger.info(f"Request {request_id} - TTFT: {ttft:.2f}s, TPS: {tps:.2f} tokens/s")
        return JSONResponse(chunk.model_dump())

@api_router.post("/cancel")
async def cancel(request: Request):
    id: str = request.query_params.get("id")

    if not id:
        raise HTTPException(status_code=400, detail="id is required")
    
    event_handler.emit_event_signal(id)
    return JSONResponse({"status": "ok"})

@api_router.post("/v1/connect")
async def connect(request: Request) -> ResponseMessage[str]:
    auth = validate_db_auth(await request.json())
    session = get_singleton_session()

    if not await session.validate(auth):
        raise HTTPException(status_code=400, detail="Invalid database authentication")

    return ResponseMessage[str](result="connected", status=APIStatus.OK)

@api_router.post("/v1/state")
async def state() -> ResponseMessage[Optional[DatabaseAuth]]:
    session = get_singleton_session()

    if not session.session_validated:
        return ResponseMessage[Optional[DatabaseAuth]](result=None, status=APIStatus.ERROR)

    return ResponseMessage[Optional[DatabaseAuth]](result=session.auth, status=APIStatus.OK)

@api_router.post("/v1/disconnect")
async def disconnect(request: Request):
    session = get_singleton_session()
    session.invalidate()
    return ResponseMessage[str](result="disconnected", status=APIStatus.OK)

db_models: dict[str, BaseModel] = {
    'postgres': PostgresAuth,
    'mysql': MySQLAuth,
    'mssql': MSSQLAuth,
#    'sqlite': SQLiteAuth,
#    'duckdb': DuckDBAuth,
#    'snowflake': SnowflakeAuth,
#    'bigquery': BigQueryAuth,
#    'oracle': OracleAuth,
#    'other': OtherDBAuth
}

@api_router.get("/db/schemas/{db_type}")
async def get_db_schemas(db_type: str):
    if db_type not in db_models:
        raise HTTPException(status_code=404, detail=f"Database type {db_type} not found")
    return db_models[db_type].model_json_schema()

@api_router.post("/db")
async def get_supported_db_types() -> list[str]:
    return list(db_models.keys())

@api_router.get("/processing-url")
def get_processing_url() -> dict:
    return {
        "url": f"http://localhost:12345/index.html",
        "status": "ready"
    }