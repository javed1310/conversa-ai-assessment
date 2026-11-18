"""Live-discovered failures: evidence must survive short follow-ups and missing filters."""

from app.contracts import SearchCoursesArguments
from app.generation import CourseQuestion
from app.knowledge import DocumentInput, KnowledgeService
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change

UNAVAILABLE = {
    "kind": "unavailable",
    "courseUse": "none",
    "evidenceKeys": [],
    "clarification": "none",
}


async def test_budget_free_catalogue_search_does_not_invent_budget(agent_env):
    result = await agent_env.send(
        "What Python courses do you offer?",
        steps=[[("searchCourses", {"category": "python"})]],
    )
    assert "PY101" in result.reply and "PY202" in result.reply
    assert result.actions[0].result.status == "success"
    record = await agent_env.storage.db.sessions.find_one(
        {"session_id": agent_env.context.session_id}
    )
    assert record["state"]["fields"]["budget"] is None


async def test_contextual_budget_search_retains_checked_results(agent_env):
    await agent_env.send(
        "I want Python courses", changes=[change("stated_interest", "python", "Python")]
    )
    result = await agent_env.send(
        "My maximum budget is 30000 INR",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "python", "budget": 30000})]],
        selection=UNAVAILABLE,
    )
    assert "PY101" in result.reply and "12000" in result.reply
    assert "PY202" not in result.reply


async def test_schedule_paraphrase_uses_checked_focus_without_regex_match(agent_env, monkeypatch):
    env = agent_env
    await env.send(
        "Find data science courses under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("second one")

    async def classified(text, course_id):
        return CourseQuestion(
            course_id=course_id, excerpt=text, fields=("schedule",), confirmation=True
        )

    monkeypatch.setattr(env.engine.generation, "select_course_question", classified)
    result = await env.send(
        "are you sure it runs on weekeds",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
        selection=UNAVAILABLE,
    )
    assert "weekend" in result.reply.lower()
    assert "28000" not in result.reply


async def test_optional_budget_does_not_allow_invented_filter(agent_env):
    result = await agent_env.send(
        "What Python courses do you offer?",
        steps=[[("searchCourses", {"category": "python", "budget": 1})]],
    )
    assert not result.actions


def test_search_accepts_assessment_max_budget_alias():
    assert SearchCoursesArguments.model_validate({"category": "python"}).budget is None
    assert (
        SearchCoursesArguments.model_validate({"category": "python", "maxBudget": 30000}).budget
        == 30000
    )


async def test_new_policy_document_is_answered_and_cited_end_to_end(agent_env):
    env = agent_env
    service = KnowledgeService(
        env.storage,
        env.registry.get("training-institute"),
        env.engine.knowledge.provider,
        env.settings,
    )
    await service.ingest(
        DocumentInput(
            document_id="library",
            title="Library access",
            format="text",
            content=(
                "Learners may visit the practice library Saturdays from 10 AM to noon. "
                "Bring your learner ID. This policy establishes no fees "
                "or course seat availability."
            ),
        )
    )
    result = await env.send(
        "When can I access the practice library and what should I bring?",
        steps=[[("readKnowledge", {"query": "practice library"})]],
    )
    assert "Saturdays" in result.reply and "learner ID" in result.reply
    assert [source.document_id for source in result.sources] == ["library"]
