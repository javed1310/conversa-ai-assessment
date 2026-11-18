"""Verify private mock configuration and routing without any model requests."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import httpx
from app.contracts import ChatResult
from app.database import open_database
from app.main import create_app
from app.settings import load_settings
from asgi_lifespan import LifespanManager
from pydantic import SecretStr


class RoutingProbe:
    ready = True

    async def respond(self, context, message):
        return ChatResult(
            message_id=message.message_id,
            session_id=context.session_id,
            reply="Routing verified for " + context.business_id,
            actions=(),
            sources=(),
        )


async def main():
    settings = load_settings()
    assert {"training-institute", "fitness-studio"} <= settings.mock_channel_keys.keys()
    name = "ai_mock_setup_probe_" + uuid4().hex
    probe = settings.model_copy(
        update={
            "mongodb_uri": SecretStr("mongodb://127.0.0.1:27017/" + name),
            "automation_enabled": False,
        }
    )
    report = {
        "passed": False,
        "providerCalls": False,
        "privateKeysExported": False,
        "businesses": [],
    }
    async with open_database(probe) as database:
        try:
            app = create_app(probe, engine=RoutingProbe())
            async with LifespanManager(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://probe"
                ) as client:
                    for business in sorted(settings.mock_channel_keys):
                        payload = {
                            "event_id": str(uuid4()),
                            "author": "fictional-routing-probe",
                            "text": "Routing check",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        reply = await client.post(
                            "/api/v1/mock/messages",
                            json=payload,
                            headers={
                                "X-Mock-Channel-Key": settings.mock_channel_keys[
                                    business
                                ].get_secret_value()
                            },
                        )
                        assert reply.status_code == 200
                        result = reply.json()
                        assert result["recipient"] == payload["author"]
                        assert result["in_reply_to"] == payload["event_id"]
                        assert (
                            result["result"]["reply"]
                            == "Routing verified for " + business
                        )
                        report["businesses"].append(
                            {
                                "business": business,
                                "httpStatus": 200,
                                "correctOrigin": True,
                            }
                        )
                    rejected = await client.post(
                        "/api/v1/mock/messages",
                        json=payload,
                        headers={"X-Mock-Channel-Key": "x" * 40},
                    )
                    assert rejected.status_code == 404
                    report["unknownKeyRejected"] = True
            report["passed"] = True
        finally:
            assert name.startswith("ai_mock_setup_probe_") and database.db.name == name
            await database.client.drop_database(name)
            report["ownedDatabaseRemoved"] = True
            (ROOT / "docs/evidence/SUBMISSION_REMEDIATION-mock.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
    print(
        "PASS: configured mock keys route both businesses correctly; unknown key rejected; no provider calls."
    )


if __name__ == "__main__":
    asyncio.run(main())
