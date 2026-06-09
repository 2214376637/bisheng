from .endpoints.knowledge import router as knowledge_router
from .endpoints.qa import router as qa_router
from .endpoints.knowledge_space import router as knowledge_space_router, org_router as knowledge_org_router

__all__ = ['knowledge_router', 'qa_router', 'knowledge_space_router', 'knowledge_org_router']
