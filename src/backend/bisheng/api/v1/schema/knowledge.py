from typing import Optional

from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileBase
from pydantic import Field


class KnowledgeFileResp(KnowledgeFileBase):
    id: Optional[int] = Field(default=None)
    title: Optional[str] = Field(default=None, description="Document Summary")
    permission: Optional[str] = Field(default=None, description="当前用户对该文档的权限级别：read / write / admin")
