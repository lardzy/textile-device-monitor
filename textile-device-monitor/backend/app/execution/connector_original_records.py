"""Business-neutral legacy record operations shared by API and workflow callers.

Project matching, result normalization and form decisions belong to portable
workflow code. This layer binds concrete records/files to the remote protocol.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import re

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict
from app.execution.models import ExecutionExternalOperation, ExecutionTaskSnapshotCache, ExecutionUser
from app.execution.persistence import build_file_gateway
from app.execution.storage import ArtifactRef
from app.execution.workbook_format import WorkbookFormat, detect_workbook_format

UPLOAD = 'legacy_fibrecheck.original_record.upload@1'
REVIEW = 'legacy_fibrecheck.original_record.review@1'
ENTRY = 'legacy_fibrecheck.check_record.generic_entry@2'
EXCEL_ENTRY = 'legacy_fibrecheck.check_record.excel_entry@1'
REFERENCES = {UPLOAD, REVIEW, ENTRY, EXCEL_ENTRY}
PROJECT_FIELDS = ('project_key', 'task_check_item_id', 'check_item_id', 'check_item_no', 'check_item_name', 'check_method', 'seq_num', 'check_count')


def project_binding(db, number, supplied):
    cached = db.get(ExecutionTaskSnapshotCache, number)
    project = next((item for item in (cached.snapshot or {}).get('projects', []) if item.get('project_key') == supplied.get('project_key')), None) if cached else None
    if project is None or any(supplied.get(key) != project.get(key) for key in PROJECT_FIELDS):
        raise conflict('connector_task_project_changed', '任务项目身份已变化，请重新查询')
    return {key: project[key] for key in PROJECT_FIELDS}, project


def _source(db, result, operation_ref):
    from app.execution.external_operations import _canonical_checksum, validate_external_receipt
    source = db.get(ExecutionExternalOperation, result['operation_id'])
    if (source is None or source.status != 'completed'
            or (source.request_summary.get('connector_submission') or {}).get('operation_ref') != operation_ref
            or _canonical_checksum(source.receipt) != _canonical_checksum(result['receipt'])):
        raise conflict('connector_source_receipt_changed', '前序操作尚未完成或回执不一致')
    validate_external_receipt(source, source.receipt)
    return source, {'operation_id': source.id, 'payload_checksum': source.payload_checksum, 'receipt_checksum': _canonical_checksum(source.receipt)}


def upload_summary(db, data):
    from app.execution.external_operations import _paper_special_wool_target_filename
    number = data['inspection_number'].strip().upper()
    project, _ = project_binding(db, number, data['project'])
    ref = ArtifactRef(**data['source'])
    gateway = build_file_gateway(db)
    path = gateway.resolve(ref, expected_type='file')
    fingerprint = gateway.fingerprint(ref)
    if fingerprint.sha256 != data['sha256']:
        raise conflict('connector_source_changed', '原始工作簿已变化，请重新读取')
    if detect_workbook_format(path) != WorkbookFormat.OLE or path.suffix.lower() != '.xls':
        raise ExecutionApiError(422, 'connector_upload_format_unsupported', '当前检务原始记录接口接收真实 XLS；XLSX 可以读取，但不能改后缀后上传')
    file = {**ref.as_dict(), 'id': fingerprint.sha256, 'artifact_id': fingerprint.sha256, 'filename': path.name,
            'fingerprint': fingerprint.sha256, 'content_sha256': fingerprint.sha256, 'size_bytes': fingerprint.size, 'is_primary': True}
    return {'schema_version': 1, 'operation_type': 'legacy_special_wool_qualitative_upload', 'profile': 'original_record_upload_v1',
        'source_inspection_number': number, 'target_sample_number': number,
        'target_filename': _paper_special_wool_target_filename(number, path.name),
        'target_allocation': {'base_number': number, 'candidate_number': number, 'suffix_policy': 'base_then_numeric_suffix',
                              'occupancy_scope': 'legacy_task_snapshot_and_execution_operation_fences', 'legacy_readonly_verification_required': True},
        'business_fields': deepcopy(data['business_fields']), 'task_project': project, 'inspector': data.get('inspector', ''),
        'files': [file], 'execution_capability': {'available': bool(settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED)},
        'safety': {'remote_write_performed': False, 'requires_final_approval': False, 'requires_source_reverification': True, 'overwrite_allowed': False, 'expected_picture_count': 0},
        'machine_contract': {'observation_type': 'legacy_special_wool_qualitative_upload_dry_run', 'receipt_type': 'legacy_special_wool_qualitative_upload',
                             'schema_version': 1, 'picture_count': 0, 'read_only_probe_required': True}}


def review_summary(db, data):
    from app.execution.external_operations import _special_wool_upload_main_id
    source, reference = _source(db, data['upload_result'], UPLOAD)
    summary = source.request_summary
    reference['main_id'] = _special_wool_upload_main_id(source)
    return {'schema_version': 1, 'operation_type': 'legacy_special_wool_qualitative_review', 'profile': 'original_record_review_v1',
        'source_inspection_number': summary['source_inspection_number'], 'target_sample_number': source.receipt['target_sample_number'],
        'source_operation': reference, 'task_project': deepcopy(summary['task_project']), 'files': deepcopy(summary['files']),
        'business_fields': {'fiber_category': summary['business_fields']['fiber_category'], 'review_action': '特纤复核',
                            'review_item': summary['business_fields']['review_item'], 'review_copies': summary['business_fields']['review_copies']},
        'execution_capability': {'available': bool(settings.EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED)},
        'safety': {'remote_write_performed': False, 'requires_final_approval': False, 'requires_source_reverification': True, 'overwrite_allowed': False, 'expected_picture_count': 0},
        'machine_contract': {'observation_type': 'legacy_special_wool_qualitative_review_dry_run', 'receipt_type': 'legacy_special_wool_qualitative_review',
                             'schema_version': 1, 'picture_count': 0, 'read_only_probe_required': True}}


def entry_summary(db, data):
    from app.execution.external_operations import _canonical_checksum
    number = data['inspection_number'].strip().upper()
    project, current = project_binding(db, number, data['project'])
    expected = data['expected_existing_register_count']
    if current.get('register_count') != expected:
        raise conflict('connector_registration_changed', '已有登记数量已变化，请重新查询')
    reference = None
    if data.get('review_result'):
        source, reference = _source(db, data['review_result'], REVIEW)
        if source.request_summary['task_project'] != project or source.request_summary['source_inspection_number'] != number:
            raise conflict('connector_review_project_mismatch', '复核回执与本次登记的项目不一致')
    record = deepcopy(data['record'])
    package = {'schema_version': 4, 'operation_type': 'generic_item_record', 'sample_number': number,
               'check_item_no': project['check_item_no'], 'check_item_name': project['check_item_name'], 'task_project': project,
               'expected_existing_register_count': expected, 'generic_record': record}
    return {'schema_version': 1, 'operation_type': 'legacy_generic_check_record_entry', 'profile': 'generic_check_record_entry_v2',
        'source_inspection_number': number, 'target_sample_number': number, 'task_project': project,
        'source_review_operation': reference, 'record_digest': _canonical_checksum(record), 'final_entry_package': package,
        'final_entry_summary': {'expected_task_check_count': project['check_count'], 'expected_existing_register_count': expected,
                               'resulting_register_count': expected + 1, 'detail_count': len(record['details']), 'expected_proofed_count': 0},
        'business_fields': {'inspection_item': project['check_item_name'], 'inspection_method': project['check_method'], 'inspection_copies': project['check_count']},
        'execution_capability': {'available': bool(settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED)},
        'safety': {'remote_write_performed': False, 'requires_final_approval': False, 'requires_source_reverification': True, 'overwrite_allowed': False, 'proof_required': False},
        'machine_contract': {'receipt_type': 'legacy_generic_check_record_entry', 'schema_version': 1, 'proof_required': False}}


def excel_entry_summary(db, data):
    """Submit a standard Excel collector package without a business template table."""
    number = data['inspection_number'].strip().upper()
    project, current = project_binding(db, number, data['project'])
    expected = data['expected_existing_register_count']
    if current.get('register_count') != expected:
        raise conflict('connector_registration_changed', '已有登记数量已变化，请重新查询')
    reference = None
    if data.get('review_result'):
        source, reference = _source(db, data['review_result'], REVIEW)
        if source.request_summary['task_project'] != project or source.request_summary['source_inspection_number'] != number:
            raise conflict('connector_review_project_mismatch', '复核回执与本次登记的项目不一致')
    ref = ArtifactRef(**data['source'])
    gateway = build_file_gateway(db)
    path = gateway.resolve(ref, expected_type='file')
    fingerprint = gateway.fingerprint(ref)
    if fingerprint.sha256 != data['sha256']:
        raise conflict('connector_source_changed', '登记工作簿已变化，请重新生成')
    if detect_workbook_format(path) != WorkbookFormat.OLE or path.suffix.lower() != '.xls':
        raise ExecutionApiError(422, 'connector_entry_format_unsupported', '当前检务 Excel 采集接口接收真实 XLS')
    file = {**ref.as_dict(), 'artifact_id': fingerprint.sha256, 'filename': path.name,
            'content_sha256': fingerprint.sha256, 'size_bytes': fingerprint.size}
    template = deepcopy(data['template'])
    excel = {'template_name': template['template_name'], 'collection_mode': 'standard',
             'expected_mapping_config_sha256': template['mapping_config_sha256'],
             'key_result_count': len(data['expected_key_identities']),
             'expected_key_identities': deepcopy(data['expected_key_identities']), 'register': deepcopy(data['register']),
             'workbook': {key: file[key] for key in ('relative_path', 'filename', 'size_bytes', 'content_sha256')}}
    package = {'schema_version': 5, 'operation_type': 'excel_check_record', 'sample_number': number,
               'check_item_no': project['check_item_no'], 'check_item_name': project['check_item_name'], 'task_project': project,
               'expected_existing_register_count': expected, 'excel_record': excel}
    return {'schema_version': 1, 'operation_type': 'legacy_microscopy_check_record_entry', 'profile': 'excel_check_record_entry_v1',
        'source_inspection_number': number, 'target_sample_number': number, 'task_project': project,
        'source_review_operation': reference, 'files': [file], 'template_binding': template, 'final_entry_package': package,
        'final_entry_summary': {'expected_task_check_count': project['check_count'], 'expected_existing_register_count': expected,
                               'resulting_register_count': expected + 1, 'key_result_count': excel['key_result_count']},
        'business_fields': {'inspection_item': project['check_item_name'], 'inspection_method': project['check_method'],
                            'inspection_copies': project['check_count'], 'sample_identity': excel['register']['sample_identity']},
        'execution_capability': {'available': bool(settings.EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED)},
        'safety': {'remote_write_performed': False, 'requires_final_approval': False, 'requires_source_reverification': True, 'overwrite_allowed': False},
        'machine_contract': {'receipt_type': 'legacy_microscopy_check_record_entry', 'schema_version': 1}}


def bind_actor(db, summary, actor_id):
    """Bind authenticated ownership once, after common service preparation."""
    for key in ('source_operation', 'source_review_operation'):
        reference = summary.get(key)
        if reference:
            source = db.get(ExecutionExternalOperation, reference['operation_id'])
            if source.created_by_id != actor_id:
                raise conflict('connector_source_owner_mismatch', '前序回执不属于当前操作账号')
    if summary['profile'] == 'original_record_upload_v1' and not summary['inspector']:
        actor = db.get(ExecutionUser, actor_id)
        summary['inspector'] = actor.display_name


def reverify_sources(db, operation):
    from app.execution.external_operations import _canonical_checksum
    summary = operation.request_summary
    if summary['profile'] in {'original_record_upload_v1', 'excel_check_record_entry_v1'}:
        for source in summary['files']:
            actual = build_file_gateway(db).fingerprint(ArtifactRef(source['root_id'], source['relative_path']))
            if actual.sha256 != source['content_sha256']:
                raise conflict('connector_source_changed', '待上传原始工作簿内容已变化')
    for key in ('source_operation', 'source_review_operation'):
        reference = summary.get(key)
        if reference:
            source = db.get(ExecutionExternalOperation, reference['operation_id'])
            if source is None or source.status != 'completed' or source.payload_checksum != reference['payload_checksum'] or _canonical_checksum(source.receipt) != reference['receipt_checksum']:
                raise conflict('connector_source_receipt_changed', '前序回执已变化')
