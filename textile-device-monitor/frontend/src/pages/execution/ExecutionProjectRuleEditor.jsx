import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  ExperimentOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import {
  getExecutionFileRoots,
  getProjectRule,
  testProjectRule,
  updateProjectRule,
} from '../../api/execution';

const { Text } = Typography;

const STRATEGY_OPTIONS = [
  {
    value: 'level1_contains_number',
    label: '一级文件夹名包含编号（纸类）',
  },
  {
    value: 'filename_contains_number',
    label: '文件名包含编号（再生纤）',
  },
  {
    value: 'electron_image_folders',
    label: '电镜图片目录（按编号文件夹）',
  },
];

const ENTRY_KIND_OPTIONS = [
  { value: 'workbook', label: '工作簿（xls/xlsx）' },
  { value: 'image', label: '图片' },
];

const PROBE_TYPE_OPTIONS = [
  { value: 'cell_value', label: '读取单元格' },
  { value: 'worksheet_exists', label: '工作表存在' },
];

const PROBE_PARSER_OPTIONS = [
  { value: 'text', label: '文本' },
  { value: 'paper_qualitative_v1', label: '纸类定性结果（100 判定）' },
];

const emptyProbe = () => ({
  name: '',
  type: 'cell_value',
  sheet: '',
  cell: '',
  parser: 'text',
});

export default function ExecutionProjectRuleEditor({ open, ruleKey, onClose }) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [rule, setRule] = useState(null);
  const [roots, setRoots] = useState([]);
  const [displayName, setDisplayName] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [rootId, setRootId] = useState('');
  const [strategy, setStrategy] = useState('level1_contains_number');
  const [entryKind, setEntryKind] = useState('workbook');
  const [maxDepth, setMaxDepth] = useState(2);
  const [aliases, setAliases] = useState([]);
  const [testMethod, setTestMethod] = useState('');
  const [hasTaskFacts, setHasTaskFacts] = useState(false);
  const [bindingNo, setBindingNo] = useState('');
  const [bindingName, setBindingName] = useState('');
  const [probes, setProbes] = useState([]);
  const [testNumber, setTestNumber] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testError, setTestError] = useState(null);

  useEffect(() => {
    if (!open || !ruleKey) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setRule(null);
    setTestResult(null);
    setTestError(null);
    (async () => {
      try {
        const [rulePayload, rootRows] = await Promise.all([
          getProjectRule(ruleKey),
          getExecutionFileRoots().catch(() => []),
        ]);
        if (cancelled) {
          return;
        }
        const item = rulePayload?.item || rulePayload;
        const config = item?.config || {};
        const folder = config?.source?.folder_match || {};
        const facts = Array.isArray(config?.task_facts) ? config.task_facts : [];
        const nameFact = facts.find(fact => fact.condition_key === 'task_item_name');
        const methodFact = facts.find(fact => fact.condition_key === 'test_method');
        setRule(item);
        setRoots(Array.isArray(rootRows) ? rootRows : []);
        setDisplayName(item?.display_name || '');
        setEnabled(item?.enabled !== false);
        setRootId(config?.source?.root_id || '');
        setStrategy(folder.strategy || 'level1_contains_number');
        setEntryKind(folder.entry_kind || 'workbook');
        setMaxDepth(folder.max_depth ?? 2);
        setAliases(
          (nameFact?.values || (nameFact?.value ? [nameFact.value] : []))
            .map(String),
        );
        setTestMethod(
          String(
            methodFact?.value
              ?? (methodFact?.values || [])[0]
              ?? '',
          ),
        );
        setHasTaskFacts(facts.length > 0 || Boolean(config?.binding));
        setBindingNo(String(config?.binding?.check_item_no || ''));
        setBindingName(String(config?.binding?.check_item_name || ''));
        setProbes(
          (Array.isArray(config?.probes) ? config.probes : []).map(probe => ({
            name: probe?.name || '',
            type: probe?.type || 'cell_value',
            sheet: probe?.sheet || '',
            cell: probe?.cell || '',
            parser: probe?.parser || 'text',
          })),
        );
      } catch (requestError) {
        if (!cancelled) {
          message.error(requestError.message || '匹配规则加载失败');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, ruleKey]);

  const buildConfig = useCallback(() => {
    const base = rule?.config || {};
    const taskFacts = [];
    if (hasTaskFacts) {
      taskFacts.push({
        condition_key: 'task_item_name',
        fact: 'check_item_name',
        op: 'in',
        values: aliases.map(item => String(item).trim()).filter(Boolean),
      });
      if (testMethod.trim()) {
        taskFacts.push({
          condition_key: 'test_method',
          fact: 'check_method',
          op: 'eq',
          values: [testMethod.trim()],
        });
      }
    }
    return {
      ...base,
      source: {
        root_id: rootId,
        folder_match: {
          strategy,
          entry_kind: entryKind,
          max_depth: maxDepth,
        },
      },
      task_facts: taskFacts,
      probes: probes.map(probe => ({
        name: String(probe.name || '').trim(),
        type: probe.type,
        sheet: String(probe.sheet || '').trim(),
        ...(probe.type === 'cell_value'
          ? { cell: String(probe.cell || '').trim().toUpperCase() }
          : {}),
        parser: probe.parser || 'text',
      })),
      binding: base.binding
        ? {
            ...base.binding,
            check_item_no: bindingNo.trim(),
            check_item_name: bindingName.trim(),
          }
        : null,
    };
  }, [
    rule,
    hasTaskFacts,
    aliases,
    testMethod,
    rootId,
    strategy,
    entryKind,
    maxDepth,
    probes,
    bindingNo,
    bindingName,
  ]);

  const originalStrategy = rule?.config?.source?.folder_match?.strategy;
  const strategyChanged = Boolean(
    rule && originalStrategy && strategy !== originalStrategy,
  );

  const handleSave = async () => {
    setSaving(true);
    try {
      const payload = await updateProjectRule(ruleKey, {
        display_name: displayName.trim(),
        enabled,
        config: buildConfig(),
      });
      const item = payload?.item || payload;
      message.success(`匹配规则已保存并即时生效（修订号 rev ${item?.revision ?? '?'}）`);
      onClose?.(true);
    } catch (requestError) {
      message.error(requestError.message || '保存匹配规则失败');
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!testNumber.trim()) {
      message.warning('请输入用于测试的检验编号');
      return;
    }
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const result = await testProjectRule({
        rule_key: ruleKey,
        config: buildConfig(),
        inspection_number: testNumber.trim(),
      });
      setTestResult(result);
    } catch (requestError) {
      setTestError(requestError.message || '匹配测试失败');
    } finally {
      setTesting(false);
    }
  };

  const probeColumns = useMemo(() => [
    {
      title: '名称',
      dataIndex: 'name',
      render: (value, _row, index) => (
        <Input
          value={value}
          placeholder="如 qualitative_result"
          onChange={(event) => {
            const next = [...probes];
            next[index] = { ...next[index], name: event.target.value };
            setProbes(next);
          }}
        />
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 130,
      render: (value, _row, index) => (
        <Select
          value={value}
          options={PROBE_TYPE_OPTIONS}
          style={{ width: '100%' }}
          onChange={(nextValue) => {
            const next = [...probes];
            next[index] = { ...next[index], type: nextValue };
            setProbes(next);
          }}
        />
      ),
    },
    {
      title: '工作表',
      dataIndex: 'sheet',
      width: 140,
      render: (value, _row, index) => (
        <Input
          value={value}
          placeholder="Sheet1"
          onChange={(event) => {
            const next = [...probes];
            next[index] = { ...next[index], sheet: event.target.value };
            setProbes(next);
          }}
        />
      ),
    },
    {
      title: '单元格',
      dataIndex: 'cell',
      width: 100,
      render: (value, row, index) => (
        <Input
          value={value}
          placeholder="W32"
          disabled={row.type !== 'cell_value'}
          onChange={(event) => {
            const next = [...probes];
            next[index] = { ...next[index], cell: event.target.value };
            setProbes(next);
          }}
        />
      ),
    },
    {
      title: '解析器',
      dataIndex: 'parser',
      width: 180,
      render: (value, _row, index) => (
        <Select
          value={value}
          options={PROBE_PARSER_OPTIONS}
          style={{ width: '100%' }}
          onChange={(nextValue) => {
            const next = [...probes];
            next[index] = { ...next[index], parser: nextValue };
            setProbes(next);
          }}
        />
      ),
    },
    {
      title: '',
      key: 'actions',
      width: 48,
      render: (_value, _row, index) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          aria-label="删除探针"
          onClick={() => setProbes(probes.filter((_, item) => item !== index))}
        />
      ),
    },
  ], [probes]);

  return (
    <Drawer
      title={rule ? `匹配规则：${rule.display_name || rule.rule_key}` : '匹配规则'}
      open={open}
      width={720}
      onClose={() => onClose?.(false)}
      destroyOnClose
      footer={(
        <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button onClick={() => onClose?.(false)}>取消</Button>
          <Button type="primary" loading={saving} onClick={handleSave}>
            保存并即时生效
          </Button>
        </Space>
      )}
    >
      {loading || !rule ? (
        <Spin />
      ) : (
        <Form layout="vertical" component="div">
          <Space size={[4, 4]} wrap style={{ marginBottom: 12 }}>
            <Tag>{rule.rule_key}</Tag>
            <Tag color="blue">rev {rule.revision}</Tag>
            {rule.config?.default_node_type && (
              <Tag color="purple">{rule.config.default_node_type}</Tag>
            )}
            {!enabled && <Tag color="warning">已停用</Tag>}
          </Space>
          <Form.Item label="展示名称" required>
            <Input
              value={displayName}
              onChange={event => setDisplayName(event.target.value)}
            />
          </Form.Item>
          <Form.Item
            label="启用状态"
            tooltip="停用后对应流程在目录中显示为规则停用，且运行会在匹配节点直接失败。"
          >
            <Switch
              checked={enabled}
              checkedChildren="启用"
              unCheckedChildren="停用"
              onChange={setEnabled}
            />
          </Form.Item>
          <Form.Item label="存储根（root_id）" required>
            <Select
              value={rootId || undefined}
              placeholder="选择存储根"
              options={roots.map(root => ({
                value: root.root_id,
                label: `${root.name}（${root.root_id}）`,
              }))}
              onChange={setRootId}
            />
          </Form.Item>
          <Space size={12} wrap style={{ display: 'flex' }}>
            <Form.Item label="目录匹配策略" required style={{ minWidth: 260 }}>
              <Select
                value={strategy}
                options={STRATEGY_OPTIONS}
                onChange={setStrategy}
              />
            </Form.Item>
            <Form.Item label="条目类型" required>
              <Select
                value={entryKind}
                options={ENTRY_KIND_OPTIONS}
                style={{ width: 160 }}
                onChange={setEntryKind}
              />
            </Form.Item>
            <Form.Item label="目录层级深度" required>
              <InputNumber
                min={1}
                max={6}
                value={maxDepth}
                onChange={value => setMaxDepth(value ?? 2)}
              />
            </Form.Item>
          </Space>
          {strategyChanged && (
            <Alert
              showIcon
              type="warning"
              style={{ marginBottom: 16 }}
              message="目录匹配策略与规则原定策略不一致"
              description="策略实现与节点类型绑定，随意更改通常会导致匹配为空。请先用下方“测试匹配”验证后再保存。"
            />
          )}
          {hasTaskFacts && (
            <>
              <Form.Item
                label="检测项目名称别名（任务单 check_item_name，命中其一即可）"
                required
              >
                <Select
                  mode="tags"
                  value={aliases}
                  placeholder="输入后回车添加别名"
                  open={false}
                  suffixIcon={null}
                  onChange={setAliases}
                />
              </Form.Item>
              <Form.Item label="测试方法（任务单 check_method，精确匹配）" required>
                <Input
                  value={testMethod}
                  placeholder="如 GB/T 4688-2020"
                  onChange={event => setTestMethod(event.target.value)}
                />
              </Form.Item>
            </>
          )}
          {rule.config?.binding && (
            <Space size={12} wrap style={{ display: 'flex' }}>
              <Form.Item
                label="写门禁项目编号（check_item_no）"
                tooltip="外部写入门禁按编号+名称精确反查；与任务单项目目录一致。"
              >
                <Input
                  value={bindingNo}
                  style={{ width: 160 }}
                  onChange={event => setBindingNo(event.target.value)}
                />
              </Form.Item>
              <Form.Item label="写门禁项目名称（check_item_name）">
                <Input
                  value={bindingName}
                  style={{ width: 220 }}
                  onChange={event => setBindingName(event.target.value)}
                />
              </Form.Item>
            </Space>
          )}
          <Form.Item label="结果探针（从候选文件读取的单元格/工作表）">
            <Table
              size="small"
              rowKey={(_row, index) => index}
              columns={probeColumns}
              dataSource={probes}
              pagination={false}
              footer={() => (
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => setProbes([...probes, emptyProbe()])}
                >
                  添加探针
                </Button>
              )}
            />
          </Form.Item>
          <Form.Item label="测试匹配（使用当前表单内容干跑，不落库）">
            <Space.Compact style={{ width: '100%' }}>
              <Input
                value={testNumber}
                placeholder="输入检验编号，如 26W006701"
                onChange={event => setTestNumber(event.target.value)}
                onPressEnter={handleTest}
              />
              <Button
                icon={<ExperimentOutlined />}
                loading={testing}
                onClick={handleTest}
              >
                测试匹配
              </Button>
            </Space.Compact>
          </Form.Item>
          {testError && (
            <Alert showIcon type="error" message="匹配测试失败" description={testError} />
          )}
          {testResult && (
            <Alert
              showIcon
              type={testResult.full_match ? 'success' : 'info'}
              message={testResult.full_match ? '完全匹配' : '部分匹配'}
              description={(
                <Space direction="vertical" size={4}>
                  <Space size={[4, 4]} wrap>
                    {(testResult.matched_conditions || []).map(condition => (
                      <Tag key={condition} color="green">{condition}</Tag>
                    ))}
                    {(testResult.matched_conditions || []).length === 0 && (
                      <Text type="secondary">未命中任何条件</Text>
                    )}
                  </Space>
                  <Text type="secondary">
                    索引状态 {testResult.index_state || '—'} · 查询状态{' '}
                    {testResult.query_state || '—'} · 候选{' '}
                    {testResult.candidate_count ?? 0} 个
                    {testResult.candidate_preview?.name
                      ? ` · 首个候选 ${testResult.candidate_preview.name}`
                      : ''}
                  </Text>
                </Space>
              )}
            />
          )}
        </Form>
      )}
    </Drawer>
  );
}
