"""Default business behavior: shared retrieval and registered tools, no catalogue assumptions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agent_context import PreparedTurn, TurnWork
from app.contracts import ActionResult, ChatResult, CustomerState, ExecutionContext
from app.facts import contains_current_fact_claim, policy_reply, requests_current_facts
from app.gemini import FunctionSpec
from app.generation import ReplySelection
from app.persistence_models import Turn
from app.repositories import Repository
from app.response_safety import selected_passages
from app.state import StateService
from app.tools import ToolCall
from app.turn_context import PendingDialogue, TurnDialogue, TurnInput

if TYPE_CHECKING:
    from app.agent import GeminiAgent


class BusinessBehavior:
    """A response strategy never grants tool access or bypasses the executor."""

    instructions = ""
    reply_guidance = ""

    def __init__(self, runtime: GeminiAgent) -> None:
        self.runtime = runtime
        self.database = runtime.database
        self.businesses = runtime.businesses
        self.tools = runtime.tools
        self.generation = runtime.generation

    async def _operate(
        self, context: ExecutionContext, call: ToolCall, service: StateService, work: TurnWork
    ) -> dict[str, object]:
        return await self.runtime._operate(context, call, service, work)

    async def _load_dialogue(
        self, context: ExecutionContext, repo: Repository, current: Turn
    ) -> TurnDialogue:
        return TurnDialogue(
            turn=TurnInput(
                turn_id=current.turn_id,
                message_id=current.message_id,
                sequence=current.sequence,
                input_text=current.input_text,
            ),
            pending=PendingDialogue(),
        )

    async def prepare(
        self,
        context: ExecutionContext,
        service: StateService,
        work: TurnWork,
        dialogue: TurnDialogue,
    ) -> PreparedTurn:
        # Generic businesses retain conversation history without interpreting it as course state.
        return PreparedTurn(state=await service.view(context))

    async def context_fields(
        self,
        context: ExecutionContext,
        service: StateService,
        state: CustomerState,
        dialogue: TurnDialogue,
    ) -> dict[str, object]:
        return {}

    def specifications(self) -> tuple[FunctionSpec, ...]:
        return ()

    def trace_fields(self, work: TurnWork) -> dict[str, object]:
        return {}

    def reply_fields(self, state: CustomerState) -> dict[str, object]:
        return {}

    async def before_tool(
        self, context: ExecutionContext, call: ToolCall, service: StateService, work: TurnWork
    ) -> dict[str, object] | None:
        return None

    async def after_tool(
        self, context: ExecutionContext, call: ToolCall, action: ActionResult, work: TurnWork
    ) -> None:
        pass

    @staticmethod
    def _write_messages(work: TurnWork) -> list[str]:
        return []

    async def _assemble(
        self,
        context: ExecutionContext,
        service: StateService,
        state: CustomerState,
        selection: ReplySelection,
        work: TurnWork,
        dialogue: TurnDialogue,
    ) -> ChatResult:
        passages = selected_passages(selection, work)
        pieces: list[str] = []
        sources = []
        current_facts = requests_current_facts(dialogue.turn.input_text) or any(
            item.source is not None and contains_current_fact_claim(item.text)
            for item in work.evidence
        )
        checked = any(
            action.result.status == "success"
            and self.tools.presents_current_facts(action.result.tool)
            for action in work.actions
        )
        keys = list(selection.evidence_keys) if selection.kind == "evidence" else []
        if current_facts:
            keys.extend(
                item.key
                for item in work.evidence
                if item.key not in keys
                and item.action is not None
                and item.action.result.status == "success"
                and self.tools.presents_current_facts(item.action.result.tool)
            )
        for item in work.evidence:
            if item.key not in keys:
                continue
            if item.source is not None:
                policy = policy_reply(
                    "\n\n".join(passages.get(item.key, [])), dialogue.turn.input_text
                )
                if policy:
                    pieces.append(policy)
                    work.response_evidence[item.key] = policy
                if policy or current_facts:
                    sources.append(item.source)
            elif item.action is not None and item.action.result.status == "success":
                result = item.action.result
                rendered = self.tools.render(result.tool, result.data)
                if rendered:
                    pieces.append(rendered)
                    work.response_evidence[item.key] = item.text
        if current_facts and sources:
            pieces.append(
                "Policy / trial evidence is available in Sources. "
                + (
                    "Current facts come only from checked business records."
                    if checked
                    else "I have not verified current fees, schedules or availability; "
                    "documents cannot confirm those values."
                )
            )
        if not pieces:
            if selection.kind == "greeting":
                pieces.append("Hello! What would you like to know?")
            elif selection.kind == "clarify":
                pieces.append(
                    "What would you like to know about this business? "
                    "Please share a little more detail."
                )
            else:
                pieces.append(
                    "I don't have verified information for that "
                    "in this business's available records."
                )
        if work.failures:
            pieces.append(
                "Some information could not be checked. I have shown only verified results."
            )
        return ChatResult(
            message_id=dialogue.turn.message_id,
            session_id=context.session_id,
            reply="\n\n".join(dict.fromkeys(pieces)),
            sources=tuple(sources),
            actions=tuple(work.actions),
        )
