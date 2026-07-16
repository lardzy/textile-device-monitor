import {
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Divider,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { areaApi } from '../../api/area';
import { formatAreaDateTime, getAreaErrorMessage } from './areaUtils';

const DEFAULT_OPTIONS = {
  mask_mode: 'auto',
  threshold_bias: 0,
  min_pixels: 64,
  smooth_min_neighbors: 3,
  overlay_alpha: 0.45,
  score_threshold: 0.15,
  top_k: 200,
  nms_top_k: 200,
  nms_conf_thresh: 0.05,
  nms_thresh: 0.5,
};
const DEFAULT_FOLDER_BLACKLIST = ['.recycle', '旧'];

function normalizeFolderBlacklist(values) {
  const normalized = [];
  const seen = new Set();
  (Array.isArray(values) ? values : []).forEach((item) => {
    const value = String(item || '').trim();
    const key = value.toLocaleLowerCase();
    if (!value || value.includes('/') || value.includes('\\') || seen.has(key)) return;
    seen.add(key);
    normalized.push(value);
  });
  return normalized.slice(0, 100);
}

function AreaSettings() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [snapshot, setSnapshot] = useState('');
  const [rootPath, setRootPath] = useState('');
  const [oldRootPath, setOldRootPath] = useState('');
  const [outputRoot, setOutputRoot] = useState('');
  const [folderBlacklist, setFolderBlacklist] = useState(DEFAULT_FOLDER_BLACKLIST);
  const [archiveEnabled, setArchiveEnabled] = useState(false);
  const [mappingRows, setMappingRows] = useState([]);
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [status, setStatus] = useState(null);
  const [archiveStatus, setArchiveStatus] = useState(null);
  const [archivePreview, setArchivePreview] = useState(null);
  const [archivePreviewOpen, setArchivePreviewOpen] = useState(false);
  const [archiveRunning, setArchiveRunning] = useState(false);

  const buildPayload = useCallback(() => ({
    root_path: rootPath.trim(),
    old_root_path: oldRootPath.trim(),
    result_output_root: outputRoot.trim(),
    folder_blacklist: normalizeFolderBlacklist(folderBlacklist),
    archive_enabled: archiveEnabled,
    model_mapping: mappingRows.reduce((result, row) => {
      const name = String(row.model_name || '').trim();
      const filename = String(row.model_file || '').trim();
      if (name && filename) result[name] = filename;
      return result;
    }, {}),
    inference_defaults: { ...DEFAULT_OPTIONS, ...options },
  }), [archiveEnabled, folderBlacklist, mappingRows, oldRootPath, options, outputRoot, rootPath]);

  const serialized = useMemo(() => JSON.stringify(buildPayload()), [buildPayload]);
  const dirty = Boolean(snapshot && snapshot !== serialized);

  const applyConfig = useCallback((config) => {
    const rows = Object.entries(config?.model_mapping || {}).map(([name, filename], index) => ({
      key: `${index}-${name}`,
      model_name: name,
      model_file: filename,
    }));
    const nextPayload = {
      root_path: config?.root_path || '',
      old_root_path: config?.old_root_path || '',
      result_output_root: config?.result_output_root || '',
      folder_blacklist: normalizeFolderBlacklist(
        Array.isArray(config?.folder_blacklist)
          ? config.folder_blacklist
          : DEFAULT_FOLDER_BLACKLIST,
      ),
      archive_enabled: Boolean(config?.archive_enabled),
      model_mapping: config?.model_mapping || {},
      inference_defaults: { ...DEFAULT_OPTIONS, ...(config?.inference_defaults || {}) },
    };
    setRootPath(nextPayload.root_path);
    setOldRootPath(nextPayload.old_root_path);
    setOutputRoot(nextPayload.result_output_root);
    setFolderBlacklist(nextPayload.folder_blacklist);
    setArchiveEnabled(nextPayload.archive_enabled);
    setMappingRows(rows);
    setOptions(nextPayload.inference_defaults);
    setSnapshot(JSON.stringify(nextPayload));
  }, []);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const [config, systemStatus, nextArchiveStatus] = await Promise.all([
        areaApi.getConfig(),
        areaApi.getStatus().catch(() => null),
        areaApi.getArchiveStatus().catch(() => null),
      ]);
      applyConfig(config);
      setStatus(systemStatus);
      setArchiveStatus(nextArchiveStatus);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '全局设置加载失败'));
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    const handleBeforeUnload = (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [dirty]);

  const validatePayload = (payload) => {
    if (!payload.root_path || !payload.old_root_path || !payload.result_output_root) {
      throw new Error('请完整填写数据路径、归档路径和结果输出路径');
    }
    if (!Object.keys(payload.model_mapping).length) {
      throw new Error('至少需要配置一个识别模型');
    }
  };

  const checkConfig = async () => {
    const payload = buildPayload();
    try {
      validatePayload(payload);
      setChecking(true);
      const result = await areaApi.validateConfig(payload);
      setStatus(result);
      if (result.ok) message.success('配置检查通过');
      else message.warning(result.issues?.map((item) => getAreaErrorMessage(item)).join('；'));
      return result;
    } catch (error) {
      message.error(getAreaErrorMessage(error, error.message || '配置检查失败'));
      return null;
    } finally {
      setChecking(false);
    }
  };

  const saveConfig = async () => {
    const payload = buildPayload();
    try {
      validatePayload(payload);
      setChecking(true);
      const validation = await areaApi.validateConfig(payload);
      setStatus(validation);
      if (!validation.ok) {
        message.error(validation.issues?.map((item) => getAreaErrorMessage(item)).join('；'));
        return;
      }
    } catch (error) {
      message.error(getAreaErrorMessage(error, error.message || '配置检查失败'));
      return;
    } finally {
      setChecking(false);
    }

    Modal.confirm({
      title: '保存全局面积识别设置',
      icon: <SafetyCertificateOutlined />,
      content: '修改会立即影响之后创建的所有任务，并可能改变模型输出和归档路径。',
      okText: '确认保存',
      cancelText: '返回检查',
      async onOk() {
        setSaving(true);
        try {
          const updated = await areaApi.updateConfig(payload);
          applyConfig(updated);
          const nextStatus = await areaApi.getStatus().catch(() => null);
          setStatus(nextStatus);
          message.success('全局设置已保存');
        } catch (error) {
          message.error(getAreaErrorMessage(error, '设置保存失败'));
          throw error;
        } finally {
          setSaving(false);
        }
      },
    });
  };

  const openArchivePreview = async () => {
    try {
      const preview = await areaApi.previewArchive();
      setArchivePreview(preview);
      setArchivePreviewOpen(true);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '归档预检失败'));
    }
  };

  const runArchive = async () => {
    setArchiveRunning(true);
    try {
      const result = await areaApi.runArchive();
      message.success(`归档完成，移动 ${Number(result?.moved_count || 0)} 个目录`);
      setArchivePreviewOpen(false);
      const nextStatus = await areaApi.getArchiveStatus();
      setArchiveStatus(nextStatus);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '归档执行失败'));
    } finally {
      setArchiveRunning(false);
    }
  };

  const mappingColumns = [
    {
      title: '模型名称',
      dataIndex: 'model_name',
      width: '42%',
      render: (_, row, index) => (
        <Input
          value={row.model_name}
          onChange={(event) => setMappingRows((current) => current.map((item, rowIndex) => (
            rowIndex === index ? { ...item, model_name: event.target.value } : item
          )))}
        />
      ),
    },
    {
      title: '权重文件',
      dataIndex: 'model_file',
      render: (_, row, index) => (
        <Input
          value={row.model_file}
          onChange={(event) => setMappingRows((current) => current.map((item, rowIndex) => (
            rowIndex === index ? { ...item, model_file: event.target.value } : item
          )))}
        />
      ),
    },
    {
      title: '',
      key: 'action',
      width: 52,
      render: (_, row) => (
        <Button
          danger
          type="text"
          icon={<DeleteOutlined />}
          title="删除模型映射"
          onClick={() => setMappingRows((current) => current.filter((item) => item.key !== row.key))}
        />
      ),
    },
  ];

  return (
    <Spin spinning={loading}>
      <div className="area-page area-settings-page">
        <div className="area-settings-heading">
          <div>
            <Typography.Title level={3}>全局设置</Typography.Title>
            <Typography.Text type="secondary">仅影响之后创建的任务</Typography.Text>
          </div>
          {dirty ? <Tag color="warning">有未保存修改</Tag> : <Tag>已同步</Tag>}
        </div>

        {status ? (
          <Alert
            showIcon
            type={status.ok ? 'success' : 'error'}
            message={status.ok ? '运行环境正常' : '运行环境存在异常'}
            description={status.ok ? '数据目录、输出目录、模型权重和识别服务均可用。' : status.issues?.map((item) => getAreaErrorMessage(item)).join('；')}
          />
        ) : null}

        <section className="area-settings-section">
          <Typography.Title level={4}>数据与输出路径</Typography.Title>
          <div className="area-settings-fields">
            <label>
              <span>数据根路径</span>
              <Input value={rootPath} onChange={(event) => setRootPath(event.target.value)} />
            </label>
            <label>
              <span>旧文件归档路径</span>
              <Input value={oldRootPath} onChange={(event) => setOldRootPath(event.target.value)} />
            </label>
            <label>
              <span>结果输出路径</span>
              <Input value={outputRoot} onChange={(event) => setOutputRoot(event.target.value)} />
            </label>
            <label>
              <span>数据目录黑名单</span>
              <Select
                mode="tags"
                value={folderBlacklist}
                tokenSeparators={[',', '，']}
                placeholder="输入要隐藏的目录名称后按回车"
                options={folderBlacklist.map((name) => ({ value: name, label: name }))}
                onChange={(values) => setFolderBlacklist(normalizeFolderBlacklist(values))}
              />
              <Typography.Text type="secondary">
                目录搜索和最近目录会按名称直接忽略这些文件夹，不读取其中内容。
              </Typography.Text>
            </label>
          </div>
        </section>

        <Divider />

        <section className="area-settings-section">
          <div className="area-section-heading">
            <div>
              <Typography.Title level={4}>模型映射</Typography.Title>
              <Typography.Text type="secondary">模型名称必须与任务选项一致</Typography.Text>
            </div>
            <Button
              icon={<PlusOutlined />}
              onClick={() => setMappingRows((current) => [
                ...current,
                { key: `new-${Date.now()}`, model_name: '', model_file: '' },
              ])}
            >
              添加模型
            </Button>
          </div>
          <Table rowKey="key" size="small" columns={mappingColumns} dataSource={mappingRows} pagination={false} />
        </section>

        <Divider />

        <section className="area-settings-section">
          <Typography.Title level={4}>推理默认参数</Typography.Title>
          <div className="area-parameter-grid">
            <label>
              <span>前景模式</span>
              <Select
                value={options.mask_mode}
                options={[
                  { value: 'auto', label: '自动' },
                  { value: 'dark', label: '深色前景' },
                  { value: 'light', label: '浅色前景' },
                ]}
                onChange={(value) => setOptions((current) => ({ ...current, mask_mode: value }))}
              />
            </label>
            <label><span>阈值偏移</span><InputNumber min={-128} max={128} value={options.threshold_bias} onChange={(value) => setOptions((current) => ({ ...current, threshold_bias: value ?? 0 }))} /></label>
            <label><span>最小像素</span><InputNumber min={1} max={100000} value={options.min_pixels} onChange={(value) => setOptions((current) => ({ ...current, min_pixels: value ?? 64 }))} /></label>
            <label><span>平滑邻域阈值</span><InputNumber min={1} max={5} value={options.smooth_min_neighbors} onChange={(value) => setOptions((current) => ({ ...current, smooth_min_neighbors: value ?? 3 }))} /></label>
            <label><span>置信度阈值</span><InputNumber min={0} max={1} step={0.01} value={options.score_threshold} onChange={(value) => setOptions((current) => ({ ...current, score_threshold: value ?? 0.15 }))} /></label>
            <label><span>候选数量</span><InputNumber min={1} max={1000} value={options.top_k} onChange={(value) => setOptions((current) => ({ ...current, top_k: value ?? 200 }))} /></label>
            <label><span>NMS 候选数量</span><InputNumber min={1} max={1000} value={options.nms_top_k} onChange={(value) => setOptions((current) => ({ ...current, nms_top_k: value ?? 200 }))} /></label>
            <label><span>NMS 置信度</span><InputNumber min={0} max={1} step={0.01} value={options.nms_conf_thresh} onChange={(value) => setOptions((current) => ({ ...current, nms_conf_thresh: value ?? 0.05 }))} /></label>
            <label><span>NMS 阈值</span><InputNumber min={0} max={1} step={0.01} value={options.nms_thresh} onChange={(value) => setOptions((current) => ({ ...current, nms_thresh: value ?? 0.5 }))} /></label>
            <label className="area-slider-field">
              <span>输出遮罩透明度</span>
              <Slider min={0.05} max={0.95} step={0.05} value={options.overlay_alpha} onChange={(value) => setOptions((current) => ({ ...current, overlay_alpha: value }))} />
            </label>
          </div>
        </section>

        <Divider />

        <section className="area-settings-section">
          <div className="area-section-heading">
            <div>
              <Typography.Title level={4}>归档维护</Typography.Title>
              <Typography.Text type="secondary">
                上次执行：{formatAreaDateTime(archiveStatus?.last_run_at)} · 间隔 {Number(archiveStatus?.interval_hours || 48)} 小时
              </Typography.Text>
            </div>
            <Space>
              <Typography.Text>定期归档</Typography.Text>
              <Switch checked={archiveEnabled} onChange={setArchiveEnabled} />
              <Button onClick={openArchivePreview}>立即归档</Button>
            </Space>
          </div>
        </section>

        <div className="area-settings-savebar">
          <Button icon={<ReloadOutlined />} disabled={!dirty} onClick={loadSettings}>放弃修改</Button>
          <Space>
            <Button loading={checking} onClick={checkConfig}>检查配置</Button>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} disabled={!dirty} onClick={saveConfig}>
              保存全局设置
            </Button>
          </Space>
        </div>
      </div>

      <Modal
        open={archivePreviewOpen}
        title="归档旧目录"
        okText="确认归档"
        okButtonProps={{ danger: true, loading: archiveRunning }}
        cancelText="取消"
        onCancel={() => setArchivePreviewOpen(false)}
        onOk={runArchive}
      >
        <Alert
          showIcon
          type="warning"
          message={`将移动 ${Number(archivePreview?.count || 0)} 个超过 ${Number(archivePreview?.threshold_hours || 24)} 小时的目录`}
          description="运行中任务对应的目录会自动跳过，历史任务的原图路径会同步更新。"
        />
        <div className="area-archive-list">
          {(archivePreview?.items || []).slice(0, 12).map((item) => (
            <div key={item.folder_name}>
              <Typography.Text>{item.folder_name}</Typography.Text>
              <Typography.Text type="secondary">{Number(item.image_count || 0)} 张</Typography.Text>
            </div>
          ))}
        </div>
      </Modal>
    </Spin>
  );
}

export default AreaSettings;
