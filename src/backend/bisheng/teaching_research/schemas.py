from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


DEFAULT_ACHIEVEMENT_FIELDS = [
    "project_name",
    "applicant",
    "project_level",
    "project_type",
    "achievement_category",
    "organization",
    "year",
    "keywords",
]


class AchievementExtractReq(BaseModel):
    knowledge_id: int = Field(..., description="Knowledge base ID")
    file_ids: Optional[List[int]] = Field(default=None, description="Knowledge file IDs to extract. Empty means all success files.")
    fields: Optional[List[str]] = Field(default=None, description="Fields to extract. Uses default teaching/research fields when empty.")
    max_files: int = Field(default=20, ge=1, le=100, description="Maximum files processed in one request")
    max_chars_per_file: int = Field(default=16000, ge=1000, le=60000, description="Maximum text characters sent to LLM per file")
    duplicate_threshold: float = Field(default=0.88, ge=0.5, le=1.0, description="Similarity threshold for duplicate project names")


class AchievementExtractItem(BaseModel):
    project_name: Optional[str] = None
    applicant: Optional[str] = None
    project_level: Optional[str] = None
    project_type: Optional[str] = None
    achievement_category: Optional[str] = None
    organization: Optional[str] = None
    year: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)
    source_file_id: int
    source_file_name: str
    duplicate: bool = False
    duplicate_of: Optional[int] = None
    duplicate_reason: Optional[str] = None
    confidence: Optional[float] = None


class AchievementExtractResp(BaseModel):
    knowledge_id: int
    total_files: int
    items: List[AchievementExtractItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

