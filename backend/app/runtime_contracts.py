"""Versioned contracts shared by the backend, desktop supervisor and diagnostics UI.

These models deliberately describe runtime truth rather than configuration intent.  A provider can
be configured and still be unavailable; callers should use these responses to decide whether a task
can run on the current machine.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1"

BackendLocation = Literal["local", "hosted", "remote"]
LifecycleState = Literal[
    "not_started",
    "starting",
    "ready",
    "degraded",
    "crashed",
    "stopping",
    "external",
    "unavailable",
]


class CapabilityDetail(BaseModel):
    available: bool
    detail: str


class BrowserCapability(CapabilityDetail):
    logged_in: bool | None = None
    checked: bool = False


class HermesCapability(CapabilityDetail):
    mode: str
    name: str


class FilesCapability(CapabilityDetail):
    roots: list[str] = Field(default_factory=list)


class ScreenCapability(CapabilityDetail):
    mode: str


class VoiceCapability(CapabilityDetail):
    enabled: bool


class SandboxCapability(BaseModel):
    provider: str
    isolation: str
    degraded: bool
    detail: Any


class CapabilitiesResponse(BaseModel):
    contract_version: str = CONTRACT_VERSION
    backend_location: BackendLocation
    browser: BrowserCapability
    hermes: HermesCapability
    local_screen: ScreenCapability
    local_files: FilesCapability
    voice: VoiceCapability
    sandbox: SandboxCapability


class RuntimeStatusResponse(BaseModel):
    contract_version: str = CONTRACT_VERSION
    state: LifecycleState
    backend_location: BackendLocation
    pid: int
    version: str
    host: str
    port: int
    uptime_seconds: float
    state_dir: str
    data_dir: str
    logs_dir: str
