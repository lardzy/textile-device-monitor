import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  addEdge,
  Panel,
  useEdgesState,
  useNodesState,
} from '@xyflow/react';
import {
  Alert,
  Button,
  Collapse,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CloudUploadOutlined,
  CodeOutlined,
  DownloadOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SaveOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  exportWorkflow,
  getExecutionNodeTypes,
  getExecutionWorkflow,
  getWorkflowVersions,
  importWorkflow,
  publishWorkflow,
  saveWorkflowDraft,
  testWorkflow,
  validateWorkflow,
} from '../../api/execution';
import {
  createWorkflowNode,
  definitionFromCanvas,
  normalizeWorkflowDefinition,
  validateWorkflowDefinition,
} from '../../utils/executionWorkflow';
import { createClientUuid } from '../../utils/clientId';
import { useExecutionAuth } from './ExecutionAuthContext';
import ExecutionChrome from './ExecutionChrome';
import SchemaFields from './SchemaFields';
import WorkflowCanvas from './WorkflowCanvas';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

const stableStringify = value => JSON.stringify(value);

const getWorkflowIdentity = payload => payload?.workflow || payload;

const responseDefinition = (payload, workflow) =>
  payload?.draft?.definition
  || payload?.draft_definition
  || workflow?.draft_definition
  || payload?.definition
  || workflow?.definition;

const responseRevision = (payload, workflow) =>
  payload?.draft?.revision
  ?? payload?.draft_revision
  ?? workflow?.draft_revision
  ?? payload?.revision
  ?? 0;

const schemaDefaults = schema => Object.fromEntries(
  Object.entries(schema?.properties || {})
    .filter(([, field]) => field.default !== undefined)
    .map(([name, field]) => [name, field.default]),
);

const testInputSchemaOf = definition => ({
  type: 'object',
  ...(definition?.input_schema || {}),
  properties: {
    inspection_number: {
      type: 'string',
      title: '测试检验编号',
      description: '仅用于标识本次测试运行，不会触发真实外部系统副作用。',
    },
    ...(definition?.input_schema?.properties || {}),
  },
  required: [...new Set([
    'inspection_number',
    ...(definition?.input_schema?.required || []),
  ])],
});

const downloadJson = (value, filename) => {
  const blob = value instanceof Blob
    ? value
    : new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
};

const NODE_CATEGORY_TONES = {
  基础: 'blue',
  人工: 'orange',
  文件: 'cyan',
  Excel: 'red',
  控制: 'gold',
  连接器: 'default',
};

const normalizeNodeType = (item) => {
  const category = item?.category || '其他';
  return {
    ...item,
    type: item?.type,
    version: Number(item?.version || 1),
    label: item?.name || item?.type,
    group: category,
    description: item?.description || '执行节点',
    configSchema: item?.config_schema || {},
    tone: NODE_CATEGORY_TONES[category] || 'purple',
  };
};

const NodePalette = ({
  nodeTypes,
  loading,
  error,
  onRetry,
  onDragStart,
}) => {
  const groups = useMemo(() => {
    const result = new Map();
    nodeTypes.forEach((node) => {
      if (!result.has(node.group)) {
        result.set(node.group, []);
      }
      result.get(node.group).push(node);
    });
    return [...result.entries()];
  }, [nodeTypes]);

  return (
    <div className="execution-node-palette">
      <Text className="execution-eyebrow">NODE LIBRARY</Text>
      <Title level={4}>节点库</Title>
      <Paragraph type="secondary">拖入画布后连接节点，运行首版仅支持无环流程。</Paragraph>
      {loading ? (
        <div className="execution-node-palette__state">
          <Spin size="small" />
          <Text type="secondary">正在读取服务端节点注册表…</Text>
        </div>
      ) : error ? (
        <Alert
          showIcon
          type="error"
          message="节点类型加载失败"
          description={error.message || '无法确认节点类型与版本，已停止新增节点。'}
          action={<Button size="small" onClick={onRetry}>重新加载</Button>}
        />
      ) : groups.length === 0 ? (
        <Alert
          showIcon
          type="warning"
          message="服务端未注册可用节点"
          description="当前草稿仍可查看，但不能新增节点。"
          action={<Button size="small" onClick={onRetry}>重新加载</Button>}
        />
      ) : (
        <Collapse
          ghost
          defaultActiveKey={groups.map(([group]) => group)}
          items={groups.map(([group, nodes]) => ({
            key: group,
            label: <strong>{group}</strong>,
            children: (
              <div className="execution-node-palette__group">
                {nodes.map(node => (
                  <button
                    key={`${node.type}@${node.version}`}
                    type="button"
                    className={`execution-node-palette__item is-${node.tone}`}
                    draggable
                    onDragStart={event => onDragStart(event, node)}
                    title={`${node.type}@${node.version}`}
                  >
                    <span>{node.label}</span>
                    <small>v{node.version} · {node.description}</small>
                  </button>
                ))}
              </div>
            ),
          }))}
        />
      )}
    </div>
  );
};

function VariablesEditor({ definition, onChange }) {
  const [scope, setScope] = useState('input');
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const schemaKey = scope === 'input' ? 'input_schema' : 'global_schema';
  const schema = definition[schemaKey] || { type: 'object', properties: {} };
  const entries = Object.entries(schema.properties || {});
  const required = schema.required || [];

  const addField = async () => {
    const values = await form.validateFields();
    const nextProperties = {
      ...(schema.properties || {}),
      [values.name]: {
        type: values.type,
        title: values.title,
        description: values.description || undefined,
      },
    };
    const nextRequired = values.required
      ? [...new Set([...required, values.name])]
      : required.filter(name => name !== values.name);
    onChange({
      ...definition,
      [schemaKey]: {
        ...schema,
        type: 'object',
        properties: nextProperties,
        required: nextRequired,
      },
    });
    form.resetFields();
    setModalOpen(false);
  };

  const removeField = (name) => {
    const nextProperties = { ...(schema.properties || {}) };
    delete nextProperties[name];
    onChange({
      ...definition,
      [schemaKey]: {
        ...schema,
        properties: nextProperties,
        required: required.filter(item => item !== name),
      },
    });
  };

  return (
    <section className="execution-designer-variables">
      <div className="execution-designer-variables__header">
        <Segmented
          size="small"
          value={scope}
          onChange={setScope}
          options={[
            { label: '输入字段', value: 'input' },
            { label: '全局变量', value: 'global' },
          ]}
        />
        <Button size="small" type="text" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新增
        </Button>
      </div>
      <List
        size="small"
        locale={{ emptyText: scope === 'input' ? '暂无输入字段' : '暂无全局变量' }}
        dataSource={entries}
        renderItem={([name, field]) => (
          <List.Item
            actions={[
              <Popconfirm
                key="remove"
                title="移除此字段？"
                onConfirm={() => removeField(name)}
              >
                <Button type="link" danger size="small">移除</Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={field.title || name}
              description={`${name} · ${field.type}${required.includes(name) ? ' · 必填' : ''}`}
            />
          </List.Item>
        )}
      />
      <Modal
        title={scope === 'input' ? '新增输入字段' : '新增全局变量'}
        open={modalOpen}
        okText="添加"
        cancelText="取消"
        onOk={addField}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={{ type: 'string', required: false }}>
          <Form.Item
            name="name"
            label="变量名"
            rules={[
              { required: true, message: '请输入变量名' },
              { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '仅支持字母、数字和下划线，且不能以数字开头' },
              {
                validator: (_, value) => schema.properties?.[value]
                  ? Promise.reject(new Error('变量名已存在'))
                  : Promise.resolve(),
              },
            ]}
          >
            <Input placeholder="例如 reviewer_result" />
          </Form.Item>
          <Form.Item name="title" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input placeholder="例如 复核结果" />
          </Form.Item>
          <Form.Item name="type" label="数据类型">
            <Select
              options={[
                { value: 'string', label: '文本' },
                { value: 'number', label: '数值' },
                { value: 'integer', label: '整数' },
                { value: 'boolean', label: '是/否' },
                { value: 'array', label: '列表' },
                { value: 'object', label: '对象' },
              ]}
            />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={2} />
          </Form.Item>
          {scope === 'input' && (
            <Form.Item name="required" label="是否必填">
              <Select
                options={[
                  { value: false, label: '选填' },
                  { value: true, label: '必填' },
                ]}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </section>
  );
}

const ROOT_ACCESS_OPTIONS = [
  { value: 'read', label: '只读' },
  { value: 'write', label: '可写' },
  { value: 'publish', label: '发布' },
];

const CREDENTIAL_SYSTEM_OPTIONS = [
  { value: 'legacy_inspection', label: '旧检务系统' },
  { value: 'new_inspection', label: '新检务系统' },
];

const ROOT_ACCESS_LABELS = Object.fromEntries(
  ROOT_ACCESS_OPTIONS.map(option => [option.value, option.label]),
);

const CREDENTIAL_SYSTEM_LABELS = Object.fromEntries(
  CREDENTIAL_SYSTEM_OPTIONS.map(option => [option.value, option.label]),
);

function ResourceSlotsEditor({ definition, onChange }) {
  const [scope, setScope] = useState('root');
  const [modalOpen, setModalOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState(null);
  const [form] = Form.useForm();

  const isRoot = scope === 'root';
  const definitionKey = isRoot ? 'root_slots' : 'credential_slots';
  const slots = Array.isArray(definition[definitionKey]) ? definition[definitionKey] : [];

  const openEditor = (index = null) => {
    setEditingIndex(index);
    const current = index === null ? null : slots[index];
    form.setFieldsValue(isRoot
      ? {
        name: current?.name,
        root_id: current?.root_id,
        access: current?.access || 'read',
      }
      : {
        name: current?.name,
        system_key: current?.system_key || 'legacy_inspection',
        required: current?.required ?? true,
      });
    setModalOpen(true);
  };

  const saveSlot = async () => {
    const values = await form.validateFields();
    const nextSlot = isRoot
      ? {
        name: values.name.trim(),
        root_id: values.root_id.trim(),
        access: values.access,
      }
      : {
        name: values.name.trim(),
        system_key: values.system_key,
        required: values.required,
      };
    const nextSlots = [...slots];
    if (editingIndex === null) {
      nextSlots.push(nextSlot);
    } else {
      nextSlots[editingIndex] = nextSlot;
    }
    onChange({
      ...definition,
      [definitionKey]: nextSlots,
    });
    setModalOpen(false);
    setEditingIndex(null);
    form.resetFields();
  };

  const removeSlot = (index) => {
    onChange({
      ...definition,
      [definitionKey]: slots.filter((_, slotIndex) => slotIndex !== index),
    });
  };

  const validateUnique = field => (_, value) => {
    const normalized = value?.trim();
    if (!normalized) {
      return Promise.resolve();
    }
    const duplicated = slots.some((slot, index) => (
      index !== editingIndex && slot?.[field] === normalized
    ));
    return duplicated
      ? Promise.reject(new Error(field === 'root_id' ? '数据根标识已存在' : '槽位名称已存在'))
      : Promise.resolve();
  };

  return (
    <section className="execution-resource-slots" aria-label="资源槽位">
      <div className="execution-resource-slots__title">
        <div>
          <Text strong>资源槽位</Text>
          <div className="execution-resource-slots__hint">
            声明流程所需的数据根与系统凭据
          </div>
        </div>
        <Button
          size="small"
          type="text"
          aria-label={`新增${isRoot ? '数据根' : '凭据'}槽位`}
          aria-expanded={modalOpen}
          icon={<PlusOutlined />}
          onClick={() => openEditor()}
        >
          新增
        </Button>
      </div>
      <Segmented
        block
        size="small"
        value={scope}
        onChange={(value) => {
          setScope(value);
          setEditingIndex(null);
          setModalOpen(false);
          form.resetFields();
        }}
        options={[
          { label: `数据根 ${definition.root_slots?.length || 0}`, value: 'root' },
          { label: `系统凭据 ${definition.credential_slots?.length || 0}`, value: 'credential' },
        ]}
      />
      <List
        className="execution-resource-slots__list"
        size="small"
        locale={{ emptyText: isRoot ? '暂无数据根槽位' : '暂无凭据槽位' }}
        dataSource={slots}
        renderItem={(slot, index) => (
          <List.Item
            actions={[
              <Button key="edit" type="link" size="small" onClick={() => openEditor(index)}>
                编辑
              </Button>,
              <Popconfirm
                key="remove"
                title={`移除槽位“${slot.name}”？`}
                description="引用此槽位的节点可能无法通过流程校验。"
                onConfirm={() => removeSlot(index)}
              >
                <Button type="link" danger size="small">移除</Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={slot.name}
              description={isRoot
                ? `${slot.root_id} · ${ROOT_ACCESS_LABELS[slot.access] || slot.access}`
                : `${CREDENTIAL_SYSTEM_LABELS[slot.system_key] || slot.system_key} · ${slot.required === false ? '选填' : '必填'}`}
            />
          </List.Item>
        )}
      />
      <Modal
        title={`${editingIndex === null ? '新增' : '编辑'}${isRoot ? '数据根槽位' : '凭据槽位'}`}
        open={modalOpen}
        okText={editingIndex === null ? '添加' : '保存'}
        cancelText="取消"
        onOk={saveSlot}
        onCancel={() => {
          setModalOpen(false);
          setEditingIndex(null);
          form.resetFields();
        }}
        destroyOnHidden
      >
        <Alert
          showIcon
          type="info"
          message={isRoot
            ? '这里只引用管理员配置的数据根标识，不填写绝对路径。'
            : '这里只声明凭据用途，不会把账号或密码写入工作流。'}
          style={{ marginBottom: 16 }}
        />
        <Form
          form={form}
          layout="vertical"
          initialValues={isRoot
            ? { access: 'read' }
            : { system_key: 'legacy_inspection', required: true }}
        >
          <Form.Item
            name="name"
            label="槽位名称"
            extra="供节点输入映射引用，例如 source_records"
            rules={[
              { required: true, whitespace: true, message: '请输入槽位名称' },
              { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: '仅支持字母、数字和下划线，且不能以数字开头' },
              { validator: validateUnique('name') },
            ]}
          >
            <Input placeholder={isRoot ? '例如 source_records' : '例如 inspection_account'} />
          </Form.Item>
          {isRoot ? (
            <>
              <Form.Item
                name="root_id"
                label="数据根标识"
                extra="必须对应部署环境中已配置的数据根"
                rules={[
                  { required: true, whitespace: true, message: '请输入数据根标识' },
                  { pattern: /^[a-z][a-z0-9_-]{0,63}$/, message: '需以小写字母开头，仅支持小写字母、数字、下划线和连字符' },
                  { validator: validateUnique('root_id') },
                ]}
              >
                <Input placeholder="例如 special_wool_records" />
              </Form.Item>
              <Form.Item name="access" label="访问权限" rules={[{ required: true }]}>
                <Select options={ROOT_ACCESS_OPTIONS} />
              </Form.Item>
            </>
          ) : (
            <>
              <Form.Item name="system_key" label="外部系统" rules={[{ required: true }]}>
                <Select options={CREDENTIAL_SYSTEM_OPTIONS} />
              </Form.Item>
              <Form.Item name="required" label="运行时要求" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: true, label: '必填：未绑定凭据时禁止运行' },
                    { value: false, label: '选填：未绑定时允许继续' },
                  ]}
                />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
    </section>
  );
}

function NodeInspector({
  node,
  nodeDefinition,
  onUpdate,
  onPendingChange,
}) {
  const [configText, setConfigText] = useState('{}');
  const [mappingText, setMappingText] = useState('{}');
  const [jsonError, setJsonError] = useState(null);

  useEffect(() => {
    setConfigText(JSON.stringify(node?.data?.config || {}, null, 2));
    setMappingText(JSON.stringify(node?.data?.inputMapping || {}, null, 2));
    setJsonError(null);
    onPendingChange?.({ dirty: false, error: null });
  }, [node?.id, node?.data?.config, node?.data?.inputMapping, onPendingChange]);

  if (!node) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="选择一个节点以编辑配置"
      />
    );
  }

  const applyJson = ({
    nextConfigText = configText,
    nextMappingText = mappingText,
    notify = true,
  } = {}) => {
    try {
      const config = JSON.parse(nextConfigText || '{}');
      const inputMapping = JSON.parse(nextMappingText || '{}');
      if (
        !config || typeof config !== 'object' || Array.isArray(config)
        || !inputMapping || typeof inputMapping !== 'object' || Array.isArray(inputMapping)
      ) {
        throw new Error('节点参数和输入映射必须是 JSON 对象');
      }
      onUpdate({
        ...node.data,
        config,
        inputMapping,
      });
      setJsonError(null);
      onPendingChange?.({ dirty: false, error: null });
      if (notify) {
        message.success('节点配置已应用');
      }
      return true;
    } catch (error) {
      const nextError = `JSON 格式错误：${error.message}`;
      setJsonError(nextError);
      onPendingChange?.({ dirty: true, error: nextError });
      return false;
    }
  };

  return (
    <div className="execution-node-inspector">
      <div className="execution-node-inspector__title">
        <Text className="execution-eyebrow">NODE SETTINGS</Text>
        <Title level={4}>节点配置</Title>
        <Tag>{node.data.nodeType}@v{node.data.typeVersion}</Tag>
      </div>
      {nodeDefinition?.description && (
        <Paragraph className="execution-node-inspector__description" type="secondary">
          {nodeDefinition.description}
        </Paragraph>
      )}
      {Object.keys(nodeDefinition?.configSchema?.properties || {}).length > 0 && (
        <div className="execution-node-inspector__schema">
          <Text type="secondary">服务端参数约束</Text>
          <Space size={[4, 4]} wrap>
            {Object.keys(nodeDefinition.configSchema.properties).map(name => (
              <Tag key={name}>
                {name}{nodeDefinition.configSchema.required?.includes(name) ? ' *' : ''}
              </Tag>
            ))}
          </Space>
        </div>
      )}
      <Form layout="vertical">
        <Form.Item label="节点名称" required>
          <Input
            aria-label="节点名称"
            value={node.data.label}
            onChange={event => onUpdate({ ...node.data, label: event.target.value })}
          />
        </Form.Item>
        <Form.Item
          label="草稿状态"
          tooltip="停放节点仍保留在草稿画布中，但发布版本和测试运行会忽略该节点及其相邻连线。"
        >
          <Switch
            checked={!node.data.disabled}
            checkedChildren="参与流程"
            unCheckedChildren="已停放"
            onChange={enabled => onUpdate({ ...node.data, disabled: !enabled })}
          />
        </Form.Item>
        <Form.Item
          label="节点参数（JSON）"
          tooltip="仅保存节点业务参数；路径必须使用 root_id 与 relative_path。"
        >
          <Input.TextArea
            aria-label="节点参数 JSON"
            value={configText}
            onChange={(event) => {
              const value = event.target.value;
              setConfigText(value);
              applyJson({ nextConfigText: value, notify: false });
            }}
            rows={9}
            className="execution-json-editor"
            spellCheck={false}
          />
        </Form.Item>
        <Form.Item
          label="输入映射（JSON）"
          tooltip="键为节点输入名，值为变量引用或上游节点输出引用。"
        >
          <Input.TextArea
            aria-label="输入映射 JSON"
            value={mappingText}
            onChange={(event) => {
              const value = event.target.value;
              setMappingText(value);
              applyJson({ nextMappingText: value, notify: false });
            }}
            rows={7}
            className="execution-json-editor"
            spellCheck={false}
          />
        </Form.Item>
        {jsonError && <Alert showIcon type="error" message={jsonError} />}
        <Button block icon={<CodeOutlined />} onClick={() => applyJson()}>校验并应用配置</Button>
      </Form>
      <Alert
        className="execution-node-inspector__notice"
        showIcon
        type="info"
        message="首版不允许删除节点"
        description="暂不使用的节点请切换为“已停放”；配置和连线仍保留，恢复后可继续编辑。"
      />
    </div>
  );
}

function EdgeInspector({
  edge,
  onUpdate,
  onRemove,
  onPendingChange,
}) {
  const [conditionText, setConditionText] = useState('');
  const [error, setError] = useState(null);

  useEffect(() => {
    setConditionText(
      edge?.data?.condition || edge?.condition
        ? JSON.stringify(edge.data?.condition || edge.condition, null, 2)
        : '',
    );
    setError(null);
    onPendingChange?.({ dirty: false, error: null });
  }, [edge?.id, edge?.data?.condition, edge?.condition, onPendingChange]);

  if (!edge) {
    return null;
  }

  const apply = ({ nextText = conditionText, notify = true } = {}) => {
    try {
      const condition = nextText.trim() ? JSON.parse(nextText) : null;
      if (condition !== null && (
        typeof condition !== 'object' || Array.isArray(condition)
      )) {
        throw new Error('条件表达式必须是 JSON 对象');
      }
      onUpdate({
        ...edge,
        label: condition ? '条件' : undefined,
        data: {
          ...(edge.data || {}),
          condition,
          joinPolicy: edge.data?.joinPolicy || edge.join_policy || 'all',
        },
      });
      setError(null);
      onPendingChange?.({ dirty: false, error: null });
      if (notify) {
        message.success('连线规则已应用');
      }
      return true;
    } catch (parseError) {
      const nextError = `JSON 格式错误：${parseError.message}`;
      setError(nextError);
      onPendingChange?.({ dirty: true, error: nextError });
      return false;
    }
  };

  return (
    <div className="execution-node-inspector">
      <div className="execution-node-inspector__title">
        <Text className="execution-eyebrow">EDGE SETTINGS</Text>
        <Title level={4}>连线规则</Title>
      </div>
      <Form layout="vertical">
        <Form.Item label="上游节点">
          <Input value={edge.source} disabled />
        </Form.Item>
        <Form.Item label="下游节点">
          <Input value={edge.target} disabled />
        </Form.Item>
        <Form.Item
          label="条件表达式（JSON）"
          tooltip="留空表示默认路径。支持 path、operator 和 value，例如 { path, operator: 'eq', value }。"
        >
          <Input.TextArea
            aria-label="条件表达式 JSON"
            value={conditionText}
            onChange={(event) => {
              const value = event.target.value;
              setConditionText(value);
              apply({ nextText: value, notify: false });
            }}
            rows={8}
            className="execution-json-editor"
            placeholder={'{\n  "path": "$.globals.review_required",\n  "operator": "eq",\n  "value": true\n}'}
          />
        </Form.Item>
        <Form.Item label="汇合策略">
          <Select
            value={edge.data?.joinPolicy || edge.join_policy || 'all'}
            options={[
              { value: 'all', label: '等待全部上游分支' },
              { value: 'any', label: '任一上游完成即可' },
            ]}
            onChange={joinPolicy => onUpdate({
              ...edge,
              data: { ...(edge.data || {}), joinPolicy },
            })}
          />
        </Form.Item>
        {error && <Alert showIcon type="error" message={error} />}
        <Space direction="vertical" style={{ width: '100%' }}>
          <Button block onClick={() => apply()}>校验并应用连线规则</Button>
          <Popconfirm title="移除此连线？节点本身会保留。" onConfirm={onRemove}>
            <Button danger block>移除连线</Button>
          </Popconfirm>
        </Space>
      </Form>
    </div>
  );
}

export default function ExecutionWorkflowDesigner() {
  const [testForm] = Form.useForm();
  const { canPublishWorkflow } = useExecutionAuth();
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const importInputRef = useRef(null);
  const baselineHashRef = useRef('');
  const currentHashRef = useRef('');
  const saveGenerationRef = useRef(0);
  const revisionRef = useRef(0);
  const savingPromiseRef = useRef(null);
  const saveDraftRef = useRef(null);
  const saveStateRef = useRef('saved');
  const editorIssueRef = useRef({ dirty: false, error: null });
  const [workflow, setWorkflow] = useState(null);
  const [definition, setDefinition] = useState(null);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [saveState, setSaveState] = useState('saved');
  const [saveError, setSaveError] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState(null);
  const [flowInstance, setFlowInstance] = useState(null);
  const [ready, setReady] = useState(false);
  const [validating, setValidating] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testModalOpen, setTestModalOpen] = useState(false);
  const [testFormDefaults, setTestFormDefaults] = useState(null);
  const [validationErrors, setValidationErrors] = useState([]);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [nodeTypes, setNodeTypes] = useState([]);
  const [nodeTypesLoading, setNodeTypesLoading] = useState(true);
  const [nodeTypesError, setNodeTypesError] = useState(null);
  const [editorIssue, setEditorIssue] = useState({
    dirty: false,
    error: null,
  });
  saveStateRef.current = saveState;

  const handleEditorPendingChange = useCallback((next) => {
    const normalized = {
      dirty: Boolean(next?.dirty),
      error: next?.error || null,
    };
    editorIssueRef.current = normalized;
    setEditorIssue(current => (
      current.dirty === normalized.dirty && current.error === normalized.error
        ? current
        : normalized
    ));
  }, []);

  const serialize = useCallback((sourceDefinition = definition, sourceNodes = nodes, sourceEdges = edges) =>
    definitionFromCanvas(
      sourceDefinition,
      sourceNodes,
      sourceEdges,
      flowInstance?.getViewport?.() || sourceDefinition?.viewport,
    ), [definition, edges, flowInstance, nodes]);

  const hydrate = useCallback((payload) => {
    const nextWorkflow = getWorkflowIdentity(payload);
    const normalized = normalizeWorkflowDefinition(
      responseDefinition(payload, nextWorkflow),
      {
        name: nextWorkflow?.name,
        category: nextWorkflow?.category,
      },
    );
    setWorkflow(nextWorkflow);
    setDefinition(normalized);
    setNodes(normalized.nodes);
    setEdges(normalized.edges);
    setRevision(responseRevision(payload, nextWorkflow));
    revisionRef.current = responseRevision(payload, nextWorkflow);
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    const baseline = stableStringify(
      definitionFromCanvas(normalized, normalized.nodes, normalized.edges, normalized.viewport),
    );
    baselineHashRef.current = baseline;
    currentHashRef.current = baseline;
    setSaveState('saved');
    setSaveError(null);
    editorIssueRef.current = { dirty: false, error: null };
    setEditorIssue({ dirty: false, error: null });
    setReady(true);
  }, [setEdges, setNodes]);

  const loadWorkflow = useCallback(async () => {
    setLoading(true);
    setReady(false);
    setLoadError(null);
    try {
      hydrate(await getExecutionWorkflow(workflowId));
    } catch (requestError) {
      setLoadError(requestError);
    } finally {
      setLoading(false);
    }
  }, [hydrate, workflowId]);

  useEffect(() => {
    loadWorkflow();
  }, [loadWorkflow]);

  const loadNodeTypes = useCallback(async () => {
    setNodeTypesLoading(true);
    setNodeTypesError(null);
    setNodeTypes([]);
    try {
      const items = await getExecutionNodeTypes();
      const normalized = items
        .filter(item => item?.type && Number.isFinite(Number(item?.version)))
        .map(normalizeNodeType);
      if (normalized.length === 0) {
        throw new Error('服务端未返回任何有效的节点类型');
      }
      setNodeTypes(normalized);
    } catch (requestError) {
      setNodeTypesError(requestError);
    } finally {
      setNodeTypesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadNodeTypes();
  }, [loadNodeTypes]);

  const nodeTypesByKey = useMemo(
    () => new Map(nodeTypes.map(item => [`${item.type}@${item.version}`, item])),
    [nodeTypes],
  );

  useEffect(() => {
    if (!ready || nodeTypesByKey.size === 0) {
      return;
    }
    setNodes(current => current.map((node) => {
      const registration = nodeTypesByKey.get(
        `${node.data?.nodeType}@${Number(node.data?.typeVersion || 1)}`,
      );
      if (!registration) {
        return node;
      }
      return {
        ...node,
        data: {
          ...node.data,
          category: registration.group,
          description: registration.description,
          configSchema: registration.configSchema,
          tone: registration.tone,
        },
      };
    }));
  }, [nodeTypesByKey, ready, setNodes]);

  const saveDraft = useCallback(async ({ silent = false } = {}) => {
    if (!definition || !ready) {
      return revisionRef.current;
    }
    if (editorIssueRef.current.dirty) {
      const error = new Error(
        editorIssueRef.current.error || '节点或连线配置尚未通过 JSON 校验',
      );
      error.code = 'designer_editor_invalid';
      throw error;
    }
    if (savingPromiseRef.current) {
      await savingPromiseRef.current;
    }
    const serialized = serialize();
    const hash = stableStringify(serialized);
    if (hash === baselineHashRef.current) {
      setSaveState('saved');
      return revisionRef.current;
    }
    const generation = ++saveGenerationRef.current;
    setSaveState('saving');
    setSaveError(null);
    try {
      const request = saveWorkflowDraft(workflowId, {
        revision: revisionRef.current,
        definition: serialized,
      });
      savingPromiseRef.current = request;
      const payload = await request;
      const nextRevision = payload?.revision
        ?? payload?.draft_revision
        ?? payload?.draft?.revision
        ?? revisionRef.current + 1;
      revisionRef.current = nextRevision;
      setRevision(nextRevision);
      baselineHashRef.current = hash;
      if (generation === saveGenerationRef.current && currentHashRef.current === hash) {
        setSaveState('saved');
      } else {
        setSaveState('dirty');
      }
      if (!silent) {
        message.success('草稿已保存');
      }
      return nextRevision;
    } catch (requestError) {
      if (requestError.status === 409) {
        setSaveState('conflict');
      } else {
        setSaveState('error');
      }
      setSaveError(requestError);
      throw requestError;
    } finally {
      savingPromiseRef.current = null;
    }
  }, [definition, ready, serialize, workflowId]);

  saveDraftRef.current = saveDraft;

  useEffect(() => {
    if (!ready || !definition) {
      return undefined;
    }
    const hash = stableStringify(serialize());
    currentHashRef.current = hash;
    if (hash === baselineHashRef.current) {
      setSaveState(current => current === 'saving' ? current : 'saved');
      return undefined;
    }
    if (editorIssue.dirty) {
      return undefined;
    }
    setSaveState(current => (
      current === 'conflict' || current === 'saving' ? current : 'dirty'
    ));
    const timer = window.setTimeout(() => {
      saveDraft({ silent: true }).catch(() => {});
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [definition, edges, editorIssue.dirty, nodes, ready, saveDraft, serialize]);

  useEffect(() => {
    const hasUnsavedChanges = () => (
      editorIssueRef.current.dirty
      || (
        currentHashRef.current
        && currentHashRef.current !== baselineHashRef.current
      )
      || saveStateRef.current === 'saving'
    );
    const handleBeforeUnload = (event) => {
      if (!hasUnsavedChanges()) {
        return;
      }
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload);
      if (
        !editorIssueRef.current.dirty
        && currentHashRef.current
        && currentHashRef.current !== baselineHashRef.current
      ) {
        // SPA 内部离页时请求仍可完成；浏览器关闭由 beforeunload 明确拦截。
        saveDraftRef.current?.({ silent: true }).catch(() => {});
      }
    };
  }, []);

  const selectedNode = nodes.find(node => node.id === selectedNodeId) || null;
  const selectedEdge = edges.find(edge => edge.id === selectedEdgeId) || null;
  const mayChangeInspectorSelection = useCallback(() => {
    if (!editorIssueRef.current.dirty) {
      return true;
    }
    message.warning('请先修复当前 JSON 配置，避免未应用内容丢失');
    return false;
  }, []);
  const parkedNodeIds = useMemo(
    () => new Set(nodes.filter(node => node.data?.disabled).map(node => node.id)),
    [nodes],
  );
  const displayEdges = useMemo(
    () => edges.map(edge => (
      parkedNodeIds.has(edge.source) || parkedNodeIds.has(edge.target)
        ? {
          ...edge,
          className: [
            edge.className,
            'execution-flow-edge--parked',
          ].filter(Boolean).join(' '),
        }
        : edge
    )),
    [edges, parkedNodeIds],
  );

  const updateSelectedNode = data => {
    setNodes(current => current.map(node =>
      node.id === selectedNodeId ? { ...node, data } : node,
    ));
  };

  const updateSelectedEdge = nextEdge => {
    setEdges(current => current.map(edge =>
      edge.id === selectedEdgeId ? nextEdge : edge,
    ));
  };

  const onConnect = useCallback((connection) => {
    if (connection.source === connection.target) {
      message.warning('节点不能连接到自身');
      return;
    }
    setEdges(current => addEdge({
      ...connection,
      id: `edge-${createClientUuid()}`,
      type: 'smoothstep',
    }, current));
  }, [setEdges]);

  const onDragStart = (event, nodeType) => {
    event.dataTransfer.setData('application/execution-node', nodeType.type);
    event.dataTransfer.setData('application/execution-node-version', String(nodeType.version));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback((event) => {
    event.preventDefault();
    if (!mayChangeInspectorSelection()) {
      return;
    }
    const nodeType = event.dataTransfer.getData('application/execution-node');
    const nodeVersion = Number(
      event.dataTransfer.getData('application/execution-node-version'),
    );
    if (!nodeType || !flowInstance) {
      return;
    }
    const registration = nodeTypesByKey.get(`${nodeType}@${nodeVersion}`);
    if (!registration) {
      message.error('该节点类型或版本已不在服务端注册表中，请重新加载节点库');
      return;
    }
    if (nodeType === 'core.start' && nodes.some(node => node.data.nodeType === 'core.start')) {
      message.warning('流程只能包含一个开始节点');
      return;
    }
    const position = flowInstance.screenToFlowPosition({
      x: event.clientX,
      y: event.clientY,
    });
    const node = createWorkflowNode(registration, position);
    setNodes(current => [...current, node]);
    setSelectedNodeId(node.id);
    setSelectedEdgeId(null);
  }, [flowInstance, mayChangeInspectorSelection, nodeTypesByKey, nodes, setNodes]);

  const runValidation = async ({ notify = true } = {}) => {
    setValidating(true);
    if (editorIssueRef.current.dirty) {
      const errors = [
        editorIssueRef.current.error || '节点或连线配置尚未通过 JSON 校验',
      ];
      setValidationErrors(errors);
      setValidating(false);
      if (notify) {
        message.error('请先修复当前 JSON 配置');
      }
      return errors;
    }
    const serialized = serialize();
    const localErrors = validateWorkflowDefinition(
      serialized,
      nodeTypes.length > 0 ? nodeTypes : null,
    );
    if (localErrors.length) {
      setValidationErrors(localErrors);
      setValidating(false);
      if (notify) {
        message.error(`发现 ${localErrors.length} 个流程问题`);
      }
      return localErrors;
    }
    try {
      const payload = await validateWorkflow(workflowId, serialized);
      const errors = payload?.issues || payload?.errors || payload?.validation_errors || [];
      setValidationErrors(errors.map(error => typeof error === 'string' ? error : error.message));
      if (notify) {
        errors.length ? message.error(`发现 ${errors.length} 个流程问题`) : message.success('流程校验通过');
      }
      return errors;
    } catch (requestError) {
      message.error(requestError.message || '服务端校验失败');
      return [requestError.message];
    } finally {
      setValidating(false);
    }
  };

  const handlePublish = async () => {
    setPublishing(true);
    try {
      const nextRevision = await saveDraft({ silent: true });
      const errors = await runValidation({ notify: false });
      if (errors.length) {
        message.error('请先修复校验问题再发布');
        return;
      }
      await publishWorkflow(workflowId, { revision: nextRevision });
      message.success('新版本已发布，现有运行不会受到影响');
      await loadWorkflow();
    } catch (requestError) {
      message.error(requestError.message || '发布失败');
    } finally {
      setPublishing(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      await saveDraft({ silent: true });
      const errors = await runValidation({ notify: false });
      if (errors.length) {
        message.error('请先修复校验问题再测试');
        return;
      }
      const inputSchema = testInputSchemaOf(definition);
      const defaults = {
        input_data: {
          ...schemaDefaults(inputSchema),
          inspection_number: `TEST-${dayjs().format('YYYYMMDD-HHmmss')}`,
        },
        global_data: schemaDefaults(definition?.global_schema),
      };
      setTestFormDefaults(defaults);
      testForm.setFieldsValue(defaults);
      setTestModalOpen(true);
    } catch (requestError) {
      message.error(requestError.message || '测试运行准备失败');
    } finally {
      setTesting(false);
    }
  };

  const startTestRun = async (values) => {
    const inspectionNumber = values.input_data?.inspection_number?.trim();
    if (!inspectionNumber) {
      message.error('请输入测试检验编号');
      return;
    }
    setTesting(true);
    try {
      const payload = await testWorkflow(workflowId, {
        inspection_number: inspectionNumber,
        input_data: {
          ...(values.input_data || {}),
          inspection_number: inspectionNumber,
        },
        global_data: values.global_data || {},
        idempotency_key: createClientUuid(),
      });
      const run = payload?.run || payload;
      const runId = run?.id || run?.run_id;
      message.success('测试运行已创建');
      setTestModalOpen(false);
      if (runId) {
        navigate(`/execution/runs/${runId}`);
      }
    } catch (requestError) {
      message.error(requestError.message || '测试运行创建失败');
    } finally {
      setTesting(false);
    }
  };

  const handleExport = async () => {
    try {
      const payload = await exportWorkflow(workflowId);
      downloadJson(payload?.definition ? payload : serialize(), `${workflow?.name || 'workflow'}.json`);
    } catch {
      downloadJson({
        workflow: {
          name: workflow?.name,
          description: workflow?.description,
        },
        definition: serialize(),
      }, `${workflow?.name || 'workflow'}.json`);
    }
  };

  const handleImport = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) {
      return;
    }
    try {
      const parsed = JSON.parse(await file.text());
      const candidate = normalizeWorkflowDefinition(parsed.definition || parsed);
      const errors = validateWorkflowDefinition(
        definitionFromCanvas(candidate, candidate.nodes, candidate.edges, candidate.viewport),
        nodeTypes.length > 0 ? nodeTypes : null,
      );
      if (errors.length) {
        setValidationErrors(errors);
        throw new Error(`导入文件存在 ${errors.length} 个问题`);
      }
      const document = parsed.format === 'textile-execution-workflow'
        // Preserve the exported document byte-for-byte at the semantic JSON
        // level so the backend can verify its checksum before changing the
        // current draft. Canvas normalization happens only after acceptance.
        ? parsed
        : {
          format: 'textile-execution-workflow',
          format_version: '1.0',
          workflow: {
            name: workflow?.name,
            description: workflow?.description,
            category_key: workflow?.category?.key,
            capabilities: workflow?.capabilities || {},
          },
          definition: definitionFromCanvas(
            candidate,
            candidate.nodes,
            candidate.edges,
            candidate.viewport,
          ),
        };
      const payload = await importWorkflow(
        workflowId,
        document,
        revisionRef.current,
      );
      hydrate(payload);
      message.success('工作流已导入为新草稿');
    } catch (requestError) {
      message.error(requestError.message || '导入失败');
    }
  };

  const openVersions = async () => {
    setVersionsOpen(true);
    setVersionsLoading(true);
    try {
      setVersions(await getWorkflowVersions(workflowId));
    } catch (requestError) {
      message.error(requestError.message || '版本列表加载失败');
    } finally {
      setVersionsLoading(false);
    }
  };

  const leaveDesigner = async (backTo) => {
    if (editorIssueRef.current.dirty) {
      message.error('当前 JSON 配置尚未通过校验，请修复后再离开');
      return;
    }
    try {
      await saveDraft({ silent: true });
      navigate(backTo.path);
    } catch (requestError) {
      message.error(requestError.message || '草稿尚未保存，暂不能离开设计器');
    }
  };

  if (loading) {
    return (
      <div className="execution-route-loading">
        <Spin size="large" />
        <span>正在加载流程设计器…</span>
      </div>
    );
  }

  if (loadError || !definition) {
    return (
      <div className="execution-route-loading">
        <Alert
          showIcon
          type="error"
          message="流程加载失败"
          description={loadError?.message}
          action={<Button onClick={loadWorkflow}>重试</Button>}
        />
      </div>
    );
  }

  const displayedSaveState = editorIssue.dirty ? 'invalid' : saveState;
  const designerActionsBlocked = ['invalid', 'conflict', 'error'].includes(
    displayedSaveState,
  );
  const saveStatus = {
    saved: { text: `草稿已保存 · r${revision}`, color: 'success', icon: <CheckCircleOutlined /> },
    dirty: { text: '有未保存更改', color: 'warning', icon: <WarningOutlined /> },
    saving: { text: '正在保存…', color: 'processing', icon: <CloudUploadOutlined /> },
    error: { text: '自动保存失败', color: 'error', icon: <WarningOutlined /> },
    conflict: { text: '草稿版本冲突', color: 'error', icon: <WarningOutlined /> },
    invalid: { text: '配置格式待修复', color: 'error', icon: <WarningOutlined /> },
  }[displayedSaveState];

  const moreMenu = {
    items: [
      { key: 'versions', icon: <HistoryOutlined />, label: '版本记录', onClick: openVersions },
      { key: 'export', icon: <DownloadOutlined />, label: '导出 JSON', onClick: handleExport },
      { key: 'import', icon: <UploadOutlined />, label: '导入 JSON', onClick: () => importInputRef.current?.click() },
    ],
  };

  return (
    <div className="execution-designer">
      <ExecutionChrome
        title={workflow?.name || '流程设计器'}
        subtitle={workflow?.description || '编辑草稿不会影响正在运行或已经发布的版本'}
        backTo={{ path: '/execution/admin', label: '流程管理' }}
        onBack={leaveDesigner}
        actions={(
          <Space>
            <Tag color={saveStatus.color} icon={saveStatus.icon}>{saveStatus.text}</Tag>
            <Button
              icon={<SaveOutlined />}
              disabled={displayedSaveState === 'saved' || displayedSaveState === 'invalid'}
              loading={saveState === 'saving'}
              onClick={() => saveDraft().catch(error => message.error(error.message))}
            >
              保存
            </Button>
            <Button
              disabled={designerActionsBlocked}
              loading={validating}
              onClick={() => runValidation()}
            >
              校验
            </Button>
            <Button
              icon={<PlayCircleOutlined />}
              disabled={designerActionsBlocked}
              loading={testing}
              onClick={handleTest}
            >
              测试运行
            </Button>
            <Tooltip
              title={
                !canPublishWorkflow
                  ? '当前账号只有设计权限，不能发布流程'
                  : designerActionsBlocked
                    ? '请先修复配置或草稿保存问题'
                    : undefined
              }
            >
              <Button
                type="primary"
                disabled={!canPublishWorkflow || designerActionsBlocked}
                loading={publishing}
                onClick={handlePublish}
              >
                发布版本
              </Button>
            </Tooltip>
            <Dropdown menu={moreMenu}><Button>更多</Button></Dropdown>
          </Space>
        )}
      />
      <input
        ref={importInputRef}
        type="file"
        accept="application/json,.json"
        hidden
        onChange={handleImport}
      />

      {(saveState === 'conflict' || saveState === 'error') && (
        <Alert
          banner
          showIcon
          type="error"
          message={saveState === 'conflict' ? '其他页面已更新此草稿' : '草稿保存失败'}
          description={saveError?.message}
          action={(
            <Space>
              <Button size="small" onClick={handleExport}>先导出当前草稿</Button>
              <Button size="small" type="primary" onClick={loadWorkflow}>重新加载服务器版本</Button>
            </Space>
          )}
        />
      )}
      {editorIssue.dirty && (
        <Alert
          banner
          showIcon
          type="error"
          message="当前节点或连线配置尚未应用"
          description={`${editorIssue.error || 'JSON 格式不正确'}；自动保存、校验、测试运行、发布和离页均已暂停。`}
        />
      )}

      <main className="execution-designer__grid">
        <aside className="execution-designer__left">
          <NodePalette
            nodeTypes={nodeTypes}
            loading={nodeTypesLoading}
            error={nodeTypesError}
            onRetry={loadNodeTypes}
            onDragStart={onDragStart}
          />
          <Divider />
          <VariablesEditor definition={definition} onChange={setDefinition} />
          <Divider />
          <ResourceSlotsEditor definition={definition} onChange={setDefinition} />
        </aside>

        <section className="execution-designer__canvas">
          <WorkflowCanvas
            nodes={nodes}
            edges={displayEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => {
              if (!mayChangeInspectorSelection()) {
                return;
              }
              setSelectedNodeId(node.id);
              setSelectedEdgeId(null);
            }}
            onEdgeClick={(_, edge) => {
              if (!mayChangeInspectorSelection()) {
                return;
              }
              setSelectedEdgeId(edge.id);
              setSelectedNodeId(null);
            }}
            onPaneClick={() => {
              if (!mayChangeInspectorSelection()) {
                return;
              }
              setSelectedNodeId(null);
              setSelectedEdgeId(null);
            }}
            onDrop={onDrop}
            onDragOver={onDragOver}
            onInit={setFlowInstance}
            defaultViewport={definition.viewport}
            fitView={!definition.viewport}
          >
            <Panel position="top-left">
              <div className="execution-designer__canvas-hint">
                拖动节点调整顺序 · 从节点圆点拉出连线
              </div>
            </Panel>
          </WorkflowCanvas>
        </section>

        <aside className="execution-designer__right">
          {selectedEdge ? (
            <EdgeInspector
              edge={selectedEdge}
              onUpdate={updateSelectedEdge}
              onRemove={() => {
                if (!mayChangeInspectorSelection()) {
                  return;
                }
                setEdges(current => current.filter(edge => edge.id !== selectedEdge.id));
                setSelectedEdgeId(null);
              }}
              onPendingChange={handleEditorPendingChange}
            />
          ) : (
            <NodeInspector
              node={selectedNode}
              nodeDefinition={selectedNode
                ? nodeTypesByKey.get(
                  `${selectedNode.data.nodeType}@${Number(selectedNode.data.typeVersion || 1)}`,
                )
                : null}
              onUpdate={updateSelectedNode}
              onPendingChange={handleEditorPendingChange}
            />
          )}
          {validationErrors.length > 0 && (
            <section className="execution-validation-panel">
              <Divider orientation="left">校验问题 ({validationErrors.length})</Divider>
              <List
                size="small"
                dataSource={validationErrors}
                renderItem={item => (
                  <List.Item>
                    <WarningOutlined />
                    <span>{typeof item === 'string' ? item : item.message}</span>
                  </List.Item>
                )}
              />
            </section>
          )}
        </aside>
      </main>

      <Modal
        title="创建测试运行"
        open={testModalOpen}
        okText="开始测试"
        cancelText="取消"
        confirmLoading={testing}
        onOk={() => testForm.submit()}
        onCancel={() => {
          if (!testing) {
            setTestModalOpen(false);
          }
        }}
        destroyOnHidden
        forceRender
        width={640}
      >
        <Alert
          showIcon
          type="info"
          message="测试运行使用当前草稿的完整输入结构"
          description="请填写所有必填输入和全局变量。测试模式默认使用测试制品，并禁止真实外部系统副作用。"
          style={{ marginBottom: 18 }}
        />
        <Form
          form={testForm}
          layout="vertical"
          initialValues={testFormDefaults || undefined}
          onFinish={startTestRun}
        >
          <SchemaFields
            schema={testInputSchemaOf(definition)}
            namePrefix="input_data"
          />
          {Object.keys(definition?.global_schema?.properties || {}).length > 0 && (
            <>
              <Divider orientation="left">流程全局变量</Divider>
              <SchemaFields
                schema={definition.global_schema}
                namePrefix="global_data"
              />
            </>
          )}
        </Form>
      </Modal>

      <Drawer
        title="已发布版本"
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        width={520}
      >
        <List
          loading={versionsLoading}
          dataSource={versions}
          locale={{ emptyText: '尚未发布任何版本' }}
          renderItem={version => (
            <List.Item>
              <List.Item.Meta
                title={`版本 ${version.version || version.version_number}`}
                description={(
                  <Space direction="vertical" size={0}>
                    <span>{version.published_by_name || version.published_by || '系统管理员'}</span>
                    <span>{version.published_at ? dayjs(version.published_at).format('YYYY-MM-DD HH:mm:ss') : ''}</span>
                    {version.checksum && <Text code>{version.checksum.slice(0, 16)}…</Text>}
                  </Space>
                )}
              />
              <Tag color="green">不可变</Tag>
            </List.Item>
          )}
        />
      </Drawer>
    </div>
  );
}
