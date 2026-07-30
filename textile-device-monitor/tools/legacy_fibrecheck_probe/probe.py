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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


CONFIG_FILENAME = "Toone.FibreCheck.Entites.dll.config"
PROFILE_NAME = "FibreCheckEntities"
SAMPLE_NO_PATTERN = re.compile(r"^[0-9A-Z]{9,20}(?:-[0-9A-Z]{1,8})?$")
ORACLE_ERROR_PATTERN = re.compile(r"\b(?:ORA|DPY|DPI)-\d{3,5}\b", re.IGNORECASE)
WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|TRUNCATE|GRANT|REVOKE|"
    r"COMMIT|ROLLBACK|CALL|EXECUTE|BEGIN|DECLARE)\b",
    re.IGNORECASE,
)
PATH_COLUMN_NAMES = {
    "FILEPATH",
    "FILENAME",
    "ORIGINALDATAFILENAME",
    "TEMPLATEFILENAME",
}
ID_COLUMN_PATTERN = re.compile(
    r"(?:^ID$|ID$|^CREATEUSER$|^REVIEWUSER\d*$|^AUDITUSER$|^CHECKUSER\d*$|^PROOFUSER$|^LASTUPDATEUSER$)",
    re.IGNORECASE,
)
LOGIN_COLUMN_NAMES = {"LOGINNAME"}


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
    if ID_COLUMN_PATTERN.search(column):
        return digest_text(str(value))
    return str(value)


def sanitize_row(columns: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    return {
        column: sanitize_scalar(column, row[index])
        for index, column in enumerate(columns)
    }


def query_parameters(sample_no: str) -> dict[str, str]:
    base_sample_no = sample_no.split("-", 1)[0]
    return {
        "sample_no": sample_no,
        # 带后缀的测试号也必须同时看见同一九位底单及其它后缀，避免
        # “260187115-1 不存在”掩盖 “260187115 已存在”。
        "sample_prefix": f"{base_sample_no}%",
    }


def build_manifest(sample_no: str) -> list[dict[str, Any]]:
    parameters = query_parameters(sample_no)
    manifest: list[dict[str, Any]] = []
    for query in QUERIES:
        entry = query.manifest_entry()
        entry["bound_parameters"] = {
            name: parameters[name]
            for name in query.parameter_names
        }
        manifest.append(entry)
    return manifest


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

    def run(self, sample_no: str) -> dict[str, Any]:
        parameters = query_parameters(sample_no)
        results: dict[str, Any] = {}
        try:
            try:
                self.connection.autocommit = False
            except AttributeError:
                pass
            self._start_read_only_transaction()
            for query in QUERIES:
                results[query.key] = self._execute_query(query, parameters)
            return {
                "read_only_transaction_started": self.read_only_transaction_started,
                "results": results,
            }
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
            rows = [
                sanitize_row(columns, row)
                for row in cursor.fetchall()
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
        "query_manifest": build_manifest(sample_no),
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
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    connector: Callable[[OracleProfile, Path | None], Any] = connect_oracle,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        sample_no = validate_sample_no(args.sample_no)
        profile = load_primary_profile(Path(args.fibrecheck_dir))
        document = build_base_document(
            sample_no=sample_no,
            mode="manifest" if args.manifest else "probe",
            profile=profile,
        )
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
                run_result = ReadOnlyProbeRunner(connection).run(sample_no)
                document.update(run_result)
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
