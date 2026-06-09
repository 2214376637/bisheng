import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, TYPE_CHECKING

from fastapi import Request
from loguru import logger

from bisheng.api.v1.schemas import KnowledgeFileOne, FileProcessBase, ExcelRule
from bisheng.common.dependencies.user_deps import UserPayload  # noqa: F401 – kept for type hints
from bisheng.common.errcode.http_error import NotFoundError
from bisheng.common.errcode.knowledge_space import (
    SpaceLimitError, SpaceNotFoundError,
    SpaceFolderNotFoundError, SpaceFolderDepthError, SpaceFolderDuplicateError,
    SpaceFileNotFoundError, SpaceFileExtensionError, SpaceFileNameDuplicateError,
    SpaceSubscribePrivateError, SpaceSubscribeLimitError,
    SpacePermissionDeniedError, SpaceTagExistsError, SpaceFileSizeLimitError,
)
from bisheng.common.errcode.llm import WorkbenchEmbeddingError
from bisheng.common.models.space_channel_member import (
    SpaceChannelMember,
    SpaceChannelMemberDao,
    BusinessTypeEnum,
    UserRoleEnum,
    MembershipStatusEnum,
)
from bisheng.database.models.group_resource import ResourceTypeEnum
from bisheng.database.models.role import RoleDao
from bisheng.database.models.tag import TagDao, TagBusinessTypeEnum, Tag
from bisheng.knowledge.domain.knowledge_rag import KnowledgeRag
from bisheng.knowledge.domain.models.knowledge import Knowledge, KnowledgeDao, KnowledgeTypeEnum, AuthTypeEnum, \
    KnowledgeRead, KnowledgeState
from bisheng.knowledge.domain.models.knowledge_file import (
    KnowledgeFile, KnowledgeFileDao, KnowledgeFileStatus, FileType, FileSource
)
from bisheng.knowledge.domain.models.knowledge_space_file import SpaceFileDao
from bisheng.knowledge.domain.schemas.knowledge_space_schema import (
    KnowledgeSpaceInfoResp, SpaceMemberResponse, SpaceMemberPageResponse,
    UpdateSpaceMemberRoleRequest, RemoveSpaceMemberRequest, AddSpaceMemberRequest,
    SpaceSubscriptionStatusEnum, KnowledgeSpaceFileResponse
)
from bisheng.knowledge.domain.services.knowledge_audit_telemetry_service import KnowledgeAuditTelemetryService
from bisheng.knowledge.domain.services.knowledge_service import KnowledgeService
from bisheng.knowledge.domain.services.knowledge_utils import KnowledgeUtils
from sqlmodel import select
from bisheng.core.database import get_async_db_session
from bisheng.llm.domain import LLMService
from bisheng.user.domain.models.user import UserDao
from bisheng.user.domain.models.user_role import UserRoleDao
from bisheng.utils import generate_uuid
from bisheng.worker.knowledge import file_worker

if TYPE_CHECKING:
    from bisheng.message.domain.services.message_service import MessageService

# Maximum number of Knowledge Spaces a user can create
_MAX_SPACE_PER_USER = 30
# Maximum number of spaces a user can subscribe to (not as creator)
_MAX_SUBSCRIBE_PER_USER = 50
SPACE_ADMIN_ASSIGNMENT_MESSAGE = "assigned_knowledge_space_admin"


class KnowledgeSpaceService(KnowledgeUtils):
    """ Service for Knowledge Space operations.
    Instance-based; each method receives login_user as an argument.
    All business logic is async; DB access is delegated to DAO classes.
    """

    def __init__(self, request: Request, login_user: UserPayload):
        self.request = request
        self.login_user = login_user
        self.message_service: Optional['MessageService'] = None

    # ──────────────────────────── Permission helpers ───────────────────────────

    # Roles with write access to a space
    _WRITE_ROLES = {UserRoleEnum.CREATOR, UserRoleEnum.ADMIN}

    @staticmethod
    def _resolve_subscription_status(
            membership: Optional[SpaceChannelMember],
    ) -> SpaceSubscriptionStatusEnum:
        if membership is None:
            return SpaceSubscriptionStatusEnum.NOT_SUBSCRIBED
        if membership.is_active:
            return SpaceSubscriptionStatusEnum.SUBSCRIBED
        if membership.is_pending:
            return SpaceSubscriptionStatusEnum.PENDING
        if membership.is_recently_rejected():
            return SpaceSubscriptionStatusEnum.REJECTED
        return SpaceSubscriptionStatusEnum.NOT_SUBSCRIBED

    @staticmethod
    def _resolve_subscription_status_from_fields(
            status: Optional[str],
            update_time: Optional[datetime],
    ) -> SpaceSubscriptionStatusEnum:
        """Resolve subscription status from raw DB fields (from SQL JOIN result)."""
        if status is None:
            return SpaceSubscriptionStatusEnum.NOT_SUBSCRIBED
        if status == MembershipStatusEnum.ACTIVE:
            return SpaceSubscriptionStatusEnum.SUBSCRIBED
        if status == MembershipStatusEnum.PENDING:
            return SpaceSubscriptionStatusEnum.PENDING
        if status == MembershipStatusEnum.REJECTED:
            from bisheng.common.models.space_channel_member import REJECTED_STATUS_DISPLAY_WINDOW
            if update_time and update_time >= datetime.now() - REJECTED_STATUS_DISPLAY_WINDOW:
                return SpaceSubscriptionStatusEnum.REJECTED
        return SpaceSubscriptionStatusEnum.NOT_SUBSCRIBED

    @staticmethod
    def _apply_subscription_flags(
            result: KnowledgeSpaceInfoResp,
            subscription_status: SpaceSubscriptionStatusEnum,
    ) -> None:
        result.subscription_status = subscription_status
        result.is_followed = subscription_status == SpaceSubscriptionStatusEnum.SUBSCRIBED
        result.is_pending = subscription_status == SpaceSubscriptionStatusEnum.PENDING

    async def _require_write_permission(self, space_id: int) -> UserRoleEnum:
        """
        Verify that the current user (self.login_user) is an active CREATOR or ADMIN
        of the given space. Raises SpacePermissionDeniedError otherwise.
        """
        role = await SpaceChannelMemberDao.async_get_active_member_role(
            space_id, self.login_user.user_id
        )
        if role not in self._WRITE_ROLES:
            raise SpacePermissionDeniedError()
        return role

    async def _is_system_super_admin(self) -> bool:
        roles = await UserRoleDao.aget_user_roles(self.login_user.user_id)
        return any(r.role_id == 1 for r in roles) if roles else False

    async def _require_write_or_super_admin(self, space_id: int) -> None:
        if await self._is_system_super_admin():
            return
        await self._require_write_permission(space_id)

    async def _require_read_permission(self, space_id: int) -> Knowledge:
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()
        if space.auth_type == AuthTypeEnum.PUBLIC:
            return space
        role = await SpaceChannelMemberDao.async_get_active_member_role(
            space_id, self.login_user.user_id
        )
        if not role:
            # 文档级权限救济通道：
            # 虽然用户不是该空间的成员，但如果空间内存在配置了"对所有人公开"规则的文档，
            # 仍允许用户进入空间。具体能访问哪些文件由 aget_authorized_file_ids 精确控制。
            has_public_doc = await self._space_has_public_doc(space_id)
            if not has_public_doc:
                raise SpacePermissionDeniedError()
            return space
        # CREATOR_ADMIN 空间：只有 CREATOR / ADMIN 可访问，普通 MEMBER 拦截
        if space.auth_type == AuthTypeEnum.CREATOR_ADMIN and role not in self._WRITE_ROLES:
            raise SpacePermissionDeniedError()
        return space

    @staticmethod
    async def _space_has_public_doc(space_id: int) -> bool:
        """检查该空间内是否存在配置了"所有人可读"文档级权限规则的文档。"""
        from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFile, DocumentAccess
        from sqlmodel import select
        stmt = (
            select(DocumentAccess.id)
            .join(KnowledgeFile, DocumentAccess.file_id == KnowledgeFile.id)
            .where(
                KnowledgeFile.knowledge_id == space_id,
                KnowledgeFile.status == 2,
                DocumentAccess.subject_type == "all",
            )
            .limit(1)
        )
        async with get_async_db_session() as session:
            result = await session.exec(stmt)
            return result.first() is not None

    # ──────────────────────────── Space CRUD ──────────────────────────────────

    async def create_knowledge_space(
            self,
            name: str,
            description: Optional[str] = None,
            icon: Optional[str] = None,
            auth_type: AuthTypeEnum = AuthTypeEnum.PUBLIC,
            is_released: bool = False,
            org_node_id: Optional[int] = None,
    ) -> Knowledge:
        """ Create a new knowledge space (max 30 per user). """

        count = await KnowledgeDao.async_count_spaces_by_user(self.login_user.user_id)
        if count >= _MAX_SPACE_PER_USER:
            raise SpaceLimitError()

        workbench_llm = await LLMService.get_workbench_llm()
        if not workbench_llm or not workbench_llm.embedding_model:
            raise WorkbenchEmbeddingError()

        # 公开空间无需绑定组织节点
        if auth_type == AuthTypeEnum.PUBLIC:
            org_node_id = None
        elif org_node_id is not None:
            # 校验组织节点存在且类型正确
            node = KnowledgeDao.query_by_id(org_node_id)
            if not node or node.type != KnowledgeTypeEnum.NORMAL.value:
                raise ValueError(f"无效的组织节点 ID={org_node_id}，必须是 type=NORMAL 的知识库节点")

        db_knowledge = Knowledge(
            name=name,
            description=description,
            icon=icon,
            auth_type=auth_type,
            type=KnowledgeTypeEnum.SPACE.value,
            model=workbench_llm.embedding_model.id,
            is_released=is_released,
            org_node_id=org_node_id,
        )

        knowledge_space = KnowledgeService.create_knowledge_base(self.request, self.login_user, db_knowledge,
                                                                 skip_hook=True)

        member = SpaceChannelMember(
            business_id=str(knowledge_space.id),
            business_type=BusinessTypeEnum.SPACE,
            user_id=self.login_user.user_id,
            user_role=UserRoleEnum.CREATOR,
            status=MembershipStatusEnum.ACTIVE,
        )
        await SpaceChannelMemberDao.async_insert_member(member)

        # 若绑定了机构节点，将节点下的职务成员自动同步到该空间
        if org_node_id:
            await self._sync_org_members_to_space(
                space_id=knowledge_space.id,
                org_node_id=org_node_id,
                exclude_user_ids=[self.login_user.user_id],  # 创建者已作为 CREATOR 写入，跳过
            )

        # Audit log for knowledge space creation
        await KnowledgeAuditTelemetryService.audit_create_knowledge_space(self.login_user, self.request,
                                                                          knowledge_space)

        return knowledge_space

    async def get_space_info(self, space_id: int) -> KnowledgeSpaceInfoResp:
        from bisheng.worker import rebuild_knowledge_celery

        space = await KnowledgeDao.aquery_by_id(space_id)

        follower_num = await SpaceChannelMemberDao.async_count_space_members(space_id)
        total_file_num = await KnowledgeFileDao.async_count_file_by_knowledge_id(space_id)
        result = KnowledgeSpaceInfoResp(**space.model_dump())
        if space.user_id != self.login_user.user_id:
            create_user = await UserDao.aget_user(space.user_id)
            result.user_name = create_user.user_name if create_user else str(space.user_id)
        else:
            result.user_name = self.login_user.user_name
        self._apply_subscription_flags(result, SpaceSubscriptionStatusEnum.SUBSCRIBED)
        if space.user_id != self.login_user.user_id:
            member_info = await SpaceChannelMemberDao.async_find_member(space_id=space.id,
                                                                        user_id=self.login_user.user_id)
            self._apply_subscription_flags(result, self._resolve_subscription_status(member_info))
            if member_info and member_info.is_active:
                result.user_role = member_info.user_role
        else:
            result.user_role = UserRoleEnum.CREATOR
        result.follower_num = follower_num
        result.file_num = total_file_num

        if space.state != KnowledgeState.PUBLISHED.value:
            rebuild_knowledge_celery.delay(space_id, new_model_id=space.model, invoke_user_id=self.login_user.user_id)

        return result

    async def delete_space(self, space_id: int) -> None:
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()
        if space.user_id != self.login_user.user_id:
            raise SpacePermissionDeniedError()

        # Cleaned vectorData in
        await asyncio.to_thread(KnowledgeService.delete_knowledge_file_in_vector, space)

        # CleanedminioData
        await asyncio.to_thread(KnowledgeService.delete_knowledge_file_in_minio, space_id)

        await KnowledgeDao.async_delete_knowledge(knowledge_id=space_id)

        # Audit log and telemetry
        await KnowledgeAuditTelemetryService.audit_delete_knowledge_space(self.login_user, self.request, space)
        KnowledgeAuditTelemetryService.telemetry_delete_knowledge(self.login_user)
        return

    async def update_knowledge_space(
            self,
            space_id: int,
            name: Optional[str] = None,
            description: Optional[str] = None,
            icon: Optional[str] = None,
            auth_type: Optional[AuthTypeEnum] = None,
            is_released: bool = False,
            org_node_id: Optional[int] = None,
    ) -> Knowledge:
        """ Modify an existing knowledge space. """
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()

        await self._require_write_permission(space_id)

        old_auth_type = space.auth_type

        if name is not None:
            space.name = name
        if description is not None:
            space.description = description
        if icon is not None:
            space.icon = icon
        if auth_type is not None:
            space.auth_type = auth_type
        space.is_released = is_released

        # 公开空间自动清除组织节点绑定
        resolved_auth = auth_type if auth_type is not None else space.auth_type
        if resolved_auth == AuthTypeEnum.PUBLIC:
            space.org_node_id = None
        elif org_node_id is not None:
            node = KnowledgeDao.query_by_id(org_node_id)
            if not node or node.type != KnowledgeTypeEnum.NORMAL.value:
                raise ValueError(f"无效的组织节点 ID={org_node_id}，必须是 type=NORMAL 的知识库节点")
            space.org_node_id = org_node_id
        else:
            # org_node_id 未传入时保持原有绑定不变
            pass

        space = await KnowledgeDao.async_update_space(space)

        # When switching to PRIVATE, remove all non-creator members
        if old_auth_type != AuthTypeEnum.PRIVATE and auth_type == AuthTypeEnum.PRIVATE:
            await SpaceChannelMemberDao.async_delete_non_creator_members(space_id)
        elif old_auth_type == AuthTypeEnum.APPROVAL and auth_type == AuthTypeEnum.PUBLIC:
            await SpaceChannelMemberDao.async_delete_rejected_members(space_id)
        elif auth_type == AuthTypeEnum.CREATOR_ADMIN:
            # 切换到 CREATOR_ADMIN：移除所有普通 MEMBER，只保留 CREATOR / ADMIN
            await SpaceChannelMemberDao.async_delete_regular_members(space_id)

        # 若本次操作绑定/更新了机构节点，将节点下的职务成员增量同步到空间成员列表
        if org_node_id is not None and resolved_auth != AuthTypeEnum.PUBLIC:
            await self._sync_org_members_to_space(
                space_id=space_id,
                org_node_id=org_node_id,
            )

        return space

    async def _sync_org_members_to_space(
            self,
            space_id: int,
            org_node_id: int,
            exclude_user_ids: Optional[List[int]] = None,
    ) -> None:
        """
        将机构节点（org_node_id）下所有持有该节点职务的用户批量同步到知识空间成员列表。

        策略：
        - 幂等式同步：对已是 ACTIVE 成员的用户直接跳过，不重复插入。
        - 仅写入 MEMBER 角色，不覆盖 CREATOR / ADMIN 的已有角色。
        - exclude_user_ids 中的用户 ID 将被跳过（用于排除创建者自身）。
        """
        from bisheng.user.domain.models.user_position import UserPositionDao

        exclude_ids = set(exclude_user_ids or [])

        # 1. 查询该机构节点下所有持有职务的用户 ID
        org_user_ids = await UserPositionDao.aget_users_by_kb_node(org_node_id)
        if not org_user_ids:
            logger.info(f"[sync_org_members] org_node_id={org_node_id} 下暂无职务成员，跳过同步")
            return

        # 2. 查询该空间当前所有活跃成员，构建快速查找集合
        existing_members = await SpaceChannelMemberDao.async_get_members_by_space(space_id)
        existing_user_ids = {m.user_id for m in existing_members}

        # 3. 计算需要新增的用户（差集），并逐一写入
        new_user_ids = [
            uid for uid in org_user_ids
            if uid not in existing_user_ids and uid not in exclude_ids
        ]

        if not new_user_ids:
            logger.info(f"[sync_org_members] space_id={space_id} 节点成员均已是空间成员，无需同步")
            return

        for uid in new_user_ids:
            try:
                member = SpaceChannelMember(
                    business_id=str(space_id),
                    business_type=BusinessTypeEnum.SPACE,
                    user_id=uid,
                    user_role=UserRoleEnum.MEMBER,
                    status=MembershipStatusEnum.ACTIVE,
                )
                await SpaceChannelMemberDao.async_insert_member(member)
            except Exception as e:
                # 忽略重复键等非致命异常，保证整体同步不中断
                logger.warning(f"[sync_org_members] 插入成员 user_id={uid} 至 space_id={space_id} 时出错（已跳过）: {e}")

        logger.info(f"[sync_org_members] space_id={space_id} 已同步 {len(new_user_ids)} 名机构成员")

    # ──────────────────────────── Listings ────────────────────────────────────

    async def _format_member_spaces(
            self, members: List[SpaceChannelMember], order_by: str
    ) -> List[KnowledgeRead]:
        if not members:
            return []

        members_map = {
            int(one.business_id): one for one in members
        }
        res = await KnowledgeDao.async_get_spaces_by_ids(list(members_map.keys()), order_by)
        pinned_spaces = []
        normal_spaces = []
        for one in res:
            member_conf = members_map.get(one.id)
            if not member_conf:
                continue

            if member_conf.is_pinned:
                pinned_spaces.append(
                    KnowledgeSpaceInfoResp(
                        **one.model_dump(),
                        is_pinned=True,
                        user_role=member_conf.user_role,
                        subscription_status=SpaceSubscriptionStatusEnum.SUBSCRIBED,
                        is_followed=True,
                    ))
            else:
                normal_spaces.append(
                    KnowledgeSpaceInfoResp(
                        **one.model_dump(),
                        is_pinned=False,
                        user_role=member_conf.user_role,
                        subscription_status=SpaceSubscriptionStatusEnum.SUBSCRIBED,
                        is_followed=True,
                    ))
        return pinned_spaces + normal_spaces

    async def get_my_created_spaces(
            self, order_by: str = 'update_time'
    ) -> List[KnowledgeRead]:
        members = await SpaceChannelMemberDao.async_get_user_created_members(self.login_user.user_id)
        return await self._format_member_spaces(members, order_by)

    async def get_my_managed_spaces(
            self, order_by: str = 'name'
    ) -> List[KnowledgeRead]:
        members = await SpaceChannelMemberDao.async_get_user_managed_members(self.login_user.user_id)
        return await self._format_member_spaces(members, order_by)

    async def get_my_followed_spaces(
            self, order_by: str = 'update_time'
    ) -> List[KnowledgeRead]:
        """
        Return the spaces the current user follows (non-creator).
        Pinned spaces always appear first; within each pinned/non-pinned group
        the caller-specified order_by is applied.
        """
        # Fetch members ordered by is_pinned DESC so we know which are pinned
        members = await SpaceChannelMemberDao.async_get_user_followed_members(self.login_user.user_id)
        return await self._format_member_spaces(members, order_by)

    async def get_accessible_spaces(self) -> List[KnowledgeRead]:
        """
        返回当前用户"虽非成员，但空间内存在对所有人公开的文档"的知识空间列表。
        这是文档级权限优先于空间级权限原则在导航层的体现：
        即使用户看不到私有空间，只要空间内有公开文档，就应该能在列表中发现它。
        """
        from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFile, DocumentAccess
        from bisheng.knowledge.domain.models.knowledge import Knowledge
        from bisheng.common.models.space_channel_member import SpaceChannelMemberDao
        from sqlmodel import select
        from sqlalchemy import distinct

        # 1. 查询所有含对所有人公开文档的空间 ID
        stmt = (
            select(distinct(KnowledgeFile.knowledge_id))
            .join(DocumentAccess, DocumentAccess.file_id == KnowledgeFile.id)
            .where(
                DocumentAccess.subject_type == "all",
                KnowledgeFile.status == 2,  # SUCCESS
            )
        )
        async with get_async_db_session() as session:
            result = await session.exec(stmt)
            candidate_space_ids = result.all()

        if not candidate_space_ids:
            return []

        # 2. 查询这些空间的元信息，过滤掉 PUBLIC 类型（PUBLIC 已在广场可见）
        async with get_async_db_session() as session:
            spaces_stmt = select(Knowledge).where(
                Knowledge.id.in_(candidate_space_ids),
                Knowledge.type == KnowledgeTypeEnum.SPACE.value,
                Knowledge.auth_type != AuthTypeEnum.PUBLIC.value,
            )
            spaces = (await session.exec(spaces_stmt)).all()

        if not spaces:
            return []

        # 3. 过滤掉用户已是成员的空间（已在 /joined 列表里，无需重复展示）
        result_spaces = []
        for space in spaces:
            member = await SpaceChannelMemberDao.async_find_member(space.id, self.login_user.user_id)
            if member is None:
                result_spaces.append(space)

        return [
            KnowledgeSpaceInfoResp(
                **space.model_dump(),
                subscription_status=SpaceSubscriptionStatusEnum.NOT_SUBSCRIBED,
                is_followed=False,
            )
            for space in result_spaces
        ]



    async def pin_space(self, space_id: int, is_pinned: bool = True) -> bool:
        return await SpaceChannelMemberDao.pin_space_id(space_id, self.login_user.user_id, is_pinned)

    async def get_knowledge_square(
            self, keyword: str = None, page: int = 1, page_size: int = 20
    ) -> dict:
        from bisheng.user.domain.services.user import UserService

        """
        Return PUBLIC/APPROVAL spaces for the Knowledge Square with pagination, sorted by:
        1. Not-joined first (easier to explore)
        2. Already-joined or pending last
        3. Within each group: sorted by update_time DESC
        Sorting and pagination are handled at the SQL level for efficiency.
        Returns: {"total": int, "page": int, "page_size": int, "data": List[KnowledgeSpaceInfoResp]}
        """
        # 1. SQL-level paginated query with multi-table JOIN
        rows, total = await asyncio.gather(
            KnowledgeDao.async_get_public_spaces_paginated(
                user_id=self.login_user.user_id,
                keyword=keyword,
                page=page,
                page_size=page_size,
            ),
            KnowledgeDao.async_count_public_spaces(keyword=keyword),
        )

        if not rows:
            return {"total": 0, "page": page, "page_size": page_size, "data": []}

        # 2. Collect current page space IDs and creator IDs for enrichment
        space_ids_int = [row[0].id for row in rows]
        space_ids_str = [str(sid) for sid in space_ids_int]
        creator_ids = list({row[0].user_id for row in rows if row[0].user_id})

        # 3. Batch fetch creator info and file counts for the current page only
        creator_users, success_file_map = await asyncio.gather(
            UserDao.aget_user_by_ids(creator_ids) if creator_ids else asyncio.coroutine(lambda: [])(),
            KnowledgeFileDao.async_count_success_files_batch(space_ids_int),
        )
        user_map = {u.user_id: u for u in (creator_users or [])}

        # 4. Build response items
        result_list: list = []
        for row in rows:
            space = row[0]
            user_subscription_status = row[1]
            user_subscription_update_time = row[2]
            subscriber_count = row[3]

            sid = str(space.id)
            creator = user_map.get(space.user_id)

            subscription_status = self._resolve_subscription_status_from_fields(
                user_subscription_status, user_subscription_update_time,
            )

            result_list.append(
                KnowledgeSpaceInfoResp(
                    **space.model_dump(),
                    **{
                        "space": space,
                        "is_followed": subscription_status == SpaceSubscriptionStatusEnum.SUBSCRIBED,
                        "is_pending": subscription_status == SpaceSubscriptionStatusEnum.PENDING,
                        "subscription_status": subscription_status,
                        "user_name": creator.user_name if creator else str(space.user_id),
                        "avatar": await UserService.get_avatar_share_link(creator.avatar) if creator else None,
                        "file_num": success_file_map.get(space.id, 0),
                        "follower_num": subscriber_count,
                    }
                )
            )

        return {"total": total, "page": page, "page_size": page_size, "data": result_list}

    # ──────────────────────────── Members ─────────────────────────────────────

    async def get_space_members(self, space_id: int, page: int, page_size: int,
                                keyword: Optional[str] = None) -> SpaceMemberPageResponse:
        from bisheng.user.domain.services.user import UserService

        """
        Paginate through the list of space members.
        - Verify if the current user has read permission
        - Support fuzzy search by username
        - Return user information and associated user groups
        - Sorting: Creators and administrators at the top, regular members sorted by user_id
        """
        await self._require_write_permission(space_id)

        search_user_ids = None
        if keyword:
            matched_users = await UserDao.afilter_users(user_ids=[], keyword=keyword)
            search_user_ids = [u.user_id for u in matched_users]
            if not search_user_ids:
                return SpaceMemberPageResponse(data=[], total=0)

        members = await SpaceChannelMemberDao.find_space_members_paginated(
            space_id=space_id, user_ids=search_user_ids, page=page, page_size=page_size
        )

        total = await SpaceChannelMemberDao.count_space_members_with_keyword(
            space_id=space_id, user_ids=search_user_ids
        )

        if not members:
            return SpaceMemberPageResponse(data=[], total=total)

        member_user_ids = [m.user_id for m in members]
        users = await UserDao.aget_user_by_ids(member_user_ids)
        user_map = {u.user_id: u for u in (users or [])}

        result_list = []
        for member in members:
            user = user_map.get(member.user_id)
            user_name = user.user_name if user else f"User {member.user_id}"

            # Query user groups the user belongs to
            user_groups = await self.login_user.get_user_groups(member.user_id)

            result_list.append(SpaceMemberResponse(
                user_id=member.user_id,
                user_name=user_name,
                user_avatar=await UserService.get_avatar_share_link(user.avatar) if user else None,
                user_role=member.user_role.value,
                user_groups=user_groups,
            ))

        return SpaceMemberPageResponse(data=result_list, total=total)

    async def add_space_member(self, req: AddSpaceMemberRequest) -> bool:
        """
        将用户加入知识空间。
        系统超级管理员或空间 Creator/Admin 可操作。
        """
        await self._require_write_or_super_admin(req.space_id)

        space = await KnowledgeDao.aquery_by_id(req.space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()

        users = await UserDao.aget_user_by_ids([req.user_id])
        if not users:
            raise ValueError('目标用户不存在')

        if space.auth_type == AuthTypeEnum.CREATOR_ADMIN and not await self._is_system_super_admin():
            current_role = await SpaceChannelMemberDao.async_get_active_member_role(
                req.space_id, self.login_user.user_id
            )
            if current_role not in self._WRITE_ROLES:
                raise SpacePermissionDeniedError()

        target_role = UserRoleEnum.ADMIN if req.role == 'admin' else UserRoleEnum.MEMBER
        if target_role == UserRoleEnum.ADMIN:
            current_admins = await SpaceChannelMemberDao.async_get_members_by_space(
                space_id=req.space_id, user_roles=[UserRoleEnum.ADMIN]
            )
            if len(current_admins) >= 5:
                raise ValueError('管理员数量已达上限')

        existing = await SpaceChannelMemberDao.async_find_member(req.space_id, req.user_id)
        if existing and existing.is_active and existing.user_role == UserRoleEnum.CREATOR:
            raise ValueError('不能修改创建者成员关系')

        status = MembershipStatusEnum.ACTIVE
        if existing:
            if existing.is_active and existing.user_role == target_role:
                return True
            existing.status = status
            existing.user_role = target_role
            await SpaceChannelMemberDao.update(existing)
            return True

        member = SpaceChannelMember(
            business_id=str(req.space_id),
            business_type=BusinessTypeEnum.SPACE,
            user_id=req.user_id,
            user_role=target_role,
            status=status,
        )
        await SpaceChannelMemberDao.async_insert_member(member)
        return True

    async def update_member_role(self, req: UpdateSpaceMemberRoleRequest) -> bool:
        """
        Set member role (admin/regular member).
        Permissions:
        - Creators can set anyone as an admin or member
        - Admins cannot promote others to admin, nor can they modify the roles of other admins or creators
        - Modifying the creator's role is not allowed
        """
        current_role = await self._require_write_permission(req.space_id)

        # 2. Query target member
        target_membership = await SpaceChannelMemberDao.async_find_member(
            space_id=req.space_id, user_id=req.user_id
        )
        if not target_membership or not target_membership.is_active:
            raise ValueError("The target user is not a member of this space")

        # 3. Modifying the creator's role is not allowed
        if target_membership.user_role == UserRoleEnum.CREATOR:
            raise ValueError("Modifying the creator's role is not allowed")

        # 4. Admin permission limits
        if current_role == UserRoleEnum.ADMIN:
            # Admins cannot set others as admins
            if req.role == UserRoleEnum.ADMIN.value:
                raise ValueError("Admins do not have permission to set others as admins")
            # Admins cannot modify the roles of other admins
            if target_membership.user_role == UserRoleEnum.ADMIN:
                raise ValueError("Admins do not have permission to modify the roles of other admins")

        # 5. Check maximum limit when setting as an admin
        if req.role == UserRoleEnum.ADMIN.value:
            current_admins = await SpaceChannelMemberDao.async_get_members_by_space(
                space_id=req.space_id, user_roles=[UserRoleEnum.ADMIN]
            )
            if len(current_admins) >= 5:
                raise ValueError("Maximum number of administrators reached")

        should_notify_admin_assignment = (
                target_membership.user_role == UserRoleEnum.MEMBER
                and req.role == UserRoleEnum.ADMIN.value
        )

        # 6. Update role
        target_membership.user_role = UserRoleEnum(req.role)
        await SpaceChannelMemberDao.update(target_membership)

        if should_notify_admin_assignment:
            await self._send_admin_assignment_notification(
                space_id=req.space_id,
                target_user_id=target_membership.user_id,
            )

        return True

    async def remove_member(self, req: RemoveSpaceMemberRequest) -> bool:
        """
        Remove a member (hard delete).
        Permissions:
        - Creators can remove anyone (except themselves)
        - Admins can remove regular members
        - Admins cannot remove other admins or creators
        """
        # 1. Verify current user permissions
        current_role = await self._require_write_permission(req.space_id)

        # 2. Cannot remove yourself
        if req.user_id == self.login_user.user_id:
            raise ValueError("Cannot remove yourself")

        # 3. Query target member
        target_membership = await SpaceChannelMemberDao.async_find_member(
            space_id=req.space_id, user_id=req.user_id
        )
        if not target_membership or not target_membership.is_active:
            raise ValueError("The target user is not a member of this space")

        # 4. Removing the creator is not allowed
        if target_membership.user_role == UserRoleEnum.CREATOR:
            raise ValueError("Removing the creator is not allowed")

        # 5. Admins cannot remove other admins
        if current_role == UserRoleEnum.ADMIN:
            if target_membership.user_role == UserRoleEnum.ADMIN:
                raise ValueError("Admins do not have permission to remove other admins")

        # 6. Hard delete: remove from database
        await SpaceChannelMemberDao.delete_space_member(space_id=req.space_id, user_id=req.user_id)

        return True

    async def _send_admin_assignment_notification(
            self,
            space_id: int,
            target_user_id: int,
    ) -> None:
        """Notify a space member after being promoted from member to admin."""
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()

        user = await UserDao.aget_user(target_user_id)
        target_user_name = user.user_name if user else f"User {target_user_id}"

        content = [
            {
                "type": "user",
                "content": f"@{target_user_name}",
                "metadata": {"user_id": target_user_id},
            },
            {
                "type": "system_text",
                "content": SPACE_ADMIN_ASSIGNMENT_MESSAGE,
            },
            {
                "type": "business_url",
                "content": f"--{space.name}",
                "metadata": {
                    "business_type": "knowledge_space_id",
                    "data": {"knowledge_space_id": str(space.id)},
                },
            },
        ]

        if not self.message_service:
            return

        await self.message_service.send_generic_notify(
            sender=self.login_user.user_id,
            receiver_user_ids=[target_user_id],
            content_item_list=content,
        )

    async def _handle_file_folder_extra_info(self, res: List[KnowledgeFile]) -> List[Dict]:
        folder_ids = []
        file_ids = []
        for one in res:
            if one.file_type == FileType.DIR:
                folder_ids.append(one.id)
            else:
                file_ids.append(one.id)

        # folder need find all success file num and all file num
        folder_counts = {}
        if folder_ids:
            from sqlmodel import select, col
            from sqlalchemy import func, or_
            from bisheng.core.database import get_async_db_session

            async def count_folder(folder: KnowledgeFile):
                prefix = f"{folder.file_level_path or ''}/{folder.id}"
                stmt = select(KnowledgeFile.status, func.count(KnowledgeFile.id)).where(
                    KnowledgeFile.knowledge_id == folder.knowledge_id,
                    KnowledgeFile.file_type == 1,
                    or_(
                        col(KnowledgeFile.file_level_path) == prefix,
                        col(KnowledgeFile.file_level_path).like(f"{prefix}/%")
                    )
                ).group_by(KnowledgeFile.status)

                async with get_async_db_session() as session:
                    rows = (await session.exec(stmt)).all()
                    total = sum(r[1] for r in rows)
                    success = sum(r[1] for r in rows if r[0] == KnowledgeFileStatus.SUCCESS.value)
                    folder_counts[folder.id] = {"file_num": total, "success_file_num": success}

            folders = [f for f in res if f.file_type == FileType.DIR]
            await asyncio.gather(*(count_folder(f) for f in folders))

        # file need find all tags
        file_tags = {}
        if file_ids:
            tag_dict = await asyncio.to_thread(
                TagDao.get_tags_by_resource_batch,
                [ResourceTypeEnum.SPACE_FILE],
                [str(fid) for fid in file_ids]
            )
            for fid_str, tags in tag_dict.items():
                file_tags[int(fid_str)] = [{"id": t.id, "name": t.name} for t in tags]

        result = []
        for one in res:
            item = one.model_dump()
            if one.file_type == FileType.DIR:
                counts = folder_counts.get(one.id, {"file_num": 0, "success_file_num": 0})
                item.update(counts)
            else:
                item["thumbnails"] = self.get_logo_share_link(one.thumbnails)
                item["tags"] = file_tags.get(one.id, [])
            # 为每个条目（文件/文件夹）附加当前用户的权限级别
            item["permission"] = await KnowledgeFileDao.aget_user_file_permission(self.login_user, one.id)
            result.append(item)

        return result

    async def list_space_children(
            self,
            space_id: int,
            parent_id: Optional[int] = None,
            order_field: str = 'file_type',
            order_sort: str = 'asc',
            file_status: List[int] = None,
            page: int = 1,
            page_size: int = 20,
    ) -> dict:
        """
        Return direct children (folders first, then files) under a parent folder.
        When parent_id is None, returns root-level items of the space.
        Returns: {"total": int, "page": int, "page_size": int, "data": List[KnowledgeFile]}
        """
        await self._require_read_permission(space_id)
        authorized_file_ids = await KnowledgeFileDao.aget_authorized_file_ids(self.login_user, space_id)
        total, items = await asyncio.gather(
            SpaceFileDao.async_count_children(space_id, parent_id, file_status, authorized_file_ids=authorized_file_ids),
            SpaceFileDao.async_list_children(space_id, parent_id, order_field, order_sort, file_status, page,
                                             page_size, authorized_file_ids=authorized_file_ids),
        )
        data = await self._handle_file_folder_extra_info(items)

        return {"total": total, "page": page, "page_size": page_size, "data": data}

    async def search_space_children(self, space_id: int, parent_id: Optional[int] = None, tag_ids: List[int] = None,
                                    keyword: str = None, page: int = 1, page_size: int = 20,
                                    file_status: List[int] = None, order_field: str = "file_type",
                                    order_sort: str = "asc") -> Dict:
        space = await self._require_read_permission(space_id)

        file_level_path = None
        filter_files = []

        if parent_id:
            parent_folder = await KnowledgeFileDao.query_by_id(parent_id)
            if not parent_folder:
                raise NotFoundError()
            file_level_path = f"{parent_folder.file_level_path}/{parent_folder.id}"
            children_ids = await SpaceFileDao.get_children_by_prefix(space_id, file_level_path)
            if not children_ids:
                return {"total": 0, "page": page, "page_size": page_size, "data": []}
            filter_files = [one.id for one in children_ids]

        if tag_ids:
            resources = await TagDao.aget_resources_by_tags(tag_ids, ResourceTypeEnum.SPACE_FILE)
            if not resources:
                return {"total": 0, "page": page, "page_size": page_size, "data": []}
            if filter_files:
                filter_files = list(set(filter_files) & set([int(one.resource_id) for one in resources]))
            else:
                filter_files = [int(one.resource_id) for one in resources]
        # 获取允许访问的文档（包括独立授权和继承知识库授权）和文件夹
        authorized_file_ids = await KnowledgeFileDao.aget_authorized_file_ids(self.login_user, space_id)
        async with get_async_db_session() as session:
            folders = (await session.exec(
                select(KnowledgeFile.id).where(
                    KnowledgeFile.knowledge_id == space_id,
                    KnowledgeFile.file_type == 0
                )
            )).all()
        allowed_ids = list(set(authorized_file_ids) | set(folders))
        if not allowed_ids:
            allowed_ids = [-1]

        if filter_files:
            filter_files = list(set(filter_files) & set(allowed_ids))
        else:
            filter_files = allowed_ids

        if not filter_files:
            filter_files = [-1]

        extra_file_ids = []

        if keyword:
            query = {"match_phrase": {"text": keyword}}
            if filter_files:
                query = {
                    "bool": {
                        "must": [
                            query,
                            {"terms": {"metadata.document_id": filter_files}},
                        ]
                    }
                }
            es_vector = await KnowledgeRag.init_knowledge_es_vectorstore(knowledge=space)
            es_result = await es_vector.client.search(index=space.index_name, body={
                "query": query,
                "aggs": {
                    "document_ids": {
                        "terms": {
                            "field": "metadata.document_id",
                        }
                    }
                },
                "size": 0,
            })
            aggregations = es_result.get("aggregations")
            if aggregations:
                for one in aggregations.get("document_ids", {}).get("buckets", []):
                    extra_file_ids.append(one["key"])
            if filter_files:
                extra_file_ids = list(set(filter_files) & set(extra_file_ids))

        res = await KnowledgeFileDao.aget_file_by_filters(space_id, file_name=keyword, file_ids=filter_files,
                                                          extra_file_ids=extra_file_ids, status=file_status,
                                                          file_level_path=file_level_path, order_by="file_type",
                                                          order_field=order_field, order_sort=order_sort,
                                                          page=page, page_size=page_size)
        total = await KnowledgeFileDao.acount_file_by_filters(space_id, file_name=keyword, file_ids=filter_files,
                                                              extra_file_ids=extra_file_ids, status=file_status,
                                                              file_level_path=file_level_path)

        data = await self._handle_file_folder_extra_info(res)
        return {"total": total, "page": page, "page_size": page_size, "data": data}

    # ──────────────────────────── Folders ─────────────────────────────────────

    async def add_folder(
            self,
            knowledge_id: int,
            folder_name: str,
            parent_id: Optional[int] = None,
    ) -> KnowledgeFile:
        await self._require_write_permission(knowledge_id)
        level = 0
        file_level_path = ""

        if parent_id:
            parent_folder = await KnowledgeFileDao.query_by_id(parent_id)
            if (
                    not parent_folder
                    or parent_folder.knowledge_id != knowledge_id
                    or parent_folder.file_type != 0
            ):
                raise SpaceFolderNotFoundError()
            level = parent_folder.level + 1
            if level > 10:
                raise SpaceFolderDepthError()
            file_level_path = f"{parent_folder.file_level_path}/{parent_id}"

        if await SpaceFileDao.count_folder_by_name(knowledge_id, folder_name, file_level_path) > 0:
            raise SpaceFolderDuplicateError()

        added_folder = await KnowledgeFileDao.aadd_file(KnowledgeFile(
            knowledge_id=knowledge_id,
            user_id=self.login_user.user_id,
            user_name=self.login_user.user_name,
            updater_id=self.login_user.user_id,
            updater_name=self.login_user.user_name,
            file_name=folder_name,
            file_type=0,
            level=level,
            file_level_path=file_level_path,
            status=KnowledgeFileStatus.SUCCESS.value,
        ))
        await KnowledgeDao.async_update_knowledge_update_time_by_id(knowledge_id)
        return added_folder

    async def rename_folder(
            self, folder_id: int, new_name: str
    ) -> KnowledgeFile:
        folder = await KnowledgeFileDao.query_by_id(folder_id)
        if not folder or folder.file_type != 0:
            raise SpaceFolderNotFoundError()

        await self._require_write_permission(folder.knowledge_id)

        if await SpaceFileDao.count_folder_by_name(
                folder.knowledge_id, new_name, folder.file_level_path, exclude_id=folder_id
        ) > 0:
            raise SpaceFolderDuplicateError()

        folder.file_name = new_name
        updated_folder = await KnowledgeFileDao.async_update(folder)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(folder.knowledge_id)
        return updated_folder

    async def delete_folder(self, space_id: int, folder_id: int):
        from bisheng.worker.knowledge.file_worker import delete_knowledge_file_celery

        folder = await KnowledgeFileDao.query_by_id(folder_id)
        if not folder or folder.file_type != FileType.DIR.value:
            raise SpaceFolderNotFoundError()

        await self._require_write_permission(space_id)

        prefix = f"{folder.file_level_path}/{folder.id}"
        children = await SpaceFileDao.get_children_by_prefix(folder.knowledge_id, prefix)
        floder_ids = [folder_id]
        file_ids = []
        for child in children:
            if child.file_type == FileType.DIR.value:
                floder_ids.append(child.id)
            else:
                file_ids.append(child.id)

        if file_ids:
            delete_knowledge_file_celery.delay(file_ids=file_ids, knowledge_id=folder.knowledge_id, clear_minio=True)

        await self.update_folder_update_time(folder.file_level_path)

        await KnowledgeFileDao.adelete_batch(file_ids + floder_ids)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(space_id)

    async def get_folder_file_parent(self, space_id: int, file_id: int) -> List[Dict]:
        await self._require_read_permission(space_id)
        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record:
            raise SpaceFileNotFoundError()
        if file_record.level == 0:
            return []
        file_level_path_list = file_record.file_level_path.split("/")
        file_ids = [int(one) for one in file_level_path_list if one]
        file_list = await KnowledgeFileDao.aget_file_by_ids(file_ids)
        file_list = {
            file.id: file for file in file_list
        }
        res = []
        for one in file_ids:
            res.append({
                "id": one,
                "file_name": file_list.get(one).file_name if file_list.get(one) else one,
            })
        return res

    # ──────────────────────────── Files ───────────────────────────────────────

    async def add_file(
            self,
            knowledge_id: int,
            file_path: List[str],
            parent_id: Optional[int] = None,
            file_source: FileSource = None,
    ) -> List[KnowledgeSpaceFileResponse]:
        if file_source is None:
            file_source = FileSource.SPACE_UPLOAD
        await self._require_write_permission(knowledge_id)

        db_knowledge = await KnowledgeDao.aquery_by_id(knowledge_id)
        if not db_knowledge:
            raise SpaceFolderNotFoundError()

        level = 0
        file_level_path = ""

        if parent_id:
            parent_folder = await KnowledgeFileDao.query_by_id(parent_id)
            if (
                    not parent_folder
                    or parent_folder.knowledge_id != knowledge_id
                    or parent_folder.file_type != 0
            ):
                raise SpaceFolderNotFoundError()
            level = parent_folder.level + 1
            file_level_path = f"{parent_folder.file_level_path}/{parent_id}"

        default_file_size_limit = 40

        roles = await UserRoleDao.aget_user_roles(db_knowledge.user_id)
        if roles:
            roles = await RoleDao.aget_role_by_ids([one.role_id for one in roles])
            for one in roles:
                default_file_size_limit = max(default_file_size_limit, one.knowledge_space_file_limit)
        default_file_size_limit = default_file_size_limit * 1024 * 1024 * 1024
        logger.debug(f"space_file_size_limit: {default_file_size_limit}")
        current_total_file_size = int(await SpaceFileDao.get_user_total_file_size(self.login_user.user_id))

        folder_id2name = {}

        async def get_folder_name(tmp_file_level_path: str) -> str:
            if not tmp_file_level_path:
                return ""
            folder_ids = [int(fid) for fid in tmp_file_level_path.split("/") if fid]
            not_exists_ids = [one for one in folder_ids if one not in folder_id2name]
            if not_exists_ids:
                folder_list = await KnowledgeFileDao.aget_file_by_ids(folder_ids)
                for folder in folder_list:
                    folder_id2name[folder.id] = folder.file_name
            tmp = ""
            for one in folder_ids:
                folder_name = folder_id2name.get(one, one)
                tmp += f"/{folder_name}"
            return tmp

        file_split_rule = FileProcessBase(knowledge_id=knowledge_id)
        process_files = []
        failed_files = []
        preview_cache_keys = []
        for one in file_path:
            if current_total_file_size > default_file_size_limit:
                raise SpaceFileSizeLimitError()
            db_file = KnowledgeService.process_one_file(self.login_user, knowledge=db_knowledge,
                                                        file_info=KnowledgeFileOne(
                                                            file_path=one,
                                                            excel_rule=ExcelRule()
                                                        ), split_rule=file_split_rule.model_dump(),
                                                        file_kwargs={"level": level,
                                                                     "file_level_path": file_level_path,
                                                                     "file_source": file_source.value,
                                                                     "is_private": True})
            if db_file.status != KnowledgeFileStatus.FAILED.value:
                # Get a preview cache of this filekey
                cache_key = self.get_preview_cache_key(
                    knowledge_id, one
                )
                preview_cache_keys.append(cache_key)
                process_files.append(db_file)
                current_total_file_size += db_file.file_size
            else:
                failed_file = KnowledgeSpaceFileResponse(**db_file.model_dump())
                failed_file.old_file_level_path = await get_folder_name(db_file.file_level_path)
                failed_file.file_level_path = file_level_path
                failed_files.append(failed_file)

        for index, one in enumerate(process_files):
            file_worker.parse_knowledge_file_celery.delay(one.id, preview_cache_keys[index])
        await self.update_folder_update_time(file_level_path)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(knowledge_id)
        return failed_files + process_files

    async def rename_file(
            self, file_id: int, new_name: str
    ) -> KnowledgeFile:
        from bisheng.worker.knowledge.rebuild_knowledge_worker import rebuild_knowledge_file_chunk
        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record or file_record.file_type != 1:
            raise SpaceFileNotFoundError()

        # 改用文档级权限校验（D-06）
        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'write')

        old_suffix = file_record.file_name.rsplit('.', 1)[-1] if '.' in file_record.file_name else ''
        new_suffix = new_name.rsplit('.', 1)[-1] if '.' in new_name else ''
        if old_suffix != new_suffix:
            raise SpaceFileExtensionError()

        if await SpaceFileDao.count_file_by_name(file_record.knowledge_id, new_name, exclude_id=file_id) > 0:
            raise SpaceFileNameDuplicateError()

        file_record.file_name = new_name
        updated_file = await KnowledgeFileDao.async_update(file_record)

        if updated_file.status == KnowledgeFileStatus.SUCCESS.value:
            rebuild_knowledge_file_chunk.delay(file_id=file_id)
        await self.update_folder_update_time(file_record.file_level_path)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(file_record.knowledge_id)
        return updated_file

    async def delete_file(self, file_id: int):
        from bisheng.worker.knowledge.file_worker import delete_knowledge_file_celery

        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record or file_record.file_type != 1:
            raise SpaceFileNotFoundError()

        # 改用文档级权限校验（D-06）
        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'write')

        await KnowledgeFileDao.adelete_batch([file_id])
        delete_knowledge_file_celery.delay(file_ids=[file_id], knowledge_id=file_record.knowledge_id, clear_minio=True)
        await self.update_folder_update_time(file_record.file_level_path)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(file_record.knowledge_id)

    async def get_file_preview(self, file_id: int) -> dict:
        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record or file_record.file_type != 1:
            raise SpaceFileNotFoundError()

        # 改用文档级权限校验，支持 D-04 跨空间文档公开场景（D-06）
        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'read')

        original_url, preview_url = KnowledgeService.get_file_share_url(file_id)
        if KnowledgeService.is_inline_previewable(file_record.file_name or ''):
            # 内嵌预览走鉴权 API，避免 MinIO 预签名路径与 nginx 不一致导致 400
            preview_url = KnowledgeService.build_inline_content_api_path(
                file_record.knowledge_id, file_id
            )
        else:
            preview_url = KnowledgeService.resolve_preview_url(file_record, original_url, preview_url)
        perm = await KnowledgeFileDao.aget_user_file_permission(self.login_user, file_id)
        can_download = perm in ('write', 'admin')

        file_ext = ''
        if file_record.file_name and '.' in file_record.file_name:
            file_ext = file_record.file_name.rsplit('.', 1)[-1].lower()

        return {
            "original_url": original_url if can_download else "",
            "preview_url": preview_url,
            "can_download": can_download,
            "file_ext": file_ext,
        }

    async def open_file_content_stream(self, space_id: int, file_id: int):
        """校验读权限后返回 (file_record, minio_stream_response)。"""
        from bisheng.core.storage.minio.minio_manager import get_minio_storage_sync

        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record or file_record.file_type != 1:
            raise SpaceFileNotFoundError()
        if file_record.knowledge_id != space_id:
            raise SpaceFileNotFoundError()

        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'read')

        object_name = file_record.object_name
        if not object_name:
            raise SpaceFileNotFoundError()

        minio = get_minio_storage_sync()
        if not minio.object_exists_sync(object_name=object_name):
            raise SpaceFileNotFoundError()

        return file_record, minio.download_object_sync(object_name=object_name)

    # ──────────────────────────── Tags ───────────────────────────────────
    async def get_space_tags(self, space_id: int) -> List[Tag]:
        await self._require_read_permission(space_id)
        tags = await TagDao.get_tags_by_business(business_type=TagBusinessTypeEnum.KNOWLEDGE_SPACE,
                                                 business_id=str(space_id))
        return tags

    async def add_space_tag(self, space_id: int, tag_name: str) -> Tag:
        await self._require_write_permission(space_id)

        existing_tags = await TagDao.get_tags_by_business(business_type=TagBusinessTypeEnum.KNOWLEDGE_SPACE,
                                                          business_id=str(space_id), name=tag_name)
        if any(t.name == tag_name for t in existing_tags):
            raise SpaceTagExistsError()

        new_tag = Tag(
            name=tag_name,
            user_id=self.login_user.user_id,
            business_type=TagBusinessTypeEnum.KNOWLEDGE_SPACE,
            business_id=str(space_id),
        )
        return await TagDao.ainsert_tag(new_tag)

    async def delete_space_tag(self, space_id: int, tag_id: int):
        await self._require_write_permission(space_id)
        return await TagDao.delete_business_tag(tag_id, business_id=str(space_id),
                                                business_type=TagBusinessTypeEnum.KNOWLEDGE_SPACE)

    async def update_file_tags(self, space_id: int, file_id: int, tag_ids: List[int]):
        """ 2：支持对单文件的标签管理: Overwrite tags for a single file. """
        # 改用文档级权限校验（D-06）
        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'write')

        file_record = await KnowledgeFileDao.query_by_id(file_id)
        if not file_record or file_record.knowledge_id != space_id:
            raise SpaceFileNotFoundError()

        resource_id = str(file_id)
        resource_type = ResourceTypeEnum.SPACE_FILE
        await TagDao.aupdate_resource_tags(tag_ids, resource_id, resource_type, self.login_user.user_id)
        await KnowledgeDao.async_update_knowledge_update_time_by_id(space_id)

    async def batch_add_file_tags(self, space_id: int, file_ids: List[int], tag_ids: List[int]):
        """ 1：支持对文件批量添加标签: Batch add tags to files. """
        await self._require_write_permission(space_id)
        if not file_ids or not tag_ids:
            return

        files = await KnowledgeFileDao.aget_file_by_ids(file_ids)
        valid_file_ids = [f.id for f in files if f.knowledge_id == space_id]
        if not valid_file_ids:
            return

        resource_type = ResourceTypeEnum.SPACE_FILE
        for file_id in valid_file_ids:
            await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file_id, 'write')
            await TagDao.add_tags(tag_ids, str(file_id), resource_type, self.login_user.user_id)

        await KnowledgeDao.async_update_knowledge_update_time_by_id(space_id)

    async def retry_space_files(self, space_id: int, req_data: dict) -> list:
        """
        Retry logic for multiple files in a knowledge space with potentially new split rules.
        Similar to KnowledgeService.retry_files but scoped to a space.
        """
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()
        await self._require_write_permission(space_id)

        db_file_retry = req_data.get("file_objs")
        if not db_file_retry:
            return []

        id2input = {file.get("id"): file for file in db_file_retry}
        file_ids = list(id2input.keys())
        db_files = await KnowledgeFileDao.aget_file_by_ids(file_ids)
        if not db_files:
            return []

        for db_file in db_files:
            if db_file.knowledge_id != space_id:
                raise SpaceFileNotFoundError()
            await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, db_file.id, 'write')

        tmp, file_level_path = await self.process_retry_files(db_files, id2input, self.login_user)

        for folder_path in file_level_path:
            await self.update_folder_update_time(folder_path)

        await KnowledgeDao.async_update_knowledge_update_time_by_id(space_id)
        return []

    async def batch_retry_failed_files(self, space_id: int, file_ids: List[int]):

        from bisheng.worker import retry_knowledge_file_celery

        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space:
            raise SpaceNotFoundError()
        await self._require_write_permission(space_id)

        retry_files = await KnowledgeFileDao.aget_file_by_ids(file_ids)
        all_file_ids = []
        all_file_level_path = set()
        for file in retry_files:
            if file.knowledge_id != space_id:
                continue
            if file.file_type == FileType.FILE.value and file.status == KnowledgeFileStatus.FAILED.value:
                await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, file.id, 'write')
                retry_knowledge_file_celery.delay(file.id)
                all_file_ids.append(file.id)
                all_file_level_path.add(file.file_level_path)
            elif file.file_type == FileType.DIR.value:
                all_failed_files = await SpaceFileDao.get_children_by_prefix(knowledge_id=space_id,
                                                                             prefix=file.file_level_path + f"/{file.id}",
                                                                             file_status=KnowledgeFileStatus.FAILED)
                for item in all_failed_files:
                    if item.status == KnowledgeFileStatus.FAILED.value and item.file_type == FileType.FILE:
                        await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, item.id, 'write')
                        retry_knowledge_file_celery.delay(item.id)
                        all_file_ids.append(item.id)
                        all_file_level_path.add(file.file_level_path)
        if all_file_ids:
            await KnowledgeFileDao.aupdate_file_status(all_file_ids, KnowledgeFileStatus.WAITING,
                                                       "batch_retry_failed_files")
            await KnowledgeDao.async_update_knowledge_update_time_by_id(space_id)
        for one in all_file_level_path:
            if one:
                await self.update_folder_update_time(one)
        return True

    # ──────────────────────────── Batch Ops ───────────────────────────────────

    async def batch_delete(
            self, knowledge_id: int, file_ids: List[int], folder_ids: List[int]
    ):
        from bisheng.worker.knowledge.file_worker import delete_knowledge_file_celery

        knowledge = await KnowledgeDao.aquery_by_id(knowledge_id)
        if not knowledge:
            raise SpaceNotFoundError()

        # 文件夹层面仍用空间写权限校验（文件夹没有独立权限级别）
        await self._require_write_permission(knowledge_id)

        for folder_id in folder_ids:
            folder = await KnowledgeFileDao.query_by_id(folder_id)
            if folder and folder.file_type == 0:
                await self.delete_folder(knowledge.id, folder_id)

        if file_ids:
            # 逐文件执行文档级写权限校验（D-06）
            for fid in file_ids:
                await KnowledgeFileDao.acheck_doc_permission(self.login_user.user_id, fid, 'write')
            await KnowledgeFileDao.adelete_batch(file_ids)
            delete_knowledge_file_celery.delay(file_ids=file_ids, knowledge_id=knowledge.id,
                                               clear_minio=True)
            await KnowledgeDao.async_update_knowledge_update_time_by_id(knowledge.id)

    async def batch_download(
            self, space_id: int, file_ids: List[int], folder_ids: List[int]
    ) -> str:
        """
        Download selected files and folders, preserving the original directory structure,
        compress into a zip archive, upload to the MinIO tmp bucket, and return a
        presigned URL (valid for 7 days).

        Directory structure is reconstructed from file_level_path (e.g. '/7/42') by
        resolving each segment id to the corresponding folder name.
        """
        await self._require_read_permission(space_id)

        # ── 1. Collect all file records to include ────────────────────────────
        # Explicit files requested directly
        direct_files: List[KnowledgeFile] = await KnowledgeFileDao.aget_file_by_ids(file_ids) if file_ids else []

        # Files & sub-folders under every requested folder_id
        folder_db_records: List[KnowledgeFile] = []
        for folder_id in folder_ids:
            folder = await KnowledgeFileDao.query_by_id(folder_id)
            if not folder or folder.file_type != FileType.DIR.value:
                continue
            prefix = f"{folder.file_level_path}/{folder.id}"
            descendants = await SpaceFileDao.get_children_by_prefix(folder.knowledge_id, prefix)
            folder_db_records.append(folder)
            folder_db_records.extend(descendants)

        # All KnowledgeFile objects this download touches
        all_records: List[KnowledgeFile] = direct_files + folder_db_records

        # 按照文档级权限过滤（D-06）：只打包用户有编辑权限的文件（只读不可下载）
        authorized_ids = set(await KnowledgeFileDao.aget_authorized_file_ids(self.login_user, space_id))
        downloadable_ids = set()
        for fid in authorized_ids:
            perm = await KnowledgeFileDao.aget_user_file_permission(self.login_user, fid)
            if perm in ('write', 'admin'):
                downloadable_ids.add(fid)
        all_records = [r for r in all_records
                       if r.file_type == FileType.DIR.value or r.id in downloadable_ids]

        # ── 2. Build id→name map for every folder encountered ─────────────────
        #       We need this to translate '/7/42' → 'Reports/Q1'
        folder_id_to_name: dict[int, str] = {}
        for rec in all_records:
            if rec.file_type == FileType.DIR.value:
                folder_id_to_name[rec.id] = rec.file_name

        # Collect any ancestor folder IDs referenced in file_level_path but not yet known
        missing_ids: set[int] = set()
        for rec in all_records:
            if not rec.file_level_path:
                continue
            for part in rec.file_level_path.split('/'):
                if not part:
                    continue
                try:
                    fid = int(part)
                    if fid not in folder_id_to_name:
                        missing_ids.add(fid)
                except ValueError:
                    pass

        if missing_ids:
            extra_folders = await KnowledgeFileDao.aget_file_by_ids(list(missing_ids))
            for f in extra_folders:
                if f.file_type == FileType.DIR.value:
                    folder_id_to_name[f.id] = f.file_name

        def resolve_dir_path(file_level_path: Optional[str]) -> str:
            """Convert '/7/42' to 'FolderA/SubFolderB' using the name map."""
            if not file_level_path:
                return ''
            parts = [p for p in file_level_path.split('/') if p]
            names = []
            for part in parts:
                try:
                    fid = int(part)
                    names.append(folder_id_to_name.get(fid, str(fid)))
                except ValueError:
                    names.append(part)
            return '/'.join(names)

        # ── 3. Download files from MinIO and write into a temp dir ────────────
        from bisheng.core.storage.minio.minio_manager import get_minio_storage_sync
        minio = get_minio_storage_sync()

        import os
        import zipfile
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp_dir:
            for rec in all_records:
                rel_dir = resolve_dir_path(rec.file_level_path)
                local_dir = os.path.join(tmp_dir, rel_dir) if rel_dir else tmp_dir
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, rec.file_name)

                if rec.file_type == FileType.DIR.value:
                    # Create the folder explicitly so empty folders exist in tmp_dir
                    os.makedirs(local_path, exist_ok=True)
                    continue

                target_object_name = rec.object_name
                # If file source is CHANNEL, use preview_object_name to download the HTML file
                if rec.file_source == FileSource.CHANNEL.value and rec.preview_file_object_name:
                    target_object_name = rec.preview_file_object_name
                    name, _ = os.path.splitext(rec.file_name)
                    local_path = os.path.join(local_dir, f"{name}.html")

                if not target_object_name:  # no stored object – skip
                    continue

                try:
                    response = minio.download_object_sync(object_name=target_object_name)
                    with open(local_path, 'wb') as f:
                        for one in response.stream(65536):
                            f.write(one)
                except Exception:
                    # Skip files that cannot be fetched (e.g. not yet parsed)
                    continue
                finally:
                    response.close()
                    response.release_conn()

            # ── 4. Create zip archive ─────────────────────────────────────────
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            zip_folder = generate_uuid()
            zip_name = f"{timestamp}_{zip_folder[:6]}.zip"
            zip_path = os.path.join(tmp_dir, zip_name)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(tmp_dir):
                    # Add directories to ensure empty directories are included in the zip
                    for dirname in dirs:
                        dir_path = os.path.join(root, dirname)
                        arcname = os.path.relpath(dir_path, tmp_dir)
                        zf.write(dir_path, arcname)
                    # Add files
                    for filename in files:
                        if filename == zip_name:
                            continue  # don't zip the zip itself
                        full_path = os.path.join(root, filename)
                        arcname = os.path.relpath(full_path, tmp_dir)
                        zf.write(full_path, arcname)

            # ── 5. Upload zip to MinIO tmp bucket & return presigned URL ──────
            minio_object_name = f"download/{zip_folder}/{zip_name}"
            await minio.put_object_tmp(minio_object_name, Path(zip_path), content_type='application/zip')
            share_url = await minio.get_share_link(minio_object_name, bucket=minio.tmp_bucket, clear_host=True,
                                                   expire_days=7)

        return share_url

    # ──────────────────────────── Subscribe ───────────────────────────────────

    async def subscribe_space(self, space_id: int) -> dict:
        """
        Subscribe the current user to a knowledge space.
        - PRIVATE spaces cannot be subscribed.
        - PUBLIC spaces: status = active (True) → 'subscribed'
        - APPROVAL spaces: status = pending (False) → 'pending'
        Returns {"status": "subscribed" | "pending", "space_id": space_id}
        """
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()

        if space.auth_type == AuthTypeEnum.PRIVATE:
            raise SpaceSubscribePrivateError()

        # CREATOR_ADMIN 空间：不允许普通用户订阅
        if space.auth_type == AuthTypeEnum.CREATOR_ADMIN:
            raise SpaceSubscribePrivateError()

        target_status = (
            MembershipStatusEnum.ACTIVE
            if space.auth_type == AuthTypeEnum.PUBLIC
            else MembershipStatusEnum.PENDING
        )

        existing = await SpaceChannelMemberDao.async_find_member(space_id, self.login_user.user_id)
        if existing is not None:
            if existing.user_role != UserRoleEnum.MEMBER:
                return {
                    "status": "subscribed",
                    "space_id": space_id,
                }
            if existing.status == MembershipStatusEnum.ACTIVE:
                return {
                    "status": "subscribed",
                    "space_id": space_id,
                }
            if existing.status == MembershipStatusEnum.PENDING and target_status == MembershipStatusEnum.PENDING:
                return {
                    "status": "pending",
                    "space_id": space_id,
                }

        if not existing or existing.status == MembershipStatusEnum.REJECTED:
            count = await SpaceChannelMemberDao.async_count_user_space_subscriptions(self.login_user.user_id)
            if count >= _MAX_SUBSCRIBE_PER_USER:
                raise SpaceSubscribeLimitError()

        previous_status = existing.status if existing else None
        if existing:
            existing.status = target_status
            existing = await SpaceChannelMemberDao.update(existing)
            member = existing
        else:
            member = SpaceChannelMember(
                business_id=str(space_id),
                business_type=BusinessTypeEnum.SPACE,
                user_id=self.login_user.user_id,
                user_role=UserRoleEnum.MEMBER,
                status=target_status,
            )
            await SpaceChannelMemberDao.async_insert_member(member)

        if previous_status != MembershipStatusEnum.PENDING:
            await self._send_subscription_notification(space)

        return {
            "status": "subscribed" if member.status == MembershipStatusEnum.ACTIVE else "pending",
            "space_id": space_id,
        }

    async def _send_subscription_notification(self, space: Knowledge):
        if space.auth_type != AuthTypeEnum.APPROVAL or not self.message_service:
            return
        members = await SpaceChannelMemberDao.async_get_members_by_space(space.id,
                                                                         user_roles=[UserRoleEnum.ADMIN,
                                                                                     UserRoleEnum.CREATOR])
        member_ids = [one.user_id for one in members]
        await self.message_service.send_generic_approval(
            applicant_user_id=self.login_user.user_id,
            applicant_user_name=self.login_user.user_name,
            action_code="request_knowledge_space",
            business_type="knowledge_space_id",
            business_id=str(space.id),
            business_name=space.name,
            button_action_code="request_knowledge_space",
            receiver_user_ids=member_ids,
        )

    async def unsubscribe_space(self, space_id: int) -> bool:
        space = await KnowledgeDao.aquery_by_id(space_id)
        if not space or space.type != KnowledgeTypeEnum.SPACE.value:
            raise SpaceNotFoundError()

        return await SpaceChannelMemberDao.delete_space_member(space_id, self.login_user.user_id)
