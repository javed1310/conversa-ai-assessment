"""Bounded Gemini generateContent REST boundary; never executes business tools."""

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError

from app.measurements import count, stage, usage
from app.provider_control import Lane, ProviderControl, ProviderControlFailure
from app.settings import Settings
from app.tools import ToolCall


class ProviderFailure(RuntimeError):
    def __init__(self, code: str, retryable: bool = False, *, detail: str = "") -> None:
        super().__init__("Generation operation unavailable.")
        self.code = code
        self.retryable = retryable
        self.detail = detail


def encode(value: object) -> str:
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ProviderFailure("INVALID_INPUT") from None


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    description: str
    arguments: type[BaseModel]

    def declaration(self) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.name):
            raise ProviderFailure("INVALID_INPUT")
        return {
            "name": self.name,
            "description": self.description,
            "parametersJsonSchema": self.arguments.model_json_schema(by_alias=True),
        }


@dataclass(frozen=True)
class ModelTurn:
    # JSON strings retain all opaque provider parts/signatures without exposing mutable history.
    content_json: str
    text: str
    calls: tuple[ToolCall, ...]
    provider_ids: tuple[str | None, ...]


@dataclass(frozen=True)
class ToolOutput:
    call_id: str
    data_json: str


@dataclass(frozen=True)
class Conversation:
    contents_json: str
    turn: ModelTurn
    rounds: int
    tool_calls: int


class GeminiClient:
    """One owner, explicit closure, no SDK retry multiplication or implicit live calls."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        provider_control: ProviderControl | None = None,
    ):
        self.settings = settings
        self.control = provider_control or ProviderControl(settings)
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False
        )
        self._closed = False

    async def __aenter__(self) -> "GeminiClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def _request(
        self, model: str, payload: dict[str, Any], *, lane: Lane = "chat"
    ) -> dict[str, Any]:
        if self._closed:
            raise ProviderFailure("CLIENT_CLOSED")
        key = self.settings.gemini_api_key
        if key is None or not key.get_secret_value().strip():
            raise ProviderFailure("MISSING_ACCESS")
        if model not in {self.settings.gemini_chat_model, self.settings.gemini_summary_model}:
            raise ProviderFailure("INVALID_INPUT")
        if len(encode(payload).encode()) > 131072:
            raise ProviderFailure("INPUT_TOO_LARGE")
        try:
            # Total operation budget includes both attempts and backoff.
            async with self.control.call(lane, self.settings.provider_generation_seconds):
                for attempt in range(self.settings.provider_attempts):
                    response = None
                    try:
                        response = await self.control.send(
                            self._client,
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                            headers={"x-goog-api-key": key.get_secret_value()},
                            payload=payload,
                            lane=lane,
                            attempt_seconds=self.settings.provider_generation_attempt_seconds,
                            kind="generation",
                        )
                    except (httpx.TimeoutException, TimeoutError):
                        failure = ProviderFailure("TIMEOUT", True)
                    except httpx.RequestError as error:
                        failure = ProviderFailure("UNAVAILABLE", True, detail=type(error).__name__)
                    else:
                        if response.status_code == 200:
                            if len(response.content) > 262144:
                                raise ProviderFailure("INVALID_RESPONSE")
                            try:
                                body = response.json()
                            except ValueError:
                                raise ProviderFailure("INVALID_RESPONSE") from None
                            if not isinstance(body, dict) or "error" in body:
                                raise ProviderFailure("INVALID_RESPONSE")
                            usage(body, "generation")
                            return body
                        if response.status_code in {401, 403}:
                            raise ProviderFailure("ACCESS_DENIED")
                        if response.status_code == 404:
                            raise ProviderFailure("MODEL_UNAVAILABLE")
                        if response.status_code == 429:
                            failure = ProviderFailure("RATE_LIMIT", True)
                        elif response.status_code >= 500:
                            failure = ProviderFailure(
                                "UNAVAILABLE", True, detail=f"HTTP_{response.status_code}"
                            )
                        else:
                            raise ProviderFailure("INVALID_REQUEST")
                    if attempt + 1 == self.settings.provider_attempts:
                        raise failure from None
                    if not await self.control.backoff(attempt, response):
                        raise failure from None
        except ProviderControlFailure as error:
            raise ProviderFailure(error.code, True) from None
        except TimeoutError:
            raise ProviderFailure("TIMEOUT", True) from None
        raise ProviderFailure("UNAVAILABLE", True)

    def _parse(
        self, body: dict[str, Any], tools: tuple[FunctionSpec, ...], *, allow_empty: bool = False
    ) -> ModelTurn:
        feedback = body.get("promptFeedback", {})
        if not isinstance(feedback, dict):
            raise ProviderFailure("INVALID_RESPONSE", detail="feedback_type")
        if feedback.get("blockReason") not in (None, "BLOCK_REASON_UNSPECIFIED"):
            raise ProviderFailure("REFUSED")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ProviderFailure("INVALID_RESPONSE", detail="candidate_count")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderFailure("INVALID_RESPONSE", detail="candidate_type")
        reason = candidate.get("finishReason")
        if not isinstance(reason, str):
            raise ProviderFailure("INVALID_RESPONSE", detail="finish_type")
        if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
            raise ProviderFailure("REFUSED")
        if reason != "STOP":
            raise ProviderFailure("INCOMPLETE_OUTPUT")
        content = candidate.get("content")
        if allow_empty and (
            content is None
            or (
                isinstance(content, dict)
                and content.get("role") in (None, "model")
                and content.get("parts") in (None, [])
            )
        ):
            # A planner may stop after receiving tool results without emitting prose.
            # This is not a structured answer and cannot itself claim any operation success.
            return ModelTurn(encode(content or {}), "", (), ())
        if not isinstance(content, dict) or content.get("role") != "model":
            raise ProviderFailure("INVALID_RESPONSE", detail="content_role")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts or len(parts) > 64:
            raise ProviderFailure("INVALID_RESPONSE", detail="parts_shape")
        catalog = {tool.name: tool for tool in tools}
        calls = []
        provider_ids = []
        texts = []
        seen = set()
        for part in parts:
            if not isinstance(part, dict):
                raise ProviderFailure("INVALID_RESPONSE", detail="part_type")
            if set(part) - {"text", "functionCall", "thought", "thoughtSignature"}:
                raise ProviderFailure("UNEXPECTED_PART")
            if "thought" in part and not isinstance(part["thought"], bool):
                raise ProviderFailure("INVALID_RESPONSE", detail="thought_type")
            if "thoughtSignature" in part and not isinstance(part["thoughtSignature"], str):
                raise ProviderFailure("INVALID_RESPONSE", detail="signature_type")
            if ("text" in part) == ("functionCall" in part):
                raise ProviderFailure("INVALID_RESPONSE", detail="part_union")
            if "text" in part:
                if not isinstance(part["text"], str):
                    raise ProviderFailure("INVALID_RESPONSE", detail="text_type")
                if not part.get("thought", False):
                    texts.append(part["text"])
                continue
            call = part["functionCall"]
            if not isinstance(call, dict) or set(call) - {"name", "args", "id"}:
                raise ProviderFailure("INVALID_TOOL_CALL")
            name = call.get("name")
            args = call.get("args", {})
            provider_id = call.get("id")
            if (
                not isinstance(name, str)
                or name not in catalog
                or not isinstance(args, dict)
                or (provider_id is not None and not isinstance(provider_id, str))
            ):
                raise ProviderFailure("INVALID_TOOL_CALL")
            call_id = provider_id if provider_id is not None else "call-" + uuid4().hex
            if call_id in seen:
                raise ProviderFailure("INVALID_TOOL_CALL")
            seen.add(call_id)
            try:
                # Normalization is identical to the actual application argument model.
                validated = catalog[name].arguments.model_validate(args)
                calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=validated.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    )
                )
            except ValidationError:
                raise ProviderFailure("INVALID_TOOL_ARGUMENTS") from None
            provider_ids.append(provider_id)
        if len(calls) > self.settings.agent_max_tool_calls:
            raise ProviderFailure("TOOL_BUDGET_EXCEEDED")
        text = "".join(texts)
        if not calls and not text.strip():
            raise ProviderFailure("EMPTY_OUTPUT")
        return ModelTurn(encode(content), text, tuple(calls), tuple(provider_ids))

    @stage("generation")
    async def generate(
        self,
        *,
        system: str,
        contents: list[dict[str, Any]],
        tools: tuple[FunctionSpec, ...] = (),
        schema: dict[str, Any] | None = None,
        summary: bool = False,
        force_tool: bool = False,
        allow_empty: bool = False,
    ) -> ModelTurn:
        if not system.strip() or not contents or (tools and schema is not None):
            raise ProviderFailure("INVALID_INPUT")
        if len(tools) > 16 or len({tool.name for tool in tools}) != len(tools):
            raise ProviderFailure("INVALID_INPUT")
        config: dict[str, Any] = {"maxOutputTokens": 4096, "candidateCount": 1}
        if schema is not None:
            config.update(responseMimeType="application/json", responseJsonSchema=schema)
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": config,
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": [tool.declaration() for tool in tools]}]
            payload["toolConfig"] = {
                "functionCallingConfig": {"mode": "ANY" if force_tool else "AUTO"}
            }
        elif force_tool:
            raise ProviderFailure("INVALID_INPUT")
        model = self.settings.gemini_summary_model if summary else self.settings.gemini_chat_model
        count("generation_calls")
        title = schema.get("title") if schema else None
        category = (
            {
                "StateDraft": "state_calls",
                "ReplySelection": "reply_calls",
                "SummaryPlan": "summary_calls",
                "SummaryDraft": "summary_calls",
            }.get(title)
            if isinstance(title, str)
            else None
        )
        count(
            category
            or (
                "summary_calls"
                if summary
                else "planner_calls"
                if tools
                else "other_generation_calls"
            )
        )
        return self._parse(
            await self._request(model, payload, lane="background" if summary else "chat"),
            tools,
            allow_empty=allow_empty,
        )

    async def start(
        self,
        system: str,
        customer_text: str,
        tools: tuple[FunctionSpec, ...],
        *,
        force_tool: bool = False,
    ) -> Conversation:
        if not customer_text.strip() or len(customer_text) > 8000:
            raise ProviderFailure("INVALID_INPUT")
        contents = [{"role": "user", "parts": [{"text": customer_text}]}]
        turn = await self.generate(
            system=system, contents=contents, tools=tools, force_tool=force_tool
        )
        return Conversation(encode(contents), turn, 1, len(turn.calls))

    @stage("planner")
    async def continue_with(
        self,
        conversation: Conversation,
        outputs: tuple[ToolOutput, ...],
        *,
        system: str,
        tools: tuple[FunctionSpec, ...],
        allow_empty: bool = False,
    ) -> Conversation:
        turn = conversation.turn
        if conversation.rounds >= self.settings.agent_max_rounds:
            raise ProviderFailure("ROUND_BUDGET_EXCEEDED")
        if (
            not turn.calls
            or len(outputs) != len(turn.calls)
            or {item.call_id for item in outputs} != {call.call_id for call in turn.calls}
        ):
            raise ProviderFailure("OUTPUT_MISMATCH")
        supplied = {item.call_id: item for item in outputs}
        parts = []
        for call, provider_id in zip(turn.calls, turn.provider_ids, strict=True):
            try:
                data = json.loads(supplied[call.call_id].data_json)
            except (ValueError, TypeError):
                raise ProviderFailure("INVALID_INPUT") from None
            if not isinstance(data, dict):
                raise ProviderFailure("INVALID_INPUT")
            response = {"name": call.name, "response": data}
            if provider_id is not None:
                response["id"] = provider_id
            parts.append({"functionResponse": response})
        contents = json.loads(conversation.contents_json)
        contents.extend([json.loads(turn.content_json), {"role": "user", "parts": parts}])
        following = await self.generate(
            system=system, contents=contents, tools=tools, allow_empty=allow_empty
        )
        tool_calls = conversation.tool_calls + len(following.calls)
        if tool_calls > self.settings.agent_max_tool_calls:
            raise ProviderFailure("TOOL_BUDGET_EXCEEDED")
        return Conversation(encode(contents), following, conversation.rounds + 1, tool_calls)

    @stage("planner")
    async def start_context(
        self, system: str, data: dict[str, object], tools: tuple[FunctionSpec, ...]
    ) -> Conversation:
        """Server-assembled bounded context; customer text alone still uses start's 8k limit."""
        text = encode(data)
        if len(text.encode()) > 65536:
            raise ProviderFailure("INPUT_TOO_LARGE")
        contents = [{"role": "user", "parts": [{"text": text}]}]
        turn = await self.generate(system=system, contents=contents, tools=tools)
        return Conversation(encode(contents), turn, 1, len(turn.calls))
