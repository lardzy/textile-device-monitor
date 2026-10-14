"""Selection follows the workbook read snapshot, not the index scan schedule."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution.engine import _validate_index_candidate
from app.execution.errors import ExecutionApiError
from app.execution.persistence import build_file_gateway
from app.execution.v2.native_handlers import _file_query, _workbook_extract_batch
from native_io_helpers import NUMBER, paper_file, write_paper_file
from workflow_native_helpers import environment


def query(env):
    return _file_query(SimpleNamespace(
        db=env.db, run=SimpleNamespace(inspection_number=NUMBER),
        node={'type_version': 2, 'config': {'root_id': 'paper_fiber_records'}},
        input_data={'inspection_number': NUMBER},
    ))['items']


def read(env, sources):
    return _workbook_extract_batch(SimpleNamespace(
        db=env.db,
        node={'config': {'fields': [{'name': 'result', 'sheet': 'Sheet1', 'cell': 'W32', 'required': True}]}},
        input_data={'sources': sources},
    ))


def validate(env, candidate):
    _validate_index_candidate(
        env.db, run=SimpleNamespace(definition_snapshot={
            'root_slots': [{'root_id': 'paper_fiber_records', 'access': 'read'}]}),
        candidate={'items': [candidate]}, gateway=build_file_gateway(env.db),
    )


@pytest.mark.parametrize('extension', ['.xls', '.xlsx'])
@pytest.mark.parametrize('cached_missing', [False, True])
def test_recent_save_is_selectable_before_index_catches_up(environment, extension, cached_missing):
    env = environment
    entry = paper_file(env, suffix=extension, value='旧结果')
    sources = query(env)
    original = deepcopy(sources)
    path = write_paper_file(env, suffix=extension, value='木浆、竹浆')

    row = read(env, sources)['items'][0]
    assert row['read_status'] == 'succeeded'
    assert row['values']['result'] == '木浆、竹浆'
    assert row['file']['fingerprint'] != entry.fingerprint
    assert row['file']['fingerprint'] == f'{path.stat().st_size}:{path.stat().st_mtime_ns}'
    assert row['file']['metadata']['size'] == row['size_bytes'] == path.stat().st_size
    assert sources == original
    if cached_missing:
        # Excel can replace a file while a background scan is observing it.
        entry.missing_since = datetime.now(timezone.utc)
        env.db.commit()
    validate(env, row['file'])
    env.db.refresh(entry)
    assert entry.fingerprint == original[0]['fingerprint']


@pytest.mark.parametrize('index_refreshed', [False, True])
def test_save_after_read_still_requires_new_selection(environment, index_refreshed):
    env = environment
    entry = paper_file(env, value='木浆')
    row = read(env, query(env))['items'][0]
    path = write_paper_file(env, value='木浆、竹浆，读取后的新结果')
    if index_refreshed:
        stat = path.stat()
        entry.fingerprint = f'{stat.st_size}:{stat.st_mtime_ns}'
        entry.size_bytes = stat.st_size
        entry.modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        env.db.commit()
    with pytest.raises(ExecutionApiError) as error:
        validate(env, row['file'])
    assert error.value.code == 'file_candidate_stale'


def test_save_during_read_is_reported_without_hiding_other_workbooks(environment, monkeypatch):
    env = environment
    entry = paper_file(env, suffix='.xls')
    paper_file(env, suffix='.xlsx')
    path = Path(env.roots['paper_fiber_records'].local_path) / entry.relative_path
    original_read = Path.read_bytes

    def changed_during_read(current):
        content = original_read(current)
        if current == path:
            current.write_bytes(content + b'changed during read')
        return content

    monkeypatch.setattr(Path, 'read_bytes', changed_during_read)
    result = read(env, query(env))
    failed = next(row for row in result['items'] if row['file']['id'] == entry.id)
    assert result['failed_count'] == 1
    assert failed['read_status'] == 'failed' and failed['sha256'] is None
    assert failed['errors'][0]['code'] == 'file_changed_during_read'
    assert any(row['read_status'] == 'succeeded' for row in result['items'])
