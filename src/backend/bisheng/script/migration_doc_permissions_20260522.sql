-- ============================================================
-- Migration: 文档权限系统字段扩展
-- Date: 2026-05-22
-- Description:
--   1. document_access 表新增 permission_type 字段（授权级别）
--   2. knowledgefile 表新增 is_private 字段（文档独立权限开关）
-- ============================================================

-- 1. DocumentAccess：新增授权权限级别字段
--    permission_type: 'read'(只读) / 'write'(编辑) / 'admin'(完全控制)
ALTER TABLE document_access
    ADD COLUMN permission_type VARCHAR(20) NOT NULL DEFAULT 'read'
        COMMENT '授权权限级别：read / write / admin';

-- 2. DocumentAccess：subject_type 补充 'all' 类型说明（无需 DDL，仅应用层约束）
--    subject_type = 'all' 时，subject_id = '*'，表示授权给所有登录用户

-- 3. KnowledgeFile：新增文档独立权限白名单开关
--    is_private = 0（默认）：无白名单规则时回退到知识库级权限
--    is_private = 1：无白名单规则时仅上传者可见；有白名单时未匹配则拒绝
ALTER TABLE knowledgefile
    ADD COLUMN is_private TINYINT(1) NOT NULL DEFAULT 0
        COMMENT '文档私有开关：0=无白名单时回退知识库级权限；1=无白名单时仅上传者可见';

-- ============================================================
-- ROLLBACK (如需回滚)
-- ============================================================
-- ALTER TABLE document_access DROP COLUMN permission_type;
-- ALTER TABLE knowledgefile DROP COLUMN is_private;
