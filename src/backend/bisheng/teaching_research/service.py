import asyncio
import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum
from bisheng.common.errcode.channel import KnowledgeSpaceLLMNotConfiguredError
from bisheng.common.errcode.http_error import NotFoundError, UnAuthorizedError
from bisheng.knowledge.domain.knowledge_rag import KnowledgeRag
from bisheng.knowledge.domain.models.knowledge import Knowledge, KnowledgeDao
from bisheng.knowledge.domain.models.knowledge_file import KnowledgeFile, KnowledgeFileDao, KnowledgeFileStatus
from bisheng.knowledge.domain.services.knowledge_utils import KnowledgeUtils
from bisheng.llm.domain import LLMService
from bisheng.llm.domain.schemas import WorkbenchModelConfig
from bisheng.teaching_research.schemas import (
    DEFAULT_ACHIEVEMENT_FIELDS,
    AchievementExtractItem,
    AchievementExtractReq,
    AchievementExtractResp,
)


class AchievementExtractService:
    @classmethod
    async def extract(cls, login_user, req: AchievementExtractReq) -> AchievementExtractResp:
        knowledge = await cls._get_authorized_knowledge(login_user.user_name, req.knowledge_id)
        files = await cls._get_target_files(req)
        if not files:
            raise NotFoundError(msg="No successful knowledge files found for achievement extraction")

        docs_by_file = await asyncio.to_thread(cls._load_text_by_file, knowledge, files, req.max_chars_per_file)
        llm = await cls._get_extract_llm(login_user.user_id)
        fields = req.fields or DEFAULT_ACHIEVEMENT_FIELDS

        warnings: List[str] = []
        items: List[AchievementExtractItem] = []
        for file in files:
            text = docs_by_file.get(file.id, "")
            if not text.strip():
                warnings.append(f"file_id={file.id} has no extracted text")
                continue
            try:
                extracted = await asyncio.to_thread(cls._extract_one_file, llm, fields, file, text)
                if not extracted:
                    warnings.append(f"file_id={file.id} no achievement items extracted")
                items.extend(extracted)
            except Exception as exc:
                logger.exception("achievement extraction failed file_id={}", file.id)
                warnings.append(f"file_id={file.id} extract failed: {exc}")

        cls._mark_duplicates(items, req.duplicate_threshold)
        return AchievementExtractResp(
            knowledge_id=req.knowledge_id,
            total_files=len(files),
            items=items,
            warnings=warnings,
        )

    @staticmethod
    async def _get_authorized_knowledge(user_name: str, knowledge_id: int) -> Knowledge:
        knowledge_list = await KnowledgeDao.ajudge_knowledge_permission(user_name, [knowledge_id])
        if not knowledge_list:
            raise UnAuthorizedError.http_exception()
        return knowledge_list[0]

    @staticmethod
    async def _get_target_files(req: AchievementExtractReq) -> List[KnowledgeFile]:
        files = await KnowledgeFileDao.aget_file_by_filters(
            knowledge_id=req.knowledge_id,
            status=[KnowledgeFileStatus.SUCCESS.value],
            file_ids=req.file_ids,
            page=1,
            page_size=req.max_files,
        )
        return files[:req.max_files]

    @staticmethod
    async def _get_extract_llm(user_id: int):
        workbench_llm: WorkbenchModelConfig = await LLMService.get_workbench_llm()
        if not workbench_llm or not workbench_llm.knowledge_space_llm:
            raise KnowledgeSpaceLLMNotConfiguredError()
        return await LLMService.get_bisheng_llm(
            model_id=int(workbench_llm.knowledge_space_llm.id),
            temperature=0.1,
            app_id="teaching_research_achievement_extract",
            app_name="teaching_research_achievement_extract",
            app_type=ApplicationTypeEnum.KNOWLEDGE_SPACE,
            user_id=user_id,
        )

    @staticmethod
    def _load_text_by_file(knowledge: Knowledge, files: List[KnowledgeFile], max_chars_per_file: int) -> Dict[int, str]:
        file_ids = [one.id for one in files]
        file_id_set = set(file_ids)
        result = {file_id: "" for file_id in file_ids}
        es_store = KnowledgeRag.init_knowledge_es_vectorstore_sync(knowledge=knowledge)
        offset = 0
        batch_size = 500

        while True:
            body: Dict[str, Any] = {
                "from": offset,
                "size": batch_size,
                "_source": ["text", "metadata.document_id", "metadata.chunk_index"],
                "query": {"terms": {"metadata.document_id": file_ids}},
                "sort": [
                    {"metadata.document_id": "asc"},
                    {"metadata.chunk_index": "asc"},
                ],
            }
            response = es_store.client.search(index=knowledge.index_name, body=body)
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                source = hit.get("_source", {})
                metadata = source.get("metadata", {}) or {}
                document_id = metadata.get("document_id")
                try:
                    document_id = int(document_id)
                except (TypeError, ValueError):
                    continue
                if document_id not in file_id_set:
                    continue
                chunk_text = KnowledgeUtils.split_chunk_metadata(source.get("text", "") or "")
                current = result.get(document_id, "")
                if len(current) < max_chars_per_file:
                    result[document_id] = (current + "\n" + chunk_text).strip()[:max_chars_per_file]

            offset += len(hits)
            if all(len(result[file_id]) >= max_chars_per_file for file_id in file_ids):
                break

        return result

    @classmethod
    def _extract_one_file(cls, llm, fields: List[str], file: KnowledgeFile, text: str) -> List[AchievementExtractItem]:
        field_desc = "\n".join(f"- {field}" for field in fields)
        system_prompt = (
            "你是教学/科研成果信息抽取助手。只根据用户提供的文件正文抽取结构化数据。"
            "必须输出合法 JSON，不要输出 Markdown，不要解释。"
        )
        user_prompt = f"""
请从下面文件中抽取教学/科研成果项。一个文件可能包含一个或多个项目。

要求：
1. 输出 JSON 对象，格式为 {{"items": [{{...}}]}}。
2. 每个 item 尽量包含以下字段：
{field_desc}
3. 字段含义：
   - project_name: 项目名称/成果名称/课题名称
   - applicant: 申请人/负责人/主持人
   - project_level: 国家级/省级/校级/院级等
   - project_type: 教学成果、科研项目、课题、论文、专利、竞赛获奖等
   - achievement_category: 教学或科研等大类
   - organization: 所属单位/学院/部门
   - year: 年份
   - keywords: 关键词数组
   - confidence: 0 到 1 的置信度
4. 无法判断的字段填 null，keywords 无法判断填 []。

文件名：{file.file_name}
文件正文：
<document>
{text}
</document>
"""
        response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
        payload = cls._parse_llm_json(response.content)
        raw_items = cls._get_raw_items(payload)

        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            normalized = cls._normalize_item(raw)
            normalized["source_file_id"] = file.id
            normalized["source_file_name"] = file.file_name
            items.append(AchievementExtractItem(**normalized))
        return items

    @staticmethod
    def _get_raw_items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [one for one in payload if isinstance(one, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("items", "data", "result", "projects", "achievements", "成果列表", "项目列表"):
            value = payload.get(key)
            if isinstance(value, list):
                return [one for one in value if isinstance(one, dict)]
            if isinstance(value, dict):
                return [value]

        field_keys = set(DEFAULT_ACHIEVEMENT_FIELDS) | {
            "项目名称", "成果名称", "课题名称", "申请人", "负责人", "主持人", "项目级别", "成果级别",
            "项目类型", "成果类型", "成果类别", "所属单位", "学院", "年份", "关键词", "置信度"
        }
        if any(key in field_keys for key in payload.keys()):
            return [payload]
        return []

    @staticmethod
    def _parse_llm_json(content: str) -> Dict[str, Any]:
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        content = (content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?", "", content).strip()
            content = re.sub(r"```$", "", content).strip()
        try:
            return json.loads(content)
        except ValueError:
            match = re.search(r"\{.*\}", content, flags=re.S)
            if not match:
                raise
            return json.loads(match.group(0))

    @staticmethod
    def _normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
        alias_map = {
            "项目名称": "project_name",
            "成果名称": "project_name",
            "课题名称": "project_name",
            "申请人": "applicant",
            "负责人": "applicant",
            "主持人": "applicant",
            "项目级别": "project_level",
            "成果级别": "project_level",
            "项目类型": "project_type",
            "成果类型": "project_type",
            "成果类别": "achievement_category",
            "所属单位": "organization",
            "学院": "organization",
            "年份": "year",
            "关键词": "keywords",
            "置信度": "confidence",
        }
        canonical = {}
        extra = {}
        allowed = set(DEFAULT_ACHIEVEMENT_FIELDS + ["confidence"])
        for key, value in raw.items():
            target_key = alias_map.get(key, key)
            if target_key in allowed:
                canonical[target_key] = value
            else:
                extra[key] = value

        keywords = canonical.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [one.strip() for one in re.split(r"[,，;；、\s]+", keywords) if one.strip()]
        if not isinstance(keywords, list):
            keywords = []
        canonical["keywords"] = keywords
        canonical["extra"] = extra
        return canonical

    @classmethod
    def _mark_duplicates(cls, items: List[AchievementExtractItem], threshold: float):
        seen: List[Tuple[int, str, str, str]] = []
        for index, item in enumerate(items):
            name_key = cls._normalize_text(item.project_name)
            applicant_key = cls._normalize_text(item.applicant)
            fingerprint = cls._fingerprint(name_key, applicant_key, item.project_type, item.year)
            for seen_index, seen_name, seen_applicant, seen_fp in seen:
                if fingerprint == seen_fp and fingerprint:
                    item.duplicate = True
                    item.duplicate_of = seen_index
                    item.duplicate_reason = "same project/applicant fingerprint"
                    break
                if name_key and seen_name and SequenceMatcher(None, name_key, seen_name).ratio() >= threshold:
                    if not applicant_key or not seen_applicant or applicant_key == seen_applicant:
                        item.duplicate = True
                        item.duplicate_of = seen_index
                        item.duplicate_reason = "similar project name"
                        break
            seen.append((index, name_key, applicant_key, fingerprint))

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        return re.sub(r"[\s《》<>\"'“”‘’：:，,。.\-_/\\]+", "", str(value)).lower()

    @staticmethod
    def _fingerprint(*values: Any) -> str:
        text = "|".join(str(value or "").strip().lower() for value in values)
        if not text.strip("|"):
            return ""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()
