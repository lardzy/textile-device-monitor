from fastapi import Response
from typing import Sequence, Any

# Accepts ORM or schema objects with matching attributes.
import pandas as pd
from io import BytesIO


def _excel_safe_text(value: Any) -> str:
    """Prevent user-entered text from being interpreted as a formula."""
    text_value = "" if value is None else str(value)
    if text_value.startswith(("=", "+", "-", "@")):
        return f"'{text_value}"
    return text_value


def export_history_to_excel(history: Sequence[Any]) -> Response:
    """将历史记录导出为Excel"""
    data = []
    for record in history:
        data.append(
            {
                "ID": record.id,
                "设备ID": record.device_id,
                "状态": _excel_safe_text(record.status),
                "任务ID": _excel_safe_text(record.task_id),
                "任务名称": _excel_safe_text(record.task_name),
                "排队人员": _excel_safe_text(
                    getattr(record, "inspector_name", None)
                ),
                "进度": record.task_progress or 0,
                "耗时(秒)": record.task_duration_seconds or 0,
                "设备指标": _excel_safe_text(record.device_metrics),
                "上报时间": record.reported_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    df = pd.DataFrame(data)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl", mode="w") as writer:  # type: ignore[arg-type]
        df.to_excel(writer, index=False, sheet_name="设备状态历史")

        # 自动调整列宽
        worksheet = writer.sheets["设备状态历史"]
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column_letter].width = adjusted_width

    output.seek(0)

    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=device_history.xlsx"},
    )
