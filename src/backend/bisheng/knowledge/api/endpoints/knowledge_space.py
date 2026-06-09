from typing import Any, Optional, List

from fastapi import APIRouter, Depends, Body, Query
from loguru import logger
from starlette.responses import StreamingResponse

from bisheng.common.errcode import BaseErrorCode
from bisheng.common.errcode.http_error import ServerError
from bisheng.common.schemas.api import resp_200, SSEResponse
from bisheng.knowledge.api.dependencies import get_knowledge_space_service, get_knowledge_space_chat_service
from bisheng.knowledge.domain.schemas.knowledge_space_schema import (
    KnowledgeSpaceCreateReq, KnowledgeSpaceUpdateReq,
    FolderCreateReq, FolderRenameReq,
    FileCreateReq, FileRenameReq,
    BatchDeleteReq, BatchDownloadReq,
    UpdateSpaceMemberRoleRequest, RemoveSpaceMemberRequest, AddSpaceMemberRequest,
    ChatReq, ChatFolderReq, )
from bisheng.knowledge.domain.services.knowledge_space_chat_service import KnowledgeSpaceChatService
from bisheng.knowledge.domain.services.knowledge_space_service import KnowledgeSpaceService
from bisheng.user.domain.services.auth import LoginUser

router = APIRouter(prefix='/knowledge/space', tags=['knowledge_space'])


# ──────────────────────────── 组织节点树接口 ──────────────────────────────────
# 注意：该路由不带 /space 前缀，挂载到上层 router，需单独注册。
# 这里临时放在 knowledge_space 文件中便于维护，通过独立 router 暴露。

from fastapi import APIRouter as _APIRouter
org_router = _APIRouter(prefix='/knowledge', tags=['knowledge_org'])


@org_router.get('/org-tree')
async def get_org_node_tree(
        login_user: LoginUser = Depends(LoginUser.get_login_user),
) -> Any:
    """
    返回 type=NORMAL 的四级机构知识库节点树，供空间创建/编辑时选择组织归属。
    - 超级管理员：返回完整树
    - 普通用户：返回自身 org_knowledge_ids 所在节点及其祖先节点
    """
    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao, KnowledgeTypeEnum, KnowledgeLevelEnum, Knowledge
    from sqlmodel import select
    from bisheng.core.database import get_async_db_session

    async with get_async_db_session() as session:
        result = await session.exec(
            select(Knowledge).where(
                Knowledge.type == KnowledgeTypeEnum.NORMAL.value,
                Knowledge.level < KnowledgeLevelEnum.LEVEL_MEMBER.value
            )
        )
        all_nodes = result.all()

    # 构建 id -> node 映射和树结构
    node_map = {n.id: {
        'id': n.id,
        'name': n.name,
        'level': n.level,
        'parent_id': n.parent_id,
        'children': []
    } for n in all_nodes}

    if not login_user.is_admin():
        # 普通用户只返回自己职务节点及其所有祖先
        allowed_ids = set(login_user.org_knowledge_ids or [])
        # 向上溯源收集祖先
        extra = set()
        for nid in list(allowed_ids):
            cur = node_map.get(nid)
            while cur and cur['parent_id']:
                extra.add(cur['parent_id'])
                cur = node_map.get(cur['parent_id'])
        allowed_ids |= extra
        node_map = {k: v for k, v in node_map.items() if k in allowed_ids}

    roots = []
    for node in node_map.values():
        pid = node['parent_id']
        if pid and pid in node_map:
            node_map[pid]['children'].append(node)
        else:
            roots.append(node)

    return resp_200(data=roots)


@org_router.post('/org-node')
async def create_org_node(
        name: str = Body(..., embed=True, description='机构节点名称'),
        description: str = Body(default=None, embed=True, description='机构节点描述'),
        parent_id: Optional[int] = Body(default=None, embed=True, description='父节点 ID，不填则为一级节点'),
        login_user: LoginUser = Depends(LoginUser.get_login_user),
) -> Any:
    """
    创建机构节点（超级管理员专用）。
    level 自动由 parent_id 所在层级 + 1 计算，最多支持 3 级（LEVEL_ORG_3=2）。
    一级节点 level=0，依次递增。
    """
    if not login_user.is_admin():
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail='无权限操作，该操作仅超级管理员可用')

    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao, KnowledgeTypeEnum, KnowledgeLevelEnum, Knowledge
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select
    from fastapi import HTTPException

    # 计算 level
    if parent_id is None:
        level = KnowledgeLevelEnum.LEVEL_ORG_1.value  # 0
    else:
        parent = await KnowledgeDao.aquery_by_id(parent_id)
        if not parent or parent.type != KnowledgeTypeEnum.NORMAL.value:
            raise HTTPException(status_code=404, detail='父节点不存在或不是有效的机构节点')
        new_level = parent.level + 1
        if new_level > KnowledgeLevelEnum.LEVEL_ORG_3.value:  # 最多 3 级
            raise HTTPException(status_code=400, detail='机构节点最多支持 3 级，无法继续添加子节点')
        level = new_level

    # 校验同级名称唯一
    async with get_async_db_session() as session:
        stmt = select(Knowledge).where(
            Knowledge.type == KnowledgeTypeEnum.NORMAL.value,
            Knowledge.name == name,
            Knowledge.parent_id == parent_id,
        )
        existing = (await session.exec(stmt)).first()
        if existing:
            raise HTTPException(status_code=400, detail=f'同级机构下已存在名称为「{name}」的节点')

    node = Knowledge(
        name=name,
        description=description,
        type=KnowledgeTypeEnum.NORMAL.value,
        level=level,
        parent_id=parent_id,
        user_id=login_user.user_id,
    )
    node = await KnowledgeDao.async_insert_one(node)
    return resp_200({
        'id': node.id,
        'name': node.name,
        'level': node.level,
        'parent_id': node.parent_id,
        'description': node.description,
        'children': [],
    })


@org_router.put('/org-node/{node_id}')
async def update_org_node(
        node_id: int,
        name: str = Body(default=None, embed=True, description='新名称'),
        description: str = Body(default=None, embed=True, description='新描述'),
        login_user: LoginUser = Depends(LoginUser.get_login_user),
) -> Any:
    """修改机构节点名称/描述（超级管理员专用）。"""
    if not login_user.is_admin():
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail='无权限操作，该操作仅超级管理员可用')

    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao, KnowledgeTypeEnum, Knowledge
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select
    from fastapi import HTTPException

    node = await KnowledgeDao.aquery_by_id(node_id)
    if not node or node.type != KnowledgeTypeEnum.NORMAL.value:
        raise HTTPException(status_code=404, detail='机构节点不存在')

    update_vals = {}
    if name and name != node.name:
        # 同级唯一校验
        async with get_async_db_session() as session:
            stmt = select(Knowledge).where(
                Knowledge.type == KnowledgeTypeEnum.NORMAL.value,
                Knowledge.name == name,
                Knowledge.parent_id == node.parent_id,
                Knowledge.id != node_id,
            )
            dup = (await session.exec(stmt)).first()
            if dup:
                raise HTTPException(status_code=400, detail=f'同级机构下已存在名称为「{name}」的节点')
        update_vals['name'] = name

    if description is not None:
        update_vals['description'] = description

    if update_vals:
        async with get_async_db_session() as session:
            from sqlmodel import update as sql_update
            await session.exec(
                sql_update(Knowledge).where(Knowledge.id == node_id).values(**update_vals)
            )
            await session.commit()

    return resp_200({'id': node_id, 'name': update_vals.get('name', node.name), 'description': update_vals.get('description', node.description)})


@org_router.delete('/org-node/{node_id}')
async def delete_org_node(
        node_id: int,
        login_user: LoginUser = Depends(LoginUser.get_login_user),
) -> Any:
    """
    删除机构节点（超级管理员专用）。
    若该节点存在子节点则拒绝删除；若有知识空间正在引用该节点，也会给出警告但仍允许删除（解除引用）。
    """
    if not login_user.is_admin():
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail='无权限操作，该操作仅超级管理员可用')

    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao, KnowledgeTypeEnum, Knowledge
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select, delete, func, col
    from fastapi import HTTPException

    node = await KnowledgeDao.aquery_by_id(node_id)
    if not node or node.type != KnowledgeTypeEnum.NORMAL.value:
        raise HTTPException(status_code=404, detail='机构节点不存在')

    # 检查子节点（在独立 session 中，避免 raise 污染事务）
    async with get_async_db_session() as session:
        child_count = await session.scalar(
            select(func.count(Knowledge.id)).where(
                Knowledge.type == KnowledgeTypeEnum.NORMAL.value,
                Knowledge.parent_id == node_id,
            )
        )
        ref_count = await session.scalar(
            select(func.count(Knowledge.id)).where(Knowledge.org_node_id == node_id)
        )

    if child_count and child_count > 0:
        raise HTTPException(status_code=400, detail=f'该节点下还有 {child_count} 个子节点，请先删除子节点')

    # 执行删除和解引用（同一事务）
    async with get_async_db_session() as session:
        await session.exec(delete(Knowledge).where(Knowledge.id == node_id))
        if ref_count and ref_count > 0:
            from sqlmodel import update as sql_update
            await session.exec(
                sql_update(Knowledge).where(Knowledge.org_node_id == node_id).values(org_node_id=None)
            )
        await session.commit()

    return resp_200({
        'success': True,
        'unlinked_spaces': ref_count or 0,
    })



# ──────────────────────────── Space CRUD ──────────────────────────────────────

@router.post('')
async def create_space(
        req: KnowledgeSpaceCreateReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    space = await svc.create_knowledge_space(
        name=req.name,
        description=req.description,
        icon=req.icon,
        auth_type=req.auth_type,
        is_released=req.is_released,
        org_node_id=req.org_node_id,
    )
    return resp_200(space)


@router.get('/{space_id}/info')
async def get_space_info(
        space_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    space_info = await svc.get_space_info(space_id)
    return resp_200(space_info)


@router.put('/{space_id}')
async def update_space(
        space_id: int,
        req: KnowledgeSpaceUpdateReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    space = await svc.update_knowledge_space(
        space_id=space_id,
        name=req.name,
        description=req.description,
        icon=req.icon,
        auth_type=req.auth_type,
        is_released=req.is_released,
        org_node_id=req.org_node_id,
    )
    return resp_200(space)


@router.post("/{space_id}/set-pin")
async def set_channel_pin(
        space_id: int,
        is_pined: bool = Body(default=True, embed=True),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    """Set channel pin status."""
    await svc.pin_space(space_id, is_pined)
    return resp_200(data=True)


@router.delete('/{space_id}')
async def delete_space(
        space_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    await svc.delete_space(space_id)
    return resp_200()


# ──────────────────────────── Space Listings ───────────────────────────────────

@router.get('/mine')
async def get_my_created_spaces(
        order_by: str = 'update_time',
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    spaces = await svc.get_my_created_spaces(order_by)
    return resp_200(spaces)


@router.get('/managed')
async def get_my_managed_spaces(
        order_by: str = 'name',
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    spaces = await svc.get_my_managed_spaces(order_by)
    return resp_200(spaces)


@router.get('/joined')
async def get_my_followed_spaces(
        order_by: str = 'update_time',
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    spaces = await svc.get_my_followed_spaces(order_by)
    return resp_200(spaces)


@router.get('/accessible')
async def get_accessible_spaces(
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    返回当前用户虽非成员、但因空间内含对所有人公开的文档而应能访问的知识空间列表。
    体现"文档级权限 > 空间级权限"的核心原则，解决用户有文档权限却找不到入口的导航盲区。
    """
    spaces = await svc.get_accessible_spaces()
    return resp_200(spaces)


@router.get('/square')
async def get_knowledge_square(
        page: int = 1,
        page_size: int = 20,
        keyword: str = None,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.get_knowledge_square(keyword, page, page_size)
    return resp_200(result)


# ──────────────────────────── Members ─────────────────────────────────────────

@router.get('/{space_id}/members')
async def get_space_members(
        space_id: int,
        page: int = Query(1, description="Page number"),
        page_size: int = Query(20, description="Page size"),
        keyword: Optional[str] = Query(None, description="Search keyword"),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.get_space_members(space_id, page, page_size, keyword)
    return resp_200(result)


@router.post('/{space_id}/members')
async def add_space_member(
        space_id: int,
        req: AddSpaceMemberRequest,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    req.space_id = space_id
    result = await svc.add_space_member(req)
    return resp_200(result)


@router.put('/{space_id}/members/role')
async def update_member_role(
        space_id: int,
        req: UpdateSpaceMemberRoleRequest,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    req.space_id = space_id
    result = await svc.update_member_role(req)
    return resp_200(result)


@router.delete('/{space_id}/members')
async def remove_member(
        space_id: int,
        req: RemoveSpaceMemberRequest,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    req.space_id = space_id
    result = await svc.remove_member(req)
    return resp_200(result)


@router.get('/{space_id}/children')
async def list_space_children(
        space_id: int,
        parent_id: Optional[int] = None,
        order_field: str = 'file_type',
        order_sort: str = 'asc',
        file_status: List[int] = Query(default=None, description="文件状态列表"),
        page: int = 1,
        page_size: int = 20,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.list_space_children(space_id, parent_id, order_field, order_sort,
                                           file_status=file_status, page=page, page_size=page_size)
    return resp_200(result)


@router.get('/{space_id}/search')
async def search_space_children(
        space_id: int,
        parent_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 20,
        order_field: str = 'file_type',
        order_sort: str = 'asc',
        tag_ids: List[int] = Query(default=None, description='标签ID列表'),
        file_status: List[int] = Query(default=None, description='文件状态列表'),
        keyword: Optional[str] = None,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.search_space_children(space_id, parent_id, tag_ids=tag_ids, keyword=keyword, page=page,
                                             page_size=page_size, file_status=file_status,
                                             order_field=order_field, order_sort=order_sort)
    return resp_200(result)


@router.get("/{space_id}/tag")
async def get_space_tag(
        space_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    result = await svc.get_space_tags(space_id)
    return resp_200(result)


@router.post('/{space_id}/tag')
async def add_space_tags(
        space_id: int,
        tag_name: str = Body(..., embed=True, description='标签名称'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    result = await svc.add_space_tag(space_id, tag_name)
    return resp_200(result)


@router.delete('/{space_id}/tag')
async def delete_space_tags(
        space_id: int,
        tag_id: int = Body(..., embed=True, description='标签ID'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    result = await svc.delete_space_tag(space_id, tag_id)
    return resp_200(result)


# ──────────────────────────── Folders ─────────────────────────────────────────

@router.post('/{space_id}/folders')
async def add_folder(
        space_id: int,
        req: FolderCreateReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    folder = await svc.add_folder(
        knowledge_id=space_id,
        folder_name=req.name,
        parent_id=req.parent_id,
    )
    return resp_200(folder)


@router.put('/{space_id}/folders/{folder_id}')
async def rename_folder(
        space_id: int,
        folder_id: int,
        req: FolderRenameReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    folder = await svc.rename_folder(folder_id, req.name)
    return resp_200(folder)


@router.delete('/{space_id}/folders/{folder_id}')
async def delete_folder(
        space_id: int,
        folder_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    await svc.delete_folder(space_id, folder_id)
    return resp_200()


@router.get('/{space_id}/folders/{folder_id}/parent')
async def get_folder_parent(
        space_id: int,
        folder_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.get_folder_file_parent(space_id, folder_id)
    return resp_200(result)


# ──────────────────────────── Files ───────────────────────────────────────────

@router.post('/{space_id}/files')
async def add_file(
        space_id: int,
        req: FileCreateReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    file_record = await svc.add_file(
        knowledge_id=space_id,
        file_path=req.file_path,
        parent_id=req.parent_id,
    )
    return resp_200(file_record)


@router.put('/{space_id}/files/{file_id}')
async def rename_file(
        space_id: int,
        file_id: int,
        req: FileRenameReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    file_record = await svc.rename_file(file_id, req.name)
    return resp_200(file_record)


@router.delete('/{space_id}/files/{file_id}')
async def delete_file(
        space_id: int,
        file_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    await svc.delete_file(file_id)
    return resp_200()


@router.get('/{space_id}/files/{file_id}/preview')
async def get_file_preview(
        space_id: int,
        file_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    urls = await svc.get_file_preview(file_id)
    return resp_200(urls)


@router.get('/{space_id}/files/{file_id}/content')
async def get_file_content(
        space_id: int,
        file_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    import mimetypes
    from urllib.parse import quote

    file_record, minio_response = await svc.open_file_content_stream(space_id, file_id)
    media_type = mimetypes.guess_type(file_record.file_name or '')[0] or 'application/octet-stream'
    encoded_name = quote(file_record.file_name or 'file')

    def iter_content():
        try:
            for chunk in minio_response.stream(65536):
                yield chunk
        finally:
            minio_response.close()
            minio_response.release_conn()

    return StreamingResponse(
        iter_content(),
        media_type=media_type,
        headers={'Content-Disposition': f"inline; filename*=UTF-8''{encoded_name}"},
    )


@router.post('/{space_id}/files/{file_id}/tag')
async def update_file_tags(
        space_id: int,
        file_id: int,
        tag_ids: List[int] = Body(..., embed=True, description='标签ID列表'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    result = await svc.update_file_tags(space_id, file_id, tag_ids)
    return resp_200(result)


# ──────────────────────────── Batch Ops ───────────────────────────────────────

@router.post('/{space_id}/files/batch-download')
async def batch_download(
        space_id: int,
        req: BatchDownloadReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    url = await svc.batch_download(space_id, req.file_ids, req.folder_ids)
    return resp_200({'url': url})


@router.post('/{space_id}/files/batch-delete')
async def batch_delete(
        space_id: int,
        req: BatchDeleteReq,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    await svc.batch_delete(space_id, req.file_ids, req.folder_ids)
    return resp_200()


@router.post('/{space_id}/files/batch-tag')
async def batch_update_tags(
        space_id: int,
        file_ids: List[int] = Body(..., embed=True, description='文件ID列表'),
        tag_ids: List[int] = Body(..., embed=True, description='标签ID列表'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.batch_add_file_tags(space_id, file_ids, tag_ids)
    return resp_200(result)


@router.post('/{space_id}/files/batch-retry')
async def batch_retry_failed_files(
        space_id: int,
        file_ids: List[int] = Body(..., embed=True, description='file or folder ids'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
):
    result = await svc.batch_retry_failed_files(space_id, file_ids)
    return resp_200(result)


@router.post('/{space_id}/files/retry')
async def retry_space_files(
        space_id: int,
        req_data: dict = Body(...),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """Retry files in a knowledge space with potentially new split rules"""
    result = await svc.retry_space_files(space_id, req_data)
    return resp_200(result)


# ──────────────────────────── Subscribe ───────────────────────────────────────

@router.post('/{space_id}/subscribe', response_model=None)
async def subscribe_space(
        space_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.subscribe_space(space_id)
    return resp_200(result)


@router.post('/{space_id}/unsubscribe', response_model=None)
async def subscribe_space(
        space_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    result = await svc.unsubscribe_space(space_id)
    return resp_200(result)


# ──────────────────────────── Chat ────────────────────────────────────────────

@router.post('/{space_id}/chat/file/{file_id}')
async def chat_single_file(
        space_id: int,
        file_id: int,
        req: ChatReq,
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
) -> Any:
    async def event_stream():
        try:
            async for one in svc.chat_single_file(space_id, file_id, req.query):
                yield SSEResponse(data=one).to_string()
        except BaseErrorCode as e:
            yield e.to_sse_event_instance_str()
        except Exception as e:
            logger.exception("chat_file error")
            yield ServerError(exception=e).to_sse_event_instance_str()

    return StreamingResponse(event_stream(), media_type='text/event-stream')


@router.get('/{space_id}/chat/file/{file_id}/history')
async def chat_single_file_history(
        space_id: int,
        file_id: int,
        page_size: int = 20,
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
) -> Any:
    response = await svc.single_file_history(space_id, file_id, page_size)
    return resp_200(response)


@router.delete('/{space_id}/chat/file/{file_id}/history')
async def clear_single_file_history(
        space_id: int,
        file_id: int,
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    response = await svc.clear_file_history(space_id, file_id)
    return resp_200(response)


@router.get('/{space_id}/chat/folder/session')
async def get_chat_folder_session(
        space_id: int,
        folder_id: int = Query(default=0, description="folder id"),
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    result = await svc.get_chat_folder_session(space_id, folder_id)
    return resp_200(result)


@router.post('/{space_id}/chat/folder/session')
async def create_chat_folder_session(
        space_id: int,
        folder_id: int = Body(default=0, embed=True, description="folder id"),
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    result = await svc.create_chat_folder_session(space_id, folder_id)
    return resp_200(result)


@router.delete('/{space_id}/chat/folder/session')
async def create_chat_folder_session(
        space_id: int,
        folder_id: int = Body(default=0, description="folder id"),
        chat_id: str = Body(..., description='Chat ID'),
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    result = await svc.delete_chat_folder_session(space_id, folder_id, chat_id)
    return resp_200(result)


@router.get('/{space_id}/chat/folder/history')
async def get_chat_folder_history(
        space_id: int,
        folder_id: int = Query(default=0, description="folder id"),
        chat_id: str = Query(..., description='Chat ID'),
        page_size: int = 20,
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    result = await svc.get_chat_folder_history(space_id, folder_id, chat_id, page_size)
    return resp_200(result)


@router.delete('/{space_id}/chat/folder/history')
async def get_chat_folder_history(
        space_id: int,
        folder_id: int = Query(default=0, description="folder id"),
        chat_id: str = Query(..., description='Chat ID'),
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
):
    result = await svc.delete_chat_folder_history(space_id, folder_id, chat_id)
    return resp_200(result)


@router.post('/{space_id}/chat/folder')
async def chat_folder(
        space_id: int,
        req: ChatFolderReq,
        svc: KnowledgeSpaceChatService = Depends(get_knowledge_space_chat_service),
) -> Any:
    async def event_stream():
        try:
            async for one in svc.chat_folder(space_id, req.folder_id, req.chat_id, req.query, req.tags):
                yield SSEResponse(data=one).to_string()
        except BaseErrorCode as e:
            yield e.to_sse_event_instance_str()
        except Exception as e:
            logger.exception("chat_folder error")
            yield ServerError(exception=e).to_sse_event_instance_str()

    return StreamingResponse(event_stream(), media_type='text/event-stream')


# ──────────────────────── Document Permissions (D-04 / D-06) ──────────────────

_VALID_SUBJECT_TYPES = frozenset({'all', 'user', 'org'})
_VALID_PERMISSION_TYPES = frozenset({'read', 'write', 'admin'})


def _validate_permission_rule(subject_type: str, subject_id: str, permission_type: str) -> None:
    from fastapi import HTTPException
    if subject_type not in _VALID_SUBJECT_TYPES:
        raise HTTPException(status_code=400, detail=f'无效的 subject_type: {subject_type}')
    if permission_type not in _VALID_PERMISSION_TYPES:
        raise HTTPException(status_code=400, detail=f'无效的 permission_type: {permission_type}')
    if subject_type == 'all' and subject_id != '*':
        raise HTTPException(status_code=400, detail='subject_type=all 时 subject_id 必须为 *')
    if subject_type in ('user', 'org') and not subject_id.strip():
        raise HTTPException(status_code=400, detail='subject_id 不能为空')


@router.get('/{space_id}/files/{file_id}/permissions')
async def get_file_permissions(
        space_id: int,
        file_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    获取指定文档的权限配置。
    返回：is_private 开关、所有白名单规则列表（含展示名称）、当前用户的计算权限。
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select

    # 校验 admin 权限
    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    file_record = await KnowledgeFileDao.query_by_id(file_id)
    if not file_record or file_record.knowledge_id != space_id:
        from bisheng.common.errcode.knowledge_space import SpaceFileNotFoundError
        raise SpaceFileNotFoundError()

    async with get_async_db_session() as session:
        stmt = select(DocumentAccess).where(DocumentAccess.file_id == file_id)
        rules = (await session.exec(stmt)).all()

    # 展示授权对象名称
    from bisheng.user.domain.models.user import UserDao
    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao
    rule_list = []
    for rule in rules:
        display_name = rule.subject_id
        if rule.subject_type == 'user':
            users = await UserDao.aget_user_by_ids([int(rule.subject_id)])
            if users:
                display_name = users[0].user_name
        elif rule.subject_type == 'org':
            kb = await KnowledgeDao.aquery_by_id(int(rule.subject_id))
            if kb:
                display_name = kb.name
        elif rule.subject_type == 'all':
            display_name = '所有人'
        rule_list.append({
            'id': rule.id,
            'subject_type': rule.subject_type,
            'subject_id': rule.subject_id,
            'subject_name': display_name,
            'permission_type': rule.permission_type,
        })

    my_permission = await KnowledgeFileDao.aget_user_file_permission(svc.login_user, file_id)
    return resp_200({
        'is_private': file_record.is_private,
        'rules': rule_list,
        'my_permission': my_permission,
    })


@router.post('/{space_id}/files/{file_id}/permissions')
async def save_file_permissions(
        space_id: int,
        file_id: int,
        is_private: bool = Body(..., embed=True, description='是否启用独立权限白名单'),
        rules: List[dict] = Body(default=[], embed=True, description='权限规则列表'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    保存指定文档的权限配置（全量覆盖白名单规则）。
    rules 格式：[{subject_type, subject_id, permission_type}]
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select, delete

    # 校验 admin 权限
    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    file_record = await KnowledgeFileDao.query_by_id(file_id)
    if not file_record or file_record.knowledge_id != space_id:
        from bisheng.common.errcode.knowledge_space import SpaceFileNotFoundError
        raise SpaceFileNotFoundError()

    async with get_async_db_session() as session:
        # 全量删除旧规则
        await session.exec(delete(DocumentAccess).where(DocumentAccess.file_id == file_id))
        # 写入新规则
        for rule in rules:
            subject_type = rule['subject_type']
            subject_id = str(rule['subject_id'])
            permission_type = rule.get('permission_type', 'read')
            _validate_permission_rule(subject_type, subject_id, permission_type)
            session.add(DocumentAccess(
                file_id=file_id,
                subject_type=subject_type,
                subject_id=subject_id,
                permission_type=permission_type,
            ))
        await session.commit()

    # 更新 is_private
    file_record.is_private = is_private
    await KnowledgeFileDao.async_update(file_record)

    return resp_200({'success': True})


@router.get('/{space_id}/permissions/candidates')
async def get_permission_candidates(
        space_id: int,
        keyword: Optional[str] = Query(default=None, description='模糊搜索关键词'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    搜索可授权的候选对象（用户 + 机构节点），供权限配置弹窗使用。
    始终在结果首部添加"所有人"快捷条目。
    要求：调用者需是空间写权限角色（admin / creator）。
    """
    await svc._require_write_permission(space_id)

    results = [{'subject_type': 'all', 'subject_id': '*', 'subject_name': '所有人'}]

    import asyncio
    # 搜索用户（search_user_by_name 为同步方法，用 asyncio.to_thread 避免阻塞）
    from bisheng.user.domain.models.user import UserDao
    users = await asyncio.to_thread(UserDao.search_user_by_name, keyword or '')
    for u in (users or [])[:20]:
        results.append({'subject_type': 'user', 'subject_id': str(u.user_id), 'subject_name': u.user_name})

    # 搜索机构知识库节点（type=3 为 SPACE 类型的机构节点）
    from bisheng.knowledge.domain.models.knowledge import KnowledgeDao, KnowledgeTypeEnum
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select
    from bisheng.knowledge.domain.models.knowledge import Knowledge
    kw = keyword or ''
    async with get_async_db_session() as session:
        stmt = select(Knowledge).where(
            Knowledge.type == KnowledgeTypeEnum.SPACE.value,
            Knowledge.name.like(f'%{kw}%'),
        ).limit(20)
        orgs = (await session.exec(stmt)).all()
    for org in orgs:
        results.append({'subject_type': 'org', 'subject_id': str(org.id), 'subject_name': org.name})

    return resp_200(results)


# ──────────────────────── Document Permission — Single Rule CRUD ──────────────

@router.post('/{space_id}/files/{file_id}/permissions/rules')
async def add_file_permission_rule(
        space_id: int,
        file_id: int,
        subject_type: str = Body(..., embed=True, description='授权对象类型: all / user / org'),
        subject_id: str = Body(..., embed=True, description='对应 ID: * / user_id / org_kb_id'),
        permission_type: str = Body(default='read', embed=True, description='权限级别: read / write / admin'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    添加单条文档授权规则。
    若该 (file_id, subject_type, subject_id) 组合已存在，则更新 permission_type（upsert 语义）。
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select

    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    _validate_permission_rule(subject_type, str(subject_id), permission_type)

    file_record = await KnowledgeFileDao.query_by_id(file_id)
    if not file_record or file_record.knowledge_id != space_id:
        from bisheng.common.errcode.knowledge_space import SpaceFileNotFoundError
        raise SpaceFileNotFoundError()

    async with get_async_db_session() as session:
        # 检查是否已存在相同主体的规则（upsert）
        stmt = select(DocumentAccess).where(
            DocumentAccess.file_id == file_id,
            DocumentAccess.subject_type == subject_type,
            DocumentAccess.subject_id == str(subject_id),
        )
        existing = (await session.exec(stmt)).first()

        if existing:
            existing.permission_type = permission_type
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
            rule_id = existing.id
            action = 'updated'
        else:
            new_rule = DocumentAccess(
                file_id=file_id,
                subject_type=subject_type,
                subject_id=str(subject_id),
                permission_type=permission_type,
            )
            session.add(new_rule)
            await session.commit()
            await session.refresh(new_rule)
            rule_id = new_rule.id
            action = 'created'

    return resp_200({'id': rule_id, 'action': action})


@router.put('/{space_id}/files/{file_id}/permissions/rules/{rule_id}')
async def update_file_permission_rule(
        space_id: int,
        file_id: int,
        rule_id: int,
        permission_type: str = Body(..., embed=True, description='新的权限级别: read / write / admin'),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    修改单条文档授权规则的权限级别。
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from fastapi import HTTPException

    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    if permission_type not in _VALID_PERMISSION_TYPES:
        raise HTTPException(status_code=400, detail=f'无效的 permission_type: {permission_type}')

    async with get_async_db_session() as session:
        rule = await session.get(DocumentAccess, rule_id)
        if not rule or rule.file_id != file_id:
            raise HTTPException(status_code=404, detail='授权规则不存在')

        rule.permission_type = permission_type
        session.add(rule)
        await session.commit()

    return resp_200({'success': True})


@router.delete('/{space_id}/files/{file_id}/permissions/rules/{rule_id}')
async def delete_file_permission_rule(
        space_id: int,
        file_id: int,
        rule_id: int,
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    删除单条文档授权规则。
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from fastapi import HTTPException

    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    async with get_async_db_session() as session:
        rule = await session.get(DocumentAccess, rule_id)
        if not rule or rule.file_id != file_id:
            raise HTTPException(status_code=404, detail='授权规则不存在')

        await session.delete(rule)
        await session.commit()

    return resp_200({'success': True})


# ──────────────────────── Document Permission — Batch Grant ──────────────────

@router.post('/{space_id}/files/{file_id}/permissions/batch')
async def batch_add_file_permission_rules(
        space_id: int,
        file_id: int,
        permission_type: str = Body(..., embed=True, description='统一权限级别: read / write / admin'),
        subjects: List[dict] = Body(
            ..., embed=True,
            description='授权对象列表，每项格式: {subject_type: str, subject_id: str}'
        ),
        svc: KnowledgeSpaceService = Depends(get_knowledge_space_service),
) -> Any:
    """
    批量追加文档授权规则，不覆盖已有规则。
    若某个 (subject_type, subject_id) 已存在规则，则执行 upsert 更新 permission_type。

    请求体示例：
    {
        "permission_type": "read",
        "subjects": [
            {"subject_type": "user", "subject_id": "101"},
            {"subject_type": "user", "subject_id": "102"},
            {"subject_type": "org",  "subject_id": "5"}
        ]
    }

    响应：{inserted: N, updated: M, total: N+M}
    要求：调用者需对该文件拥有 admin 权限。
    """
    from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFileDao, DocumentAccess
    from bisheng.core.database import get_async_db_session
    from sqlmodel import select, col
    from fastapi import HTTPException

    await KnowledgeFileDao.acheck_doc_permission(svc.login_user.user_id, file_id, 'admin')

    if permission_type not in _VALID_PERMISSION_TYPES:
        raise HTTPException(status_code=400, detail=f'无效的 permission_type: {permission_type}')

    file_record = await KnowledgeFileDao.query_by_id(file_id)
    if not file_record or file_record.knowledge_id != space_id:
        from bisheng.common.errcode.knowledge_space import SpaceFileNotFoundError
        raise SpaceFileNotFoundError()

    if not subjects:
        return resp_200({'inserted': 0, 'updated': 0, 'total': 0})

    inserted = 0
    updated = 0

    async with get_async_db_session() as session:
        # 批量查询已存在的规则，减少 N+1 查询
        subject_keys = [(s['subject_type'], str(s['subject_id'])) for s in subjects]
        existing_stmt = select(DocumentAccess).where(
            DocumentAccess.file_id == file_id,
            col(DocumentAccess.subject_type).in_([k[0] for k in subject_keys]),
        )
        existing_rules = (await session.exec(existing_stmt)).all()
        existing_map = {(r.subject_type, r.subject_id): r for r in existing_rules}

        for subj in subjects:
            s_type = subj.get('subject_type', '')
            s_id = str(subj.get('subject_id', ''))
            if not s_type or not s_id:
                continue
            _validate_permission_rule(s_type, s_id, permission_type)

            key = (s_type, s_id)
            if key in existing_map:
                # upsert：更新已有规则的权限级别
                rule = existing_map[key]
                if rule.permission_type != permission_type:
                    rule.permission_type = permission_type
                    session.add(rule)
                updated += 1
            else:
                session.add(DocumentAccess(
                    file_id=file_id,
                    subject_type=s_type,
                    subject_id=s_id,
                    permission_type=permission_type,
                ))
                inserted += 1

        await session.commit()

    return resp_200({'inserted': inserted, 'updated': updated, 'total': inserted + updated})
