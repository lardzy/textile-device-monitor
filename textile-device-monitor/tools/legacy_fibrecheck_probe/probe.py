from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CONFIG_FILENAME = "Toone.FibreCheck.Entites.dll.config"
PROFILE_NAME = "FibreCheckEntities"
SAMPLE_NO_PATTERN = re.compile(r"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$")
DATA_SOURCE_OVERRIDE_PATTERN = re.compile(
    r"^[A-Za-z0-9._-]+(?::[0-9]{1,5})?/[A-Za-z0-9._$#-]+$",
)
ORACLE_ERROR_PATTERN = re.compile(r"\b(?:ORA|DPY|DPI)-\d{3,5}\b", re.IGNORECASE)
WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|GRANT|REVOKE|"
    r"COMMIT|ROLLBACK|CALL|EXECUTE|BEGIN|DECLARE)\b",
    re.IGNORECASE,
)
PATH_COLUMN_NAMES = {
    "ATTACHINFO",
    "DOCUMENTNAME",
    "FILEPATH",
    "FILENAME",
    "ORIGINALDATAFILENAME",
    "TEMPLATEFILENAME",
    "REPORTNAME",
}
SENSITIVE_ID_COLUMN_NAMES = {"DOCUMENTUPLOADINDEX"}
ID_COLUMN_PATTERN = re.compile(
    r"(?:^ID$|ID$|USER\d*$)",
    re.IGNORECASE,
)
LOGIN_COLUMN_NAMES = {"LOGINNAME"}
MAPPING_CONFIG_QUERY_KEY = "original_key_data_mapping_configs"
ORACLE_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_$#]{0,29}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAPPING_CONFIG_RAW_COLUMNS = (
    "TaskCheckItemID",
    "CheckItemID",
    "DocumentID",
    "MappingCount",
    "DataTableName",
    "MappedTableExists",
    "ConfigPresent",
    "SeqNum",
    "KeyDataField",
    "KeyDataType",
    "ConfigValue",
    "ConfigValue_En",
    "ConfigValue_CnEn",
    "ConfigValue_NewCnEn",
)
MAPPING_CONFIG_CANONICAL_FIELDS = (
    "SeqNum",
    "KeyDataField",
    "KeyDataType",
    "ConfigValue",
    "ConfigValue_En",
    "ConfigValue_CnEn",
    "ConfigValue_NewCnEn",
)
MAPPING_CONFIG_FAILURE_REASONS = frozenset(
    {
        "mapping_count_invalid",
        "mapping_missing",
        "mapping_not_unique",
        "mapping_table_name_invalid",
        "mapped_table_state_invalid",
        "config_presence_invalid",
        "config_presence_inconsistent",
        "config_missing",
        "config_value_invalid",
        "duplicate_config_sort_key",
        "mapping_fingerprint_missing",
        "mapping_fingerprint_not_unique",
        "mapping_fingerprint_invalid",
        "mapping_query_incomplete",
    }
)


@dataclass(frozen=True)
class OracleProfile:
    name: str
    source_file: str
    provider: str
    data_source: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)

    def public_metadata(self) -> dict[str, str]:
        return {
            "name": self.name,
            "source_file": self.source_file,
            "provider": "oracle",
            "endpoint_fingerprint": digest_text(normalize_data_source(self.data_source)),
        }


@dataclass(frozen=True)
class QueryDefinition:
    key: str
    purpose: str
    sql: str
    parameter_names: tuple[str, ...] = ("sample_no",)

    def manifest_entry(self) -> dict[str, Any]:
        assert_read_only_sql(self.sql)
        return {
            "key": self.key,
            "purpose": self.purpose,
            "parameters": list(self.parameter_names),
            "sql": compact_sql(self.sql),
            "sql_sha256": hashlib.sha256(compact_sql(self.sql).encode("utf-8")).hexdigest(),
        }


READ_ONLY_TRANSACTION_SQL = "SET TRANSACTION READ ONLY"

QUERIES: tuple[QueryDefinition, ...] = (
    QueryDefinition(
        key="special_wool_exact",
        purpose="核验完全一致的特纤管理主记录",
        sql="""
            SELECT
                sw."ID", sw."SampleNo", sw."FibreSort", sw."SubSort", sw."CheckWay",
                sw."Content", sw."CheckTache1", sw."CheckTache2", sw."CheckTache3",
                sw."CheckTache4", sw."CheckTache5", sw."CheckUser1", sw."CheckUser2",
                sw."CheckUser3", sw."CheckUser4", sw."CheckUser5", sw."CreateUser",
                sw."CreateTime", sw."AuditUser", sw."AuditTime", sw."ReviewUser",
                sw."ReviewTime", sw."FilePath", sw."FileType",
                sw."CheckRecordRegisterID", sw."IsImmediacyPublish",
                sw."CheckUserItem1", sw."CheckUserItem2", sw."CheckUserItem3",
                sw."CheckUserItem4", sw."CheckUserItem5", sw."CheckUserNumber1",
                sw."CheckUserNumber2", sw."CheckUserNumber3", sw."CheckUserNumber4",
                sw."CheckUserNumber5", sw."ReviewUser1", sw."ReviewUser2",
                sw."ReviewUser3", sw."ReviewUser4", sw."ReviewUser5",
                sw."ReviewUserItem1", sw."ReviewUserItem2", sw."ReviewUserItem3",
                sw."ReviewUserItem4", sw."ReviewUserItem5", sw."ReviewUserNumber1",
                sw."ReviewUserNumber2", sw."ReviewUserNumber3", sw."ReviewUserNumber4",
                sw."ReviewUserNumber5"
            FROM "SpecialWoolManage" sw
            WHERE sw."SampleNo" = :sample_no
            ORDER BY sw."CreateTime" DESC
        """,
    ),
    QueryDefinition(
        key="special_wool_prefix",
        purpose="核验同编号前缀下是否存在追加版本或冲突记录",
        sql="""
            SELECT
                sw."ID", sw."SampleNo", sw."FibreSort", sw."SubSort", sw."CheckWay",
                sw."CreateTime", sw."FilePath", sw."FileType",
                sw."CheckRecordRegisterID", sw."IsImmediacyPublish"
            FROM "SpecialWoolManage" sw
            WHERE sw."SampleNo" LIKE :sample_prefix ESCAPE '\\'
            ORDER BY sw."SampleNo", sw."CreateTime" DESC
        """,
        parameter_names=("sample_prefix",),
    ),
    QueryDefinition(
        key="user_mapping",
        purpose="解析主记录引用的检验员、复核员、创建人和审核人",
        sql="""
            SELECT DISTINCT u."ID", u."LoginName", u."ChineseName", u."IsDeleted"
            FROM "User" u
            WHERE EXISTS (
                SELECT 1
                FROM "SpecialWoolManage" sw
                WHERE sw."SampleNo" = :sample_no
                  AND (
                    u."ID" = sw."CheckUser1" OR u."ID" = sw."CheckUser2"
                    OR u."ID" = sw."CheckUser3" OR u."ID" = sw."CheckUser4"
                    OR u."ID" = sw."CheckUser5" OR u."ID" = sw."ReviewUser1"
                    OR u."ID" = sw."ReviewUser2" OR u."ID" = sw."ReviewUser3"
                    OR u."ID" = sw."ReviewUser4" OR u."ID" = sw."ReviewUser5"
                    OR u."ID" = sw."CreateUser" OR u."ID" = sw."AuditUser"
                    OR u."ID" = sw."ReviewUser"
                  )
            )
            ORDER BY u."ChineseName"
        """,
    ),
    QueryDefinition(
        key="quantification_tests",
        purpose="核验定量试验主结果",
        sql="""
            SELECT
                qt."ID", qt."CheckRecordRegisterID", qt."SampleNo", qt."SampleName",
                qt."CheckItem", qt."SampleDescription", qt."Unit", qt."JudgeBasis",
                qt."Remark", qt."CheckUser", qt."ReviewUser", qt."AuditUser",
                qt."TestMethod", qt."SpecialWoolManageID", qt."FileName",
                qt."Decision", qt."Grade"
            FROM "QuantificationTest" qt
            WHERE qt."SampleNo" = :sample_no
            ORDER BY qt."ID"
        """,
    ),
    QueryDefinition(
        key="quantification_test_details",
        purpose="核验定量试验明细结果",
        sql="""
            SELECT
                d."ID", d."QuantificationTestID", d."TestMethod", d."Location",
                d."StandardValue", d."RealValue", d."JudgeBasis", d."SeqNum", d."Unit"
            FROM "QuantificationTest_Detail" d
            JOIN "QuantificationTest" qt ON qt."ID" = d."QuantificationTestID"
            WHERE qt."SampleNo" = :sample_no
            ORDER BY d."QuantificationTestID", d."SeqNum", d."ID"
        """,
    ),
    QueryDefinition(
        key="currency_item_records",
        purpose="核验通用项目记录登记主记录",
        sql="""
            SELECT
                cir."ID", cir."CheckRecordRegisterID", cir."SampleNo",
                cir."Grade", cir."JudgeBasis", cir."CheckItemID",
                cir."CheckItemName", cir."SampleDescription", cir."TestMethod",
                cir."StandardType", cir."Unit", cir."ReportCheckItemName",
                cir."AttachInfo", cir."Remark", cir."TotalJudge", cir."CheckUser"
            FROM "CurrencyItemRecordNew" cir
            WHERE cir."SampleNo" = :sample_no
            ORDER BY cir."ID"
        """,
    ),
    QueryDefinition(
        key="currency_item_record_details",
        purpose="核验通用项目记录登记的标准值、允差及实测值明细",
        sql="""
            SELECT
                d."ID", d."CurrencyItemRecordNewID", d."StandardLocation",
                d."StandardValue", d."RealLocation", d."RealValue", d."SeqNum"
            FROM "CurrencyItemRecordNewDetail" d
            JOIN "CurrencyItemRecordNew" cir
              ON cir."ID" = d."CurrencyItemRecordNewID"
            WHERE cir."SampleNo" = :sample_no
            ORDER BY d."CurrencyItemRecordNewID", d."SeqNum", d."ID"
        """,
    ),
    QueryDefinition(
        key="currency_excel_records",
        purpose="诊断相似旧链 CurrExcelOriRecord；不作为最终录入事实源",
        sql="""
            SELECT
                cer."ID", cer."ReportNo", cer."CheckItemID", cer."CheckItemName",
                cer."DepartmentID", cer."Department", cer."PositionID",
                cer."Position", cer."CheckUser", cer."ReviewUser", cer."AuditUser",
                cer."CheckDate", cer."FilePath", cer."FileName", cer."CopyRegion",
                cer."SheetName", cer."Remark", cer."ReportCheckItem",
                cer."OriginalDataFilename", cer."Judgement"
            FROM "CurrExcelOriRecord" cer
            WHERE cer."ReportNo" = :sample_no
            ORDER BY cer."CheckItemID", cer."FileName", cer."SheetName", cer."ID"
        """,
    ),
    QueryDefinition(
        key="original_key_data_list",
        purpose="核验 Excel 原始记录的列表型关键数据",
        sql="""
            SELECT
                okdl."SampleNo", okdl."CheckItemID", okdl."ExcelTemplateName",
                okdl."KeyDataField", okdl."SeqNum", okdl."OriginalRecordID",
                okdl."Column1", okdl."Column2", okdl."Column3", okdl."Column4",
                okdl."Column5", okdl."Column6", okdl."Column7", okdl."Column8",
                okdl."Column9", okdl."Column10", okdl."Column11", okdl."Column12",
                okdl."Column13", okdl."Column14", okdl."Column15", okdl."Column16",
                okdl."Column17", okdl."Column18", okdl."Column19", okdl."Column20"
            FROM "OriginalKeyData_List" okdl
            WHERE okdl."SampleNo" = :sample_no
            ORDER BY okdl."CheckItemID", okdl."ExcelTemplateName",
                     okdl."SeqNum", okdl."OriginalRecordID"
        """,
    ),
    QueryDefinition(
        key="original_key_data_other",
        purpose="核验以原始记录 ID 关联的其它 Excel 关键数据",
        sql="""
            SELECT
                okdo."ID", okdo."SampleNo", okdo."ExcelTemplateName",
                okdo."OriginalRecordID", okdo."CheckItemNo",
                okdo."OriginalData", okdo."DataType"
            FROM "OriginalKeyData_Other" okdo
            WHERE okdo."SampleNo" = :sample_no
            ORDER BY okdo."CheckItemNo", okdo."ExcelTemplateName",
                     okdo."OriginalRecordID", okdo."ID"
        """,
    ),
    QueryDefinition(
        key="check_record_register",
        purpose="核验检验记录登记及其原始文件引用",
        sql="""
            SELECT
                crr."ID", crr."SampleNo", crr."CheckItemID", crr."PositionID",
                crr."TemplateFilename", crr."OriginalDataFilename", crr."Level",
                crr."CreateUser", crr."CreateTime", crr."ReviewUser", crr."ReviewTime",
                crr."AuditUser", crr."AuditTime", crr."LastUpdateTime",
                crr."LastUpdateUser", crr."OriginalRecordID", crr."SampleIdentity",
                crr."CheckUser", crr."EquipmentNo", crr."ProofUser", crr."ProofTime",
                crr."CheckBasis", crr."OriginalPictureID"
            FROM "CheckRecordRegister" crr
            WHERE crr."SampleNo" = :sample_no
            ORDER BY crr."CreateTime", crr."ID"
        """,
    ),
    QueryDefinition(
        key="original_key_data",
        purpose="核验原始关键数据项目",
        sql="""
            SELECT
                okd."SampleNo", okd."CheckItemID", okd."ExcelTemplateName",
                okd."ConfigGroupKey", okd."OriginalRecordID", okd."SeqNum",
                okd."CheckItemName", okd."MeasureUnit", okd."SampleIdentity",
                okd."CheckMethod", okd."StandardValue", okd."CheckResult",
                okd."Judgement", okd."Remark", okd."JudgeBasis", okd."IsSubCheckItem",
                okd."CheckResult2", okd."IsShowAfterDetail", okd."IsShowBeforeDetail",
                okd."Grade", okd."TestLocation", okd."CheckResult3", okd."CheckMethod1"
            FROM "OriginalKeyData_CheckItem" okd
            WHERE okd."SampleNo" = :sample_no
            ORDER BY okd."SeqNum", okd."CheckItemID"
        """,
    ),
    QueryDefinition(
        key="tasks",
        purpose="核验委托任务主记录",
        sql="""
            SELECT
                t."ID", t."ReportNo", t."CheckProperty", t."SampleReceiveTime",
                t."Status", t."CheckBasis", t."CreateTime", t."TaskAssignUser",
                t."TaskAssignTime", t."IsAddVersion", t."SourceRptNo",
                t."ReportName", t."CheckUserName"
            FROM "Task" t
            WHERE t."ReportNo" = :sample_no
            ORDER BY t."CreateTime", t."ID"
        """,
    ),
    QueryDefinition(
        key="task_samples",
        purpose="读取委托任务的样品名称",
        sql="""
            SELECT
                s."TaskID", s."SampleName"
            FROM "Task_Sample" s
            JOIN "Task" t ON t."ID" = s."TaskID"
            WHERE t."ReportNo" = :sample_no
            ORDER BY s."SampleName"
        """,
    ),
    QueryDefinition(
        key="task_check_items",
        purpose="核验委托任务中的检测项目",
        sql="""
            SELECT
                ci."ID", ci."TaskID", ci."CheckItemID", ci."CheckItemNo",
                ci."CheckItemName", ci."CheckMethod", ci."Remark", ci."GiveJudgement",
                ci."SampleIdentify", ci."CheckCount", ci."SeqNum",
                ci."CheckItemCategory", ci."CheckItemEngName"
            FROM "Task_CheckItem" ci
            JOIN "Task" t ON t."ID" = ci."TaskID"
            WHERE t."ReportNo" = :sample_no
            ORDER BY ci."SeqNum", ci."ID"
        """,
    ),
    QueryDefinition(
        key="task_entry_routes",
        purpose="按任务项目读取 CheckItem 配置的实际原始记录入口类",
        sql="""
            SELECT
                ci."ID" AS "TaskCheckItemID", ci."TaskID", ci."CheckItemID",
                item."No" AS "CatalogCheckItemNo",
                item."ItemName" AS "CatalogCheckItemName",
                item."OriginalDataInputUIClassName", item."PositionID"
            FROM "Task_CheckItem" ci
            JOIN "Task" t ON t."ID" = ci."TaskID"
            JOIN "CheckItem" item ON item."ID" = ci."CheckItemID"
            WHERE t."ReportNo" = :sample_no
            ORDER BY ci."SeqNum", ci."ID"
        """,
    ),
    QueryDefinition(
        key="check_record_templates",
        purpose="按 CheckItem 的 StandardDocument 关系读取实际原始记录模板",
        sql="""
            SELECT
                ci."ID" AS "TaskCheckItemID", ci."CheckItemID",
                d."ID" AS "DocumentID", d."DocumentName",
                d."DocumentUploadTime", d."DocumentUploadIndex"
            FROM "Task_CheckItem" ci
            JOIN "Task" t ON t."ID" = ci."TaskID"
            JOIN "CheckItem" item ON item."ID" = ci."CheckItemID"
            JOIN "StandardDocument" sd ON sd."StandardID" = item."ID"
            JOIN "Document" d ON d."ID" = sd."DocumentID"
            WHERE t."ReportNo" = :sample_no
            ORDER BY ci."SeqNum", d."DocumentName", d."ID"
        """,
    ),
    QueryDefinition(
        key=MAPPING_CONFIG_QUERY_KEY,
        purpose="为每个已配置原始记录模板生成与 writer 一致的映射配置指纹",
        sql="""
            SELECT
                ci."ID" AS "TaskCheckItemID",
                ci."CheckItemID" AS "CheckItemID",
                d."ID" AS "DocumentID",
                (
                    SELECT COUNT(*)
                    FROM "OriginalKeyDataTableMapping" mx
                    WHERE mx."CheckItemID" = ci."CheckItemID"
                      AND mx."ExcelTemplateName" = d."DocumentName"
                ) AS "MappingCount",
                m."DataTableName" AS "DataTableName",
                CASE WHEN ut."TABLE_NAME" IS NULL THEN 0 ELSE 1 END
                    AS "MappedTableExists",
                CASE WHEN c."ID" IS NULL THEN 0 ELSE 1 END AS "ConfigPresent",
                c."SeqNum" AS "SeqNum",
                c."KeyDataField" AS "KeyDataField",
                c."KeyDataType" AS "KeyDataType",
                c."ConfigValue" AS "ConfigValue",
                c."ConfigValue_En" AS "ConfigValue_En",
                c."ConfigValue_CnEn" AS "ConfigValue_CnEn",
                c."ConfigValue_NewCnEn" AS "ConfigValue_NewCnEn"
            FROM "Task_CheckItem" ci
            JOIN "Task" t ON t."ID" = ci."TaskID"
            JOIN "StandardDocument" sd ON sd."StandardID" = ci."CheckItemID"
            JOIN "Document" d ON d."ID" = sd."DocumentID"
            LEFT JOIN "OriginalKeyDataTableMapping" m
              ON m."CheckItemID" = ci."CheckItemID"
             AND m."ExcelTemplateName" = d."DocumentName"
            LEFT JOIN USER_TABLES ut ON ut."TABLE_NAME" = m."DataTableName"
            LEFT JOIN "OriginalKeyDataConfig" c
              ON c."CheckItemTable" = m."DataTableName"
            WHERE t."ReportNo" = :sample_no
            ORDER BY ci."SeqNum", d."DocumentName", d."ID",
                     m."DataTableName", c."SeqNum", c."KeyDataField"
        """,
    ),
)

# 高频推荐刷新只需要任务主表、样品名称和任务项目。保持 QUERIES 及
# 默认探针模式完全不变；仅当 CLI 显式传入
# --task-snapshot-only 时采用这个严格白名单。
TASK_SNAPSHOT_QUERY_KEYS = ("tasks", "task_samples", "task_check_items")
TASK_SNAPSHOT_QUERIES: tuple[QueryDefinition, ...] = tuple(
    query for query in QUERIES if query.key in TASK_SNAPSHOT_QUERY_KEYS
)
if tuple(query.key for query in TASK_SNAPSHOT_QUERIES) != TASK_SNAPSHOT_QUERY_KEYS:
    raise RuntimeError("任务快照查询定义缺失或顺序错误")

# 图片类特种毛上传/复核的独立只读事实集。它不会加入默认探针范围，只有
# 显式 --special-wool-dry-run 才执行，且仍受 SELECT-only 检查、只读事务和
# 最终 rollback 约束。
SPECIAL_WOOL_DRY_RUN_QUERIES: tuple[QueryDefinition, ...] = (
    QueryDefinition(
        key="special_wool_number_family",
        purpose="读取基础编号及数字后缀的远端占用事实",
        sql="""
            SELECT sw."SampleNo", COUNT(*) AS "RecordCount",
                   MIN(sw."CreateTime") AS "FirstCreateTime",
                   MAX(sw."CreateTime") AS "LastCreateTime"
            FROM "SpecialWoolManage" sw
            WHERE sw."SampleNo" = :target_base
               OR sw."SampleNo" LIKE :target_suffix_prefix ESCAPE '\\'
            GROUP BY sw."SampleNo"
            ORDER BY sw."SampleNo"
        """,
        parameter_names=("target_base", "target_suffix_prefix"),
    ),
    QueryDefinition(
        key="special_wool_task_project",
        purpose="精确读取微观形貌任务项目及 CheckItem 目录事实",
        sql="""
            SELECT t."ID" AS "TaskID", t."ReportNo", t."Status",
                   t."IsAddVersion",
                   tci."ID" AS "TaskCheckItemID", tci."CheckItemID",
                   tci."CheckItemNo", tci."CheckItemName",
                   tci."CheckMethod", tci."SeqNum", tci."Remark",
                   tci."SampleIdentify", tci."CheckCount",
                   tci."GiveJudgement",
                   ci."No" AS "CatalogCheckItemNo",
                   ci."ItemName" AS "CatalogCheckItemName",
                   ci."OriginalDataInputUIClassName"
            FROM "Task" t
            JOIN "Task_CheckItem" tci ON tci."TaskID" = t."ID"
            LEFT JOIN "CheckItem" ci ON ci."ID" = tci."CheckItemID"
            WHERE t."ReportNo" = :sample_no
            ORDER BY tci."SeqNum", tci."ID"
        """,
    ),
    QueryDefinition(
        key="special_wool_picture_records",
        purpose="读取目标特种毛主记录与 OriginalDataPictureFile 子记录",
        sql="""
            SELECT sw."ID" AS "MainID", sw."SampleNo", sw."FibreSort",
                   sw."CheckWay", sw."CheckUser1", sw."CheckUserItem1",
                   sw."CheckUserNumber1", sw."ReviewUserNumber1",
                   sw."FilePath", sw."FileType",
                   sw."CreateUser" AS "MainCreateUser",
                   sw."CreateTime" AS "MainCreateTime",
                   sw."ReviewUser", sw."ReviewTime", sw."AuditUser",
                   sw."AuditTime", p."ID" AS "PictureID",
                   p."SampleNo" AS "PictureSampleNo", p."CheckItemID",
                   p."PictureFileName", p."OriginalDataFileName",
                   p."CreateUser" AS "PictureCreateUser",
                   p."CreateTime" AS "PictureCreateTime",
                   p."SpecialWoolManageID", p."TestMethod", p."Judgement",
                   p."IsAloneShow", p."SampleIdentify", p."JudgeBasis",
                   p."PictureDesc", ci."No" AS "CatalogCheckItemNo",
                   ci."ItemName" AS "CatalogCheckItemName",
                   ci."OriginalDataInputUIClassName"
            FROM "SpecialWoolManage" sw
            LEFT JOIN "OriginalDataPictureFile" p
              ON p."SpecialWoolManageID" = sw."ID"
            LEFT JOIN "CheckItem" ci ON ci."ID" = p."CheckItemID"
            WHERE sw."SampleNo" = :target_sample_no
            ORDER BY sw."CreateTime" DESC, p."CreateTime", p."ID"
        """,
        parameter_names=("target_sample_no",),
    ),
    QueryDefinition(
        key="special_wool_sample_number_unique",
        purpose="确认 SampleNo 是否存在单列唯一索引或唯一约束",
        sql="""
            SELECT ui."INDEX_NAME" AS "IndexName",
                   ui."UNIQUENESS" AS "Uniqueness",
                   COUNT(*) AS "ColumnCount",
                   MIN(uic."COLUMN_NAME") AS "ColumnName"
            FROM USER_INDEXES ui
            JOIN USER_IND_COLUMNS uic
              ON uic."INDEX_NAME" = ui."INDEX_NAME"
            WHERE ui."TABLE_NAME" = 'SpecialWoolManage'
            GROUP BY ui."INDEX_NAME", ui."UNIQUENESS"
            HAVING COUNT(*) = 1
               AND MIN(uic."COLUMN_NAME") = 'SampleNo'
            ORDER BY ui."INDEX_NAME"
        """,
        parameter_names=(),
    ),
    QueryDefinition(
        key="special_wool_server_time",
        purpose="记录 Oracle 服务器观察时间",
        sql='SELECT SYSDATE AS "ServerTime" FROM DUAL',
        parameter_names=(),
    ),
)

FINAL_ENTRY_REQUIRED_QUERY_KEYS = (
    "task_check_items",
    "task_entry_routes",
    "check_record_templates",
    MAPPING_CONFIG_QUERY_KEY,
    "currency_item_records",
    "currency_item_record_details",
    "check_record_register",
    "original_key_data",
    "original_key_data_list",
    "original_key_data_other",
)
FINAL_ENTRY_EXCLUDED_INFERENCE_SOURCES = (
    "special_wool_prefix",
    "quantification_tests",
    "quantification_test_details",
    "currency_excel_records",
)
COMMON_DETAIL_TABLE_COLUMNS = (
    "standard_location",
    "standard_value",
    "real_location",
    "real_value",
)


class ProbeError(RuntimeError):
    """An error safe to show without exposing connection secrets."""


def digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def compact_sql(sql: str) -> str:
    return " ".join(sql.split())


def assert_read_only_sql(sql: str) -> None:
    compact = compact_sql(sql)
    if ";" in compact:
        raise ProbeError("SQL 中不允许出现分号。")
    if WRITE_KEYWORD_PATTERN.search(compact):
        raise ProbeError("检测到非只读 SQL 关键字。")
    upper = compact.upper()
    if upper == READ_ONLY_TRANSACTION_SQL:
        return
    if not upper.startswith("SELECT "):
        raise ProbeError("只允许 SELECT 或只读事务声明。")


def validate_sample_no(value: str) -> str:
    sample_no = value
    if not SAMPLE_NO_PATTERN.fullmatch(sample_no):
        raise ProbeError(
            "样品编号格式无效：只允许 9–20 位大写字母或数字，以及可选的单个短横线后缀。"
        )
    return sample_no


def split_semicolon_kv(text: str) -> dict[str, str]:
    parts: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    for char in text:
        if char in {"'", '"'}:
            if quote_char == char:
                quote_char = None
            elif quote_char is None:
                quote_char = char
        if char == ";" and quote_char is None:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)

    values: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        values[key.strip().lower()] = raw_value.strip().strip("'\"")
    return values


def normalize_data_source(data_source: str) -> str:
    return data_source.strip().rstrip("/")


def validate_data_source_override(value: str) -> str:
    candidate = normalize_data_source(value)
    if not DATA_SOURCE_OVERRIDE_PATTERN.fullmatch(candidate):
        raise ProbeError("--data-source 仅支持 host[:port]/service 形式的 Easy Connect 地址。")
    return candidate


def load_primary_profile(fibrecheck_dir: Path) -> OracleProfile:
    config_dir = fibrecheck_dir.expanduser()
    config_path = config_dir / CONFIG_FILENAME
    if not config_path.is_file():
        raise ProbeError(f"找不到主配置文件 {CONFIG_FILENAME}。")
    if config_path.is_symlink():
        raise ProbeError("主配置文件不能是符号链接。")
    if config_path.stat().st_size > 2 * 1024 * 1024:
        raise ProbeError("主配置文件异常过大。")

    try:
        root = ET.parse(config_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ProbeError("主配置文件无法安全解析。") from exc

    selected: ET.Element | None = None
    for item in root.findall(".//connectionStrings/add"):
        if item.attrib.get("name") == PROFILE_NAME:
            selected = item
            break
    if selected is None:
        raise ProbeError(f"主配置中不存在 {PROFILE_NAME}。")

    outer = split_semicolon_kv(html.unescape(selected.attrib.get("connectionString", "")))
    provider = outer.get("provider", "")
    if "oracle" not in provider.lower():
        raise ProbeError("主配置不是 Oracle 数据源。")
    provider_text = outer.get("provider connection string", "")
    values = split_semicolon_kv(html.unescape(provider_text))
    data_source = normalize_data_source(values.get("data source", ""))
    user = values.get("user id") or values.get("user") or ""
    password = values.get("password") or values.get("pwd") or ""
    if not data_source or not user or not password:
        raise ProbeError("主 Oracle 配置缺少必要字段。")

    return OracleProfile(
        name=PROFILE_NAME,
        source_file=config_path.name,
        provider=provider,
        data_source=data_source,
        user=user,
        password=password,
    )


def load_credential_profile(fibrecheck_dir: Path, spec: str) -> OracleProfile:
    if ":" not in spec:
        raise ProbeError("--credential-profile 需要 配置文件名:条目名 形式。")
    filename, entry_name = (part.strip() for part in spec.split(":", 1))
    if not filename or not entry_name:
        raise ProbeError("--credential-profile 需要 配置文件名:条目名 形式。")
    if Path(filename).name != filename:
        raise ProbeError("--credential-profile 的配置文件必须直接位于 FibreCheck 目录内。")
    config_path = fibrecheck_dir.expanduser() / filename
    if not config_path.is_file():
        raise ProbeError(f"找不到凭据配置文件 {filename}。")
    if config_path.is_symlink():
        raise ProbeError("凭据配置文件不能是符号链接。")
    if config_path.stat().st_size > 2 * 1024 * 1024:
        raise ProbeError("凭据配置文件异常过大。")

    try:
        root = ET.parse(config_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ProbeError("凭据配置文件无法安全解析。") from exc

    raw: str | None = None
    for item in root.findall(".//connectionStrings/add"):
        if item.attrib.get("name") == entry_name:
            outer = split_semicolon_kv(html.unescape(item.attrib.get("connectionString", "")))
            raw = outer.get("provider connection string") or item.attrib.get("connectionString", "")
            break
    if raw is None:
        for item in root.findall(".//appSettings/add"):
            if item.attrib.get("key") == entry_name:
                raw = item.attrib.get("value", "")
                break
    if raw is None:
        raise ProbeError(f"{filename} 中不存在凭据条目 {entry_name}。")

    values = split_semicolon_kv(html.unescape(raw))
    data_source = normalize_data_source(values.get("data source", ""))
    user = values.get("user id") or values.get("user") or ""
    password = values.get("password") or values.get("pwd") or ""
    if not data_source or not user or not password:
        raise ProbeError("凭据配置条目缺少必要字段。")

    return OracleProfile(
        name=f"{filename}:{entry_name}",
        source_file=filename,
        provider="oracle",
        data_source=data_source,
        user=user,
        password=password,
    )


def safe_error(exc: BaseException) -> dict[str, str]:
    class_name = type(exc).__name__
    matches = ORACLE_ERROR_PATTERN.findall(str(exc))
    return {
        "type": class_name[:120],
        "code": matches[0].upper() if matches else "unclassified",
    }


def mask_login(value: str) -> str:
    if not value:
        return ""
    if len(value) == 1:
        return "*"
    if len(value) == 2:
        return f"{value[0]}*"
    return f"{value[0]}{'*' * min(len(value) - 2, 6)}{value[-1]}"


def sanitize_path_value(value: Any) -> dict[str, Any]:
    raw = "" if value is None else str(value)
    if not raw:
        return {"count": 0, "items": []}
    items: list[dict[str, str]] = []
    for part in (item.strip() for item in raw.split(",")):
        if not part:
            continue
        normalized = part.replace("\\", "/")
        basename = normalized.rsplit("/", 1)[-1]
        items.append(
            {
                "basename": basename,
                "path_hash": digest_text(part),
            }
        )
    return {"count": len(items), "items": items}


def sanitize_scalar(column: str, value: Any) -> Any:
    upper = column.upper()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {
            "byte_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    if upper in PATH_COLUMN_NAMES:
        return sanitize_path_value(value)
    if upper in LOGIN_COLUMN_NAMES:
        return mask_login(str(value))
    if upper in SENSITIVE_ID_COLUMN_NAMES or ID_COLUMN_PATTERN.search(column):
        return digest_text(str(value))
    return str(value)


def sanitize_row(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    return {
        column: sanitize_scalar(column, row[index])
        for index, column in enumerate(columns)
    }


def query_parameters(
    sample_no: str,
    *,
    target_sample_no: str | None = None,
) -> dict[str, str]:
    base_sample_no = sample_no.split("-", 1)[0]
    target = target_sample_no or sample_no
    target_base = target.split("-", 1)[0]
    return {
        "sample_no": sample_no,
        # 带后缀的测试号也必须同时看见同一九位底单及其它后缀，避免
        # “260187115-1 不存在”掩盖 “260187115 已存在”。
        "sample_prefix": f"{base_sample_no}%",
        "target_sample_no": target,
        "target_base": target_base,
        "target_suffix_prefix": f"{target_base}-%",
    }


def _required_query_rows(
    results: Mapping[str, Any], key: str
) -> list[dict[str, Any]]:
    state = results.get(key)
    if not isinstance(state, Mapping) or state.get("status") != "ok":
        raise ProbeError(f"特种毛 dry-run 查询 {key} 未成功。")
    rows = state.get("rows")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        raise ProbeError(f"特种毛 dry-run 查询 {key} 返回格式无效。")
    if state.get("row_count") != len(rows):
        raise ProbeError(f"特种毛 dry-run 查询 {key} 计数不一致。")
    return rows


def _special_wool_project_key(row: Mapping[str, Any]) -> str:
    identity = "\0".join(
        " ".join(str(row.get(key) or "").strip().split())
        for key in (
            "TaskCheckItemID",
            "CheckItemID",
            "CheckItemNo",
            "CheckItemName",
            "CheckMethod",
            "SeqNum",
        )
    )
    return "task-project:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:24]


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_special_wool_image_observation(
    *,
    results: Mapping[str, Any],
    source_inspection_number: str,
    target_sample_number: str,
    selected_project_key: str,
    operation_id: str,
    payload_checksum: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build the typed, zero-write image-upload database observation."""

    family_rows = _required_query_rows(
        results, "special_wool_number_family"
    )
    project_rows = _required_query_rows(
        results, "special_wool_task_project"
    )
    picture_rows = _required_query_rows(
        results, "special_wool_picture_records"
    )
    index_rows = _required_query_rows(
        results, "special_wool_sample_number_unique"
    )
    server_rows = _required_query_rows(results, "special_wool_server_time")
    if len(server_rows) != 1:
        raise ProbeError("Oracle 服务器时间查询必须且只能返回一行。")

    selected_rows = [
        row
        for row in project_rows
        if _special_wool_project_key(row) == selected_project_key
    ]
    if len(selected_rows) != 1:
        raise ProbeError("所选任务项目在旧系统中不存在或不唯一。")
    selected = selected_rows[0]
    if " ".join(str(selected.get("CheckItemName") or "").split()) not in {
        "纤维微观形貌",
        "膜平面形貌",
    } or " ".join(str(selected.get("CheckMethod") or "").split()) != (
        "GB/T 36422-2018"
    ):
        raise ProbeError("所选任务项目不是受支持的 GB/T 36422-2018 微观形貌项目。")

    base = target_sample_number.split("-", 1)[0]
    exact_pattern = re.compile(re.escape(base) + r"(?:-([1-9][0-9]*))?$")
    occupied: list[str] = []
    ignored: list[str] = []
    exact_count = 0
    for row in family_rows:
        number = str(row.get("SampleNo") or "").strip()
        raw_count = row.get("RecordCount")
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = -1
        if isinstance(raw_count, bool) or count < 0:
            raise ProbeError("编号族查询返回了无效记录数。")
        if exact_pattern.fullmatch(number):
            if count > 0:
                occupied.append(number)
            if number == target_sample_number:
                exact_count = count
        else:
            ignored.append(number)

    main_ids = {
        row.get("MainID") for row in picture_rows if row.get("MainID")
    }
    picture_ids = {
        row.get("PictureID") for row in picture_rows if row.get("PictureID")
    }
    unique_proven = any(
        str(row.get("Uniqueness") or "").upper() == "UNIQUE"
        and str(row.get("ColumnName") or "") == "SampleNo"
        and str(row.get("ColumnCount")) == "1"
        for row in index_rows
    )
    task_project = {
        "project_key": selected_project_key,
        "task_check_item_id": selected.get("TaskCheckItemID"),
        "check_item_id": selected.get("CheckItemID"),
        "check_item_no": selected.get("CheckItemNo"),
        "check_item_name": selected.get("CheckItemName"),
        "check_method": selected.get("CheckMethod"),
        "seq_num": selected.get("SeqNum"),
        "match_count": 1,
    }
    return {
        "schema_version": 1,
        "observation_type": "legacy_special_wool_image_upload_dry_run",
        "mode": "read_only",
        "operation_id": operation_id,
        "payload_checksum": payload_checksum,
        "generated_at": generated_at,
        "source_inspection_number": source_inspection_number,
        "target_sample_number": target_sample_number,
        "target_family": {
            "base_number": base,
            "occupied_numbers": occupied,
            "ignored_numbers": ignored,
            "candidate_number": target_sample_number,
            "candidate_exact_count": exact_count,
            "unique_sample_number_constraint": unique_proven,
        },
        "task_project": task_project,
        "picture_readback": {
            "main_count": len(main_ids),
            "picture_count": len(picture_ids),
            "records": picture_rows,
        },
        "write_performed": False,
        # capability 仍关闭；即使全部数据库事实满足，也不能由该观察授权写入。
        "ready_for_write": False,
        "observation_checksum": _canonical_sha256(
            {
                "server_time": server_rows[0].get("ServerTime"),
                "family": family_rows,
                "task_project": task_project,
                "pictures": picture_rows,
                "unique_indexes": index_rows,
            }
        ),
    }


def build_manifest(
    sample_no: str,
    queries: Sequence[QueryDefinition] = QUERIES,
    *,
    target_sample_no: str | None = None,
) -> list[dict[str, Any]]:
    parameters = query_parameters(
        sample_no, target_sample_no=target_sample_no
    )
    manifest: list[dict[str, Any]] = []
    for query in queries:
        entry = query.manifest_entry()
        entry["bound_parameters"] = {
            name: parameters[name]
            for name in query.parameter_names
        }
        manifest.append(entry)
    return manifest


def _final_entry_query_rows(
    results: Mapping[str, Any],
    key: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    state = results.get(key)
    if not isinstance(state, Mapping):
        return None, {"query": key, "status": "missing"}
    status = state.get("status")
    if status != "ok":
        issue: dict[str, Any] = {
            "query": key,
            "status": str(status or "invalid"),
        }
        raw_error = state.get("error")
        if isinstance(raw_error, Mapping):
            error = {
                name: str(raw_error[name])[:120]
                for name in ("type", "code")
                if raw_error.get(name) is not None
            }
            if error:
                issue["error"] = error
        return None, issue
    raw_rows = state.get("rows")
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, Mapping) for row in raw_rows
    ):
        return None, {"query": key, "status": "invalid_rows"}
    return [dict(row) for row in raw_rows], None


def _nonnegative_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _writer_integer_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return str(int(number))


def _writer_canonical_piece(value: str) -> str:
    # .NET string.Length counts UTF-16 code units, not Unicode code points.
    utf16_length = len(value.encode("utf-16-le")) // 2
    return f"{utf16_length}:{value}|"


def _mapping_config_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    for column in ("TaskCheckItemID", "CheckItemID", "DocumentID"):
        raw_value = row.get(column)
        if not isinstance(raw_value, str) or not raw_value:
            raise ProbeError("映射配置查询返回了无效的模板作用域。")
        scope[column] = sanitize_scalar(column, raw_value)
    return scope


def _incomplete_mapping_config_row(
    row: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    if reason not in MAPPING_CONFIG_FAILURE_REASONS:
        reason = "mapping_fingerprint_invalid"
    table_state = _binary_state(row.get("MappedTableExists"))
    return {
        **_mapping_config_scope(row),
        "MappingConfigStatus": "incomplete",
        "MappingConfigCount": None,
        "MappingConfigSha256": None,
        "MappingConfigReason": reason,
        "MappedTableExists": (
            bool(table_state) if table_state is not None else None
        ),
    }


def _binary_state(value: Any) -> int | None:
    parsed = _nonnegative_integer(value)
    return parsed if parsed in {0, 1} else None


def _fingerprint_mapping_config_rows(
    columns: Sequence[str],
    raw_rows: Sequence[Sequence[Any]],
) -> list[dict[str, Any]]:
    """Collapse private mapping/config rows into safe writer-compatible hashes."""
    if tuple(columns) != MAPPING_CONFIG_RAW_COLUMNS:
        raise ProbeError("映射配置查询返回列与预期不一致。")

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for values in raw_rows:
        if len(values) != len(columns):
            raise ProbeError("映射配置查询返回行与预期不一致。")
        row = dict(zip(columns, values))
        raw_scope = tuple(row[column] for column in MAPPING_CONFIG_RAW_COLUMNS[:3])
        if any(not isinstance(value, str) or not value for value in raw_scope):
            raise ProbeError("映射配置查询返回了无效的模板作用域。")
        grouped.setdefault(raw_scope, []).append(row)

    fingerprints: list[dict[str, Any]] = []
    for rows in grouped.values():
        first = rows[0]
        mapping_counts = {
            _nonnegative_integer(row.get("MappingCount")) for row in rows
        }
        if None in mapping_counts or len(mapping_counts) != 1:
            fingerprints.append(
                _incomplete_mapping_config_row(first, "mapping_count_invalid")
            )
            continue
        mapping_count = next(iter(mapping_counts))
        if mapping_count == 0:
            fingerprints.append(
                _incomplete_mapping_config_row(first, "mapping_missing")
            )
            continue
        if mapping_count != 1:
            fingerprints.append(
                _incomplete_mapping_config_row(first, "mapping_not_unique")
            )
            continue

        table_names = {row.get("DataTableName") for row in rows}
        if (
            len(table_names) != 1
            or not isinstance(next(iter(table_names)), str)
            or not ORACLE_IDENTIFIER_PATTERN.fullmatch(next(iter(table_names)))
        ):
            fingerprints.append(
                _incomplete_mapping_config_row(
                    first,
                    "mapping_table_name_invalid",
                )
            )
            continue
        table_name = next(iter(table_names))

        table_states = {_binary_state(row.get("MappedTableExists")) for row in rows}
        if None in table_states or len(table_states) != 1:
            fingerprints.append(
                _incomplete_mapping_config_row(
                    first,
                    "mapped_table_state_invalid",
                )
            )
            continue
        mapped_table_exists = table_states == {1}

        presence_states = [_binary_state(row.get("ConfigPresent")) for row in rows]
        if any(state is None for state in presence_states):
            fingerprints.append(
                _incomplete_mapping_config_row(first, "config_presence_invalid")
            )
            continue
        if len(set(presence_states)) != 1:
            fingerprints.append(
                _incomplete_mapping_config_row(
                    first,
                    "config_presence_inconsistent",
                )
            )
            continue
        if presence_states[0] == 0:
            fingerprints.append(
                _incomplete_mapping_config_row(first, "config_missing")
            )
            continue

        canonical_rows: list[list[str]] = []
        sort_keys: set[tuple[str, str]] = set()
        config_invalid = False
        duplicate_sort_key = False
        for row in rows:
            seq_num = _writer_integer_text(row.get("SeqNum"))
            key_data_field = row.get("KeyDataField")
            if (
                seq_num is None
                or not isinstance(key_data_field, str)
                or not key_data_field
            ):
                config_invalid = True
                break
            sort_key = (seq_num, key_data_field)
            if sort_key in sort_keys:
                duplicate_sort_key = True
                break
            sort_keys.add(sort_key)

            values = [seq_num, key_data_field]
            for field_name in MAPPING_CONFIG_CANONICAL_FIELDS[2:]:
                value = row.get(field_name)
                if value is not None and not isinstance(value, str):
                    config_invalid = True
                    break
                values.append(value or "")
            if config_invalid:
                break
            canonical_rows.append(values)

        if config_invalid:
            fingerprints.append(
                _incomplete_mapping_config_row(first, "config_value_invalid")
            )
            continue
        if duplicate_sort_key:
            fingerprints.append(
                _incomplete_mapping_config_row(
                    first,
                    "duplicate_config_sort_key",
                )
            )
            continue

        canonical_parts = [_writer_canonical_piece(table_name)]
        for values in canonical_rows:
            canonical_parts.extend(
                _writer_canonical_piece(value) for value in values
            )
        fingerprint = hashlib.sha256(
            "".join(canonical_parts).encode("utf-8")
        ).hexdigest()
        fingerprints.append(
            {
                **_mapping_config_scope(first),
                "MappingConfigStatus": "complete",
                "MappingConfigCount": len(canonical_rows),
                "MappingConfigSha256": fingerprint,
                "MappingConfigReason": None,
                "MappedTableExists": mapped_table_exists,
            }
        )
    return fingerprints


def _sequence_sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (2, "")
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return (1, str(value))
    if number.is_finite():
        return (0, number)
    return (1, str(value))


def _ordered_by_sequence(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed_rows = list(enumerate(rows))
    indexed_rows.sort(
        key=lambda item: (_sequence_sort_key(item[1].get("SeqNum")), item[0]),
    )
    return [row for _, row in indexed_rows]


def _normalized_path_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"count": 0, "items": []}
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        return {"count": 0, "items": []}
    items: list[dict[str, str]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        basename = raw_item.get("basename")
        path_hash = raw_item.get("path_hash")
        if not isinstance(basename, str) or not isinstance(path_hash, str):
            continue
        if not path_hash.startswith("sha256:"):
            continue
        safe_basename = basename.replace(chr(92), "/").rsplit("/", 1)[-1]
        items.append({"basename": safe_basename, "path_hash": path_hash})
    return {"count": len(items), "items": items}


def _normalize_common_detail_table(
    rows: Sequence[dict[str, Any]],
    *,
    complete: bool,
) -> dict[str, Any]:
    normalized_rows = []
    for row in _ordered_by_sequence(rows):
        normalized_rows.append(
            {
                "detail_id": row.get("ID"),
                "seq_num": row.get("SeqNum"),
                "values": [
                    row.get("StandardLocation"),
                    row.get("StandardValue"),
                    row.get("RealLocation"),
                    row.get("RealValue"),
                ],
            }
        )
    return {
        "status": "complete" if complete else "incomplete",
        "columns": list(COMMON_DETAIL_TABLE_COLUMNS),
        "rows": normalized_rows,
    }


def _normalize_common_record(
    row: Mapping[str, Any],
    detail_rows: Sequence[dict[str, Any]],
    key_results: Sequence[dict[str, Any]],
    *,
    details_complete: bool,
    key_results_complete: bool,
) -> dict[str, Any]:
    return {
        "record_id": row.get("ID"),
        "check_record_register_id": row.get("CheckRecordRegisterID"),
        "check_item_id": row.get("CheckItemID"),
        "main_fields": {
            "sample_no": row.get("SampleNo"),
            "grade": row.get("Grade"),
            "judge_basis": row.get("JudgeBasis"),
            "check_item_name": row.get("CheckItemName"),
            "sample_description": row.get("SampleDescription"),
            "test_method": row.get("TestMethod"),
            "standard_type": row.get("StandardType"),
            "unit": row.get("Unit"),
            "report_check_item_name": row.get("ReportCheckItemName"),
            "remark": row.get("Remark"),
            "total_judge": row.get("TotalJudge"),
            "check_user": row.get("CheckUser"),
        },
        "attachment_references": _normalized_path_reference(row.get("AttachInfo")),
        "detail_table": _normalize_common_detail_table(
            detail_rows,
            complete=details_complete,
        ),
        "key_results_status": (
            "complete" if key_results_complete else "incomplete"
        ),
        "key_results": [
            _normalize_key_result(item)
            for item in _ordered_by_sequence(key_results)
        ],
    }


def _normalize_key_result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "original_record_id": row.get("OriginalRecordID"),
        "check_item_id": row.get("CheckItemID"),
        "excel_template_name": row.get("ExcelTemplateName"),
        "config_group_key": row.get("ConfigGroupKey"),
        "seq_num": row.get("SeqNum"),
        "check_item_name": row.get("CheckItemName"),
        "measure_unit": row.get("MeasureUnit"),
        "sample_identity": row.get("SampleIdentity"),
        "check_method": row.get("CheckMethod"),
        "check_method_1": row.get("CheckMethod1"),
        "standard_value": row.get("StandardValue"),
        "check_result": row.get("CheckResult"),
        "check_result_2": row.get("CheckResult2"),
        "check_result_3": row.get("CheckResult3"),
        "judgement": row.get("Judgement"),
        "remark": row.get("Remark"),
        "judge_basis": row.get("JudgeBasis"),
        "is_sub_check_item": row.get("IsSubCheckItem"),
        "is_show_after_detail": row.get("IsShowAfterDetail"),
        "is_show_before_detail": row.get("IsShowBeforeDetail"),
        "grade": row.get("Grade"),
        "test_location": row.get("TestLocation"),
    }


def _normalize_list_data(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "original_record_id": row.get("OriginalRecordID"),
        "check_item_id": row.get("CheckItemID"),
        "excel_template_name": row.get("ExcelTemplateName"),
        "key_data_field": row.get("KeyDataField"),
        "seq_num": row.get("SeqNum"),
        "values": [row.get(f"Column{index}") for index in range(1, 21)],
    }


def _normalize_other_data(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "other_data_id": row.get("ID"),
        "original_record_id": row.get("OriginalRecordID"),
        "sample_no": row.get("SampleNo"),
        "excel_template_name": row.get("ExcelTemplateName"),
        "check_item_no": row.get("CheckItemNo"),
        "original_data": row.get("OriginalData"),
        "data_type": row.get("DataType"),
    }


def _normalize_entry_route(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_check_item_id": row.get("TaskCheckItemID"),
        "task_id": row.get("TaskID"),
        "check_item_id": row.get("CheckItemID"),
        "catalog_check_item_no": row.get("CatalogCheckItemNo"),
        "catalog_check_item_name": row.get("CatalogCheckItemName"),
        "original_data_input_ui_class_name": row.get(
            "OriginalDataInputUIClassName"
        ),
        "position_id": row.get("PositionID"),
    }


def _normalize_mapping_config(
    row: Mapping[str, Any] | None,
    *,
    missing_reason: str,
) -> dict[str, Any]:
    if row is None:
        reason = (
            missing_reason
            if missing_reason in MAPPING_CONFIG_FAILURE_REASONS
            else "mapping_fingerprint_invalid"
        )
        return {
            "status": "incomplete",
            "config_count": None,
            "expected_mapping_config_sha256": None,
            "mapped_table_exists": None,
            "reason": reason,
        }

    status = row.get("MappingConfigStatus")
    if status == "complete":
        count = _nonnegative_integer(row.get("MappingConfigCount"))
        fingerprint = row.get("MappingConfigSha256")
        mapped_table_exists = row.get("MappedTableExists")
        if (
            count is not None
            and count > 0
            and isinstance(fingerprint, str)
            and SHA256_HEX_PATTERN.fullmatch(fingerprint)
            and isinstance(mapped_table_exists, bool)
            and row.get("MappingConfigReason") is None
        ):
            return {
                "status": "complete",
                "config_count": count,
                "expected_mapping_config_sha256": fingerprint,
                "mapped_table_exists": mapped_table_exists,
                "reason": None,
            }
        reason = "mapping_fingerprint_invalid"
    elif status == "incomplete":
        raw_reason = row.get("MappingConfigReason")
        reason = (
            raw_reason
            if raw_reason in MAPPING_CONFIG_FAILURE_REASONS
            else "mapping_fingerprint_invalid"
        )
    else:
        reason = "mapping_fingerprint_invalid"
    return {
        "status": "incomplete",
        "config_count": None,
        "expected_mapping_config_sha256": None,
        "mapped_table_exists": (
            row.get("MappedTableExists")
            if isinstance(row.get("MappedTableExists"), bool)
            else None
        ),
        "reason": reason,
    }


def _normalize_configured_template(
    row: Mapping[str, Any],
    mapping_row: Mapping[str, Any] | None,
    *,
    missing_reason: str,
) -> dict[str, Any]:
    return {
        "task_check_item_id": row.get("TaskCheckItemID"),
        "check_item_id": row.get("CheckItemID"),
        "document_id": row.get("DocumentID"),
        "document_name": _normalized_path_reference(row.get("DocumentName")),
        "document_upload_time": row.get("DocumentUploadTime"),
        "document_upload_index": row.get("DocumentUploadIndex"),
        "mapping_config": _normalize_mapping_config(
            mapping_row,
            missing_reason=missing_reason,
        ),
    }


def _normalize_excel_register(
    row: Mapping[str, Any],
    key_results: Sequence[dict[str, Any]],
    list_data: Sequence[dict[str, Any]],
    other_data: Sequence[dict[str, Any]],
    *,
    association_complete: bool,
) -> dict[str, Any]:
    return {
        "status": "complete" if association_complete else "incomplete",
        "register_id": row.get("ID"),
        "check_item_id": row.get("CheckItemID"),
        "main_fields": {
            "sample_no": row.get("SampleNo"),
            "position_id": row.get("PositionID"),
            "level": row.get("Level"),
            "create_user": row.get("CreateUser"),
            "create_time": row.get("CreateTime"),
            "review_user": row.get("ReviewUser"),
            "review_time": row.get("ReviewTime"),
            "audit_user": row.get("AuditUser"),
            "audit_time": row.get("AuditTime"),
            "last_update_time": row.get("LastUpdateTime"),
            "last_update_user": row.get("LastUpdateUser"),
            "legacy_original_record_id": row.get("OriginalRecordID"),
            "sample_identity": row.get("SampleIdentity"),
            "check_user": row.get("CheckUser"),
            "equipment_no": row.get("EquipmentNo"),
            "proof_user": row.get("ProofUser"),
            "proof_time": row.get("ProofTime"),
            "check_basis": row.get("CheckBasis"),
            "original_picture_id": row.get("OriginalPictureID"),
        },
        "template_reference": _normalized_path_reference(
            row.get("TemplateFilename")
        ),
        "original_file_reference": _normalized_path_reference(
            row.get("OriginalDataFilename")
        ),
        "key_results": [
            _normalize_key_result(item)
            for item in _ordered_by_sequence(key_results)
        ],
        "list_data": [
            _normalize_list_data(item)
            for item in _ordered_by_sequence(list_data)
        ],
        "other_data": [_normalize_other_data(item) for item in other_data],
    }


def _add_final_entry_reason(
    reasons: list[dict[str, Any]],
    code: str,
    **details: Any,
) -> None:
    reason = {"code": code, **details}
    if reason not in reasons:
        reasons.append(reason)


def build_final_entry_view(
    sample_no: str,
    results: Mapping[str, Any],
) -> dict[str, Any]:
    source_rows: dict[str, list[dict[str, Any]] | None] = {}
    incomplete_queries: list[dict[str, Any]] = []
    mapping_config_issue: dict[str, Any] | None = None
    for key in FINAL_ENTRY_REQUIRED_QUERY_KEYS:
        rows, issue = _final_entry_query_rows(results, key)
        source_rows[key] = rows
        if issue is not None:
            if key == MAPPING_CONFIG_QUERY_KEY:
                mapping_config_issue = issue
            else:
                incomplete_queries.append(issue)

    view: dict[str, Any] = {
        "schema_version": 1,
        "sample_no": sample_no,
        "status": "complete",
        "incomplete": False,
        "association_policy": {
            "project_scope": "Task_CheckItem.CheckItemID exact match",
            "generic_key_result_link": (
                "CurrencyItemRecordNew.ID = OriginalKeyData.OriginalRecordID; "
                "verified by CheckRecordRegister.OriginalRecordID = "
                "CurrencyItemRecordNew.ID"
            ),
            "excel_key_result_link": (
                "CheckRecordRegister.ID = OriginalKeyData.OriginalRecordID"
            ),
            "entry_route_source": "CheckItem.OriginalDataInputUIClassName",
            "template_source": "StandardDocument JOIN Document",
            "excluded_inference_sources": list(
                FINAL_ENTRY_EXCLUDED_INFERENCE_SOURCES
            ),
        },
        "incomplete_queries": incomplete_queries,
        "writer_preflight_incomplete_queries": (
            [mapping_config_issue] if mapping_config_issue is not None else []
        ),
        "projects": [],
    }

    task_items = source_rows["task_check_items"]
    if task_items is None:
        view["status"] = "incomplete"
        view["incomplete"] = True
        view["project_count"] = None
        return view

    check_item_occurrences: dict[Any, int] = {}
    for task_item in task_items:
        check_item_id = task_item.get("CheckItemID")
        if check_item_id:
            check_item_occurrences[check_item_id] = (
                check_item_occurrences.get(check_item_id, 0) + 1
            )

    projects: list[dict[str, Any]] = []
    for task_item in task_items:
        reasons: list[dict[str, Any]] = []
        for issue in incomplete_queries:
            _add_final_entry_reason(
                reasons,
                "query_incomplete",
                query=issue["query"],
                status=issue["status"],
            )

        task_check_item_id = task_item.get("ID")
        check_item_id = task_item.get("CheckItemID")
        expected_result_count = _nonnegative_integer(task_item.get("CheckCount"))
        if expected_result_count is None:
            _add_final_entry_reason(reasons, "invalid_expected_result_count")

        if not task_check_item_id:
            _add_final_entry_reason(reasons, "missing_task_check_item_id")
        if not check_item_id:
            _add_final_entry_reason(reasons, "missing_check_item_id")
        elif check_item_occurrences.get(check_item_id, 0) != 1:
            _add_final_entry_reason(
                reasons,
                "ambiguous_check_item_id",
                occurrence_count=check_item_occurrences.get(check_item_id, 0),
            )
        project_scope_complete = bool(check_item_id) and (
            check_item_occurrences.get(check_item_id, 0) == 1
        )

        route = None
        route_rows = source_rows["task_entry_routes"]
        if route_rows is not None and task_check_item_id:
            route_candidates = [
                row
                for row in route_rows
                if row.get("TaskCheckItemID") == task_check_item_id
            ]
            exact_routes = [
                row
                for row in route_candidates
                if row.get("CheckItemID") == check_item_id
            ]
            if len(route_candidates) != len(exact_routes):
                _add_final_entry_reason(reasons, "entry_route_project_mismatch")
            if len(exact_routes) == 1:
                route = _normalize_entry_route(exact_routes[0])
            elif not exact_routes:
                _add_final_entry_reason(reasons, "missing_entry_route")
            else:
                _add_final_entry_reason(
                    reasons,
                    "ambiguous_entry_route",
                    occurrence_count=len(exact_routes),
                )

        template_rows = source_rows["check_record_templates"]
        mapping_config_rows = source_rows[MAPPING_CONFIG_QUERY_KEY]
        configured_templates: list[dict[str, Any]] = []
        configured_template_count: int | None = None
        if template_rows is not None and task_check_item_id:
            template_candidates = [
                row
                for row in template_rows
                if row.get("TaskCheckItemID") == task_check_item_id
            ]
            exact_templates = [
                row
                for row in template_candidates
                if row.get("CheckItemID") == check_item_id
            ]
            if len(template_candidates) != len(exact_templates):
                _add_final_entry_reason(reasons, "template_project_mismatch")
            exact_mapping_rows: list[dict[str, Any]] = []
            if mapping_config_rows is not None:
                mapping_candidates = [
                    row
                    for row in mapping_config_rows
                    if row.get("TaskCheckItemID") == task_check_item_id
                ]
                exact_mapping_rows = [
                    row
                    for row in mapping_candidates
                    if row.get("CheckItemID") == check_item_id
                ]

            for template in exact_templates:
                matching_rows = [
                    row
                    for row in exact_mapping_rows
                    if row.get("DocumentID") == template.get("DocumentID")
                ]
                mapping_row = matching_rows[0] if len(matching_rows) == 1 else None
                if mapping_config_rows is None:
                    missing_reason = "mapping_query_incomplete"
                elif not matching_rows:
                    missing_reason = "mapping_fingerprint_missing"
                elif len(matching_rows) != 1:
                    missing_reason = "mapping_fingerprint_not_unique"
                else:
                    missing_reason = "mapping_fingerprint_invalid"
                normalized_template = _normalize_configured_template(
                    template,
                    mapping_row,
                    missing_reason=missing_reason,
                )
                configured_templates.append(normalized_template)
            configured_template_count = len(configured_templates)

        generic_records: list[dict[str, Any]] = []
        project_common_rows: list[dict[str, Any]] = []
        valid_common_ids: list[Any] = []
        generic_record_count: int | None = None
        common_rows = source_rows["currency_item_records"]
        common_detail_rows = source_rows["currency_item_record_details"]
        if project_scope_complete and common_rows is not None:
            project_common_rows = [
                row for row in common_rows if row.get("CheckItemID") == check_item_id
            ]
            generic_record_count = len(project_common_rows)
            common_ids = [row.get("ID") for row in project_common_rows]
            valid_common_ids = [value for value in common_ids if value]
            if len(valid_common_ids) != len(common_ids):
                _add_final_entry_reason(reasons, "missing_common_record_id")
            if len(valid_common_ids) != len(set(valid_common_ids)):
                _add_final_entry_reason(reasons, "duplicate_common_record_id")
        common_linkage_complete = (
            project_scope_complete
            and common_rows is not None
            and len(valid_common_ids) == len(project_common_rows)
            and len(valid_common_ids) == len(set(valid_common_ids))
        )
        generic_id_set = set(valid_common_ids)

        registers: list[dict[str, Any]] = []
        register_count: int | None = None
        file_reference_count: int | None = None
        template_reference_count: int | None = None
        unique_template_names: list[str] | None = None
        referenced_template_paths: dict[str, str] = {}
        register_rows = source_rows["check_record_register"]
        if project_scope_complete and register_rows is not None:
            registers = [
                row for row in register_rows if row.get("CheckItemID") == check_item_id
            ]
            register_count = len(registers)
            file_reference_count = sum(
                1
                for row in registers
                if _normalized_path_reference(
                    row.get("OriginalDataFilename")
                )["count"]
                > 0
            )
            template_reference_count = sum(
                1
                for row in registers
                if _normalized_path_reference(row.get("TemplateFilename"))["count"]
                > 0
            )
            unique_template_names = []
            for row in registers:
                reference = _normalized_path_reference(row.get("TemplateFilename"))
                for item in reference["items"]:
                    name = item["basename"]
                    referenced_template_paths.setdefault(
                        item["path_hash"],
                        name,
                    )
                    if name not in unique_template_names:
                        unique_template_names.append(name)

        referenced_path_hashes = set(referenced_template_paths)
        for template in configured_templates:
            configured_path_hashes = {
                item["path_hash"]
                for item in template["document_name"]["items"]
            }
            template["referenced_by_existing_records"] = bool(
                configured_path_hashes & referenced_path_hashes
            )
        for referenced_path_hash, referenced_name in referenced_template_paths.items():
            matching_templates = [
                template
                for template in configured_templates
                if referenced_path_hash
                in {
                    item["path_hash"]
                    for item in template["document_name"]["items"]
                }
            ]
            if not matching_templates:
                _add_final_entry_reason(
                    reasons,
                    "referenced_template_configuration_missing",
                    template_basename=referenced_name,
                )
                continue
            if len(matching_templates) != 1:
                _add_final_entry_reason(
                    reasons,
                    "referenced_template_configuration_not_unique",
                    template_basename=referenced_name,
                    occurrence_count=len(matching_templates),
                )
                continue
            mapping_config = matching_templates[0]["mapping_config"]
            if mapping_config["status"] != "complete":
                _add_final_entry_reason(
                    reasons,
                    "template_mapping_config_incomplete",
                    template_basename=referenced_name,
                    reason=mapping_config["reason"],
                )

        register_ids = [row.get("ID") for row in registers]
        valid_register_ids = [value for value in register_ids if value]
        register_linkage_complete = (
            project_scope_complete
            and register_rows is not None
            and len(valid_register_ids) == len(register_ids)
            and len(valid_register_ids) == len(set(valid_register_ids))
        )
        if registers and len(valid_register_ids) != len(register_ids):
            _add_final_entry_reason(reasons, "missing_register_id")
        if len(valid_register_ids) != len(set(valid_register_ids)):
            _add_final_entry_reason(reasons, "duplicate_register_id")
        register_id_set = set(valid_register_ids)

        key_result_linkage_mode: str | None = None
        if project_scope_complete and common_rows is not None:
            key_result_linkage_mode = (
                "generic_record"
                if project_common_rows
                else "check_record_register"
            )

        generic_bridge_complete = False
        if (
            key_result_linkage_mode == "generic_record"
            and common_linkage_complete
            and register_linkage_complete
        ):
            bridge_ids = {
                row.get("OriginalRecordID")
                for row in registers
                if row.get("OriginalRecordID") in generic_id_set
            }
            bridge_mismatches = [
                row
                for row in registers
                if row.get("OriginalRecordID") not in generic_id_set
            ]
            unbridged_generic_ids = generic_id_set - bridge_ids
            if bridge_mismatches:
                _add_final_entry_reason(
                    reasons,
                    "generic_register_bridge_mismatch",
                    occurrence_count=len(bridge_mismatches),
                )
            if unbridged_generic_ids:
                _add_final_entry_reason(
                    reasons,
                    "unbridged_generic_records",
                    occurrence_count=len(unbridged_generic_ids),
                )
            generic_bridge_complete = (
                not bridge_mismatches and not unbridged_generic_ids
            )

        key_rows = source_rows["original_key_data"]
        linked_generic_key_rows: list[dict[str, Any]] = []
        linked_excel_key_rows: list[dict[str, Any]] = []
        key_result_count: int | None = None
        key_results_complete = False
        if (
            key_result_linkage_mode is not None
            and common_linkage_complete
            and register_linkage_complete
            and key_rows is not None
        ):
            target_key_rows = [
                row for row in key_rows if row.get("CheckItemID") == check_item_id
            ]
            allowed_original_record_ids = (
                generic_id_set
                if key_result_linkage_mode == "generic_record"
                else register_id_set
            )
            linked_key_rows = [
                row
                for row in target_key_rows
                if row.get("OriginalRecordID") in allowed_original_record_ids
            ]
            unlinked_key_rows = [
                row
                for row in target_key_rows
                if row.get("OriginalRecordID") not in allowed_original_record_ids
            ]
            mismatched_key_rows = [
                row
                for row in key_rows
                if row.get("OriginalRecordID") in allowed_original_record_ids
                and row.get("CheckItemID") != check_item_id
            ]
            if unlinked_key_rows:
                _add_final_entry_reason(
                    reasons,
                    "unlinked_key_results",
                    occurrence_count=len(unlinked_key_rows),
                )
            if mismatched_key_rows:
                _add_final_entry_reason(
                    reasons,
                    "key_result_project_mismatch",
                    occurrence_count=len(mismatched_key_rows),
                )
            association_path_complete = (
                generic_bridge_complete
                if key_result_linkage_mode == "generic_record"
                else True
            )
            if association_path_complete:
                if key_result_linkage_mode == "generic_record":
                    linked_generic_key_rows = linked_key_rows
                else:
                    linked_excel_key_rows = linked_key_rows
            if (
                association_path_complete
                and not unlinked_key_rows
                and not mismatched_key_rows
            ):
                key_result_count = len(linked_key_rows)
                key_results_complete = True

        for row in project_common_rows:
            record_id = row.get("ID")
            details = []
            if common_detail_rows is not None and record_id:
                details = [
                    detail
                    for detail in common_detail_rows
                    if detail.get("CurrencyItemRecordNewID") == record_id
                ]
            record_key_rows = [
                key_row
                for key_row in linked_generic_key_rows
                if key_row.get("OriginalRecordID") == record_id
            ]
            generic_records.append(
                _normalize_common_record(
                    row,
                    details,
                    record_key_rows,
                    details_complete=(common_detail_rows is not None),
                    key_results_complete=key_results_complete,
                )
            )

        list_rows = source_rows["original_key_data_list"]
        linked_list_rows: list[dict[str, Any]] = []
        list_data_count: int | None = None
        list_data_complete = False
        if (
            key_result_linkage_mode is not None
            and register_linkage_complete
            and list_rows is not None
        ):
            target_list_rows = [
                row for row in list_rows if row.get("CheckItemID") == check_item_id
            ]
            mismatched_list_rows = [
                row
                for row in list_rows
                if row.get("OriginalRecordID") in register_id_set
                and row.get("CheckItemID") != check_item_id
            ]
            unlinked_list_rows: list[dict[str, Any]] = []
            if key_result_linkage_mode == "check_record_register":
                linked_list_rows = [
                    row
                    for row in target_list_rows
                    if row.get("OriginalRecordID") in register_id_set
                ]
                unlinked_list_rows = [
                    row
                    for row in target_list_rows
                    if row.get("OriginalRecordID") not in register_id_set
                ]
            elif target_list_rows:
                _add_final_entry_reason(
                    reasons,
                    "list_data_not_excel_linkage",
                    occurrence_count=len(target_list_rows),
                )
            if unlinked_list_rows:
                _add_final_entry_reason(
                    reasons,
                    "unlinked_list_data",
                    occurrence_count=len(unlinked_list_rows),
                )
            if mismatched_list_rows:
                _add_final_entry_reason(
                    reasons,
                    "list_data_project_mismatch",
                    occurrence_count=len(mismatched_list_rows),
                )
            if (
                not target_list_rows
                and key_result_linkage_mode == "generic_record"
                and not mismatched_list_rows
            ):
                list_data_count = 0
                list_data_complete = True
            elif (
                key_result_linkage_mode == "check_record_register"
                and not unlinked_list_rows
                and not mismatched_list_rows
            ):
                list_data_count = len(linked_list_rows)
                list_data_complete = True

        other_rows = source_rows["original_key_data_other"]
        linked_other_rows: list[dict[str, Any]] = []
        other_data_count: int | None = None
        other_data_complete = False
        if (
            key_result_linkage_mode is not None
            and register_linkage_complete
            and other_rows is not None
        ):
            check_item_no = task_item.get("CheckItemNo")
            target_other_rows = [
                row
                for row in other_rows
                if check_item_no
                and row.get("CheckItemNo") == check_item_no
            ]
            register_linked_other_rows = [
                row
                for row in other_rows
                if row.get("OriginalRecordID") in register_id_set
            ]
            mismatched_other_rows = [
                row
                for row in register_linked_other_rows
                if (
                    not check_item_no
                    or (
                        row.get("CheckItemNo")
                        and row.get("CheckItemNo") != check_item_no
                    )
                )
            ]
            unlinked_other_rows: list[dict[str, Any]] = []
            if key_result_linkage_mode == "check_record_register":
                linked_other_rows = [
                    row
                    for row in register_linked_other_rows
                    if row not in mismatched_other_rows
                ]
                unlinked_other_rows = [
                    row
                    for row in target_other_rows
                    if row.get("OriginalRecordID") not in register_id_set
                ]
            elif target_other_rows:
                _add_final_entry_reason(
                    reasons,
                    "other_data_not_excel_linkage",
                    occurrence_count=len(target_other_rows),
                )
            if unlinked_other_rows:
                _add_final_entry_reason(
                    reasons,
                    "unlinked_other_data",
                    occurrence_count=len(unlinked_other_rows),
                )
            if mismatched_other_rows:
                _add_final_entry_reason(
                    reasons,
                    "other_data_project_mismatch",
                    occurrence_count=len(mismatched_other_rows),
                )
            if (
                not target_other_rows
                and key_result_linkage_mode == "generic_record"
                and not mismatched_other_rows
            ):
                other_data_count = 0
                other_data_complete = True
            elif (
                key_result_linkage_mode == "check_record_register"
                and not unlinked_other_rows
                and not mismatched_other_rows
            ):
                other_data_count = len(linked_other_rows)
                other_data_complete = True

        excel_records: list[dict[str, Any]] = []
        if (
            key_result_linkage_mode == "check_record_register"
            and register_rows is not None
            and project_scope_complete
        ):
            for register in registers:
                register_id = register.get("ID")
                register_key_rows = [
                    row
                    for row in linked_excel_key_rows
                    if row.get("OriginalRecordID") == register_id
                ]
                register_list_rows = [
                    row
                    for row in linked_list_rows
                    if row.get("OriginalRecordID") == register_id
                ]
                register_other_rows = [
                    row
                    for row in linked_other_rows
                    if row.get("OriginalRecordID") == register_id
                ]
                excel_records.append(
                    _normalize_excel_register(
                        register,
                        register_key_rows,
                        register_list_rows,
                        register_other_rows,
                        association_complete=(
                            bool(register_id)
                            and key_results_complete
                            and list_data_complete
                            and other_data_complete
                        ),
                    )
                )

        project = {
            "status": "incomplete" if reasons else "complete",
            "incomplete": bool(reasons),
            "incomplete_reasons": reasons,
            "task_check_item_id": task_check_item_id,
            "task_id": task_item.get("TaskID"),
            "check_item_id": check_item_id,
            "check_item_no": task_item.get("CheckItemNo"),
            "check_item_name": task_item.get("CheckItemName"),
            "check_method": task_item.get("CheckMethod"),
            "remark": task_item.get("Remark"),
            "give_judgement": task_item.get("GiveJudgement"),
            "sample_identify": task_item.get("SampleIdentify"),
            "seq_num": task_item.get("SeqNum"),
            "check_item_category": task_item.get("CheckItemCategory"),
            "check_item_eng_name": task_item.get("CheckItemEngName"),
            "expected_result_count": expected_result_count,
            "generic_record_count": generic_record_count,
            "register_count": register_count,
            "file_reference_count": file_reference_count,
            "template_reference_count": template_reference_count,
            "unique_template_names": unique_template_names,
            "key_result_linkage_mode": key_result_linkage_mode,
            "key_result_count": key_result_count,
            "list_data_count": list_data_count,
            "other_data_count": other_data_count,
            "entry_route": route,
            "configured_template_count": configured_template_count,
            "configured_templates": configured_templates,
            "generic_records": generic_records,
            "excel_records": excel_records,
        }
        projects.append(project)

    view["projects"] = projects
    view["project_count"] = len(projects)
    view["incomplete"] = bool(incomplete_queries) or any(
        project["incomplete"] for project in projects
    )
    view["status"] = "incomplete" if view["incomplete"] else "complete"
    return view


def connect_oracle(
    profile: OracleProfile,
    oracle_client_dir: Path | None = None,
) -> Any:
    try:
        import oracledb
    except ImportError as exc:
        raise ProbeError("缺少 oracledb，请先安装 requirements.txt。") from exc

    if oracle_client_dir is not None:
        try:
            oracledb.init_oracle_client(lib_dir=str(oracle_client_dir))
        except Exception as exc:  # noqa: BLE001
            raise ProbeError(
                f"Oracle Thick 模式初始化失败（{safe_error(exc)['code']}）。"
            ) from exc

    try:
        return oracledb.connect(
            user=profile.user,
            password=profile.password,
            dsn=profile.data_source,
            tcp_connect_timeout=8,
        )
    except TypeError:
        return oracledb.connect(
            user=profile.user,
            password=profile.password,
            dsn=profile.data_source,
        )


class ReadOnlyProbeRunner:
    def __init__(self, connection: Any):
        self.connection = connection
        self.read_only_transaction_started = False

    def run(
        self,
        sample_no: str,
        *,
        queries: Sequence[QueryDefinition] = QUERIES,
        include_final_entry_view: bool = True,
        target_sample_no: str | None = None,
    ) -> dict[str, Any]:
        parameters = query_parameters(
            sample_no, target_sample_no=target_sample_no
        )
        results: dict[str, Any] = {}
        try:
            try:
                self.connection.autocommit = False
            except AttributeError:
                pass
            self._start_read_only_transaction()
            for query in queries:
                results[query.key] = self._execute_query(query, parameters)
            document = {
                "read_only_transaction_started": self.read_only_transaction_started,
                "results": results,
            }
            if include_final_entry_view:
                document["final_entry_view"] = build_final_entry_view(
                    sample_no,
                    results,
                )
            return document
        finally:
            try:
                self.connection.rollback()
            except Exception:
                pass

    def _start_read_only_transaction(self) -> None:
        assert_read_only_sql(READ_ONLY_TRANSACTION_SQL)
        cursor = self.connection.cursor()
        try:
            cursor.execute(READ_ONLY_TRANSACTION_SQL)
            self.read_only_transaction_started = True
        except Exception as exc:  # noqa: BLE001
            raise ProbeError(
                f"无法建立 Oracle 只读事务（{safe_error(exc)['code']}），已停止全部查询。"
            ) from exc
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def _execute_query(
        self,
        query: QueryDefinition,
        all_parameters: Mapping[str, str],
    ) -> dict[str, Any]:
        assert_read_only_sql(query.sql)
        parameters = {
            name: all_parameters[name]
            for name in query.parameter_names
        }
        cursor = self.connection.cursor()
        try:
            cursor.execute(compact_sql(query.sql), parameters)
            columns = [str(item[0]) for item in cursor.description or ()]
            raw_rows = cursor.fetchall()
            if query.key == MAPPING_CONFIG_QUERY_KEY:
                rows = _fingerprint_mapping_config_rows(columns, raw_rows)
            else:
                rows = [
                    sanitize_row(columns, row)
                    for row in raw_rows
                ]
            return {
                "status": "ok",
                "row_count": len(rows),
                "rows": rows,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "query_error",
                "row_count": 0,
                "rows": [],
                "error": safe_error(exc),
            }
        finally:
            try:
                cursor.close()
            except Exception:
                pass


def build_base_document(
    *,
    sample_no: str,
    mode: str,
    profile: OracleProfile | None,
    queries: Sequence[QueryDefinition] = QUERIES,
    target_sample_no: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_no": sample_no,
        "profile": profile.public_metadata() if profile else None,
        "safety": {
            "database_transaction": "SET TRANSACTION READ ONLY",
            "queries": "parameterized_select_only",
            "commit": False,
            "file_server_access": False,
            "attachment_paths": "basename_and_hash_only",
        },
        "query_manifest": build_manifest(
            sample_no,
            queries,
            target_sample_no=target_sample_no,
        ),
    }


def write_json(document: Mapping[str, Any], output: str) -> None:
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    if output == "-":
        sys.stdout.write(rendered)
        sys.stdout.write("\n")
        return

    destination = Path(output).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    try:
        try:
            os.chmod(temp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.write("\n")
        os.replace(temp_name, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FibreCheck 旧系统绝对只读对账探针",
    )
    parser.add_argument(
        "--fibrecheck-dir",
        required=True,
        help=f"包含 {CONFIG_FILENAME} 的 FibreCheck 安装目录",
    )
    parser.add_argument("--sample-no", required=True, help="需要核验的样品编号")
    parser.add_argument(
        "--output",
        default="-",
        help="JSON 输出路径；默认为 -，即标准输出",
    )
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="仅生成查询清单，不加载 Oracle 驱动、不建立网络连接",
    )
    parser.add_argument(
        "--oracle-client-dir",
        help="可选的 Oracle Instant Client 目录，用于 Thick 模式",
    )
    parser.add_argument(
        "--data-source",
        help="可选：覆盖主配置中的 DATA SOURCE，仅支持 host[:port]/service 形式，"
        "用于同一数据库在当前网络可达的备用地址；凭据仍只读取主配置",
    )
    parser.add_argument(
        "--credential-profile",
        help="可选：改用 FibreCheck 目录内其它配置条目的凭据，格式 配置文件名:条目名；"
        "不提供时仍使用主配置 FibreCheckEntities",
    )
    parser.add_argument(
        "--task-snapshot-only",
        action="store_true",
        help="仅查询 Task、Task_Sample 和 Task_CheckItem，供任务推荐缓存刷新使用",
    )
    parser.add_argument(
        "--special-wool-image-dry-run",
        action="store_true",
        help=(
            "仅执行图片类特种毛上传所需的编号族、任务项目、图片子表、"
            "唯一索引和服务器时间查询，并生成类型化零写入观察文档"
        ),
    )
    parser.add_argument(
        "--target-sample-no",
        help="图片上传预检单分配的候选样品编号",
    )
    parser.add_argument(
        "--selected-project-key",
        help="人工任务签发的 task-project 项目键",
    )
    parser.add_argument("--operation-id", help="执行系统外部操作 ID")
    parser.add_argument(
        "--payload-checksum",
        help="执行系统外部操作预检单 SHA-256",
    )
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    connector: Callable[[OracleProfile, Path | None], Any] = connect_oracle,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        sample_no = validate_sample_no(args.sample_no)
        if args.task_snapshot_only and args.special_wool_image_dry_run:
            raise ProbeError("任务快照模式和特种毛图片 dry-run 不能同时启用。")
        target_sample_no = None
        if args.special_wool_image_dry_run:
            if not args.target_sample_no:
                raise ProbeError("图片 dry-run 必须提供 --target-sample-no。")
            target_sample_no = validate_sample_no(args.target_sample_no)
            if not re.fullmatch(
                r"task-project:[0-9a-f]{24}",
                str(args.selected_project_key or ""),
            ):
                raise ProbeError("图片 dry-run 缺少有效的 --selected-project-key。")
            if not str(args.operation_id or "").strip():
                raise ProbeError("图片 dry-run 缺少 --operation-id。")
            if not SHA256_HEX_PATTERN.fullmatch(
                str(args.payload_checksum or "")
            ):
                raise ProbeError("图片 dry-run 缺少有效的 --payload-checksum。")
        if args.credential_profile:
            profile = load_credential_profile(
                Path(args.fibrecheck_dir),
                args.credential_profile,
            )
        else:
            profile = load_primary_profile(Path(args.fibrecheck_dir))
        data_source_overridden = False
        if args.data_source:
            profile = replace(
                profile,
                data_source=validate_data_source_override(args.data_source),
            )
            data_source_overridden = True
        selected_queries = (
            TASK_SNAPSHOT_QUERIES
            if args.task_snapshot_only
            else SPECIAL_WOOL_DRY_RUN_QUERIES
            if args.special_wool_image_dry_run
            else QUERIES
        )
        document = build_base_document(
            sample_no=sample_no,
            mode="manifest" if args.manifest else "probe",
            profile=profile,
            queries=selected_queries,
            target_sample_no=target_sample_no,
        )
        if args.task_snapshot_only:
            document["query_scope"] = "task_snapshot"
        elif args.special_wool_image_dry_run:
            document["query_scope"] = "special_wool_image_dry_run"
        if data_source_overridden and document["profile"] is not None:
            document["profile"]["data_source_overridden"] = True
        if args.manifest:
            document["connection_attempted"] = False
        else:
            oracle_client_dir = (
                Path(args.oracle_client_dir).expanduser()
                if args.oracle_client_dir
                else None
            )
            connection = connector(profile, oracle_client_dir)
            try:
                run_result = ReadOnlyProbeRunner(connection).run(
                    sample_no,
                    queries=selected_queries,
                    include_final_entry_view=(
                        not args.task_snapshot_only
                        and not args.special_wool_image_dry_run
                    ),
                    target_sample_no=target_sample_no,
                )
                document.update(run_result)
                if args.special_wool_image_dry_run:
                    document["observation"] = (
                        build_special_wool_image_observation(
                            results=run_result["results"],
                            source_inspection_number=sample_no,
                            target_sample_number=target_sample_no,
                            selected_project_key=args.selected_project_key,
                            operation_id=args.operation_id,
                            payload_checksum=args.payload_checksum,
                            generated_at=document["generated_at"],
                        )
                    )
                document["connection_attempted"] = True
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
        write_json(document, args.output)
        return 0
    except ProbeError as exc:
        error_document = {
            "schema_version": 1,
            "mode": "error",
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        write_json(error_document, args.output)
        return 2
    except Exception as exc:  # noqa: BLE001
        error_document = {
            "schema_version": 1,
            "mode": "error",
            "error": safe_error(exc),
        }
        write_json(error_document, args.output)
        return 3


if __name__ == "__main__":
    raise SystemExit(run_cli())
