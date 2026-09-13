"""Pydantic models for Web API request/response."""

from pydantic import BaseModel, Field
from typing import Optional, List


class FileAttachment(BaseModel):
    file_id: str
    mime_type: str
    base64: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    user_id: str = Field(default="web-user")
    session_id: Optional[str] = None
    route_hint: Optional[str] = None
    response_mode: Optional[str] = None
    attachments: Optional[List[FileAttachment]] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    timestamp: float


class SessionInfo(BaseModel):
    user_id: str
    session_id: str
    message_count: int
    last_activity: Optional[float]
    active: bool = False


class AgentInfo(BaseModel):
    name: str
    model: str
    description: str
    tools: List[str]


class TraceEvent(BaseModel):
    type: str
    name: str
    author: str
    timestamp: float
    args: Optional[str] = None
    result: Optional[str] = None
