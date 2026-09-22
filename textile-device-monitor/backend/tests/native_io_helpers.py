"""Test-only source files and snapshots; no runtime business-node registration."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from openpyxl import Workbook
import xlwt
from PIL import Image
from app.execution import paper_fiber
from app.execution.microscopy_families import MICROSCOPY_RECORD_FAMILIES
from app.execution.models import ExecutionFileIndexEntry, ExecutionTaskSnapshotCache
NUMBER = '26W006701'
def index_file(env, root_id, path):
    stat = path.stat()
    row = ExecutionFileIndexEntry(
        storage_root_id=env.roots[root_id].id,
        relative_path=path.relative_to(env.roots[root_id].local_path).as_posix(),
        filename=path.name, extension=path.suffix, file_kind="file",
        inspection_number=NUMBER, size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}", scan_generation=1,
    )
    env.db.add(row)
    env.db.commit()
    return row

def paper_file(env, suffix=".xlsx", value="木浆 100"):
    path = Path(env.roots["paper_fiber_records"].local_path) / NUMBER / f"record{suffix}"
    path.parent.mkdir(exist_ok=True)
    if suffix == ".xlsx":
        book = Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet["W32"], sheet["M32"] = value, "标准值"
        book.save(path)
        book.close()
    else:
        book = xlwt.Workbook()
        sheet = book.add_sheet("Sheet1")
        sheet.write(31, 22, value)
        sheet.write(31, 12, "标准值")
        book.save(str(path))
    return index_file(env, "paper_fiber_records", path)

def image_files(env, count=1):
    for i in range(count):
        path = Path(env.roots["electron_microscopy_records"].local_path) / NUMBER / f"image-{i}.png"
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (60 + i * 20, 40), "red").save(path)
        index_file(env, "electron_microscopy_records", path)

def task_snapshot(env, family="paper"):
    item = (paper_fiber.PAPER_FIBER_PROJECT_NAME if family == "paper"
            else MICROSCOPY_RECORD_FAMILIES[family].check_item_name)
    method = "GB/T 4688-2020" if family == "paper" else "GB/T 36422-2018"
    snapshot = {
        "schema_version": 5, "inspection_number": NUMBER,
        "sample_names": ["棉布"], "check_basis": method,
        "special_wool_occupied_numbers": [],
        "projects": [{
            "project_key": "task-project:domain-test",
            "task_check_item_id": "sha256:1111111111111111",
            "check_item_id": "sha256:2222222222222222",
            "check_item_name": item, "check_method": method,
            "check_count": 1, "register_count": 0, "sample_identify": "正面",
            "give_judgement": 0,
        }],
    }
    env.db.add(ExecutionTaskSnapshotCache(
        inspection_number=NUMBER, status="ready", snapshot=snapshot,
        fetched_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    env.db.commit()
    return snapshot
