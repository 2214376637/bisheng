-- ============================================================
-- 用户职务体系 & 知识空间权限等级 数据库迁移脚本
-- 执行环境：MySQL / MariaDB
-- 执行顺序：按本文件从上到下依次执行
-- ============================================================

-- 1. 新增用户职务表 user_position
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_position (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL            COMMENT '用户 ID',
    kb_node_id    INT NOT NULL            COMMENT '组织节点知识库 ID（knowledge 表，type=NORMAL）',
    position_name VARCHAR(100) NULL       COMMENT '职务名称，如：学生、院长、辅导员',
    create_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    update_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id    (user_id),
    INDEX idx_kb_node_id (kb_node_id),
    UNIQUE KEY uk_user_kb (user_id, kb_node_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户职务（组织节点归属）';

-- 2. 知识空间绑定组织节点字段
-- ---------------------------------------------------------------
-- 提示：如果字段已存在，重复执行此段可能会报错，但能保证在任何 MySQL/MariaDB 版本中均能成功初始化。
ALTER TABLE knowledge ADD COLUMN org_node_id INT NULL COMMENT '绑定的组织节点知识库 ID（type=NORMAL）。NULL 表示不限制组织范围。' AFTER parent_id;
ALTER TABLE knowledge ADD INDEX idx_knowledge_org_node_id (org_node_id);

-- ============================================================
-- 迁移完成，请重启 bisheng-backend 容器使变更生效：
--   sudo docker restart bisheng-backend
-- ============================================================
