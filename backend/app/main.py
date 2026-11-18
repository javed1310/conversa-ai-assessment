"""API lifecycle, safe errors, health and validated business metadata."""

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.agent import GeminiAgent
from app.automation import AutomationWorker, SummaryPlanner
from app.businesses import BusinessList, BusinessRegistry, BusinessSummary
from app.channels import (
    AdapterRegistry,
    MockAdapter,
    MockMessage,
    MockReply,
    SessionStart,
    SessionStarted,
    WebAdapter,
    WebMessage,
)
from app.contracts import (
    ChatResult,
    ErrorDetail,
    ErrorEnvelope,
    Identifier,
    ProcessingResult,
    ReadinessResult,
)
from app.database import Database, open_database
from app.embeddings import EmbeddingFailure, EmbeddingProvider, GeminiEmbedder
from app.engine import ConversationEngine, UnavailableEngine
from app.gateway import Gateway, GatewayFailure
from app.gemini import GeminiClient
from app.generation import GenerationService
from app.inspection import router as inspection_router
from app.knowledge import KnowledgeFailure, KnowledgeReader
from app.provider_control import ProviderControl
from app.repositories import RecordConflict, RecordNotFound
from app.settings import Settings, load_settings
from app.tools import build_tool_registry

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """Process-local lifecycle state; the database is owned by the enclosing lifespan."""

    started_at: datetime
    active: bool = True


def request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    correlation_id = request_id(request)
    return JSONResponse(
        status_code=status,
        content=ErrorEnvelope(
            error=ErrorDetail(
                code=code, message=message, retryable=status >= 500, request_id=UUID(correlation_id)
            )
        ).model_dump(mode="json", by_alias=True),
        headers={"X-Request-ID": correlation_id},
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: ConversationEngine | None = None,
    summary_planner: SummaryPlanner | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> FastAPI:
    configuration = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = Runtime(started_at=datetime.now(UTC))
        application.state.business_registry = BusinessRegistry.load(
            configuration.business_config_dir
        )
        application.state.runtime = runtime
        try:
            tools = build_tool_registry()
            for business in application.state.business_registry.configurations():
                tools.validate_business(business)
            application.state.tool_registry = tools
            for business_id in configuration.mock_channel_keys:
                if application.state.business_registry.get(business_id) is None:
                    raise ValueError("Mock channel mapping references an unknown business")
            control = ProviderControl(configuration)
            application.state.provider_control = control
            async with (
                GeminiClient(configuration, provider_control=control) as provider,
                open_database(configuration) as database,
            ):
                application.state.provider = provider
                application.state.database = database
                embedder = GeminiEmbedder(configuration, provider_control=control)
                application.state.embedder = embedding_provider or embedder
                worker: AutomationWorker | None = None
                try:
                    selected_engine: ConversationEngine = engine or UnavailableEngine()
                    if (
                        engine is None
                        and configuration.ai_chat_enabled
                        and configuration.gemini_api_key is not None
                        and configuration.gemini_api_key.get_secret_value().strip()
                    ):
                        selected_engine = GeminiAgent(
                            database,
                            application.state.business_registry,
                            tools,
                            provider,
                            KnowledgeReader(
                                database,
                                application.state.business_registry,
                                application.state.embedder,
                                configuration,
                            ),
                            configuration,
                        )
                    application.state.gateway = Gateway(
                        database,
                        application.state.business_registry,
                        AdapterRegistry((WebAdapter(), MockAdapter())),
                        selected_engine,
                    )
                    if (
                        configuration.automation_enabled
                        and configuration.gemini_api_key is not None
                        and configuration.gemini_api_key.get_secret_value().strip()
                    ):
                        worker = AutomationWorker(
                            database,
                            application.state.business_registry,
                            summary_planner or GenerationService(provider),
                            configuration,
                        )
                        application.state.automation_worker = worker
                        worker.start()
                    yield
                finally:
                    if worker is not None:
                        await worker.close()
                    await embedder.close()
        finally:
            runtime.active = False
            application.state.runtime = None
            application.state.business_registry = None
            application.state.database = None
            application.state.tool_registry = None
            application.state.gateway = None
            application.state.provider = None
            application.state.provider_control = None
            application.state.embedder = None
            application.state.automation_worker = None

    application = FastAPI(
        title="AI Customer Engagement API",
        version="0.1.0",
        description=(
            "Local assessment API. Business metadata and database readiness are available. "
            "Web messages use the Gemini agent when a backend key is configured. "
            "Validation errors use 400."
        ),
        responses={
            400: {"model": ErrorEnvelope},
            404: {"model": ErrorEnvelope},
            500: {"model": ErrorEnvelope},
        },
        debug=False,
        lifespan=lifespan,
    )
    application.state.settings = configuration
    application.state.runtime = None
    application.state.business_registry = None
    application.state.database = None
    application.state.tool_registry = None
    application.state.gateway = None
    application.state.automation_worker = None
    application.state.provider = None
    application.state.embedder = None
    application.include_router(inspection_router)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[configuration.web_origin],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Session-Token"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
    )

    @application.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Do not reflect untrusted incoming IDs into logs or headers.
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(request, 400, "VALIDATION_ERROR", "Request validation failed.")

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        code = "RESOURCE_NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        message = (
            "Resource not found." if error.status_code == 404 else "Request could not be processed."
        )
        return error_response(request, error.status_code, code, message)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.error("Unhandled %s; request_id=%s", type(error).__name__, request_id(request))
        return error_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred.")

    @application.exception_handler(RecordNotFound)
    async def missing_record(request: Request, _error: RecordNotFound) -> JSONResponse:
        return error_response(request, 404, "RESOURCE_NOT_FOUND", "Resource not found.")

    @application.exception_handler(RecordConflict)
    async def conflicting_record(request: Request, _error: RecordConflict) -> JSONResponse:
        if request.method == "POST" and request.url.path.endswith("/documents"):
            return error_response(
                request,
                409,
                "DOCUMENT_VERSION_CONFLICT",
                "This document version has different saved content. Use a new version.",
            )
        return error_response(
            request, 409, "MESSAGE_CONFLICT", "Message identity conflicts with saved data."
        )

    @application.exception_handler(GatewayFailure)
    async def gateway_failure(request: Request, error: GatewayFailure) -> JSONResponse:
        return error_response(request, error.status, error.code, error.message)

    @application.exception_handler(KnowledgeFailure)
    @application.exception_handler(EmbeddingFailure)
    async def knowledge_failure(
        request: Request, error: KnowledgeFailure | EmbeddingFailure
    ) -> JSONResponse:
        if (
            isinstance(error, KnowledgeFailure)
            and error.code == "CORPUS_LIMIT"
            and request.method == "POST"
            and request.url.path.endswith("/documents")
        ):
            return error_response(
                request,
                409,
                "CORPUS_LIMIT",
                "This document would exceed the configured knowledge capacity. "
                "Check its saved status and use a smaller document or replacement version.",
            )
        return error_response(
            request,
            503,
            "KNOWLEDGE_UNAVAILABLE",
            "Document processing could not finish. Check its saved status before retrying.",
        )

    @application.get("/api/v1/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        # Liveness is not database/provider readiness and performs no outbound calls.
        return {"status": "alive", "service": "ai-engagement-api"}

    @application.get(
        "/api/v1/health/ready",
        tags=["health"],
        response_model=ReadinessResult,
        responses={503: {"model": ErrorEnvelope, "description": "Database unavailable"}},
    )
    async def readiness(request: Request) -> ReadinessResult | JSONResponse:
        database: Database | None = application.state.database
        if database is None or not await database.ready():
            return error_response(request, 503, "DATABASE_UNAVAILABLE", "Database is unavailable.")
        return ReadinessResult()

    @application.get("/api/v1/businesses", response_model=BusinessList, tags=["businesses"])
    async def businesses() -> BusinessList:
        registry: BusinessRegistry = application.state.business_registry
        return registry.summaries()

    @application.get(
        "/api/v1/businesses/{business_id}",
        response_model=BusinessSummary,
        tags=["businesses"],
    )
    async def business(
        business_id: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ) -> BusinessSummary:
        registry: BusinessRegistry = application.state.business_registry
        configuration = registry.get(business_id)
        if configuration is None:
            raise HTTPException(404)
        return BusinessSummary(
            business_id=configuration.business_id,
            business_name=configuration.business_name,
            agent_role=configuration.agent_role,
        )

    @application.post(
        "/api/v1/businesses/{business_id}/sessions",
        response_model=SessionStarted,
        status_code=201,
        tags=["web"],
    )
    async def start_session(
        business_id: Identifier,
        body: SessionStart,
        response: Response,
    ) -> SessionStarted:
        response.headers["Cache-Control"] = "no-store"
        gateway: Gateway = application.state.gateway
        return await gateway.start_web_session(business_id)

    @application.post(
        "/api/v1/businesses/{business_id}/sessions/{session_id}/messages",
        response_model=ChatResult,
        tags=["web"],
        responses={
            202: {"model": ProcessingResult},
            409: {"model": ErrorEnvelope},
            503: {"model": ErrorEnvelope},
        },
    )
    async def web_message(
        business_id: Identifier,
        session_id: Identifier,
        body: WebMessage,
        request: Request,
        token: Annotated[str | None, Header(alias="X-Session-Token")] = None,
    ) -> JSONResponse:
        gateway: Gateway = application.state.gateway
        context = await gateway.web_context(
            business_id, session_id, token, UUID(request_id(request))
        )
        delivery = await gateway.handle(context, body.model_dump(mode="python"))
        status = 202 if isinstance(delivery.payload, ProcessingResult) else 200
        return JSONResponse(
            status_code=status, content=delivery.payload.model_dump(mode="json", by_alias=True)
        )

    @application.post(
        "/api/v1/mock/messages",
        response_model=MockReply,
        tags=["mock"],
        responses={
            202: {"model": MockReply},
            409: {"model": ErrorEnvelope},
            503: {"model": ErrorEnvelope},
        },
    )
    async def mock_message(
        body: MockMessage,
        request: Request,
        key: Annotated[str | None, Header(alias="X-Mock-Channel-Key")] = None,
    ) -> JSONResponse:
        business_id = None
        if key is not None and 32 <= len(key) <= 128 and key.isascii():
            for identifier, configured_key in configuration.mock_channel_keys.items():
                if secrets.compare_digest(key, configured_key.get_secret_value()):
                    business_id = identifier
        if business_id is None:
            raise RecordNotFound("Record not found.")
        gateway: Gateway = application.state.gateway
        context = await gateway.mock_context(business_id, body.author, UUID(request_id(request)))
        delivery = await gateway.handle(context, body.model_dump(mode="python"))
        outgoing = MockReply(
            recipient=delivery.recipient_id,
            in_reply_to=delivery.payload.message_id,
            result=delivery.payload,
        )
        return JSONResponse(
            status_code=202 if isinstance(delivery.payload, ProcessingResult) else 200,
            content=outgoing.model_dump(mode="json", by_alias=True),
            headers={"Cache-Control": "no-store"},
        )

    original_openapi = application.openapi

    def openapi_without_default_422() -> dict[str, object]:
        schema = original_openapi()
        for path in schema["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation["responses"].pop("422", None)
        return schema

    application.openapi = openapi_without_default_422  # type: ignore[method-assign]
    return application
