import sys
import os
import asyncio

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
sys.path.insert(0, os.path.dirname(script_dir))
sys.path.insert(0, os.path.dirname(os.path.dirname(script_dir)))

from sqlalchemy import text
from sqlmodel import select, col
from bisheng.core.database import get_sync_db_session
from bisheng.user.domain.models.user import User
from bisheng.knowledge.domain.models.knowledge import Knowledge, AuthTypeEnum
from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFile, DocumentAccess, KnowledgeFileDao
from bisheng.common.models.space_channel_member import (
    SpaceChannelMember,
    BusinessTypeEnum,
    UserRoleEnum,
    MembershipStatusEnum
)

def setup_db():
    print("=== 开始数据库初始化检查 ===")
    with get_sync_db_session() as session:
        try:
            tables = session.execute(text("SHOW TABLES")).all()
            print(f"[诊断] 数据库包含的表: {[t[0] for t in tables]}")
        except Exception as diag_e:
            print(f"[诊断] 获取数据库信息失败: {diag_e}")
            session.rollback()

        try:
            session.execute(text("SELECT 1 FROM document_access LIMIT 1"))
            print("[✓] 表 'document_access' 已存在。")
        except Exception:
            session.rollback()
            print("[!] 创建表 'document_access'...")
            create_table_sql = """
            CREATE TABLE document_access (
                id INT AUTO_INCREMENT PRIMARY KEY,
                file_id INT NOT NULL,
                subject_type VARCHAR(50) NOT NULL,
                subject_id VARCHAR(100) NOT NULL,
                permission_type VARCHAR(20) NOT NULL DEFAULT 'read',
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_doc_access_file_id (file_id),
                INDEX idx_doc_access_subject (subject_type, subject_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
            session.execute(text(create_table_sql))
            session.commit()
            print("[✓] 表 'document_access' 创建成功。")
    print("=== 数据库初始化检查结束 ===\n")

async def run_tests():
    setup_db()
    print("=== 开始二开权限判定用例测试 ===")
    
    with get_sync_db_session() as session:
        session.execute(text("DELETE FROM document_access WHERE file_id >= 99990"))
        session.execute(text("DELETE FROM knowledgefile WHERE id >= 99990"))
        session.execute(text("DELETE FROM space_channel_member WHERE user_id >= 99990"))
        session.execute(text("DELETE FROM knowledge WHERE id >= 99990"))
        session.commit()

        user_a = User(
            user_id=99990,
            user_name="测试用户A",
            password="mock_password",
            org_knowledge_ids=[99995]
        )
        user_b = User(
            user_id=99991,
            user_name="测试用户B",
            password="mock_password",
            org_knowledge_ids=[99996]
        )
        user_c = User(
            user_id=99992,
            user_name="测试用户C",
            password="mock_password",
            org_knowledge_ids=[]
        )

        kb_main = Knowledge(id=99994, name="教务处通知库", user_id=1, auth_type=AuthTypeEnum.PUBLIC)
        kb_org_a = Knowledge(id=99995, name="普通学院节点", user_id=1, parent_id=99994)
        kb_org_b = Knowledge(id=99996, name="人事处节点", user_id=1, parent_id=99994)
        session.add(kb_main)
        session.add(kb_org_a)
        session.add(kb_org_b)

        file_normal = KnowledgeFile(
            id=99990,
            knowledge_id=99994,
            file_name="公共选课指南.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=None,
            is_private=False
        )
        file_hr_perf = KnowledgeFile(
            id=99991,
            knowledge_id=99994,
            file_name="教职工绩效核算表.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=99996,
            is_private=True
        )
        file_secret = KnowledgeFile(
            id=99992,
            knowledge_id=99994,
            file_name="核心机密成果.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=None,
            is_private=True
        )
        file_everyone = KnowledgeFile(
            id=99993,
            knowledge_id=99994,
            file_name="全校共享手册.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=None,
            is_private=True
        )
        session.add(file_normal)
        session.add(file_hr_perf)
        session.add(file_secret)
        session.add(file_everyone)

        member_c = SpaceChannelMember(
            business_id="99994",
            business_type=BusinessTypeEnum.SPACE,
            user_id=99992,
            user_role=UserRoleEnum.MEMBER,
            status=MembershipStatusEnum.ACTIVE
        )
        session.add(member_c)
        session.commit()

        access_org = DocumentAccess(file_id=99991, subject_type="org", subject_id="99996", permission_type="read")
        access_user_a = DocumentAccess(file_id=99991, subject_type="user", subject_id="99990", permission_type="write")
        access_user_b = DocumentAccess(file_id=99992, subject_type="user", subject_id="99991", permission_type="admin")
        access_all = DocumentAccess(file_id=99993, subject_type="all", subject_id="*", permission_type="read")
        
        session.add(access_org)
        session.add(access_user_a)
        session.add(access_user_b)
        session.add(access_all)
        session.commit()

    print("=== 测试数据准备就绪 ===")

    try:
        print("\n--- 用例 1: 所有人（all）权限过滤与判定测试 ---")
        authorized_ids_a = await KnowledgeFileDao.aget_authorized_file_ids(user_a, 99994)
        authorized_ids_b = await KnowledgeFileDao.aget_authorized_file_ids(user_b, 99994)
        authorized_ids_c = await KnowledgeFileDao.aget_authorized_file_ids(user_c, 99994)
        
        print(f"用户 A 可访问文件列表: {authorized_ids_a}")
        print(f"用户 B 可访问文件列表: {authorized_ids_b}")
        print(f"用户 C 可访问文件列表: {authorized_ids_c}")
        
        assert 99993 in authorized_ids_a, "测试失败: 用户 A 应能看到所有人可见文档"
        assert 99993 in authorized_ids_b, "测试失败: 用户 B 应能看到所有人可见文档"
        assert 99993 in authorized_ids_c, "测试失败: 用户 C 应能看到所有人可见文档"
        
        perm_a_f4 = await KnowledgeFileDao.aget_user_file_permission(user_a, 99993)
        perm_b_f4 = await KnowledgeFileDao.aget_user_file_permission(user_b, 99993)
        perm_c_f4 = await KnowledgeFileDao.aget_user_file_permission(user_c, 99993)
        print(f"用户 A 对文档 4 的计算权限: {perm_a_f4}")
        print(f"用户 B 对文档 4 的计算权限: {perm_b_f4}")
        print(f"用户 C 对文档 4 的计算权限: {perm_c_f4}")
        assert perm_a_f4 == 'read'
        assert perm_b_f4 == 'read'
        assert perm_c_f4 == 'read'
        print("[✓] 用例 1 测试成功！")

        print("\n--- 用例 2: 用户 C（未分配组织/未接等级）默认文档可见性测试 ---")
        assert 99990 in authorized_ids_c, "测试失败: 未分配组织的用户 C 应该能看到空间默认文档 1（通过空间级鉴权放行）"
        perm_c_f1 = await KnowledgeFileDao.aget_user_file_permission(user_c, 99990)
        print(f"用户 C 对文档 1 的计算权限: {perm_c_f1}")
        assert perm_c_f1 == 'read'
        print("[✓] 用例 2 测试成功！")

        print("\n--- 用例 3: 文档独立授权 (D-01) 与 操作权限类型 (D-02) 判定 ---")
        perm_a_f2 = await KnowledgeFileDao.aget_user_file_permission(user_a, 99991)
        print(f"用户 A 对文档 2 的计算权限: {perm_a_f2} (期望值为 'write')")
        assert perm_a_f2 == 'write'
        
        perm_b_f3 = await KnowledgeFileDao.aget_user_file_permission(user_b, 99992)
        print(f"用户 B 对文档 3 的计算权限: {perm_b_f3} (期望值为 'admin')")
        assert perm_b_f3 == 'admin'
        print("[✓] 用例 3 测试成功！")

        print("\n--- 用例 4: 私有文档 (D-03) 与 优先级拦截 (D-04) 校验 ---")
        assert 99992 not in authorized_ids_a, "测试失败: 用户 A 不应该能看到机密文档 3"
        perm_a_f3 = await KnowledgeFileDao.aget_user_file_permission(user_a, 99992)
        print(f"用户 A 对私有文档 3 的计算权限: {perm_a_f3} (期望值为 None)")
        assert perm_a_f3 is None
        print("[✓] 用例 4 测试成功！")

    finally:
        print("\n--- 开始清理测试数据 ---")
        with get_sync_db_session() as session:
            session.execute(text("DELETE FROM document_access WHERE file_id >= 99990"))
            session.execute(text("DELETE FROM knowledgefile WHERE id >= 99990"))
            session.execute(text("DELETE FROM space_channel_member WHERE user_id >= 99990"))
            session.execute(text("DELETE FROM knowledge WHERE id >= 99990"))
            session.commit()
        print("[✓] 测试数据清理完成。")
        
    print("\n==================================================")
    print(" 恭喜！所有二开权限判定用例全部通过！")
    print("==================================================")

if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(run_tests())
    except Exception as e:
        print("\n!!! 测试运行期间捕获到未处理的异常，堆栈如下 !!!")
        traceback.print_exc()
        sys.exit(1)
