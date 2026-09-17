import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Button, Card, Descriptions, Input, Space, Spin, Tag, message } from 'antd';
import {
  activateWorkflowReplacementV2,
  getWorkflowReplacementV2,
  revertWorkflowReplacementV2,
} from '../../api/executionV2';

export default function WorkflowReplacementPanel({ workflowId, revisionKey }) {
  const navigate = useNavigate();
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setState(await getWorkflowReplacementV2(workflowId));
      setError(null);
    } catch (requestError) {
      setState(null);
      setError(requestError.message || '读取接替状态失败');
    }
  }, [workflowId]);

  useEffect(() => { load(); }, [load, revisionKey]);

  const switchWorkflow = async action => {
    if (!state?.source || !reason.trim()) return;
    setBusy(true);
    try {
      const payload = {
        expected_source_revision: state.source.draft_revision,
        expected_target_revision: state.target.draft_revision,
        expected_source_version: state.source.version_number,
        expected_target_version: state.target.version_number,
        reason: reason.trim(),
        ...(action === 'revert' ? { activation_audit_id: state.last_action?.id } : {}),
      };
      const perform = action === 'activate' ? activateWorkflowReplacementV2 : revertWorkflowReplacementV2;
      setState(await perform(workflowId, payload));
      setReason('');
      message.success(action === 'activate' ? '旧流程已归档，新流程已启用' : '已恢复旧流程原状态，新流程已停用');
    } catch (requestError) {
      message.error(requestError.message || '切换失败');
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="新旧流程接替" extra={<Button disabled={busy} onClick={load}>刷新接替状态</Button>}>
      {error ? <Alert type="error" showIcon message={error} /> : !state ? <Spin /> : (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Alert
            showIcon
            type={state.status === 'active' ? 'success' : 'info'}
            message={state.internal_acceptance_only ? '仅用于内部验收' : state.status === 'active' ? '新流程已接替' : state.status === 'reverted' ? '已回切旧流程' : '已发布，等待接替'}
            description={state.internal_acceptance_only ? '受控 Excel 候选不接替生产入口，请在独立验收环境完成执行验证。' : '接替会同时归档并停用旧流程、启用新流程。回切恢复旧流程原状态。已有运行和人工待办继续使用原版本。'}
          />
          <Descriptions size="small" column={2}>
            {['source', 'target'].map(key => {
              const item = state[key];
              return item && (
                <Descriptions.Item key={key} label={key === 'source' ? '旧流程' : '新流程'}>
                  <Space direction="vertical" size={4}>
                    <Button type="link" onClick={() => navigate(`/execution/workflows/${item.workflow_id}/start`)}>{item.name}</Button>
                    <span>{item.slug} · v{item.version_number} · rev {item.draft_revision}</span>
                    <Tag>{item.archived_at ? '已归档' : item.is_enabled ? '已启用' : '已停用'}</Tag>
                  </Space>
                </Descriptions.Item>
              );
            })}
          </Descriptions>
          <Input.TextArea aria-label="接替或回切原因" placeholder="填写本次接替或回切原因" maxLength={2000} value={reason} onChange={event => setReason(event.target.value)} />
          <Space>
            <Button type="primary" loading={busy} disabled={state.internal_acceptance_only || !state.source || state.status === 'active' || !reason.trim()} onClick={() => switchWorkflow('activate')}>归档旧流程并启用新流程</Button>
            <Button danger loading={busy} disabled={state.status !== 'active' || !reason.trim()} onClick={() => switchWorkflow('revert')}>回切旧流程</Button>
          </Space>
          {state.last_action && <span>最近操作审计编号：{state.last_action.id}</span>}
        </Space>
      )}
    </Card>
  );
}
