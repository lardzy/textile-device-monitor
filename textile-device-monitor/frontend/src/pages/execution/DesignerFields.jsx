import { useEffect, useState } from 'react';
import { Button, Checkbox, Collapse, Form, Input, InputNumber, Select, Space, Switch, TreeSelect, Typography, message } from 'antd';
import { isReference } from './v2Designer';

const TYPES = ['string', 'number', 'integer', 'boolean', 'object', 'array'];
const LABELS = { string: '文本', number: '数字', integer: '整数', boolean: '是/否', object: '对象', array: '列表' };
export const objectSchema = () => ({ type: 'object', properties: {}, required: [], additionalProperties: false });
const typeOf = schema => Array.isArray(schema?.type) ? schema.type.find(type => type !== 'null') : schema?.type || 'string';
const defaultValue = schema => schema?.default ?? ({ object: {}, array: [], boolean: false, number: 0, integer: 0 }[typeOf(schema)] ?? '');
const typedSchema = type => type === 'object' ? objectSchema() : type === 'array' ? { type, items: { type: 'string' } } : { type };
const inferredSchema = value => ({ type: Array.isArray(value) ? 'array' : value === null ? 'string' : typeof value });

function FieldName({ name, onRename }) {
  const [text, setText] = useState(name);
  useEffect(() => setText(name), [name]);
  return <Input aria-label={`字段名称 ${name}`} style={{ width: 150 }} value={text} onChange={event => setText(event.target.value)} onBlur={() => {
    if (text !== name && /^[A-Za-z_][A-Za-z0-9_]*$/.test(text)) onRename(text);
    else if (text !== name) { setText(name); message.error('字段名称使用字母、数字和下划线，且不能以数字开头'); }
  }} />;
}

export function SchemaEditor({ value, onChange, label = '字段', prefix = '' }) {
  const schema = value?.type === 'object' ? value : objectSchema();
  const properties = schema.properties || {};
  const change = (key, next, rename) => onChange({ ...schema, properties: { ...properties, [key]: next } }, rename);
  return <div className="execution-schema-editor" aria-label={label}>
    {Object.entries(properties).map(([key, item]) => <div className="execution-schema-editor__field" key={key}>
      <Space wrap>
        <FieldName name={key} onRename={next => {
          if (Object.hasOwn(properties, next)) { message.error('字段名称已存在'); return; }
          const updated = Object.fromEntries(Object.entries(properties).map(([name, field]) => [name === key ? next : name, field]));
          onChange({ ...schema, properties: updated, required: (schema.required || []).map(name => name === key ? next : name) }, { oldKey: `${prefix}${key}`, newKey: `${prefix}${next}` });
        }} />
        <Input aria-label={`字段标题 ${key}`} placeholder="显示名称" style={{ width: 135 }} value={item.title || ''} onChange={event => change(key, { ...item, title: event.target.value })} />
        <Select aria-label={`字段类型 ${key}`} style={{ width: 92 }} value={typeOf(item)} options={TYPES.map(type => ({ value: type, label: LABELS[type] }))} onChange={type => change(key, { ...typedSchema(type), ...(item.title ? { title: item.title } : {}) })} />
        <Checkbox checked={(schema.required || []).includes(key)} onChange={event => onChange({ ...schema, required: event.target.checked ? [...new Set([...(schema.required || []), key])] : (schema.required || []).filter(name => name !== key) })}>必填</Checkbox>
        <Button danger type="text" aria-label={`删除字段 ${key}`} onClick={() => onChange({ ...schema, properties: Object.fromEntries(Object.entries(properties).filter(([name]) => name !== key)), required: (schema.required || []).filter(name => name !== key) })}>删除</Button>
      </Space>
      {typeOf(item) === 'object' && <Collapse ghost items={[{ key: 'children', label: '对象字段', children: <SchemaEditor value={item} onChange={(next, rename) => change(key, next, rename)} prefix={`${prefix}${key}.`} /> }]} />}
      {typeOf(item) === 'array' && <Space direction="vertical" style={{ margin: 8 }}>
        <Space>列表项目类型<Select aria-label={`列表项目类型 ${key}`} value={typeOf(item.items)} style={{ width: 100 }} options={TYPES.map(type => ({ value: type, label: LABELS[type] }))} onChange={type => change(key, { ...item, items: typedSchema(type) })} /></Space>
        {typeOf(item.items) === 'object' && <SchemaEditor value={item.items} onChange={(next, rename) => change(key, { ...item, items: next }, rename)} prefix={`${prefix}${key}.*.`} />}
      </Space>}
    </div>)}
    <Button size="small" onClick={() => {
      let index = Object.keys(properties).length + 1;
      while (Object.hasOwn(properties, `field_${index}`)) index += 1;
      change(`field_${index}`, { type: 'string' });
    }}>添加{label}</Button>
  </div>;
}

export function ValueEditor({ schema = {}, value, onChange, label, depth = 0, bindChildren = false, variables = [] }) {
  const type = typeOf(schema);
  const Child = bindChildren ? BindingEditor : ValueEditor;
  if (schema.enum) return <Select aria-label={label} value={value} allowClear options={schema.enum.map(item => ({ value: item, label: String(item) }))} onChange={onChange} />;
  if (type === 'boolean') return <Switch aria-label={label} checked={Boolean(value)} onChange={onChange} />;
  if (type === 'number' || type === 'integer') return <InputNumber aria-label={label} value={value} min={schema.minimum} max={schema.maximum} precision={type === 'integer' ? 0 : undefined} onChange={onChange} />;
  if (type === 'array' && depth < 8) return <Space direction="vertical" style={{ width: '100%' }}>
    {(Array.isArray(value) ? value : []).map((item, index) => <div key={index} className="execution-designer-array-row">
      <Child variables={variables} bindChildren={bindChildren} schema={schema.items || inferredSchema(item)} value={item} label={`${label} ${index + 1}`} depth={depth + 1} onChange={next => onChange(value.map((old, position) => position === index ? next : old))} />
      <Button size="small" danger aria-label={`删除 ${label} ${index + 1}`} onClick={() => onChange(value.filter((_, position) => position !== index))}>删除</Button>
    </div>)}
    <Button size="small" onClick={() => onChange([...(Array.isArray(value) ? value : []), defaultValue(schema.items || {})])}>添加项目</Button>
  </Space>;
  if (type === 'object' && depth < 8) {
    const fields = { ...(schema.properties || {}) };
    Object.entries(value || {}).forEach(([key, item]) => { fields[key] ||= inferredSchema(item); });
    return <div className="execution-designer-object">
      {Object.entries(fields).map(([key, field]) => <div key={key}>
        <Typography.Text type="secondary">{field.title || key}{schema.required?.includes(key) ? ' *' : ''}</Typography.Text>
        <Child variables={variables} bindChildren={bindChildren} schema={field} value={value?.[key]} label={`${label} ${key}`} depth={depth + 1} onChange={next => onChange({ ...(value || {}), [key]: next })} />
        {!schema.properties?.[key] && <Button size="small" type="text" danger onClick={() => onChange(Object.fromEntries(Object.entries(value || {}).filter(([name]) => name !== key)))}>删除字段</Button>}
      </div>)}
      {schema.additionalProperties !== false && <Input.Search aria-label={`添加属性 ${label}`} placeholder="新字段名称" enterButton="添加" onSearch={name => { if (name.trim() && !Object.hasOwn(fields, name)) onChange({ ...(value || {}), [name]: '' }); }} />}
    </div>;
  }
  const control = { 'aria-label': label, value: value ?? '', onChange: event => onChange(event.target.value) };
  return schema.format === 'textarea' ? <Input.TextArea {...control} autoSize={{ minRows: 8, maxRows: 24 }} spellCheck={false} style={{ fontFamily: 'monospace' }} /> : <Input {...control} />;
}

export function BindingEditor({ value, onChange, schema, label, variables = [], issue, referenceOnly = false, depth = 0 }) {
  const inferred = isReference(value) ? (value.startsWith('$.inputs') ? 'input' : 'upstream') : 'literal';
  const [mode, setMode] = useState(referenceOnly && inferred === 'literal' ? 'upstream' : inferred);
  useEffect(() => { if (isReference(value)) setMode(value.startsWith('$.inputs') ? 'input' : 'upstream'); }, [value]);
  const options = mode === 'input' ? variables.filter(item => item.value.startsWith('$.inputs')) : variables.filter(item => !item.value.startsWith('$.inputs'));
  return <Form.Item label={label} validateStatus={issue ? 'error' : ''} help={issue}>
    <Space.Compact style={{ width: '100%' }}>
      <Select aria-label={`${label} 来源方式`} style={{ width: 115, flexShrink: 0 }} value={mode} options={[...(!referenceOnly ? [{ value: 'literal', label: '固定值' }] : []), { value: 'input', label: '流程输入' }, { value: 'upstream', label: '上游输出' }]} onChange={next => { setMode(next); onChange(undefined); }} />
      {mode !== 'literal' && <TreeSelect aria-label={`输入 ${label}`} style={{ width: '100%' }} allowClear showSearch treeNodeFilterProp="title" treeData={options} value={isReference(value) ? value : undefined} placeholder="展开并选择字段" status={issue ? 'error' : ''} onChange={onChange} />}
    </Space.Compact>
    {mode === 'literal' && <ValueEditor bindChildren variables={variables} depth={depth} schema={schema} value={isReference(value) ? undefined : value} label={`输入 ${label}`} onChange={onChange} />}
    {issue && <Typography.Text type="danger">{String(value)}</Typography.Text>}
    {value !== undefined && <Button size="small" type="link" onClick={() => onChange(undefined)}>清除绑定</Button>}
  </Form.Item>;
}
