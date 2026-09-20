import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { Alert, AutoComplete, Button, Card, Collapse, Form, Input, InputNumber, List, Select, Space, Spin, Switch, Tag, Typography, message } from 'antd';
import { applyWorkflowReleaseV2, preflightWorkflowReleaseV2, compileWorkflowDesignerV2, getWorkflowDesignerCatalogV2, getWorkflowReleaseV2 } from '../../api/executionV2';
import ExecutionChrome from './ExecutionChrome';
import WorkflowCanvas from './WorkflowCanvas';
import SchemaFields from './SchemaFields';
import TemplatePicker from './TemplatePicker';
import { effectiveSchemas, graphEdges, graphNodes, parseMapping, portableEdge, schemaDefaults, specKey } from './v2Designer';
import './execution.css';

const jsonText = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);

function JsonEditor({ label, value, onChange }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState('');
  useEffect(() => setText(JSON.stringify(value, null, 2)), [value]);
  return <Form.Item label={label} validateStatus={error ? 'error' : ''} help={error}>
    <Input.TextArea aria-label={label} value={text} autoSize={{ minRows: 3, maxRows: 14 }} onChange={event => setText(event.target.value)} />
    <Button size="small" onClick={() => {
      try { onChange(JSON.parse(text)); setError(''); } catch { setError('请输入有效 JSON'); }
    }}>应用{label}</Button>
  </Form.Item>;
}

export default function ExecutionWorkflowV2Designer() {
  const { releaseId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [catalog, setCatalog] = useState(null);
  const [document, setDocument] = useState(null);
  const [source, setSource] = useState(location.state?.replacementSource || null);
  const [selectedId, setSelectedId] = useState(null);
  const [edgeId, setEdgeId] = useState(null);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [report, setReport] = useState(null);
  const [saved, setSaved] = useState(false);
  const [configForm] = Form.useForm();
  const storageKey = `execution:v2-designer:${releaseId || 'new'}`;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const next = await getWorkflowDesignerCatalogV2();
        let initial = location.state?.releaseDocument || next.starter;
        let replacement = location.state?.replacementSource || null;
        if (releaseId) {
          const payload = await getWorkflowReleaseV2(releaseId);
          const release = payload.release || payload;
          initial = structuredClone(payload.document || release.document || release.portable_document);
          replacement = release.migration_source || null;
          initial.release.release_version += 1;
        }
        if (!location.state?.releaseDocument) {
          try {
            const draft = JSON.parse(localStorage.getItem(storageKey));
            if (draft?.document?.format === 'textile-workflow-release') {
              initial = draft.document;
              replacement = draft.source;
            }
          } catch { /* Ignore an incomplete browser draft. */ }
        }
        if (!cancelled) { setCatalog(next); setDocument(initial); setSource(replacement); }
      } catch (cause) { if (!cancelled) setError(cause.message || '设计器加载失败'); }
    })();
    return () => { cancelled = true; };
  }, [releaseId, storageKey, location.state]);

  useEffect(() => {
    if (!document) return;
    setSaved(false);
    const timer = setTimeout(() => {
      try { localStorage.setItem(storageKey, JSON.stringify({ document, source })); setSaved(true); }
      catch { setSaved(false); }
    }, 300);
    return () => clearTimeout(timer);
  }, [document, source, storageKey]);

  const specs = catalog?.node_specs || [];
  const selected = document?.definition.nodes.find(node => node.id === selectedId);
  const selectedSpec = specs.find(item => selected && specKey(item) === specKey(selected));
  const edge = document?.definition.edges.find(item => item.id === edgeId);
  const nodes = useMemo(() => document ? graphNodes(document, specs).map(node => ({ ...node, selected: node.id === selectedId })) : [], [document, specs, selectedId]);
  const edges = useMemo(() => document ? graphEdges(document) : [], [document]);
  const schemas = effectiveSchemas(selected, selectedSpec, catalog?.connectors, document?.definition);
  useEffect(() => { configForm.resetFields(); configForm.setFieldsValue(selected?.config || {}); }, [selectedId, selected?.type, configForm]);

  const edit = updater => { setDocument(previous => updater(structuredClone(previous))); setReport(null); };
  const editNode = changes => edit(value => {
    value.definition.nodes = value.definition.nodes.map(node => node.id === selectedId ? { ...node, ...changes } : node);
    return value;
  });
  const editEdge = changes => edit(value => {
    value.definition.edges = value.definition.edges.map(item => item.id === edgeId ? { ...item, ...changes } : item);
    return value;
  });
  const addNode = key => {
    const spec = specs.find(item => specKey(item) === key);
    if (!spec) return;
    const id = `node-${crypto.randomUUID()}`;
    edit(value => {
      value.definition.nodes.push({ id, type: spec.type, type_version: spec.type_version, name: spec.name,
        config: schemaDefaults(spec.config_schema), input_mapping: {}, ui: { x: 280, y: 300 + value.definition.nodes.length * 25 } });
      return value;
    });
    setSelectedId(id); setEdgeId(null); setAdvanced(true);
  };
  const prepare = async () => {
    setBusy(true);
    try {
      const result = await compileWorkflowDesignerV2(document);
      setReport(result);
      if (result.content_valid) {
        localStorage.setItem(storageKey, JSON.stringify({ document: result.document, source }));
        const preflight = await preflightWorkflowReleaseV2(result.document);
        if (!preflight.content_valid) { setReport(preflight); return; }
        const staged = await applyWorkflowReleaseV2(preflight.preflight_token);
        navigate(`/execution/admin/releases/${staged.id || staged.release?.id}`, { state: { replacementSource: source } });
      }
    } catch (cause) { message.error(cause.message || '生成发布候选失败'); }
    finally { setBusy(false); }
  };
  const dataReferences = useMemo(() => {
    if (!document) return [];
    const choices = Object.keys(document.definition.input_schema?.properties || {}).map(key => ({ value: `$.inputs.${key}`, label: `输入 · ${key}` }));
    document.definition.nodes.filter(node => node.id !== selectedId).forEach(node => {
      const spec = specs.find(item => specKey(item) === specKey(node));
      const output = effectiveSchemas(node, spec, catalog?.connectors, document.definition).output;
      choices.push({ value: `$.nodes.${node.id}.output`, label: `${node.name} · 全部结果` });
      Object.keys(output?.properties || {}).forEach(key => choices.push({ value: `$.nodes.${node.id}.output.${key}`, label: `${node.name} · ${output.properties[key].title || key}` }));
    });
    return choices;
  }, [document, selectedId, specs, catalog]);

  if (!document || !catalog) return <div className="execution-page">{error ? <Alert type="error" message={error} /> : <Spin tip="加载设计器" />}</div>;
  const configSchema = structuredClone(selectedSpec?.config_schema || { type: 'object', properties: {} });
  ['query_ref', 'operation_ref'].forEach(key => {
    if (!configSchema.properties?.[key]) return;
    const contracts = catalog.connectors.flatMap(item => item[key === 'query_ref' ? 'queries' : 'operations'] || []);
    configSchema.properties[key] = { ...configSchema.properties[key], enum: contracts.filter(item => item.ready !== false).map(item => item[key]) };
  });
  return <div className="execution-page execution-v2-designer">
    <ExecutionChrome title="工作流设计器" subtitle="选择模板、调整步骤，生成新版本" backTo={{ path: '/execution/admin', label: '流程管理' }} actions={
      <Space><Tag>{saved ? '草稿已保存到此浏览器' : '草稿尚未保存'}</Tag><Button type="primary" loading={busy} onClick={prepare}>准备发布</Button></Space>
    } />
    <Card size="small">
      <Space wrap align="start">
        <Form.Item label="名称"><Input aria-label="流程名称" value={document.release.name} onChange={event => edit(value => { value.release.name = event.target.value; return value; })} /></Form.Item>
        <Form.Item label="标识"><Input aria-label="流程标识" value={document.release.slug} onChange={event => edit(value => { value.release.slug = event.target.value; return value; })} /></Form.Item>
        <Form.Item label="版本"><InputNumber min={1} aria-label="发布版本" value={document.release.release_version} onChange={version => edit(value => { value.release.release_version = version; return value; })} /></Form.Item>
        <Form.Item label="使用模板"><Select aria-label="使用模板" style={{ width: 280 }} value={null} placeholder="选用内置业务流程" options={catalog.templates.map((item, index) => ({ value: index, label: item.name }))} onChange={index => {
          const template = catalog.templates[index]; setDocument(structuredClone(template.candidate)); setSource(template.replacement_source); setSelectedId(null); setEdgeId(null); setReport(null);
        }} /></Form.Item>
      </Space>
    </Card>
    {report && <Alert type="error" showIcon message="请先修正以下问题" description={<ul>{report.issues.map((issue, index) => <li key={index}>{issue.message} <Typography.Text type="secondary">{issue.path}</Typography.Text></li>)}</ul>} />}
    <div className="execution-v2-designer__layout">
      <Card title="流程步骤" extra={<Space>展开画布<Switch checked={advanced} onChange={setAdvanced} aria-label="展开画布" /></Space>}>
        <Select showSearch optionFilterProp="label" aria-label="添加节点" placeholder="添加步骤" value={null} style={{ width: '100%', marginBottom: 12 }} options={specs.map(spec => ({ value: specKey(spec), label: `${spec.category} · ${spec.name} (${spec.type}@${spec.type_version})` }))} onChange={addNode} />
        {advanced ? <div style={{ height: 580 }}><WorkflowCanvas nodes={nodes} edges={edges} fitView onNodeClick={(_event, node) => { setSelectedId(node.id); setEdgeId(null); }} onEdgeClick={(_event, item) => { setEdgeId(item.id); setSelectedId(null); }} onNodesChange={changes => {
          const positions = changes.filter(change => change.type === 'position' && change.position);
          if (!positions.length) return;
          edit(value => {
          positions.forEach(change => {
            const node = value.definition.nodes.find(item => item.id === change.id); if (node) node.ui = { ...node.ui, ...change.position };
          }); return value;
        }); }} onConnect={connection => edit(value => { value.definition.edges.push(portableEdge(connection)); return value; })} /></div>
          : <List dataSource={nodes} renderItem={(node, index) => <List.Item actions={[<Button key="edit" type="link" onClick={() => { setSelectedId(node.id); setEdgeId(null); }}>调整</Button>]}><List.Item.Meta title={`${index + 1}. ${node.data.label}`} description={node.data.description} /></List.Item>} />}
      </Card>
      <Card title={selected?.name || (edge ? '连接设置' : '步骤设置')}>
        {selected && <>
          <Form layout="vertical"><Form.Item label="步骤名称"><Input aria-label="步骤名称" value={selected.name} onChange={event => editNode({ name: event.target.value })} /></Form.Item></Form>
          <Form form={configForm} layout="vertical" onValuesChange={(_changed, values) => editNode({ config: values })}><SchemaFields schema={configSchema} /></Form>
          {selectedSpec?.config_schema?.properties?.template && <Form.Item label="从安装或共享目录选择模板"><TemplatePicker onSelect={item => {
            edit(value => {
              const slot = `template_${selected.id.replace(/[^a-z0-9_]/gi, '_').toLowerCase()}`.slice(0, 64);
              if (!value.resources.root_slots.some(root => root.slot_id === slot)) value.resources.root_slots.push({ slot_id: slot, name: '模板目录', access: 'read', required: true });
              const node = value.definition.nodes.find(entry => entry.id === selected.id);
              node.config = { ...node.config, template: { root_slot: slot, relative_path: item.relative_path, sha256: item.sha256 } };
              return value;
            });
          }} /></Form.Item>}
          <Typography.Title level={5}>输入来源</Typography.Title>
          <Form layout="vertical">{[...new Set([...Object.keys(schemas.input?.properties || {}), ...Object.keys(selected.input_mapping || {})])].map(key => <Form.Item key={key} label={schemas.input?.properties?.[key]?.title || key} required={schemas.input?.required?.includes(key)}>
            <AutoComplete aria-label={`输入 ${key}`} options={dataReferences} value={jsonText(selected.input_mapping?.[key]) || ''} filterOption={(text, option) => `${option.label} ${option.value}`.toLowerCase().includes(text.toLowerCase())} onChange={text => {
              const mapping = { ...selected.input_mapping }; const value = parseMapping(text);
              if (value === undefined) delete mapping[key]; else mapping[key] = value;
              editNode({ input_mapping: mapping });
            }} placeholder="选择上游结果，或输入固定值" />
          </Form.Item>)}</Form>
          <Collapse items={[{ key: 'mapping', label: '高级输入映射', children: <JsonEditor label="输入映射" value={selected.input_mapping} onChange={input_mapping => editNode({ input_mapping })} /> }]} />
          <Button danger style={{ marginTop: 16 }} onClick={() => { edit(value => { value.definition.nodes = value.definition.nodes.filter(node => node.id !== selectedId); value.definition.edges = value.definition.edges.filter(item => item.source !== selectedId && item.target !== selectedId); return value; }); setSelectedId(null); }}>删除步骤</Button>
        </>}
        {edge && <>
          <Form layout="vertical"><Form.Item label="连接名称"><Input value={edge.label} onChange={event => editEdge({ label: event.target.value })} /></Form.Item>
            <Form.Item label="等待方式"><Select value={edge.join_policy} options={[{ value: 'all', label: '等待全部入边' }, { value: 'any', label: '任一入边即可' }]} onChange={join_policy => editEdge({ join_policy })} /></Form.Item>
            <JsonEditor label="分支条件" value={edge.condition || null} onChange={condition => edit(value => { const current = value.definition.edges.find(item => item.id === edgeId); if (condition === null) delete current.condition; else current.condition = condition; return value; })} />
          </Form><Button danger onClick={() => { edit(value => { value.definition.edges = value.definition.edges.filter(item => item.id !== edgeId); return value; }); setEdgeId(null); }}>删除连接</Button>
        </>}
        {!selected && !edge && <Typography.Text type="secondary">选择步骤查看配置和输入，展开画布可以拖动和连接。</Typography.Text>}
      </Card>
    </div>
    <Collapse items={[{ key: 'resources', label: '高级：输入输出、资源槽和 JSON', children: <>
      <JsonEditor label="流程输入" value={document.definition.input_schema} onChange={schema => edit(value => { value.definition.input_schema = schema; return value; })} />
      <JsonEditor label="流程输出" value={document.definition.output_schema} onChange={schema => edit(value => { value.definition.output_schema = schema; return value; })} />
      <JsonEditor label="资源槽" value={document.resources} onChange={resources => edit(value => { value.resources = resources; return value; })} />
      <JsonEditor label="完整流程" value={document} onChange={value => { if (value?.format === 'textile-workflow-release' && Array.isArray(value.definition?.nodes) && Array.isArray(value.definition?.edges) && value.release) setDocument(value); else message.error('请输入完整 Workflow Release 文档'); }} />
    </> }]} />
  </div>;
}
