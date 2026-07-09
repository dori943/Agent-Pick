from pydantic import BaseModel, HttpUrl
from typing import Optional, Any

class ArchiveRequest(BaseModel):
    url: HttpUrl

class ArchiveResponse(BaseModel):
    success: bool
    crawl: Optional[Any] = None
    analysis: Optional[Any] = None
    deeplinks: Optional[Any] = None
    error: Optional[str] = None