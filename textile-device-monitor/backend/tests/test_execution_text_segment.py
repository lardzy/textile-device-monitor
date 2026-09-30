from types import SimpleNamespace

from app.execution.v2 import text_handlers
from app.execution.v2.designer import starter_document
from workflow_native_helpers import environment, publish_native_document, run, drain, request


def test_chinese_and_mixed_text_matches_legacy_word_selection():
    result = text_handlers.segment(SimpleNamespace(node={"config": {}}, input_data={
        "texts": ["重装徒步冲锋衣 ７号", "氧化再生纤维素纤维（纱布）", "Surgicel-Fibrillar"]}))
    assert result["method"] == "jieba"
    assert {"冲锋衣", "纤维素", "纱布", "7", "Surgicel", "Fibrillar"} <= set(result["tokens"])
    assert len(result["tokens"]) == len(set(result["tokens"]))
    assert not any(word in result["tokens"] for word in ("(", ")", "-", " "))


def test_text_segments_are_bounded_and_dictionary_failure_still_offers_plain_choices(monkeypatch):
    context = SimpleNamespace(node={"config": {"max_tokens": 2}}, input_data={"text": "冲锋衣 纱布 无纺布"})
    assert text_handlers.segment(context) == {"tokens": ["冲锋衣", "纱布"], "method": "jieba", "truncated": True}
    def broken():
        raise ValueError("dictionary unavailable")
    monkeypatch.setattr(text_handlers, "_tokenizer", broken)
    context.input_data = {"texts": [], "text": "Surgicel-Fibrillar，纱布"}
    assert text_handlers.segment(context) == {"tokens": ["Surgicel", "Fibrillar"], "method": "separator_fallback", "truncated": True}
    context.input_data = {}
    assert text_handlers.segment(context)["tokens"] == []


def test_segment_node_runs_with_worker_and_optional_missing_source(environment):
    document = starter_document()
    document["definition"]["input_schema"]["properties"]["sample_name"] = {"type": "string"}
    document["definition"]["output_schema"] = {"type": "object", "additionalProperties": True}
    document["definition"]["nodes"].insert(1, {
        "id": "words", "name": "文本分词", "type": "text.segment", "type_version": 1, "config": {},
        "input_mapping": {"text": "$.inputs.sample_name"}})
    document["definition"]["nodes"][-1]["input_mapping"] = {"tokens": "$.nodes.words.output.tokens"}
    document["definition"]["edges"] = [
        {"id": "a", "source": "start", "target": "words", "join_policy": "all"},
        {"id": "b", "source": "words", "target": "end", "join_policy": "all"}]
    workflow = publish_native_document(environment.db, environment.admin, document)
    for inputs, expected in [({"sample_name": "重装徒步冲锋衣"}, ["重装", "徒步", "冲锋衣"]), ({}, [])]:
        record = run(environment, workflow.id, inputs=inputs)
        drain(environment)
        detail = request(environment, "GET", f"v1/runs/{record['id']}")
        assert detail["status"] == "completed", detail
        assert detail["output_data"]["tokens"] == expected
