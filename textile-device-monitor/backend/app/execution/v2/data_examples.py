"""A self-contained computation example; no filesystem or Connector binding."""

from app.execution.v2.designer import compile_document, starter_document


def build_data_python_release():
    document = starter_document()
    document["release"].update(slug="json-data-python-example", name="JSON 数据计算示例")
    array = {"type": "array", "items": {"type": "number"}}
    inputs = {"type": "object", "properties": {"values": array}, "required": ["values"], "additionalProperties": False}
    outputs = {"type": "object", "properties": {"total": {"type": "number"}}, "required": ["total"], "additionalProperties": False}
    document["definition"]["input_schema"] = {**inputs, "properties": {**inputs["properties"], "inspection_number": {"type": "string"}}}
    document["definition"]["output_schema"] = outputs
    document["definition"]["nodes"].insert(1, {"id": "compute", "type": "data.python", "type_version": 1, "name": "合计",
        "config": {"runtime_version": 1, "code": "def main(inputs):\n    return {'total': sum(inputs['values'])}",
                   "timeout_seconds": 5, "input_schema": inputs, "output_schema": outputs},
        "input_mapping": {"values": "$.inputs.values"}})
    document["definition"]["nodes"][-1]["input_mapping"] = {"total": "$.nodes.compute.output.total"}
    document["definition"]["edges"] = [{"id": "start-compute", "source": "start", "target": "compute", "join_policy": "all"},
                                          {"id": "compute-end", "source": "compute", "target": "end", "join_policy": "all"}]
    document["fixtures"] = [{"fixture_id": "sum", "name": "求和", "inputs": {"values": [1, 2, 3]}, "globals": {}, "mocks": [],
        "assertions": [{"path": "$.run.status", "operator": "eq", "value": "completed"},
                       {"path": "$.outputs.total", "operator": "eq", "value": 6}]}]
    result = compile_document(document)
    if not result["content_valid"]:
        raise ValueError(result["issues"])
    return result["document"]
