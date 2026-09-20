"""Installed templates exposed through the same roots as mounted LAN shares."""

import hashlib
from pathlib import Path
import shutil

from app.config import settings
from app.execution.errors import ExecutionApiError
from app.execution.persistence import build_file_gateway
from app.execution.storage import ArtifactRef


def install_templates():
    target = Path(settings.EXECUTION_TEMPLATE_ROOT or Path(settings.EXECUTION_RUNTIME_ROOT) / "templates")
    target.mkdir(parents=True, exist_ok=True)
    for source in sorted((Path(__file__).parents[1] / "templates").glob("*.xls*")):
        destination = target / source.name
        # Never replace user edits. New package versions should use new names.
        if not destination.exists():
            try:
                with destination.open("xb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
            except FileExistsError:
                pass
    return target


def list_templates(db, root_id="execution_templates", directory=""):
    gateway = build_file_gateway(db)
    # A synthetic child lets FileGateway validate the requested directory.
    parent = gateway.resolve(ArtifactRef(root_id, f"{directory}/.catalog".lstrip("/")), must_exist=False).parent
    items = []
    for path in sorted(parent.iterdir()):
        if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"}:
            relative = f"{directory}/{path.name}".lstrip("/")
            checked = gateway.resolve(ArtifactRef(root_id, relative))
            items.append({"name": path.name, "root_id": root_id, "relative_path": relative,
                          "sha256": hashlib.sha256(checked.read_bytes()).hexdigest()})
        if len(items) >= 1000:
            break
    return items


def resolve_template(db, reference):
    path = build_file_gateway(db).resolve(ArtifactRef(reference["root_id"], reference["relative_path"]))
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ExecutionApiError(409, "template_changed", "模板内容已更改，请重新选择并发布流程")
    return path
