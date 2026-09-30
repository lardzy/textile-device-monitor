import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Form,
  Input,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import SchemaFields from './SchemaFields';
import ExecutionResultFiles from './ExecutionResultFiles';
import ExecutionImageSelector from './ExecutionImageSelector';
import { executionIndexedImagePreviewUrl } from '../../api/execution';

const { Text } = Typography;

const itemId = item => String(item?.id || '');
const itemLabel = item => (
  item?.label
  || item?.name
  || item?.metadata?.name
  || item?.relative_path
  || itemId(item)
);

function ResultFileSelection({ value = [], onChange, items, form, disabled, maximum }) {
  const primaryId = Form.useWatch('primary_id', form);
  return (
    <ExecutionResultFiles
      files={items}
      selectable
      selectedIds={value}
      primaryId={primaryId}
      onSelectedIdsChange={onChange}
      onPrimaryIdChange={id => form.setFieldValue('primary_id', id)}
      selectionMode={maximum === 1 ? 'single' : 'multiple'}
      disabled={disabled}
    />
  );
}

export function ImageSelection({ value = [], onChange, items, form, disabled, maximum }) {
  const [directory, setDirectory] = useState(null);
  const [query, setQuery] = useState('');
  const images = useMemo(() => items.map(item => ({
    ...item.metadata,
    id: item.id,
    relative_path: item.relative_path,
    name: item.label || item.metadata?.name,
    preview_url: executionIndexedImagePreviewUrl(item.id),
  })), [items]);
  const parentOf = image => String(image.relative_path || '').split('/').slice(0, -1).join('/');
  const directories = [...new Set(images.map(parentOf))];
  const move = (index, direction) => {
    const next = [...value];
    [next[index], next[index + direction]] = [next[index + direction], next[index]];
    onChange?.(next);
    form?.setFieldValue('primary_id', next[0] || null);
  };
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Input aria-label="筛选图片文件名" placeholder="筛选文件名，例如 H_" value={query} onChange={event => setQuery(event.target.value)} disabled={disabled} />
      <Select
        aria-label="筛选图片目录"
        placeholder="全部图片目录"
        allowClear
        showSearch
        disabled={disabled}
        value={directory}
        onChange={setDirectory}
        style={{ width: '100%' }}
        options={directories.map(path => ({ value: path, label: path || '根目录' }))}
      />
    <ExecutionImageSelector
      images={images.filter(image => (!directory || parentOf(image) === directory) && String(image.name || image.relative_path).toLowerCase().includes(query.toLowerCase()))}
      selectedImageIds={value}
      onSelectedImageIdsChange={onChange}
      onPrimaryImageIdChange={id => form?.setFieldValue('primary_id', id)}
      disabled={disabled}
      maxImages={maximum}
    />
      {value.map((id, index) => (
        <Space key={id}>
          <Text>{index + 1}. {items.find(item => item.id === id)?.label || id}</Text>
          <Button size="small" disabled={disabled || index === 0} onClick={() => move(index, -1)}>前移</Button>
          <Button size="small" disabled={disabled || index === value.length - 1} onClick={() => move(index, 1)}>后移</Button>
        </Space>
      ))}
    </Space>
  );
}

export default function NativeHumanTaskRenderer({
  renderer,
  rendererContract,
  form,
  schema,
  inputData,
  disabled,
}) {
  if (renderer.capability === 'human.group_select') {
    return <GroupSelection inputData={inputData} form={form} disabled={disabled} config={rendererContract.payload || {}} />;
  }
  if (renderer.capability === 'human.form') {
    return <SchemaFields schema={schema} disabled={disabled} />;
  }

  if (renderer.capability === 'human.select') {
    const items = Array.isArray(inputData?.items) ? inputData.items : [];
    const selectedSchema = schema?.properties?.selected_ids || {};
    const allowedCounts = inputData?.allowed_selected_counts;
    const showResults = items.length > 0 && items.every(item => item?.metadata?.presentation === 'result_file');
    const showImages = items.length > 0 && items.every(item => item.kind === 'image');
    return (
      <>
        {Array.isArray(allowedCounts) && <Alert type="info" showIcon message={`可选择 ${allowedCounts.join('、')} 项；顺序决定图片排版和文件编号`} style={{ marginBottom: 12 }} />}
        <Form.Item
          name="selected_ids"
          label={`候选项（${items.length}）`}
          rules={[{
            validator: (_, value) => {
              const count = Array.isArray(value) ? value.length : 0;
              const minimum = Number(selectedSchema.minItems || 0);
              const maximum = Number(selectedSchema.maxItems || items.length || 1);
              if (Array.isArray(allowedCounts) && !allowedCounts.includes(count)) {
                return Promise.reject(new Error(`请选择 ${allowedCounts.join('、')} 项`));
              }
              return count >= minimum && count <= maximum
                ? Promise.resolve()
                : Promise.reject(new Error(`请选择 ${minimum} 至 ${maximum} 项`));
            },
          }]}
        >
          {showImages ? (
            <ImageSelection
              items={items}
              form={form}
              disabled={disabled}
              maximum={selectedSchema.maxItems}
            />
          ) : showResults ? (
            <ResultFileSelection
              items={items}
              form={form}
              disabled={disabled}
              maximum={selectedSchema.maxItems}
            />
          ) : (
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
          )}
        </Form.Item>
        {schema?.properties?.primary_id && (
          <Form.Item name="primary_id" label="主项（如节点要求）" hidden={showResults || showImages}>
            {showResults || showImages ? <Input /> : <Select
              allowClear
              disabled={disabled}
              options={items.map(item => ({
                value: itemId(item),
                label: itemLabel(item),
              }))}
            />}
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

function GroupSelection({ inputData, form, disabled, config }) {
  const values = Form.useWatch('groups', form) || [];
  const groups = inputData.groups || [], items = inputData.items || [];
  return <Space direction="vertical" style={{ width: '100%' }}>
    {inputData.form_schema && <Card size="small" title={inputData.form_schema.title || '各组共用信息'}>
      {Object.entries(inputData.context || {}).map(([label, value]) => <Typography.Paragraph key={label} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <Text type="secondary">{label}：</Text>{typeof value === 'string' ? value : JSON.stringify(value)}
      </Typography.Paragraph>)}
      {inputData.form_schema.description && <Typography.Paragraph type="secondary">{inputData.form_schema.description}</Typography.Paragraph>}
      <SchemaFields schema={inputData.form_schema} namePrefix="form_data" disabled={disabled} />
    </Card>}
    <Alert showIcon type="info" message="按样品识别分组选图，组内顺序决定排版和共享文件编号" description={config.require_all_groups === false ? '可留空暂不处理的组；已完成的组不会因其他组重试而重复提交。' : '请为每组选择图片；已完成的组不会因其他组重试而重复提交。'} />
    {groups.map((group, index) => <Card size="small" title={group.label} key={group.id}>
      <Typography.Paragraph type="secondary">可选择 {group.allowed_selected_counts.join('、')} 张；已选 {values[index]?.selected_ids?.length || 0} 张</Typography.Paragraph>
      <Form.Item name={['groups', index, 'id']} initialValue={group.id} hidden><Input /></Form.Item>
      <Form.Item name={['groups', index, 'selected_ids']} initialValue={[]} rules={[{ validator: async (_, value = []) => {
        if (!value.length && config.require_all_groups === false) return;
        if (!group.allowed_selected_counts.includes(value.length)) throw new Error(`${group.label}请选择 ${group.allowed_selected_counts.join('、')} 张`);
        const others = form.getFieldValue('groups') || [];
        if (!config.allow_item_reuse && others.some((other, at) => at !== index && other?.selected_ids?.some(id => value.includes(id)))) throw new Error('同一图片不能分配给多个部位');
      } }]}>
        {items.every(item => item.kind === 'image')
          ? <ImageSelection items={items} disabled={disabled} maximum={Math.max(...group.allowed_selected_counts)} />
          : <Checkbox.Group disabled={disabled}>{items.map(item => <Checkbox key={item.id} value={item.id}>{itemLabel(item)}</Checkbox>)}</Checkbox.Group>}
      </Form.Item>
    </Card>)}
  </Space>;
}
