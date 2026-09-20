"""Business input validation shared by historical and native record flows."""

import re
from typing import Any

from app.execution.errors import ExecutionApiError


def validate_microscopy_input(context: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    projects = context.get("projects") or []
    if not isinstance(projects, list) or not projects:
        raise ExecutionApiError(
            422,
            "microscopy_project_missing",
            "未读取到可用于生成微观形貌原始记录的检测项目",
        )
    selected_key = str(data.get("selected_project_key") or "").strip()
    selected_project = next(
        (
            value
            for index, value in enumerate(projects)
            if isinstance(value, dict)
            and str(
                value.get("project_key")
                or value.get("key")
                or value.get("task_check_item_id")
                or value.get("id")
                or f"project-{index + 1}"
            )
            == selected_key
        ),
        None,
    )
    if selected_project is None:
        raise ExecutionApiError(
            409,
            "microscopy_project_not_offered",
            "所选检测项目不在当前任务快照中，请刷新后重试",
        )

    sample_name = " ".join(str(data.get("sample_name") or "").strip().split())
    if not sample_name:
        raise ExecutionApiError(
            422, "microscopy_sample_name_required", "请选择或填写样品名称"
        )
    if len(sample_name) > 500:
        raise ExecutionApiError(
            422, "microscopy_sample_name_too_long", "样品名称不能超过 500 个字符"
        )

    def compact_options(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            parts = (
                re.split(r"[，,、]", item)
                if isinstance(item, str)
                else [item]
            )
            for part in parts:
                text = " ".join(str(part or "").strip().split())
                if text and text.casefold() not in seen:
                    result.append(text)
                    seen.add(text.casefold())
        return result

    identities = compact_options(
        selected_project.get("sample_identify")
        or selected_project.get("sample_identity")
        or selected_project.get("sample_identification")
        or context.get("sample_identify")
        or context.get("sample_identity")
    )
    submitted_identity = " ".join(
        str(data.get("sample_identity") or "").strip().split()
    )
    if len(identities) == 1:
        sample_identity = identities[0]
    elif identities:
        if submitted_identity not in identities:
            raise ExecutionApiError(
                409,
                "microscopy_sample_identity_invalid",
                "填写的样品识别不在任务单列表中，无法对应旧系统下拉框",
            )
        sample_identity = submitted_identity
    elif submitted_identity:
        raise ExecutionApiError(
            409,
            "microscopy_sample_identity_invalid",
            "任务单未提供样品识别，不能写入旧系统下拉框",
        )
    else:
        sample_identity = None

    raw_judgement_flag = selected_project.get(
        "give_judgement", context.get("give_judgement")
    )
    judgement_required = not (
        raw_judgement_flag is None
        or raw_judgement_flag is False
        or raw_judgement_flag == 0
        or str(raw_judgement_flag).strip().casefold()
        in {"", "0", "false", "no", "否", "否定"}
    )
    basis_options = compact_options(
        selected_project.get("check_basis_options")
        or selected_project.get("judge_basis_options")
        or context.get("check_basis_options")
    )
    judgement_options = compact_options(
        selected_project.get("judgement_options")
        or context.get("judgement_options")
        or ["符合", "不符合"]
    )
    if judgement_required:
        submitted_basis = " ".join(
            str(data.get("judge_basis") or "").strip().split()
        )
        if submitted_basis:
            # 候选仅供快捷选择，允许人工改写——判定依据写入的是 Excel
            # 文本单元格，不受旧系统下拉框约束。
            judge_basis = submitted_basis
        elif len(basis_options) == 1:
            judge_basis = basis_options[0]
        else:
            raise ExecutionApiError(
                422,
                "microscopy_judge_basis_required",
                "任务单要求判定，请填写判定依据",
            )
        indicator_requirement = " ".join(
            str(data.get("indicator_requirement") or "").strip().split()
        )
        if not indicator_requirement:
            raise ExecutionApiError(
                422,
                "microscopy_indicator_requirement_required",
                "任务单要求判定，请填写指标要求",
            )
        test_result = " ".join(
            str(data.get("test_result") or "").strip().split()
        )
        if not test_result:
            raise ExecutionApiError(
                422,
                "microscopy_test_result_required",
                "任务单要求判定，请填写测试结果",
            )
        judgement = " ".join(
            str(data.get("judgement") or "").strip().split()
        )
        if not judgement or (
            judgement_options and judgement not in judgement_options
        ):
            raise ExecutionApiError(
                422,
                "microscopy_judgement_invalid",
                "请选择本次判定结果",
            )
    else:
        judge_basis = None
        indicator_requirement = None
        test_result = None
        judgement = None

    remark = " ".join(str(data.get("remark") or "").strip().split())
    if any(
        len(value or "") > 1000
        for value in (
            judge_basis,
            indicator_requirement,
            test_result,
            remark,
        )
    ):
        raise ExecutionApiError(
            422,
            "microscopy_record_field_too_long",
            "判定信息、测试结果或备注不能超过 1000 个字符",
        )
    check_count = selected_project.get("check_count")
    identity_count_mismatch = bool(
        identities
        and isinstance(check_count, int)
        and not isinstance(check_count, bool)
        and len(identities) != check_count
    )

    return {
        "selected_project_key": selected_key,
        "selected_project": selected_project,
        "sample_name": sample_name,
        "sample_identity": sample_identity,
        "sample_identity_options": identities,
        "identity_count_mismatch": identity_count_mismatch,
        "judgement_required": judgement_required,
        "judge_basis": judge_basis,
        "indicator_requirement": indicator_requirement,
        "test_result": test_result,
        "judgement": judgement,
        "remark": remark,
    }
