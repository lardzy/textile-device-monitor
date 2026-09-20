import { useEffect, useState } from 'react';
import { Alert, Button, Input, Select, Space } from 'antd';
import { getExecutionFileRoots } from '../../api/execution';
import { getExecutionTemplatesV2 } from '../../api/executionV2';

export default function TemplatePicker({ onSelect }) {
  const [roots, setRoots] = useState([]);
  const [rootId, setRootId] = useState('execution_templates');
  const [directory, setDirectory] = useState('');
  const [items, setItems] = useState([]);
  const [error, setError] = useState('');
  useEffect(() => { getExecutionFileRoots().then(setRoots).catch(err => setError(err.message)); }, []);
  const load = async () => {
    try { setItems(await getExecutionTemplatesV2({ root_id: rootId, directory })); setError(''); }
    catch (err) { setItems([]); setError(err.message || '模板目录无法读取'); }
  };
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Select aria-label="模板目录" value={rootId} options={roots.filter(root => root.is_available !== false).map(root => ({ value: root.root_id, label: root.name }))} onChange={value => { setRootId(value); setItems([]); }} />
    <Input aria-label="模板子目录" value={directory} placeholder="子目录（可留空）" onChange={event => { setDirectory(event.target.value); setItems([]); }} />
    <Button onClick={load}>读取模板目录</Button>
    {error && <Alert type="error" message={error} />}
    <Select aria-label="选择文件模板" value={null} placeholder="选择工作簿模板" options={items.map((item, index) => ({ value: index, label: item.name }))} onChange={index => onSelect(items[index])} />
  </Space>;
}
