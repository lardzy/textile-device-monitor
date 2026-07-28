import {
  CheckCircleFilled,
  ClearOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Empty,
  Image,
  Input,
  message,
  Select,
  Space,
  Spin,
  Table,
  Tooltip,
  Typography,
} from 'antd';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { areaApi } from '../../api/area';
import AreaFolderCleanupModal from './AreaFolderCleanupModal';
import { formatAreaDateTime, getAreaErrorMessage } from './areaUtils';

function AreaStartRecognition() {
  const navigate = useNavigate();
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [folders, setFolders] = useState([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [folderError, setFolderError] = useState('');
  const [selectedFolder, setSelectedFolder] = useState(null);
  const [modelOptions, setModelOptions] = useState([]);
  const [modelName, setModelName] = useState('');
  const [systemStatus, setSystemStatus] = useState(null);
  const [contextError, setContextError] = useState('');
  const [previewItems, setPreviewItems] = useState([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [cleanupFolder, setCleanupFolder] = useState(null);
  const folderRequestIdRef = useRef(0);
  const contextRequestIdRef = useRef(0);

  const loadFolders = useCallback(async (nextQuery = '') => {
    const requestId = folderRequestIdRef.current + 1;
    folderRequestIdRef.current = requestId;
    setFoldersLoading(true);
    setFolderError('');
    try {
      const payload = nextQuery
        ? await areaApi.searchFolders({ q: nextQuery, limit: 50 })
        : await areaApi.listRecentFolders({ page: 1, page_size: 50, limit: 100 });
      if (folderRequestIdRef.current !== requestId) return;
      setFolders(payload?.items || []);
    } catch (error) {
      if (folderRequestIdRef.current !== requestId) return;
      setFolders([]);
      setFolderError(getAreaErrorMessage(error, '目录加载失败'));
    } finally {
      if (folderRequestIdRef.current === requestId) setFoldersLoading(false);
    }
  }, []);

  const loadPageContext = useCallback(async () => {
    const requestId = contextRequestIdRef.current + 1;
    contextRequestIdRef.current = requestId;
    setContextError('');
    const [configResult, statusResult] = await Promise.allSettled([
      areaApi.getConfig(),
      areaApi.getStatus(),
    ]);
    if (contextRequestIdRef.current !== requestId) return;
    const errors = [];

    if (configResult.status === 'fulfilled') {
      const options = configResult.value?.model_options || [];
      setModelOptions(options);
      setModelName((current) => (current && options.includes(current) ? current : (options[0] || '')));
    } else {
      setModelOptions([]);
      setModelName('');
      errors.push(getAreaErrorMessage(configResult.reason, '识别配置加载失败'));
    }

    if (statusResult.status === 'fulfilled') {
      setSystemStatus(statusResult.value);
    } else {
      setSystemStatus(null);
      errors.push(getAreaErrorMessage(statusResult.reason, '运行状态加载失败'));
    }
    setContextError(errors.join('；'));
  }, []);

  useEffect(() => {
    loadPageContext();
    loadFolders('');
    return () => {
      folderRequestIdRef.current += 1;
      contextRequestIdRef.current += 1;
    };
  }, [loadFolders, loadPageContext]);

  useEffect(() => {
    if (!selectedFolder?.folder_name) {
      setPreviewItems([]);
      return undefined;
    }

    let active = true;
    setPreviewLoading(true);
    areaApi.listFolderPreviewImages(selectedFolder.folder_name, { limit: 6 })
      .then((payload) => {
        if (active) setPreviewItems(payload?.items || []);
      })
      .catch(() => {
        if (active) setPreviewItems([]);
      })
      .finally(() => {
        if (active) setPreviewLoading(false);
      });

    return () => {
      active = false;
    };
  }, [selectedFolder]);

  const submitSearch = () => {
    const nextQuery = queryInput.trim();
    setQuery(nextQuery);
    setSelectedFolder(null);
    loadFolders(nextQuery);
  };

  const handleCreate = async () => {
    if (!selectedFolder?.folder_name) {
      message.warning('请选择一个数据目录');
      return;
    }
    if (!modelName) {
      message.warning('请选择识别模型');
      return;
    }

    setCreating(true);
    try {
      const job = await areaApi.createJob({
        folder_name: selectedFolder.folder_name,
        model_name: modelName,
      });
      message.success('识别任务已提交');
      navigate(job?.job_id ? `/tools/area/jobs/${job.job_id}` : '/tools/area/tasks');
    } catch (error) {
      message.error(getAreaErrorMessage(error, '任务创建失败'));
    } finally {
      setCreating(false);
    }
  };

  const handleCleanupCompleted = async (result) => {
    const cleanedFolderName = result?.old_folder;
    setSelectedFolder((current) => (
      current?.folder_name === cleanedFolderName ? null : current
    ));
    await loadFolders(query);
  };

  const columns = useMemo(() => [
    {
      title: '数据目录',
      dataIndex: 'folder_name',
      ellipsis: true,
      render: (value, row) => (
        <Space className="area-start-folder-name">
          {selectedFolder?.folder_name === row.folder_name
            ? <CheckCircleFilled className="area-selected-icon" />
            : <FolderOpenOutlined className="area-muted-icon" />}
          <Typography.Text
            strong={selectedFolder?.folder_name === row.folder_name}
            ellipsis={{ tooltip: value }}
          >
            {value}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 172,
      responsive: ['md'],
      render: formatAreaDateTime,
    },
    {
      title: '操作',
      key: 'actions',
      width: 64,
      align: 'center',
      render: (_, row) => (
        <Tooltip title="整理目录：删除多余图片并可重命名">
          <Button
            type="text"
            icon={<ClearOutlined />}
            aria-label={`整理目录：${row.folder_name}`}
            onClick={(event) => {
              event.stopPropagation();
              setCleanupFolder(row);
            }}
          />
        </Tooltip>
      ),
    },
  ], [selectedFolder]);

  const runtimeUnavailable = Boolean(systemStatus && !systemStatus.ok);
  const canStart = Boolean(selectedFolder?.folder_name && modelName && systemStatus?.ok);

  return (
    <div className="area-page area-start-page">
      {runtimeUnavailable ? (
        <Alert
          className="area-system-alert"
          type="error"
          showIcon
          message="面积识别运行环境存在异常"
          description={systemStatus.issues?.map((item) => getAreaErrorMessage(item)).join('；')}
          action={<Button size="small" onClick={() => navigate('/tools/area/settings')}>检查设置</Button>}
        />
      ) : null}
      {contextError ? (
        <Alert
          className="area-system-alert"
          type="error"
          showIcon
          message={contextError}
          action={<Button size="small" onClick={loadPageContext}>重新加载</Button>}
        />
      ) : null}

      <div className="area-start-layout">
        <Card
          className="area-start-card area-start-source-card"
          title={(
            <Space>
              <span className="area-start-step">1</span>
              <span>选择数据目录</span>
            </Space>
          )}
          extra={<Typography.Text type="secondary">单击一行进行选择</Typography.Text>}
        >
          <Space.Compact block className="area-start-search">
            <Input
              value={queryInput}
              allowClear
              autoFocus
              prefix={<SearchOutlined />}
              placeholder="输入编号或文件夹名称"
              onChange={(event) => setQueryInput(event.target.value)}
              onPressEnter={submitSearch}
            />
            <Button type="primary" onClick={submitSearch}>搜索</Button>
            <Button
              icon={<ReloadOutlined />}
              title="刷新目录"
              onClick={() => loadFolders(query)}
            />
          </Space.Compact>

          {folderError ? <Alert type="error" showIcon message={folderError} /> : null}

          <Table
            className="area-select-table area-start-table"
            rowKey="folder_name"
            size="small"
            loading={foldersLoading}
            columns={columns}
            dataSource={folders}
            pagination={{
              pageSize: 8,
              hideOnSinglePage: true,
              showSizeChanger: false,
            }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={query ? '没有匹配的数据目录' : '暂无可用数据目录'}
                />
              ),
            }}
            rowClassName={(row) => (
              selectedFolder?.folder_name === row.folder_name ? 'area-row-selected' : ''
            )}
            onRow={(row) => ({
              onClick: () => setSelectedFolder(row),
            })}
          />
        </Card>

        <Card
          className="area-start-card area-start-confirm-card"
          title={(
            <Space>
              <span className="area-start-step">2</span>
              <span>核对并开始</span>
            </Space>
          )}
        >
          <div className={selectedFolder ? 'area-start-selected' : 'area-start-selected area-start-selected--empty'}>
            {selectedFolder ? (
              <>
                <CheckCircleFilled className="area-selected-icon" />
                <div>
                  <Typography.Text type="secondary">已选择数据目录</Typography.Text>
                  <Typography.Text strong ellipsis={{ tooltip: selectedFolder.folder_name }}>
                    {selectedFolder.folder_name}
                  </Typography.Text>
                </div>
              </>
            ) : (
              <>
                <FolderOpenOutlined />
                <Typography.Text type="secondary">请先从左侧选择数据目录</Typography.Text>
              </>
            )}
          </div>

          <label className="area-start-model">
            <Typography.Text strong>识别模型</Typography.Text>
            <Select
              value={modelName || undefined}
              placeholder="选择识别模型"
              options={modelOptions.map((name) => ({ value: name, label: name }))}
              onChange={setModelName}
              notFoundContent="暂无可用模型"
            />
          </label>

          <div className="area-start-preview-heading">
            <Typography.Text strong>图片预览</Typography.Text>
            <Typography.Text type="secondary">自动读取最多 6 张</Typography.Text>
          </div>
          <Spin spinning={previewLoading}>
            {selectedFolder && previewItems.length ? (
              <Image.PreviewGroup>
                <div className="area-preview-strip area-start-preview-strip">
                  {previewItems.map((item) => (
                    <Image
                      key={item.name}
                      src={areaApi.getFolderImageUrl(selectedFolder.folder_name, item.name)}
                      alt={item.name}
                      loading="lazy"
                      fallback="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
                    />
                  ))}
                </div>
              </Image.PreviewGroup>
            ) : (
              <div className="area-start-preview-empty">
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={selectedFolder ? '暂无可预览图片' : '选择目录后显示预览'}
                />
              </div>
            )}
          </Spin>

          <Button
            className="area-start-submit"
            type="primary"
            size="large"
            block
            icon={<PlayCircleOutlined />}
            loading={creating}
            disabled={!canStart}
            onClick={handleCreate}
          >
            开始识别
          </Button>
          <Typography.Text className="area-start-submit-hint" type="secondary">
            提交后将直接进入任务进度与结果页面
          </Typography.Text>
        </Card>
      </div>

      <AreaFolderCleanupModal
        folder={cleanupFolder}
        onClose={() => setCleanupFolder(null)}
        onCompleted={handleCleanupCompleted}
      />
    </div>
  );
}

export default AreaStartRecognition;
