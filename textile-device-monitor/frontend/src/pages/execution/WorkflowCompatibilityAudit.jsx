import { useState } from 'react';
import { Alert, Button, Card, Space, Table, Tag } from 'antd';
import { getExecutionCompatibilityAudit } from '../../api/executionV2';

export default function WorkflowCompatibilityAudit() {
  const [audit, setAudit] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = async () => {
    setBusy(true);
    try { setAudit(await getExecutionCompatibilityAudit()); setError(''); }
    catch (cause) { setError(cause.message || '引用审计失败'); }
    finally { setBusy(false); }
  };
  return <Card size="small" title="历史兼容引用" extra={<Button loading={busy} onClick={load}>检查引用</Button>}>
    {error && <Alert type="error" message={error} />}
    {audit ? <>
      <Space wrap><Tag>仍需兼容 {audit.required_count}</Tag><Tag color="success">可退场 {audit.retired_count}</Tag></Space>
      <p>{audit.policy}</p>
      {audit.unknown_contracts?.length > 0 && <Alert type="warning" message="存在旧版本契约，请保留对应冻结 Worker" />}
      <Table size="small" rowKey={item => `${item.type}@${item.type_version}`} dataSource={audit.items} pagination={{ pageSize: 8 }} columns={[
        { title: '节点', render: (_, item) => `${item.type}@${item.type_version}` },
        { title: '有效引用', dataIndex: 'active_reference_count' },
        { title: '历史引用', dataIndex: 'historical_reference_count' },
      ]} expandable={{ expandedRowRender: item => item.references.length ? <ul>{item.references.map((reference, index) => <li key={index}>{reference.kind} · {reference.id} · {reference.node_id}</li>)}</ul> : '无需保留在线兼容能力' }} />
    </> : <span>检查草稿、发布版本、待发布候选、运行和待办；历史数据继续保留。</span>}
  </Card>;
}
