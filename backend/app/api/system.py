"""System endpoints: the Doctor, budgets, identity, interceptors, the ledger, and voice.

Everything here is documentation-grade introspection of the running system rather than task control —
which providers are genuinely live, what a task spent, what rules are enforcing what, and what Dobot has
decided it knows about you.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.agents.transcriber import get_transcriber
from app.api.deps import ServicesDep
from app.core.identity import GENOME, MEMORY, TELOS
from app.doctor import build_doctor
from app.memory.canonical import CanonicalFact
from app.voice import probe_voice

router = APIRouter(tags=["system"])

IDENTITY_DOCUMENTS = (GENOME, TELOS, MEMORY)


# ---------------------------------------------------------------------- schemas


class IdentityWriteRequest(BaseModel):
    content: str = Field(..., min_length=1)


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1)
    force: bool = False


class CanonicalStateRequest(BaseModel):
    content: str = Field(..., min_length=1)
    key: str = ""
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)


class InterceptorEvalRequest(BaseModel):
    tool: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------- doctor


@router.get("/doctor")
async def doctor(services: ServicesDep) -> dict:
    """Every capability, its real state, and the exact fix for anything that is not live."""
    return await build_doctor(services).report()


# ---------------------------------------------------------------------- budgets


@router.get("/system/usage")
async def usage(services: ServicesDep) -> dict:
    """What this session spent, and what compression saved before it was spent."""
    from app.core.cost import get_cost_ledger

    ledger = get_cost_ledger()
    return {
        "model": ledger.summary(),
        "recent_calls": ledger.recent(limit=25),
        "tokenjuice": services.tokenjuice.summary() if services.tokenjuice else {},
        "pricing_configured": services.settings.pricing_configured,
        "recent_compressions": services.tokenjuice.ledger.recent(limit=15) if services.tokenjuice else [],
    }


@router.post("/system/reset-usage")
async def reset_usage(services: ServicesDep) -> dict:
    """Clear session counters. Totals are session-scoped, so nothing durable is lost."""
    from app.core.cost import get_cost_ledger

    get_cost_ledger().reset()
    if services.tokenjuice:
        services.tokenjuice.ledger.reset()
    return {"reset": True}


@router.post("/system/consolidate")
async def consolidate(services: ServicesDep, dry_run: bool = False) -> dict:
    """Run the memory maintenance pass now: decay, merge, forget, promote, rewrite notes."""
    if services.consolidator is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "CONSOLIDATION_UNAVAILABLE", "message": "not wired", "recoverable": True},
        )
    report = await services.consolidator.run(dry_run=dry_run)
    return report.as_dict()


@router.get("/system/journal/{task_id}")
async def journal(services: ServicesDep, task_id: str, limit: int = 200) -> dict:
    """The ordered, replayable record of what a task did, read back from disk."""
    entries = await services.journal.entries(task_id, limit=limit)
    return {
        "task_id": task_id,
        "entries": entries,
        "count": len(entries),
        "usage": services.cost.for_task(task_id) if services.cost else {},
    }


# ---------------------------------------------------------------------- identity


@router.get("/identity")
async def identity_summary(services: ServicesDep) -> dict:
    """Who Dobot works for, what rules it may never break, and where you are going."""
    summary = await services.identity.summarise()
    return {
        **summary,
        "rendered": await services.identity.render_for_prompt(max_chars=2000),
        "documents_available": list(IDENTITY_DOCUMENTS),
    }


@router.get("/identity/{name}")
async def identity_document(services: ServicesDep, name: str) -> dict:
    if name not in IDENTITY_DOCUMENTS:
        raise HTTPException(
            status_code=404,
            detail={"code": "UNKNOWN_DOCUMENT", "message": name, "recoverable": False},
        )
    return {"name": name, "content": await services.identity.read(name)}


@router.put("/identity/{name}")
async def identity_write(services: ServicesDep, name: str, request: IdentityWriteRequest) -> dict:
    """Save a document. GENOME.md and TELOS.md are yours alone — the agent cannot write them."""
    if name not in IDENTITY_DOCUMENTS:
        raise HTTPException(
            status_code=404,
            detail={"code": "UNKNOWN_DOCUMENT", "message": name, "recoverable": False},
        )
    try:
        result = await services.identity.write(name, request.content, actor="user")
    except (PermissionError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "IDENTITY_WRITE_REFUSED", "message": str(exc), "recoverable": True},
        ) from exc
    # Editing GENOME.md can change the enforced rule set, so recompile immediately rather than
    # waiting for a restart — a stale guard is the failure mode this whole module exists to prevent.
    if name == GENOME:
        await services.reload_interceptors()
    return {**result, "interceptors": services.interceptors.summarise() if name == GENOME else None}


# ---------------------------------------------------------------------- interceptors


@router.get("/interceptors")
async def interceptors(services: ServicesDep) -> dict:
    return services.interceptors.summarise()


@router.post("/interceptors/reload")
async def interceptors_reload(services: ServicesDep) -> dict:
    """Recompile the rules from GENOME.md and every skill's workflow.json."""
    return await services.reload_interceptors()


@router.post("/interceptors/evaluate")
async def interceptors_evaluate(services: ServicesDep, request: InterceptorEvalRequest) -> dict:
    """Dry-run the rules against a tool call, without running it. The rule debugger."""
    outcome = services.interceptors.evaluate(request.tool, request.params)
    return {
        "tool": request.tool,
        "params": request.params,
        "outcome": outcome.as_dict(),
        "rule_count": len(services.interceptors.rules),
    }


# ---------------------------------------------------------------------- canonical ledger


@router.get("/canonical")
async def canonical_list(services: ServicesDep, limit: int = Query(40, ge=1, le=200)) -> dict:
    facts = await services.canonical.active(limit=limit)
    return {
        "facts": [fact.model_dump(mode="json") for fact in facts],
        "stats": await services.canonical.stats(),
        "rendered": await services.canonical.render(),
    }


@router.post("/canonical", response_model=CanonicalFact)
async def canonical_state(services: ServicesDep, request: CanonicalStateRequest) -> CanonicalFact:
    """State a durable fact. Repeating it strengthens it; contradicting it is recorded, not obeyed."""
    return await services.canonical.state(
        request.content,
        key=request.key,
        pinned=request.pinned,
        source="manual",
        tags=request.tags,
    )


@router.post("/canonical/{fact_id}/pin")
async def canonical_pin(services: ServicesDep, fact_id: str, pinned: bool = True) -> dict:
    fact = await services.canonical.pin(fact_id, pinned=pinned)
    if fact is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "FACT_NOT_FOUND", "message": fact_id, "recoverable": False},
        )
    return {"id": fact.id, "pinned": fact.pinned}


@router.delete("/canonical/{fact_id}")
async def canonical_forget(services: ServicesDep, fact_id: str) -> dict:
    deleted = await services.canonical.forget(fact_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "FACT_NOT_FOUND", "message": fact_id, "recoverable": False},
        )
    return {"deleted": True, "id": fact_id}


# ---------------------------------------------------------------------- voice


@router.get("/voice")
async def voice_status(services: ServicesDep) -> dict:
    return {**services.voice.summarise(), "probe": probe_voice(services.settings)}


@router.post("/voice/speak")
async def voice_speak(services: ServicesDep, request: SpeakRequest) -> dict:
    """Say something out loud. Uses the machine's own speech engine; nothing leaves the device."""
    result = await services.voice.speak(request.text, force=request.force)
    if not result.ok:
        raise HTTPException(
            status_code=409,
            detail={"code": "VOICE_UNAVAILABLE", "message": result.detail, "recoverable": True},
        )
    return result.as_dict()


# ---------------------------------------------------------------------- speech-to-text


@router.post("/voice/transcribe")
async def voice_transcribe(services: ServicesDep, file: Annotated[UploadFile, File()]) -> dict:
    """Transcribe a microphone recording locally with Whisper (faster-whisper, CPU/GPU).

    The audio is written to a temp file, transcribed in-process, and deleted. It never leaves the
    machine. Accepts webm/ogg/mp3/m4a/wav; non-native formats need ffmpeg on PATH.
    """
    del services  # the transcriber is self-contained; settings come from get_settings()
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    tmp = Path(tempfile.gettempdir()) / f"dobot_stt_{os.getpid()}_{next(tempfile._get_candidate_names())}{suffix}"
    try:
        data = await file.read()
        if not data:
            raise HTTPException(
                status_code=422,
                detail={"code": "EMPTY_AUDIO", "message": "the recording contained no audio bytes", "recoverable": True},
            )
        tmp.write_bytes(data)
        result = await get_transcriber().transcribe(tmp)
        if not result["ok"] and "not installed" in result.get("error", ""):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TRANSCRIBER_UNAVAILABLE",
                    "message": result["error"],
                    "recoverable": True,
                },
            )
        return result
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/voice/transcribe/status")
async def voice_transcribe_status(services: ServicesDep) -> dict:
    """Whether speech-to-text will work right now, and what is missing when it will not."""
    del services
    info = get_transcriber().probe()
    return {
        **info,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ready": info.get("state") == "live",
    }
