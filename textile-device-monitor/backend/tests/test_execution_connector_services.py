import pytest

from app.config import settings
from app.execution.connector_operations import submit_operation
from app.execution.external_operations import claim_approved_external_operation, complete_external_attempt, record_external_attempt_stage, _operation_stage_profile
from app.execution.models import ExecutionRun
from app.execution.schemas import ConnectorOperationRequest
from tests import test_execution_paper_external_operations as paper_tests


def test_paper_upload_review_entry_service_has_no_run_dependency(monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED", True)
    fixture = paper_tests.PaperExternalOperationTests()
    fixture.setUp()
    try:
        before = fixture.db.query(ExecutionRun).count()
        def submit(name, data):
            return submit_operation(fixture.db, actor=fixture.user, request=ConnectorOperationRequest(
                operation_ref=f"legacy_fibrecheck.{name}@1", credential_id=fixture.credential.id,
                inspection_number=fixture.run.inspection_number, idempotency_key=name, input=data,
            ))[0]
        def complete(operation, receipt):
            fixture.db.commit()
            current, attempt, _ = claim_approved_external_operation(fixture.db, bridge_id="service-test", account_name="legacy-user",
                supported_operation_types={operation.request_summary["operation_type"]})
            assert current.id == operation.id
            record_external_attempt_stage(fixture.db, attempt_id=attempt.id, bridge_id="service-test", stage=_operation_stage_profile(operation)[2])
            fixture.db.commit()
            complete_external_attempt(fixture.db, attempt_id=attempt.id, bridge_id="service-test", receipt=receipt)
            fixture.db.commit()
        upload = submit("paper_fiber.qualitative_upload", fixture._input())
        assert upload.run_id is None and upload.node_run_id is None
        complete(upload, fixture._upload_receipt(upload))
        review = submit("paper_fiber.qualitative_review", {"upload_result": {"operation_id": upload.id}})
        complete(review, fixture._review_receipt(review))
        project = fixture._project()
        entry = submit("paper_fiber.check_record_entry", {"selected_project_key": project["project_key"], "selected_project": project,
            "review_result": {"operation_id": review.id}, "registration_decision": fixture._registration_decision(project)})
        assert entry.run_id is None and entry.status == "approved"
        assert fixture.db.query(ExecutionRun).count() == before
    finally:
        fixture.tearDown()
