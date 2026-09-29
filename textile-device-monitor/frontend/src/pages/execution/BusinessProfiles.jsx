import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Collapse, Form, Input, InputNumber, List, Select, Space, Switch, Tag, Typography } from 'antd';
import { queryExecutionConnector } from '../../api/execution';
import { testPythonNodeV2 } from '../../api/executionV2';
import { ValueEditor } from './DesignerFields';
import TemplatePicker from './TemplatePicker';

export const profileSources = document => (document?.definition?.nodes || []).flatMap(node => (
  node.type === 'data.python' ? Object.entries(node.config?.input_schema?.properties || {})
    .filter(([, schema]) => schema['x-editor'] === 'business-profiles')
    .map(([key, schema]) => ({ node, key, schema })) : []
));

export default function BusinessProfiles({ document, onChange, onTemplateSelect }) {
  const sources = profileSources(document);
  const [sourceIndex, setSourceIndex] = useState(0), [selectedIndex, setSelectedIndex] = useState(0);
  const [number, setNumber] = useState(''), [preview, setPreview] = useState(null), [busy, setBusy] = useState(false);
  const [count, setCount] = useState(1), [error, setError] = useState('');
  const requestId = useRef(0);
  useEffect(() => () => { requestId.current += 1; }, []);
  useEffect(() => { requestId.current += 1; setBusy(false); setPreview(null); }, [document]);
  if (!sources.length) return <Alert type="info" message="此流程没有业务方案配置" description="可在 Python 输入变量的 Schema 中为方案列表设置 x-editor: business-profiles；列表与代码一起保存到流程 JSON。" />;
  const source = sources[sourceIndex] || sources[0];
  const profiles = Array.isArray(source.node.input_mapping[source.key]) ? source.node.input_mapping[source.key] : [];
  const selected = profiles[selectedIndex];
  const fields = source.schema.items.properties;
  const original = document.definition.nodes.find(node => node.id === fields.original_template_key?.['x-template-node']);
  const templateSchema = Object.values(fields.templates?.patternProperties || {})[0];
  const check = document.definition.nodes.find(node => node.id === templateSchema?.properties?.template_key?.['x-template-node']);
  const templateOptions = node => Object.entries(node?.config?.templates || {}).map(([key, ref]) => ({ value: key, label: `${key} · ${ref.relative_path}` }));
  const update = next => onChange(value => {
    value.definition.nodes.find(node => node.id === source.node.id).input_mapping[source.key] = next;
    return value;
  });
  const change = (key, value) => update(profiles.map((profile, index) => index === selectedIndex ? { ...profile, [key]: value } : profile));
  const create = copy => {
    let id = `profile-${profiles.length + 1}`;
    while (profiles.some(profile => profile.id === id)) id += '-new';
    const value = copy && selected ? structuredClone(selected) : {
      id, name: '新业务方案', enabled: false, matches: [{ project_number: '', method: '' }],
      record_title: '', original_template_key: '', templates: {}, target_directory: '',
      filename_pattern: '{number}{identity}{index}.{ext}', upload_fields: {},
      form_rules: { name_separator: '[;；\\r\\n]+', identity_separator: '[、,，;；\\r\\n]+', basis_separator: '[;；\\r\\n]+', judgements: ['符合', '不符合'], defaults: {} },
    };
    value.id = id; value.name = copy ? `${selected.name}（副本）` : value.name; value.enabled = false;
    update([...profiles, value]); setSelectedIndex(profiles.length);
  };
  const match = async () => {
    const token = ++requestId.current;
    setBusy(true); setError(''); setPreview(null);
    try {
      let snapshot;
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const response = await queryExecutionConnector({ query_ref: 'legacy_fibrecheck.task_snapshot.get@1', input: { inspection_number: number.trim(), refresh: attempt === 0 } });
        if (token !== requestId.current) return;
        const result = response.result;
        if (result.refresh_status === 'failed') throw new Error(result.error_message || '检务任务刷新失败');
        if (result.snapshot && !['queued', 'running', 'pending'].includes(result.refresh_status)) { snapshot = result.snapshot; break; }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      if (!snapshot) throw new Error('检务任务刷新尚未完成，请稍后再预览');
      const report = await testPythonNodeV2({ config: source.node.config, inputs: { ...source.node.input_mapping, snapshot } });
      if (token !== requestId.current) return;
      if (!report.passed) throw new Error(report.error?.message || '匹配代码执行失败');
      setPreview(report.output.projects || []);
    } catch (cause) { if (token === requestId.current) setError(cause.message); }
    finally { if (token === requestId.current) setBusy(false); }
  };
  const templateEditor = node => <Card size="small" title={node.name} key={node.id}>
    {Object.entries(node.config.templates || {}).map(([key, ref]) => <Collapse key={key} items={[{ key, label: `${key} · ${ref.relative_path}`, children: <TemplatePicker onSelect={item => onTemplateSelect(node.id, key, item)} /> }]} />)}
    <Input.Search aria-label={`新增模板键 ${node.id}`} placeholder="新增模板键" enterButton="添加" onSearch={key => {
      if (!key.trim() || Object.hasOwn(node.config.templates, key)) return;
      onChange(value => { value.definition.nodes.find(item => item.id === node.id).config.templates[key] = {}; return value; });
    }} />
    <Collapse ghost items={[{ key: 'layout', label: '公共字段映射与排版', children: <>
      {['fields', 'image_layout', 'number_formats'].filter(key => node.config[key] !== undefined).map(key => <Form.Item key={key} label={key}>
        <ValueEditor value={node.config[key]} label={`${node.name} ${key}`} onChange={next => onChange(value => { value.definition.nodes.find(item => item.id === node.id).config[key] = next; return value; })} />
      </Form.Item>)}
    </> }]} />
  </Card>;
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Alert type="info" showIcon message="方案和 Python 代码一起随流程发布；运行采用发布时的配置" description="项目编号与方法按组合匹配。唯一方案自动选定，多个匹配才需要选择。" />
    {sources.length > 1 && <Select aria-label="方案配置节点" value={sourceIndex} options={sources.map((item, index) => ({ value: index, label: item.node.name }))} onChange={index => { setSourceIndex(index); setSelectedIndex(0); }} />}
    <Space wrap><Button onClick={() => create(false)}>新增方案</Button><Button disabled={!selected} onClick={() => create(true)}>复制方案</Button><Button danger disabled={!selected} onClick={() => { update(profiles.filter((_, index) => index !== selectedIndex)); setSelectedIndex(0); }}>删除方案</Button></Space>
    <List size="small" dataSource={profiles} renderItem={(profile, index) => <List.Item actions={[<Button key="edit" type="link" onClick={() => setSelectedIndex(index)}>编辑方案</Button>]}><Space><Tag color={profile.enabled ? 'green' : 'default'}>{profile.enabled ? '启用' : '停用'}</Tag>{profile.name}<Typography.Text type="secondary">{profile.matches.map(rule => `${rule.project_number} / ${rule.method}`).join('；')}</Typography.Text></Space></List.Item>} />
    {selected && <Card size="small" title={selected.name}><Form layout="vertical">
      <Form.Item label="启用"><Switch aria-label="启用方案" checked={selected.enabled} onChange={value => change('enabled', value)} /></Form.Item>
      {['name', 'id', 'matches', 'record_title', 'target_directory', 'filename_pattern'].map(key => <Form.Item key={key} label={fields[key].title}><ValueEditor schema={fields[key]} value={selected[key]} label={`方案 ${key}`} onChange={value => change(key, value)} /></Form.Item>)}
      <Form.Item label="原始记录模板"><Select aria-label="方案原始记录模板" value={selected.original_template_key} options={templateOptions(original)} onChange={value => change('original_template_key', value)} /></Form.Item>
      <Typography.Paragraph>允许图片数量：{Object.keys(selected.templates).sort((a, b) => Number(a) - Number(b)).join('、') || '尚未配置'}</Typography.Paragraph>
      {Object.entries(selected.templates).map(([key, template]) => <Card size="small" key={key} title={`${key} 张图片`} extra={<Button danger type="text" onClick={() => change('templates', Object.fromEntries(Object.entries(selected.templates).filter(([countKey]) => countKey !== key)))}>删除数量</Button>}>
        <Form.Item label="登记工作簿模板"><Select aria-label={`登记模板 ${key}`} value={template.template_key} options={templateOptions(check)} onChange={value => change('templates', { ...selected.templates, [key]: { ...template, template_key: value } })} /></Form.Item>
        {['legacy_template_name', 'mapping_config_sha256'].map(field => <Form.Item key={field} label={templateSchema.properties[field].title}><Input aria-label={`${key} ${field}`} value={template[field]} onChange={event => change('templates', { ...selected.templates, [key]: { ...template, [field]: event.target.value } })} /></Form.Item>)}
      </Card>)}
      <Space><InputNumber aria-label="新增图片数量" min={1} max={100} value={count} onChange={setCount} /><Button disabled={!count || Boolean(selected.templates[count])} onClick={() => change('templates', { ...selected.templates, [count]: { template_key: '', legacy_template_name: '', mapping_config_sha256: '' } })}>添加图片数量</Button></Space>
      <Collapse style={{ marginTop: 16 }} items={[{ key: 'advanced', label: '表单默认值与上传字段', children: <>{['form_rules', 'upload_fields'].map(key => <Form.Item key={key} label={fields[key].title}><ValueEditor schema={fields[key]} value={selected[key]} label={`方案 ${key}`} onChange={value => change(key, value)} /></Form.Item>)}</> }]} />
    </Form></Card>}
    <Card size="small" title="只读匹配预览"><Space><Input aria-label="预览检验编号" placeholder="检验编号" value={number} onChange={event => { requestId.current += 1; setBusy(false); setPreview(null); setNumber(event.target.value); }} /><Button loading={busy} disabled={!number.trim()} onClick={match}>预览匹配</Button></Space>
      {error && <Alert type="error" message={error} />}
      {preview && <Alert type={preview.length ? 'success' : 'warning'} message={preview.length === 1 ? '唯一匹配，运行时自动采用' : preview.length ? `${preview.length} 个匹配，运行时选择` : '没有匹配的已启用方案'} description={preview.map(item => <div key={item.id}>{item.label}</div>)} />}
    </Card>
    <Collapse items={[{ key: 'templates', label: '安装或共享目录中的模板与公共排版', children: [original, check].filter(Boolean).map(templateEditor) }]} />
  </Space>;
}
