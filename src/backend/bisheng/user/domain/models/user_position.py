from datetime import datetime
from typing import List, Optional

from sqlalchemy import Column, DateTime, text, UniqueConstraint
from sqlmodel import Field, select, delete

from bisheng.common.models.base import SQLModelSerializable
from bisheng.core.database import get_async_db_session, get_sync_db_session


class UserPosition(SQLModelSerializable, table=True):
    """用户职务表：记录每个用户归属的组织知识库节点（即四级知识库树中的某个节点 ID）。"""
    __tablename__ = "user_position"
    __table_args__ = (
        UniqueConstraint("user_id", "kb_node_id", name="uk_user_kb"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(nullable=False, index=True, description="用户 ID")
    kb_node_id: int = Field(nullable=False, index=True, description="组织节点知识库 ID（knowledge 表，type=NORMAL）")
    position_name: Optional[str] = Field(default=None, max_length=100, description="职务名称，如：学生、院长、辅导员")
    create_time: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP")),
    )
    update_time: Optional[datetime] = Field(
        default=None,
        sa_column=Column(
            DateTime, nullable=True,
            server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
        ),
    )


class UserPositionDao(SQLModelSerializable):
    """用户职务 DAO：封装职务查询、分配和删除操作。"""

    # ──────────────────── 查询 ────────────────────

    @classmethod
    def get_user_positions(cls, user_id: int) -> List[UserPosition]:
        """同步：返回用户所有职务记录。"""
        with get_sync_db_session() as session:
            return session.exec(
                select(UserPosition).where(UserPosition.user_id == user_id)
            ).all()

    @classmethod
    async def aget_user_positions(cls, user_id: int) -> List[UserPosition]:
        """异步：返回用户所有职务记录。"""
        async with get_async_db_session() as session:
            result = await session.exec(
                select(UserPosition).where(UserPosition.user_id == user_id)
            )
            return result.all()

    @classmethod
    def get_user_kb_node_ids(cls, user_id: int) -> List[int]:
        """同步：返回用户持有的所有组织节点 ID 列表（即 org_knowledge_ids）。"""
        with get_sync_db_session() as session:
            result = session.exec(
                select(UserPosition.kb_node_id).where(UserPosition.user_id == user_id)
            )
            return list(result.all())

    @classmethod
    async def aget_user_kb_node_ids(cls, user_id: int) -> List[int]:
        """异步：返回用户持有的所有组织节点 ID 列表。"""
        async with get_async_db_session() as session:
            result = await session.exec(
                select(UserPosition.kb_node_id).where(UserPosition.user_id == user_id)
            )
            return list(result.all())

    @classmethod
    async def aget_users_by_kb_node(cls, kb_node_id: int) -> List[int]:
        """异步：查询持有指定组织节点的所有用户 ID（用于批量展示节点成员）。"""
        async with get_async_db_session() as session:
            result = await session.exec(
                select(UserPosition.user_id).where(UserPosition.kb_node_id == kb_node_id)
            )
            return list(result.all())

    # ──────────────────── 写入 ────────────────────

    @classmethod
    async def aset_user_positions(
        cls,
        user_id: int,
        positions: List[dict],
    ) -> List[UserPosition]:
        """
        覆盖式更新用户职务（先全删，再批量插入），并同步更新 user.org_knowledge_ids 冗余字段。

        参数：
            user_id: 目标用户 ID。
            positions: 职务列表，每项格式为 {"kb_node_id": int, "position_name": str|None}。

        返回：
            插入成功的 UserPosition 记录列表。
        """
        async with get_async_db_session() as session:
            # 先清空该用户所有职务并立刻提交，防止混合 DML 造成事务覆盖
            await session.exec(delete(UserPosition).where(UserPosition.user_id == user_id))
            await session.commit()

        new_positions = []
        async with get_async_db_session() as session:
            for p in positions:
                pos = UserPosition(
                    user_id=user_id,
                    kb_node_id=p["kb_node_id"],
                    position_name=p.get("position_name"),
                )
                session.add(pos)
                new_positions.append(pos)
            await session.commit()

        # 同步更新 user 表中的冗余字段，保持向后兼容
        kb_ids = [p["kb_node_id"] for p in positions]
        from bisheng.user.domain.models.user import UserDao
        user_db = UserDao.get_user(user_id)
        if user_db:
            user_db.org_knowledge_ids = kb_ids
            UserDao.update_user(user_db)

        return new_positions

    @classmethod
    async def adelete_position(cls, position_id: int) -> bool:
        """异步：删除单条职务记录，同步更新 user.org_knowledge_ids。返回是否成功删除。"""
        async with get_async_db_session() as session:
            pos = await session.get(UserPosition, position_id)
            if not pos:
                return False
            user_id = pos.user_id
            await session.delete(pos)
            await session.commit()

        # 重新计算该用户的 org_knowledge_ids
        remaining_node_ids = await cls.aget_user_kb_node_ids(user_id)
        from bisheng.user.domain.models.user import UserDao
        user_db = UserDao.get_user(user_id)
        if user_db:
            user_db.org_knowledge_ids = remaining_node_ids
            UserDao.update_user(user_db)

        return True
