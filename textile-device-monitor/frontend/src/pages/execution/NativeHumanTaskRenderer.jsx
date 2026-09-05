import {
  Alert,
  Checkbox,
  Descriptions,
  Form,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import SchemaFields from './SchemaFields';

const { Text } = Typography;

const itemId = item => String(item?.id || '');
const itemLabel = item => (
  item?.label
  || item?.name
  || item?.metadata?.name
  || item?.relative_path
  || itemId(item)
);

export default function NativeHumanTaskRenderer({
  renderer,
  rendererContract,
  form,
  schema,
  inputData,
  disabled,
}) {
  if (renderer.capability === 'human.form') {
    return <SchemaFields schema={schema} disabled={disabled} />;
  }

  if (renderer.capability === 'human.select') {
    const items = Array.isArray(inputData?.items) ? inputData.items : [];
    const selectedSchema = schema?.properties?.selected_ids || {};
    return (
      <>
        <Form.Item
          name="selected_ids"
          label={`候选项（${items.length}）`}
          rules={[{
            validator: (_, value) => {
              const count = Array.isArray(value) ? value.length : 0;
              const minimum = Number(selectedSchema.minItems || 0);
              const maximum = Number(selectedSchema.maxItems || items.length || 1);
              return count >= minimum && count <= maximum
                ? Promise.resolve()
                : Promise.reject(new Error(`请选择 ${minimum} 至 ${maximum} 项`));
            },
          }]}
        >
          <Checkbox.Group className="execution-candidate-list" disabled={disabled}>
            {items.map(item => (
              <Checkbox key={itemId(item)} value={itemId(item)}>
                <span className="execution-candidate-list__item">
                  <strong>{itemLabel(item)}</strong>
                  <small>{item?.relative_path || item?.metadata?.parent || itemId(item)}</small>
                </span>
              </Checkbox>
            ))}
          </Checkbox.Group>
        </Form.Item>
        {schema?.properties?.primary_id && (
          <Form.Item name="primary_id" label="主项（如节点要求）">
            <Select
              allowClear
              disabled={disabled}
              options={items.map(item => ({
                value: itemId(item),
                label: itemLabel(item),
              }))}
            />
          </Form.Item>
        )}
      </>
    );
  }

  if (renderer.capability === 'human.approval') {
    const subject = inputData?.subject || {};
    return (
      <>
        <Alert
          showIcon
          type="warning"
          message="批准决定将生成不可伪造、一次性使用的服务端回执"
          style={{ marginBottom: 12 }}
        />
        <Descriptions size="small" bordered column={1} style={{ marginBottom: 12 }}>
          <Descriptions.Item label="Subject 类型">
            {subject.type || '—'}
          </Descriptions.Item>
          <Descriptions.Item label="Subject 摘要">
            <Text code copyable>{subject.digest || '—'}</Text>
          </Descriptions.Item>
        </Descriptions>
        <SchemaFields schema={schema} disabled={disabled} />
      </>
    );
  }

  if (renderer.capability === 'human.decision') {
    return (
      <>
        <Alert
          showIcon
          type="info"
          message="此决定仅用于流程分支，不构成发布批准凭证"
          style={{ marginBottom: 12 }}
        />
        <SchemaFields schema={schema} disabled={disabled} />
      </>
    );
  }

  const payload = rendererContract?.payload || {};
  const conflicts = Array.isArray(payload.conflicts) ? payload.conflicts : [];
  return (
    <>
      <Alert
        showIcon
        type="warning"
        message={`目标目录存在 ${conflicts.length} 个同名文件`}
        description="该决定仅绑定下方计划摘要；Worker 恢复后会重新核对计划和源文件指纹。"
        style={{ marginBottom: 12 }}
      />
      <Descriptions size="small" bordered column={1} style={{ marginBottom: 12 }}>
        <Descriptions.Item label="目标目录">
          {payload.display_directory || payload.target_relative_dir || '—'}
        </Descriptions.Item>
        <Descriptions.Item label="计划摘要">
          <Text code copyable>{payload.plan_digest || '—'}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="冲突文件">
          <Space wrap>
            {conflicts.map(value => <Tag color="error" key={value}>{value}</Tag>)}
          </Space>
        </Descriptions.Item>
      </Descriptions>
      <SchemaFields schema={schema} disabled={disabled} />
    </>
  );
}
