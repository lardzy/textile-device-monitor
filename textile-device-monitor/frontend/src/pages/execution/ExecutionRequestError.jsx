import { useEffect, useRef } from 'react';
import { Alert, Typography } from 'antd';

const textOf = value => typeof value === 'string' ? value : '';

const nodeLabel = (path, definition) => {
  const match = /^\$\.nodes\[([^\]]+)\]/.exec(path);
  if (!match) return '';
  const nodes = Array.isArray(definition?.nodes) ? definition.nodes : [];
  const node = /^\d+$/.test(match[1])
    ? nodes[Number(match[1])]
    : nodes.find(item => item?.id === match[1]);
  return textOf(node?.name) || textOf(node?.id);
};

// Display only the API's diagnostic fields, never its request body or inputs.
export default function ExecutionRequestError({
  error,
  definition,
  workflowName,
  fallback = '操作失败，请重试',
  onClose,
}) {
  const containerRef = useRef(null);
  useEffect(() => {
    if (error) containerRef.current?.focus();
  }, [error]);
  if (!error) return null;
  const issues = Array.isArray(error.details?.issues)
    ? error.details.issues.filter(issue => textOf(issue?.message)) : [];
  const requestId = textOf(error.requestId);
  return (
    <div className="execution-request-error" tabIndex={-1} ref={containerRef} aria-label="操作失败详情">
      <Alert
        type="error"
        showIcon
        closable={Boolean(onClose)}
        onClose={onClose}
        message={textOf(error.message) || fallback}
        description={(
          <>
            {workflowName && <div>流程：{workflowName}</div>}
            {issues.length > 0 && (
              <ul className="execution-request-error__issues" aria-label="具体错误">
                {issues.map((issue, index) => {
                  const path = textOf(issue.path);
                  const label = nodeLabel(path, definition);
                  return (
                    <li key={index}>
                      {label && <strong>{label}： </strong>}{issue.message}
                      {path && <div><Typography.Text type="secondary">位置：{path}</Typography.Text></div>}
                    </li>
                  );
                })}
              </ul>
            )}
            {requestId && <div><Typography.Text type="secondary">请求编号：{requestId}</Typography.Text></div>}
          </>
        )}
      />
    </div>
  );
}
