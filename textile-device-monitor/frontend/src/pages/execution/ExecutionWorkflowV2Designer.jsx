import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { Alert, Button, Card, Collapse, Form, Input, List, Select, Space, Spin, Tag, Typography, message } from 'antd';
import { applyWorkflowReleaseV2, preflightWorkflowReleaseV2, compileWorkflowDesignerV2, getWorkflowDesignerCatalogV2, getWorkflowReleaseV2, getWorkflowReleasesV2, testWorkflowDesignerV2, createDesignerDraftV2, getDesignerDraftV2, saveDesignerDraftV2, updateWorkflowReleaseBindingV2, preflightStagedWorkflowReleaseV2, publishWorkflowReleaseV2, testPythonNodeV2 } from '../../api/executionV2';
import { getExecutionCredentials, getExecutionFileRoots } from '../../api/execution';
import ExecutionChrome from './ExecutionChrome';
import WorkflowCanvas from './WorkflowCanvas';
import TemplatePicker from './TemplatePicker';
import { SchemaEditor, ValueEditor, BindingEditor } from './DesignerFields';
import { upstreamNodeIds, effectiveSchemas, graphEdges, graphNodes, portableEdge, schemaDefaults, specKey, referenceIssues, deleteSelection, insertOnEdge, validConnection, variableTree, designerHistory, rewriteOutputReferences, rewriteReferences } from './v2Designer';
import './execution.css';

const emptyBindings = () => ({ root_slots: {}, credential_slots: {}, role_slots: {}, rule_slots: {} });
const noSelection = () => ({ nodes: [], edges: [] });
const idOf = value => value?.id || value?.release?.id;

function JsonEditor({ label, value, onChange, onValidity }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState('');
  const focused = useRef(false);
  useEffect(() => { if (!focused.current) setText(JSON.stringify(value, null, 2)); }, [value]);
  useEffect(() => () => onValidity(label, true), [label, onValidity]);
  return <Form.Item label={label} validateStatus={error ? 'error' : ''} help={error}>
    <Input.TextArea aria-label={label} value={text} autoSize={{ minRows: 3, maxRows: 14 }} onFocus={() => { focused.current = true; }} onBlur={() => { focused.current = false; }} onChange={event => {
      const next = event.target.value; setText(next);
      try { const parsed = JSON.parse(next); onChange(parsed); setError(''); onValidity(label, true); }
      catch { setError('JSON 尚未完成；请修正后保存或发布'); onValidity(label, false); }
    }} />
  </Form.Item>;
}

export default function ExecutionWorkflowV2Designer() {
  const { releaseId, workflowId } = useParams();
  const location = useLocation(), navigate = useNavigate();
  const reconnecting = useRef(null);
  const [catalog, setCatalog] = useState(null), [roots, setRoots] = useState([]), [credentials, setCredentials] = useState([]);
  const initialHistory = { present: null, past: [], future: [] };
  const [history, setHistory] = useState(initialHistory), historyRef = useRef(initialHistory);
  const [identity, setIdentity] = useState({}), identityRef = useRef({});
  const [busy, setBusy] = useState(false), [dirty, setDirty] = useState(false), [error, setError] = useState('');
  const [report, setReport] = useState(null), [fixtureReport, setFixtureReport] = useState(null), [pythonReport, setPythonReport] = useState(null);
  const [sampleInputs, setSampleInputs] = useState({}), [invalidEditors, setInvalidEditors] = useState({});
  const saving = useRef(null), loaded = useRef(null);
  const document = history.present?.document, bindings = history.present?.bindings || emptyBindings();
  const selection = history.present?.selection || noSelection();
  const cacheKey = `execution:v2-designer:${workflowId || releaseId || location.key}`;
  const action = useCallback(next => {
    const updated = designerHistory(historyRef.current, next);
    if (updated === historyRef.current) return;
    historyRef.current = updated; setHistory(updated);
    if (['edit', 'undo', 'redo'].includes(next.type)) { setDirty(true); setReport(null); setFixtureReport(null); setPythonReport(null); }
  }, []);
  const edit = (update, group) => action({ type: 'edit', update, group, at: Date.now() });
  const editDocument = (update, group) => edit(value => { value.document = update(value.document); return value; }, group);
  const setOwner = value => { identityRef.current = value; setIdentity(value); };
  const validity = useCallback((label, valid) => setInvalidEditors(previous => {
    if (Boolean(previous[label]) === !valid) return previous;
    const next = { ...previous }; if (valid) delete next[label]; else next[label] = true; return next;
  }), []);

  useEffect(() => {
    if (workflowId && loaded.current === workflowId) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const [next, nextRoots, nextCredentials] = await Promise.all([getWorkflowDesignerCatalogV2(), getExecutionFileRoots(), getExecutionCredentials()]);
        let initial = structuredClone(location.state?.releaseDocument || next.starter), localBindings = emptyBindings(), owner = {};
        if (workflowId) {
          const draft = await getDesignerDraftV2(workflowId); initial = draft.document; localBindings = draft.bindings || emptyBindings(); owner = draft;
        } else if (releaseId) {
          const payload = await getWorkflowReleaseV2(releaseId), release = payload.release || payload;
          initial = structuredClone(payload.document || release.document || release.portable_document); initial.release.release_version += 1;
          if (release.workflow_id) { const draft = await getDesignerDraftV2(release.workflow_id); owner = draft; localBindings = draft.bindings || emptyBindings(); }
        } else {
          try { const cached = JSON.parse(localStorage.getItem(cacheKey)); if (cached?.document?.format === 'textile-workflow-release') { initial = cached.document; localBindings = cached.bindings || emptyBindings(); } } catch { /* A broken cache never replaces the server draft. */ }
        }
        if (cancelled) return;
        loaded.current = workflowId || releaseId || 'new'; setOwner(owner);
        setCatalog(next); setRoots(nextRoots); setCredentials(nextCredentials);
        action({ type: 'reset', value: { document: initial, bindings: { ...emptyBindings(), ...localBindings }, selection: noSelection() } }); setDirty(false);
      } catch (cause) { if (!cancelled) setError(cause.message || '设计器加载失败'); }
    })();
    return () => { cancelled = true; };
  }, [workflowId, releaseId, cacheKey, location.state, action]);

  useEffect(() => {
    if (!document) return;
    try { localStorage.setItem(cacheKey, JSON.stringify({ document, bindings })); } catch { /* The server remains the authoritative saved draft. */ }
  }, [document, bindings, cacheKey]);

  const persist = async () => {
    if (saving.current) await saving.current;
    const snapshot = structuredClone(historyRef.current.present), owner = identityRef.current;
    const payload = { document: snapshot.document, bindings: snapshot.bindings, ...(owner.workflow_id ? { revision: owner.revision } : {}) };
    const task = owner.workflow_id ? saveDesignerDraftV2(owner.workflow_id, payload) : createDesignerDraftV2(payload);
    saving.current = task;
    try {
      const result = await task;
      const unchanged = JSON.stringify(historyRef.current.present.document) === JSON.stringify(snapshot.document) && JSON.stringify(historyRef.current.present.bindings) === JSON.stringify(snapshot.bindings);
      setOwner(result);
      if (!owner.workflow_id) {
        const next = { ...historyRef.current, present: { ...historyRef.current.present, document: { ...historyRef.current.present.document, release: { ...historyRef.current.present.document.release, slug: result.document.release.slug } } } };
        historyRef.current = next; setHistory(next); loaded.current = result.workflow_id;
        navigate(`/execution/admin/designer/workflows/${result.workflow_id}`, { replace: true });
      }
      if (unchanged) setDirty(false);
      return { ...snapshot, document: { ...snapshot.document, release: { ...snapshot.document.release, slug: result.document.release.slug } }, owner: result };
    } finally { if (saving.current === task) saving.current = null; }
  };
  const save = async () => {
    setBusy(true); try { await persist(); setError(''); message.success('草稿已保存'); } catch (cause) { setError(cause.message || '保存失败，当前修改已保留'); } finally { setBusy(false); }
  };

  const specs = catalog?.node_specs || [];
  const selected = document?.definition.nodes.find(node => node.id === selection.nodes[0]);
  const selectedSpec = specs.find(spec => selected && specKey(spec) === specKey(selected));
  const edge = document?.definition.edges.find(item => item.id === selection.edges[0]);
  const schemas = effectiveSchemas(selected, selectedSpec, catalog?.connectors, document?.definition);
  const issues = useMemo(() => referenceIssues(document, specs, catalog?.connectors), [document, specs, catalog]);
  const nodes = useMemo(() => document ? graphNodes(document, specs).map(node => ({ ...node, selected: selection.nodes.includes(node.id), data: { ...node.data, ...(issues.some(issue => issue.nodeId === node.id && !issue.edgeId) ? { tone: 'red', message: '有输入来源需要修复' } : {}) } })) : [], [document, specs, selection, issues]);
  const edges = useMemo(() => document ? graphEdges(document).map(item => ({ ...item, selected: selection.edges.includes(item.id), ...(issues.some(issue => issue.edgeId === item.id) ? { style: { stroke: '#ff4d4f' }, label: '条件来源失效' } : {}) })) : [], [document, selection, issues]);
  const select = (nodeIds = [], edgeIds = []) => action({ type: 'select', selection: { nodes: nodeIds, edges: edgeIds } });
  const remove = () => edit(value => { value.document = deleteSelection(value.document, value.selection.nodes, value.selection.edges); value.selection = noSelection(); return value; });
  const editNode = (change, group) => editDocument(value => { const node = value.definition.nodes.find(item => item.id === selected.id); Object.assign(node, change); return value; }, group);
  const editConfig = (key, value) => editNode({ config: { ...selected.config, [key]: value } }, `config:${selected.id}:${key}`);
  const editEdge = change => editDocument(value => { Object.assign(value.definition.edges.find(item => item.id === edge.id), change); return value; });
  const dataReferences = useMemo(() => {
    if (!document) return [];
    const result = [variableTree(document.definition.input_schema, '$.inputs', '流程输入')];
    const upstream = upstreamNodeIds(document.definition, selected?.id || edge?.source);
    if (edge) upstream.add(edge.source);
    document.definition.nodes.filter(node => upstream.has(node.id)).forEach(node => result.push(variableTree(effectiveSchemas(node, specs.find(spec => specKey(spec) === specKey(node)), catalog?.connectors, document.definition).output, `$.nodes.${node.id}.output`, node.name)));
    return result;
  }, [document, selected, edge, specs, catalog]);

  const addNode = key => {
    const preset = catalog.python_presets?.find(item => `preset:${item.id}` === key);
    const spec = specs.find(item => specKey(item) === (preset ? 'data.python@1' : key));
    if (!spec) return;
    const id = `node-${crypto.randomUUID()}`, config = preset ? structuredClone(preset.config) : schemaDefaults(spec.config_schema);
    const source = edge && document.definition.nodes.find(node => node.id === edge.source);
    const node = { id, type: spec.type, type_version: spec.type_version, name: preset?.name || spec.name, config, input_mapping: {}, ui: { x: (source?.ui?.x || 40) + 240, y: source?.ui?.y || 300 } };
    edit(value => { value.document = insertOnEdge(value.document, edge?.id, node); value.selection = { nodes: [id], edges: [] }; return value; });
  };
  const bindRoot = (key, rootId, access = 'read', template) => edit(value => {
    const root = roots.find(item => item.root_id === rootId);
    const slot = `${selected.id}_${key}`.replace(/[^a-z0-9_]/gi, '_').toLowerCase().slice(0, 64);
    const resources = value.document.resources;
    resources.root_slots ||= [];
    if (!resources.root_slots.some(item => item.slot_id === slot)) resources.root_slots.push({ slot_id: slot, name: template ? '模板目录' : `${selected.name}目录`, access, required: true });
    const node = value.document.definition.nodes.find(item => item.id === selected.id);
    node.config[key] = template ? { root_slot: slot, relative_path: template.relative_path, sha256: template.sha256 } : slot;
    value.bindings.root_slots[slot] = { root_id: root.root_id, revision: root.binding_revision || 1 };
    return value;
  });
  const bindCredential = (key, credentialId) => edit(value => {
    const credential = credentials.find(item => item.id === credentialId);
    const connector = catalog.connectors.find(item => (selected.config.operation_ref || '').startsWith(`${item.connector_id}.`));
    const slot = `${selected.id}_account`.replace(/[^a-z0-9_]/gi, '_').toLowerCase().slice(0, 64);
    const resources = value.document.resources; resources.credential_slots ||= [];
    resources.credential_slots = resources.credential_slots.filter(item => item.slot_id !== slot);
    resources.credential_slots.push({ slot_id: slot, name: '检务账号', connector_id: connector?.connector_id || credential.system_key, credential_kind: 'password', required: true });
    value.document.definition.nodes.find(item => item.id === selected.id).config[key] = slot;
    value.bindings.credential_slots[slot] = { credential_id: credential.id, revision: credential.revision };
    return value;
  });

  const publish = async () => {
    setBusy(true);
    try {
      const saved = await persist(), candidate = structuredClone(saved.document);
      const releases = await getWorkflowReleasesV2({ slug: candidate.release.slug, limit: 100 });
      candidate.release.release_version = Math.max(candidate.release.release_version, 1 + Math.max(0, ...(releases.items || []).map(item => item.source_version || item.release_version || 0)));
      const compiled = await compileWorkflowDesignerV2(candidate); setReport(compiled);
      if (!compiled.content_valid) return;
      const preflight = await preflightWorkflowReleaseV2(compiled.document); setReport(preflight);
      if (!preflight.content_valid) return;
      const staged = await applyWorkflowReleaseV2(preflight.preflight_token), id = idOf(staged);
      await updateWorkflowReleaseBindingV2(id, { environment: 'default', expected_revision: 0, bindings: saved.bindings });
      const ready = await preflightStagedWorkflowReleaseV2(id); setReport(ready);
      if (!ready.publish_ready) return;
      await publishWorkflowReleaseV2(id, ready.preflight_token);
      const owner = await getDesignerDraftV2(saved.owner.workflow_id); setOwner(owner);
      setReport(null); message.success('已发布，可运行流程');
    } catch (cause) { setError(cause.message || '发布失败'); } finally { setBusy(false); }
  };

  if (!document || !catalog) return <div className="execution-page">{error ? <Alert type="error" message={error} /> : <Spin tip="加载设计器" />}</div>;
  const jsonInvalid = Object.keys(invalidEditors).length > 0;
  const referenceMessage = key => issues.filter(issue => issue.nodeId === selected?.id && !issue.edgeId && issue.path[4] === key).map(issue => issue.message).join('；');
  const configProperties = selectedSpec?.config_schema?.properties || {};
  return <div className="execution-page execution-v2-designer">
    <ExecutionChrome title="工作流设计器" subtitle="添加步骤、连接、配置和运行" backTo={{ path: '/execution/admin', label: '流程管理' }} actions={<Space wrap>
      <Tag>{dirty ? '有未保存修改' : identity.workflow_id ? '草稿已保存到服务器' : '新草稿'}</Tag>
      <Button disabled={busy || jsonInvalid} onClick={() => { const url = URL.createObjectURL(new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })); const link = window.document.createElement('a'); link.href = url; link.download = `${document.release.slug}.json`; link.click(); URL.revokeObjectURL(url); }}>导出 JSON</Button>
      <Button disabled={busy || jsonInvalid} onClick={save}>保存草稿</Button>
      <Button type="primary" disabled={jsonInvalid || issues.length > 0} loading={busy} onClick={publish}>发布</Button>
      <Button disabled={!identity.published_version || busy} onClick={() => navigate(`/execution/workflows/${identity.workflow_id}/start`)}>运行</Button>
    </Space>} />
    {error && <Alert type="error" showIcon closable onClose={() => setError('')} message={error} />}
    {report?.issues?.length > 0 && <Alert type="error" showIcon message="请修正配置" description={report.issues.map((issue, index) => <div key={index}>{issue.message} <Typography.Text type="secondary">{issue.path}</Typography.Text></div>)} />}
    {issues.length > 0 && <Alert type="warning" showIcon message={`${issues.length} 处来源需要修复；草稿可以保存`} description={issues.map((issue, index) => <div key={index}><Button type="link" onClick={() => issue.edgeId ? select([], [issue.edgeId]) : select([issue.nodeId])}>{issue.label}：{issue.message}</Button><Typography.Text code>{issue.expression}</Typography.Text></div>)} />}
    <Card size="small"><Space wrap>
      <Form.Item label="名称"><Input aria-label="流程名称" value={document.release.name} onChange={event => editDocument(value => { value.release.name = event.target.value; return value; }, 'name')} /></Form.Item>
      <Form.Item label="标识"><Input aria-label="流程标识" disabled={Boolean(identity.workflow_id)} value={document.release.slug} onChange={event => editDocument(value => { value.release.slug = event.target.value; return value; }, 'slug')} /></Form.Item>
      {!identity.workflow_id && catalog.templates.length > 0 && <Form.Item label="从示例开始"><Select aria-label="使用模板" style={{ width: 280 }} value={null} options={catalog.templates.map((item, index) => ({ value: index, label: item.name }))} onChange={index => edit(value => ({ ...value, document: structuredClone(catalog.templates[index].candidate), selection: noSelection() }))} /></Form.Item>}
    </Space></Card>
    <div className="execution-v2-designer__layout">
      <Card title="流程画布" extra={<Space><Button disabled={!history.past.length} onClick={() => action({ type: 'undo' })}>撤销</Button><Button disabled={!history.future.length} onClick={() => action({ type: 'redo' })}>重做</Button><Button danger disabled={!selection.nodes.length && !selection.edges.length} onClick={remove}>删除选中</Button></Space>}>
        <Select showSearch optionFilterProp="label" aria-label="添加节点" placeholder={edge ? '在选中的连接上插入步骤' : '添加步骤'} value={null} style={{ width: '100%', marginBottom: 12 }} options={[...specs.map(spec => ({ value: specKey(spec), label: `${spec.category} · ${spec.name}` })), ...(catalog.python_presets || []).map(item => ({ value: `preset:${item.id}`, label: `Python 预设 · ${item.name}` }))]} onChange={addNode} />
        {!nodes.length && <Alert message="画布为空，请添加开始、处理和结束节点" type="info" />}
        <div style={{ height: 580 }}><WorkflowCanvas nodes={nodes} edges={edges} fitView onDeleteSelection={remove} onUndo={() => action({ type: 'undo' })} onRedo={() => action({ type: 'redo' })}
          onNodeClick={(event, node) => { if (!event.shiftKey && !event.ctrlKey && !event.metaKey) select([node.id]); }} onEdgeClick={(event, item) => { if (!event.shiftKey && !event.ctrlKey && !event.metaKey) select([], [item.id]); }} onPaneClick={() => select()}
          onNodeDragStop={() => action({ type: 'break' })}
          onNodesChange={changes => {
            const selectedChanges = changes.filter(change => change.type === 'select');
            if (selectedChanges.length) { const ids = new Set(historyRef.current.present.selection.nodes); selectedChanges.forEach(change => change.selected ? ids.add(change.id) : ids.delete(change.id)); select([...ids], historyRef.current.present.selection.edges); }
            const positions = changes.filter(change => change.type === 'position' && change.position);
            if (positions.length) editDocument(value => { positions.forEach(change => { const node = value.definition.nodes.find(item => item.id === change.id); if (node) node.ui = { ...node.ui, ...change.position }; }); return value; }, 'drag');
          }}
          onEdgesChange={changes => { const selectedChanges = changes.filter(change => change.type === 'select'); if (!selectedChanges.length) return; const ids = new Set(historyRef.current.present.selection.edges); selectedChanges.forEach(change => change.selected ? ids.add(change.id) : ids.delete(change.id)); select(historyRef.current.present.selection.nodes, [...ids]); }}
          onReconnectStart={(event, item) => { reconnecting.current = item.id; }} onReconnectEnd={() => { reconnecting.current = null; }}
          isValidConnection={connection => validConnection(document.definition, connection, reconnecting.current)}
          onConnect={connection => { if (validConnection(document.definition, connection)) editDocument(value => { value.definition.edges.push(portableEdge(connection)); return value; }); }}
          onReconnect={(old, connection) => { if (validConnection(document.definition, connection, old.id)) editDocument(value => { const item = value.definition.edges.find(candidate => candidate.id === old.id); const next = portableEdge(connection); delete item.source_handle; delete item.target_handle; Object.assign(item, next, { id: old.id, join_policy: item.join_policy }); return value; }); }} />
        </div>
        <Collapse ghost items={[{ key: 'list', label: '步骤列表', children: <List size="small" dataSource={nodes} renderItem={node => <List.Item actions={[<Button key="edit" type="link" onClick={() => select([node.id])}>调整</Button>]}>{node.data.label}</List.Item>} /> }]} />
      </Card>
      <Card title={selected?.name || (edge ? '连接设置' : '步骤设置')}>
        {selected && <>
          <Form layout="vertical"><Form.Item label="步骤名称"><Input aria-label="步骤名称" value={selected.name} onChange={event => editNode({ name: event.target.value }, `name:${selected.id}`)} /></Form.Item>
            {configProperties.input_schema && <Form.Item label="输入变量定义"><SchemaEditor value={selected.config.input_schema} label="输入字段" onChange={value => editConfig('input_schema', value)} /></Form.Item>}
            {Object.entries(configProperties).filter(([key]) => !['input_schema', 'output_schema', ...(selected.type === 'data.python' ? ['code'] : [])].includes(key)).map(([key, schema]) => {
              if (key === 'template') return <Form.Item label="选择安装或共享目录中的模板" key={key}><TemplatePicker onSelect={item => bindRoot('template', item.root_id, 'read', item)} /><Typography.Text>{selected.config.template?.relative_path}</Typography.Text></Form.Item>;
              if (key === 'root_slot' || key.endsWith('_root_slot')) {
                const requirement = selectedSpec.requirements?.resources?.root_slots?.find(item => item.config_pointer === `/${key}`);
                const access = requirement?.access || (key.includes('publish') ? 'publish' : 'write');
                return <Form.Item label={schema.title || key.replace('_slot', '目录')} key={key}><Select aria-label={`目录 ${key}`} value={bindings.root_slots[selected.config[key]]?.root_id} options={roots.filter(root => access === 'read' || root.access_mode === access).map(root => ({ value: root.root_id, label: root.name }))} onChange={id => bindRoot(key, id, access)} /></Form.Item>;
              }
              if (key === 'credential_slot') return <Form.Item label="检务账号" key={key}><Select aria-label="检务账号" value={bindings.credential_slots[selected.config[key]]?.credential_id} options={credentials.map(item => ({ value: item.id, label: item.display_name || item.username || item.system_key }))} onChange={id => bindCredential(key, id)} /></Form.Item>;
              if (['query_ref', 'operation_ref'].includes(key)) { const choices = catalog.connectors.flatMap(item => item[key === 'query_ref' ? 'queries' : 'operations'] || []); return <Form.Item label={key === 'query_ref' ? '查询服务' : '操作服务'} key={key}><Select aria-label={key} value={selected.config[key]} options={choices.filter(item => item.ready !== false).map(item => ({ value: item[key], label: item.name || item[key] }))} onChange={value => editConfig(key, value)} /></Form.Item>; }
              return <Form.Item label={schema.title || key} key={key}>{key.endsWith('_schema') ? <SchemaEditor value={selected.config[key]} label="表单字段" onChange={value => editConfig(key, value)} /> : <ValueEditor schema={key === 'code' ? { ...schema, format: 'textarea' } : schema} value={selected.config[key]} label={`配置 ${key}`} onChange={value => editConfig(key, value)} />}</Form.Item>;
            })}
            {[...new Set([...Object.keys(schemas.input?.properties || {}), ...Object.keys(selected.input_mapping || {})])].map(key => <BindingEditor key={`${selected.id}:${key}`} label={key} value={selected.input_mapping?.[key]} schema={schemas.input?.properties?.[key]} variables={dataReferences} issue={referenceMessage(key)} onChange={value => { const mapping = { ...selected.input_mapping }; if (value === undefined) delete mapping[key]; else mapping[key] = value; editNode({ input_mapping: mapping }, `mapping:${selected.id}:${key}`); }} />)}
            {selected.type === 'data.python' && <Form.Item label="Python · main(inputs) → dict"><ValueEditor schema={{ type: 'string', format: 'textarea' }} value={selected.config.code} label="Python 代码" onChange={value => editConfig('code', value)} /></Form.Item>}
            {configProperties.output_schema && <Form.Item label="输出变量定义"><SchemaEditor value={selected.config.output_schema} label="输出字段" onChange={(schema, rename) => editDocument(value => { value.definition.nodes.find(item => item.id === selected.id).config.output_schema = schema; return rename ? rewriteOutputReferences(value, selected.id, rename.oldKey, rename.newKey) : value; })} /></Form.Item>}
          </Form>
          {selected.type === 'data.python' && <Collapse items={[{ key: 'test', label: 'Python 单节点试算', children: <>
            <ValueEditor schema={selected.config.input_schema} value={sampleInputs[selected.id] || {}} label="试算输入" onChange={value => setSampleInputs(previous => ({ ...previous, [selected.id]: value }))} />
            <Button loading={busy} onClick={async () => { setBusy(true); try { setPythonReport(await testPythonNodeV2({ config: selected.config, inputs: sampleInputs[selected.id] || {} })); } catch (cause) { setError(cause.message); } finally { setBusy(false); } }}>试算</Button>
            {pythonReport && <Alert type={pythonReport.passed ? 'success' : 'error'} message={pythonReport.passed ? `试算完成 · ${pythonReport.duration_ms} ms` : `${pythonReport.error?.message} ${pythonReport.error?.line ? `（第 ${pythonReport.error.line} 行）` : ''}`} description={<pre>{JSON.stringify(pythonReport.output, null, 2)}{pythonReport.logs}</pre>} />}
          </> }]} />}
          <Button danger style={{ marginTop: 16 }} onClick={remove}>删除步骤</Button>
        </>}
        {!selected && edge && <Form layout="vertical">
          <Form.Item label="连接名称"><Input value={edge.label || ''} onChange={event => editEdge({ label: event.target.value })} /></Form.Item>
          <Form.Item label="等待方式"><Select value={edge.join_policy} options={[{ value: 'all', label: '等待全部入边' }, { value: 'any', label: '任一入边即可' }]} onChange={join_policy => editEdge({ join_policy })} /></Form.Item>
          <Form.Item label="条件"><Select aria-label="条件模式" value={edge.condition === 'default' ? 'default' : edge.condition && typeof edge.condition === 'object' ? 'expression' : 'none'} options={[{ value: 'none', label: '无条件' }, { value: 'default', label: '其他情况' }, { value: 'expression', label: '字段判断' }]} onChange={mode => editEdge({ condition: mode === 'default' ? 'default' : mode === 'none' ? null : { path: '', operator: 'truthy' } })} /></Form.Item>
          {edge.condition && typeof edge.condition === 'object' && <><BindingEditor referenceOnly label="条件字段" value={edge.condition.path} onChange={path => editEdge({ condition: { ...edge.condition, path } })} variables={dataReferences} issue={issues.find(issue => issue.edgeId === edge.id)?.message} /><Select aria-label="条件运算符" value={edge.condition.operator} options={['truthy', 'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'contains'].map(value => ({ value, label: value }))} onChange={operator => editEdge({ condition: { ...edge.condition, operator } })} /><ValueEditor schema={typeof edge.condition.value === 'number' ? { type: 'number' } : { type: 'string' }} label="比较值" value={edge.condition.value} onChange={value => editEdge({ condition: { ...edge.condition, value } })} /></>}
          <Button danger style={{ marginTop: 12 }} onClick={remove}>删除连接</Button>
        </Form>}
        {!selected && !edge && <Typography.Text type="secondary">选择步骤配置参数；选中连接后可以插入节点。</Typography.Text>}
      </Card>
    </div>
    <Collapse items={[{ key: 'inputs', label: '流程输入与输出', children: <Space direction="vertical" style={{ width: '100%' }}><SchemaEditor label="流程输入字段" value={document.definition.input_schema} onChange={(schema, rename) => editDocument(value => { value.definition.input_schema = schema; return rename ? rewriteReferences(value, "$.inputs", rename.oldKey, rename.newKey) : value; })} /><SchemaEditor label="流程输出字段" value={document.definition.output_schema} onChange={schema => editDocument(value => { value.definition.output_schema = schema; return value; })} /></Space> }, { key: 'resources', label: '本地资源绑定', children: <Form layout="vertical">{(document.resources.root_slots || []).map(slot => <Form.Item key={slot.slot_id} label={slot.name}><Select value={bindings.root_slots[slot.slot_id]?.root_id} options={roots.map(root => ({ value: root.root_id, label: root.name }))} onChange={id => edit(value => { const root = roots.find(item => item.root_id === id); value.bindings.root_slots[slot.slot_id] = { root_id: id, revision: root.binding_revision || 1 }; return value; })} /></Form.Item>)}{(document.resources.credential_slots || []).map(slot => <Form.Item key={slot.slot_id} label={slot.name}><Select value={bindings.credential_slots[slot.slot_id]?.credential_id} options={credentials.map(item => ({ value: item.id, label: item.display_name || item.username || item.system_key }))} onChange={id => edit(value => { const credential = credentials.find(item => item.id === id); value.bindings.credential_slots[slot.slot_id] = { credential_id: id, revision: credential.revision }; return value; })} /></Form.Item>)}</Form> }, { key: 'advanced', label: '高级：JSON 与离线样例', children: <>
      <JsonEditor label="离线样例" value={document.fixtures || []} onValidity={validity} onChange={fixtures => editDocument(value => { value.fixtures = fixtures; return value; })} />
      <JsonEditor label="完整流程" value={document} onValidity={validity} onChange={value => { if (!value?.definition || !Array.isArray(value.definition.nodes) || !Array.isArray(value.definition.edges) || !value.release || !value.resources) throw new Error('流程结构不完整'); editDocument(() => value); }} />
      <Button disabled={busy || jsonInvalid || issues.length > 0} onClick={async () => { setBusy(true); try { setFixtureReport(await testWorkflowDesignerV2(document)); } catch (cause) { setError(cause.message); } finally { setBusy(false); } }}>运行离线样例</Button>
      {fixtureReport && <Alert type={fixtureReport.passed ? 'success' : 'warning'} message={fixtureReport.message || (fixtureReport.passed ? '离线样例通过' : '离线样例未通过')} description={<pre>{JSON.stringify(fixtureReport.items || fixtureReport.issues, null, 2)}</pre>} />}
    </> }]} />
  </div>;
}
