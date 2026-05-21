import sys
import os
import asyncio

# 将当前目录、父目录及祖父目录加入 Python Path，以兼容脚本被拷贝到 backend 或 backend/bisheng 运行的场景
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
sys.path.insert(0, os.path.dirname(script_dir))
sys.path.insert(0, os.path.dirname(os.path.dirname(script_dir)))

from sqlalchemy import text
from sqlmodel import select, col, delete
from bisheng.core.database import get_sync_db_session, get_async_db_session
from bisheng.user.domain.models.user import User, UserDao
from bisheng.knowledge.domain.models.knowledge import Knowledge, KnowledgeDao
from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFile, DocumentAccess, KnowledgeFileDao

def setup_db():
    print("=== 开始数据库初始化检查 ===")
    with get_sync_db_session() as session:
        # 诊断：打印所有表及 user 表的字段结构
        try:
            tables = session.execute(text("SHOW TABLES")).all()
            print(f"[Diagnostic] 数据库包含的表: {[t[0] for t in tables]}")
            
            # 兼容各种大小写形式的表名
            user_table_name = next((t[0] for t in tables if t[0].lower() == 'user'), None)
            if user_table_name:
                cols = session.execute(text(f"SHOW COLUMNS FROM {user_table_name}")).all()
                print(f"[Diagnostic] '{user_table_name}' 表的字段: {[c[0] for c in cols]}")
            else:
                print("[Diagnostic] 未找到 'user' 表！")
        except Exception as diag_e:
            print(f"[Diagnostic] 获取数据库诊断信息失败: {diag_e}")
            session.rollback()

        # Check table 'document_access'
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
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_doc_access_file_id (file_id),
                INDEX idx_doc_access_subject (subject_type, subject_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
            session.execute(text(create_table_sql))
            session.commit()
            print("[✓] 表 'document_access' 创建成功。")

        # 动态检查并追加 'knowledgefile' 表缺失的所有字段
        try:
            res = session.execute(text("SHOW COLUMNS FROM knowledgefile")).all()
            existing_cols = {col[0].lower() for col in res}
            print(f"[Diagnostic] 'knowledgefile' 表的现有字段: {existing_cols}")
            
            for col_name, col_obj in KnowledgeFile.__mapper__.columns.items():
                if col_name.lower() not in existing_cols:
                    col_type_ddl = str(col_obj.type.compile(dialect=session.bind.dialect))
                    print(f"[!] 'knowledgefile' 表缺少字段 '{col_name}'，类型为 '{col_type_ddl}'，正在自动追加...")
                    alter_sql = f"ALTER TABLE knowledgefile ADD COLUMN {col_name} {col_type_ddl} NULL;"
                    session.execute(text(alter_sql))
                    session.commit()
                    print(f"[✓] 字段 '{col_name}' 追加成功。")
        except Exception as col_e:
            print(f"[!] 自动修补 'knowledgefile' 表结构时发生异常: {col_e}")
            session.rollback()

        # 动态检查并追加 'knowledge' 表缺失的所有字段
        try:
            res = session.execute(text("SHOW COLUMNS FROM knowledge")).all()
            existing_cols = {col[0].lower() for col in res}
            print(f"[Diagnostic] 'knowledge' 表的现有字段: {existing_cols}")
            
            for col_name, col_obj in Knowledge.__mapper__.columns.items():
                if col_name.lower() not in existing_cols:
                    col_type_ddl = str(col_obj.type.compile(dialect=session.bind.dialect))
                    print(f"[!] 'knowledge' 表缺少字段 '{col_name}'，类型为 '{col_type_ddl}'，正在自动追加...")
                    alter_sql = f"ALTER TABLE knowledge ADD COLUMN {col_name} {col_type_ddl} NULL;"
                    session.execute(text(alter_sql))
                    session.commit()
                    print(f"[✓] 字段 '{col_name}' 追加成功。")
        except Exception as col_e:
            print(f"[!] 自动修补 'knowledge' 表结构时发生异常: {col_e}")
            session.rollback()
    print("=== 数据库初始化检查结束 ===\n")

async def run_tests():
    import bisheng.knowledge.domain.models.knowledge_file as kf_mod
    print(f"[Diagnostic] 载入的 'knowledge_file' 模块物理路径: {kf_mod.__file__}")
    print(f"[Diagnostic] KnowledgeFileDao 中的公开属性: {[attr for attr in dir(kf_mod.KnowledgeFileDao) if not attr.startswith('_')]}")

    setup_db()
    
    print("=== 开始权限用例测试 ===")
    
    # 准备测试数据
    with get_sync_db_session() as session:
        # 1. 清理以往残留测试数据（防污染）
        session.execute(text("DELETE FROM document_access WHERE file_id >= 99990"))
        session.execute(text("DELETE FROM knowledgefile WHERE id >= 99990"))
        session.execute(text("DELETE FROM knowledge WHERE id >= 99990"))
        session.commit()

        # 2. 准备内存中的测试用户（无需持久化到数据库中，规避字段名不兼容问题）
        user_a = User(
            user_id=99990,
            user_name="test_teacher_a",
            password="mock_password",
            org_knowledge_ids=[99995]  # 四级知识库中被授权的机构节点
        )
        user_b = User(
            user_id=99991,
            user_name="test_hr_b",
            password="mock_password",
            org_knowledge_ids=[99996]
        )

        # 3. 插入测试知识库（教务处通知库，ID 99994）
        # 并且有两个机构知识库节点，Level 1 级（比如：99995 和 99996）
        kb_main = Knowledge(id=99994, name="教务处通知库", user_id=1)
        kb_org_a = Knowledge(id=99995, name="普通学院节点", user_id=1, parent_id=99994)
        kb_org_b = Knowledge(id=99996, name="人事处节点", user_id=1, parent_id=99994)
        session.add(kb_main)
        session.add(kb_org_a)
        session.add(kb_org_b)

        # 4. 插入测试文档
        # 文档 1：普通文档，未设置任何独立权限。直接属于主知识库 99994，不继承其他
        file_normal = KnowledgeFile(
            id=99990,
            knowledge_id=99994,
            file_name="公共选课指南.pdf",
            status=2,  # SUCCESS
            user_id=1,
            file_type=1,
            org_kb_id=None
        )
        # 文档 2：人事处绩效表。设置了独立权限：仅限人事处机构节点(99996)或指定用户A(99990)访问
        file_hr_perf = KnowledgeFile(
            id=99991,
            knowledge_id=99994,
            file_name="教职工绩效核算表.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=99996 # 其机构归属为人事处节点
        )
        # 文档 3：机密科研成果。独立授权给了用户B，其他人全被屏蔽
        file_secret = KnowledgeFile(
            id=99992,
            knowledge_id=99994,
            file_name="核心机密成果.pdf",
            status=2,
            user_id=1,
            file_type=1,
            org_kb_id=None
        )
        session.add(file_normal)
        session.add(file_hr_perf)
        session.add(file_secret)
        session.commit()

        # 5. 配置文档独立权限记录
        # 文档 2 (99991)：独立授权给人事处节点 (org 99996) 和 指定用户 A (user 99990)
        access_hr_node = DocumentAccess(file_id=99991, subject_type="org", subject_id="99996")
        access_hr_user_a = DocumentAccess(file_id=99991, subject_type="user", subject_id="99990")
        # 文档 3 (99992)：仅独立授权给用户 B (user 99991)
        access_secret_user_b = DocumentAccess(file_id=99992, subject_type="user", subject_id="99991")
        
        session.add(access_hr_node)
        session.add(access_hr_user_a)
        session.add(access_secret_user_b)
        session.commit()

    print("=== 测试数据准备就绪 ===")

    # 执行鉴权判定测试
    try:
        # 直接使用内存中的 User 对象，无需从数据库中重新加载
        db_user_a = user_a
        db_user_b = user_b
            
        print("\n--- 用例 1: 用户 A（普通教师）权限校验 ---")
        print(f"用户 A ID: {db_user_a.user_id}, 被授权的机构知识库节点: {db_user_a.org_knowledge_ids}")
        # 用户 A 有权访问 99995 节点。对于 99994 知识库：
        # - 文档 1 (公共选课指南.pdf)：无独立权限。根据 4 级知识库层级规则，拥有子节点 99995 权限的用户对父节点 99994 也是放行的。
        #   所以文档 1 允许访问。
        # - 文档 2 (教职工绩效核算表.pdf)：有独立权限。检查独立权限列表，发现匹配了指定用户 "99990" (用户 A)，匹配成功，允许访问！
        # - 文档 3 (核心机密成果.pdf)：有独立权限。只对用户 B 授权，不匹配用户 A，默认拒绝！
        
        authorized_ids_a = await KnowledgeFileDao.aget_authorized_file_ids(db_user_a, 99994)
        print(f"用户 A 可访问的文档 ID 列表: {authorized_ids_a}")
        assert 99990 in authorized_ids_a, "测试失败: 用户 A 应该能访问公共文档 99990"
        assert 99991 in authorized_ids_a, "测试失败: 用户 A 应该能访问被独立授权的绩效表 99991"
        assert 99992 not in authorized_ids_a, "测试失败: 用户 A 不能访问机密文档 99992"
        print("[✓] 用例 1 测试成功！")

        print("\n--- 用例 2: 用户 B（人事处成员）权限校验 ---")
        print(f"用户 B ID: {db_user_b.user_id}, 被授权的机构知识库节点: {db_user_b.org_knowledge_ids}")
        # 用户 B 有权访问 99996 节点。对于 99994 知识库：
        # - 文档 1 (公共选课指南.pdf)：允许。
        # - 文档 2 (教职工绩效核算表.pdf)：独立授权匹配机构节点 "99996" (用户 B 拥有该节点权限)，允许访问！
        # - 文档 3 (核心机密成果.pdf)：独立授权匹配用户 B 个人 "99991"，允许访问！
        
        authorized_ids_b = await KnowledgeFileDao.aget_authorized_file_ids(db_user_b, 99994)
        print(f"用户 B 可访问的文档 ID 列表: {authorized_ids_b}")
        assert 99990 in authorized_ids_b, "测试失败: 用户 B 应该能访问公共文档 99990"
        assert 99991 in authorized_ids_b, "测试失败: 用户 B 应该能访问被独立授权给机构 99996 的绩效表 99991"
        assert 99992 in authorized_ids_b, "测试失败: 用户 B 应该能访问被独立授权给个人的机密文档 99992"
        print("[✓] 用例 2 测试成功！")

        print("\n--- 用例 3: 优先级与冲突拦截校验（修改用户 A 授权） ---")
        # 移除个人授权后，文档 2 的独立授权不再能匹配用户 A（它有独立授权但仅限 99996，而 A 只有 99995）。
        # 由于存在文档级独立权限，判定流程直接在独立权限匹配失败后拒绝，不再回退检查知识库级权限，确保完全物理防越权。
        with get_sync_db_session() as session:
            session.execute(text("DELETE FROM document_access WHERE file_id = 99991 AND subject_type = 'user' AND subject_id = '99990'"))
            session.commit()
            
        authorized_ids_a_after = await KnowledgeFileDao.aget_authorized_file_ids(db_user_a, 99994)
        print(f"移除个人授权后，用户 A 可访问 the 文档 ID 列表: {authorized_ids_a_after}")
        assert 99990 in authorized_ids_a_after, "公共文档 99990 仍应可见"
        assert 99991 not in authorized_ids_a_after, "测试失败: 移除用户独立授权后，用户 A 应该无法访问绩效表 99991（即使包含在同一个知识库下）"
        print("[✓] 用例 3 测试成功！")

    finally:
        # 清理测试数据
        print("\n--- 开始清理测试数据 ---")
        with get_sync_db_session() as session:
            session.execute(text("DELETE FROM document_access WHERE file_id >= 99990"))
            session.execute(text("DELETE FROM knowledgefile WHERE id >= 99990"))
            session.execute(text("DELETE FROM knowledge WHERE id >= 99990"))
            session.commit()
        print("[✓] 测试数据清理完成。")
        
    print("\n=================================")
    print(" 恭喜！所有二开权限判定用例全部通过！")
    print("=================================")

if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(run_tests())
    except Exception as e:
        print("\n!!! 测试运行期间捕获到未处理的异常，堆栈如下 !!!")
        traceback.print_exc()
        sys.exit(1)
