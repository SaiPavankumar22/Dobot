"""Tests for the Laya judgement integration (backend/app/security/laya.py)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import reset_settings_cache
from app.schemas import ActionSpec, RiskLevel
from app.security.jev import JEVEngine, JEVSignals
from app.security.laya import LayaClient, LayaOpinion, build_laya_client


class StubLaya:
    """Stands in for LayaClient where tests only need a canned opinion."""

    def __init__(self, opinion: LayaOpinion) -> None:
        self.opinion = opinion

    async def judge(self, **_: Any) -> LayaOpinion:
        return self.opinion


# --------------------------------------------------------------------------- client



async def test_mode_is_heuristic_without_configuration() -> None:
    client = build_laya_client()
    assert client.mode == "heuristic"
    health = await client.health()
    assert health["mode"] == "heuristic"
    assert health["reachable"] is False



async def test_judge_degrades_to_heuristic_without_server() -> None:
    client = build_laya_client()
    opinion = await client.judge(action_text="fs_delete(path=~/x)", user_message="clean up")
    assert opinion.available is False
    assert opinion.engine == "heuristic"
    assert opinion.error == ""  # no error: nothing was configured, this is the normal path



async def test_judge_server_parses_noul_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("LAYA_SERVER_URL", "http://127.0.0.1:9")
    from app.config import Settings

    settings = Settings()
    client = LayaClient(settings)

    payload = {
        "answers": {
            "risky": {"noul": 0.82},
            "urgent": {"noul": 0.15},
            "sensitive": {"noul": 0.91},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        body = request.read()
        assert b"risky" in body
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    client._http = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url=settings.laya_server_url.rstrip("/"), transport=transport
    )
    opinion = await client.judge(action_text="fs_delete path=/tmp/x", user_message="delete it")
    await client.aclose()
    assert opinion.available is True
    assert opinion.engine == "server"
    assert opinion.score >= 0.82
    assert opinion.sensitive == pytest.approx(0.91, abs=0.01)
    assert any("Laya" in note for note in opinion.notes)



async def test_judge_server_failure_degrades_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("LAYA_SERVER_URL", "http://127.0.0.1:9")
    from app.config import Settings

    settings = Settings()
    client = LayaClient(settings)
    opinion = await client.judge(action_text="fs_move a b", user_message="move")
    await client.aclose()
    assert opinion.available is False
    assert "unreachable" in opinion.error
    assert opinion.engine == "server"



async def test_probability_clamping() -> None:
    """A misbehaving server must not smuggle an out-of-range probability into decisions."""
    client = LayaClient()
    opinion = LayaClient._opinion_from_answers(
        {"risky": {"noul": 4.2}, "urgent": {"noul": "oops"}, "sensitive": {"noul": -1}},
        engine="server",
        latency_ms=1,
    )
    assert client is not None
    assert opinion.score <= 1.0
    assert opinion.sensitive == 0.0
    assert opinion.urgency == 0.0


# --------------------------------------------------------------------------- JEV blending



async def test_jev_uses_laya_when_it_scores_higher(services) -> None:  # noqa: ANN001
    laya = StubLaya(
        LayaOpinion(available=True, engine="server", score=0.8, latency_ms=5, notes=["Laya: risky action (p=0.80)"])
    )
    engine = JEVEngine(services.settings, laya=laya)
    action = ActionSpec(tool="fs_write", params={"path": "~/note.txt", "content": "hi"})
    advice = await engine.advise(action, JEVSignals(user_message="write a note"), base_risk=RiskLevel.LOW)
    assert advice.judge == "laya"
    assert advice.score >= 0.8
    assert advice.laya is not None and advice.laya["available"] is True



async def test_jev_never_lets_laya_deescalate(services) -> None:  # noqa: ANN001
    """A LOW-risk action stays LOW when Laya sees no risk, and heuristics still add theirs."""
    laya = StubLaya(LayaOpinion(available=True, engine="server", score=0.0, latency_ms=5))
    engine = JEVEngine(services.settings, laya=laya)
    action = ActionSpec(tool="fs_delete", params={"path": "~/Documents/thesis.docx"})
    advice = await engine.advise(action, JEVSignals(user_message="delete"), base_risk=RiskLevel.LOW)
    # Heuristics flag the irreplaceable document even though Laya said nothing.
    assert advice.score > 0
    assert "irreplaceable" in " ".join(advice.notes) or advice.escalate_to is not None



async def test_jev_reports_heuristic_judge_when_laya_absent(services) -> None:  # noqa: ANN001
    engine = JEVEngine(services.settings)
    action = ActionSpec(tool="fs_list", params={"path": "~"})
    advice = await engine.advise(action, JEVSignals(user_message="list"), base_risk=RiskLevel.LOW)
    assert advice.judge == "heuristic"
    assert advice.laya is not None
    assert advice.laya["available"] is False



async def test_jev_swallows_laya_crash(services) -> None:  # noqa: ANN001
    class ExplodingLaya:
        async def judge(self, **_: Any) -> LayaOpinion:
            raise RuntimeError("boom")

    engine = JEVEngine(services.settings, laya=ExplodingLaya())
    action = ActionSpec(tool="fs_list", params={"path": "~"})
    advice = await engine.advise(action, JEVSignals(user_message="list"), base_risk=RiskLevel.LOW)
    assert advice.judge == "heuristic"
    assert any("failed" in note for note in advice.notes)
