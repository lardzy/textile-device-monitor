import {
  DeleteOutlined,
  EyeOutlined,
  FolderOpenOutlined,
  MoreOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Dropdown,
  Empty,
  Image,
  Input,
  List,
  message,
  Modal,
  Pagination,
  Space,
  Spin,
  Table,
  Typography,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { areaApi } from '../../api/area';
import { formatAreaDateTime, getAreaErrorMessage } from './areaUtils';

function AreaFolders() {
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState('');

  const [previewFolder, setPreviewFolder] = useState(null);
  const [previewItems, setPreviewItems] = useState([]);
  const [previewTotal, setPreviewTotal] = useState(0);
  const [previewPage, setPreviewPage] = useState(1);
  const [previewLoading, setPreviewLoading] = useState(false);

  const [cleanupFolder, setCleanupFolder] = useState(null);
  const [cleanupPreview, setCleanupPreview] = useState(null);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [cleanupExecuting, setCleanupExecuting] = useState(false);
  const [renameEnabled, setRenameEnabled] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');

  const loadFolders = useCallback(async ({ nextPage = page, nextQuery = query } = {}) => {
    setLoading(true);
    setErrorText('');
    try {
      const payload = nextQuery
        ? await areaApi.searchFolders({ q: nextQuery, limit: 100 })
        : await areaApi.listRecentFolders({ page: nextPage, page_size: 20, limit: 100 });
      setItems(payload?.items || []);
      setTotal(nextQuery ? Number(payload?.items?.length || 0) : Number(payload?.total || 0));
      setPage(nextQuery ? 1 : Number(payload?.page || nextPage));
    } catch (error) {
      setItems([]);
      setTotal(0);
      setErrorText(getAreaErrorMessage(error, '目录加载失败'));
    } finally {
      setLoading(false);
    }
  }, [page, query]);

  useEffect(() => {
    loadFolders({ nextPage: 1, nextQuery: query });
  }, [query]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadPreviewImages = useCallback(async (folderName, nextPage = 1) => {
    setPreviewLoading(true);
    try {
      const payload = await areaApi.listFolderImages(folderName, { page: nextPage, page_size: 50 });
      setPreviewItems(payload?.items || []);
      setPreviewTotal(Number(payload?.total || 0));
      setPreviewPage(Number(payload?.page || nextPage));
    } catch (error) {
      message.error(getAreaErrorMessage(error, '图片加载失败'));
      setPreviewItems([]);
      setPreviewTotal(0);
    } finally {
      setPreviewLoading(false);
    }
  }, []);

  const openPreview = (folder) => {
    setPreviewFolder(folder);
    setPreviewPage(1);
    loadPreviewImages(folder.folder_name, 1);
  };

  const openCleanup = (folder) => {
    setCleanupFolder(folder);
    setCleanupPreview(null);
    setRenameEnabled(false);
    setNewFolderName(folder.folder_name);
  };

  useEffect(() => {
    if (!cleanupFolder) return undefined;
    let active = true;
    const timer = window.setTimeout(async () => {
      setCleanupLoading(true);
      try {
        const payload = await areaApi.previewFolderCleanup(cleanupFolder.folder_name, {
          rename_enabled: renameEnabled,
          new_folder_name: renameEnabled ? newFolderName.trim() : null,
        });
        if (active) setCleanupPreview(payload);
      } catch (error) {
        if (active) {
          setCleanupPreview(null);
          message.error(getAreaErrorMessage(error, '目录整理预检失败'));
        }
      } finally {
        if (active) setCleanupLoading(false);
      }
    }, 250);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [cleanupFolder, newFolderName, renameEnabled]);

  const executeCleanup = async () => {
    if (!cleanupFolder) return;
    if (renameEnabled && !newFolderName.trim()) {
      message.warning('请输入新的文件夹名称');
      return;
    }
    if (cleanupPreview?.rename_target_exists) {
      message.error('目标文件夹名称已存在');
      return;
    }
    setCleanupExecuting(true);
    try {
      const result = await areaApi.cleanupFolder(cleanupFolder.folder_name, {
        rename_enabled: renameEnabled,
        new_folder_name: renameEnabled ? newFolderName.trim() : null,
      });
      message.success(`目录整理完成，已移动 ${Number(result?.moved || 0)} 个文件`);
      setCleanupFolder(null);
      setQuery('');
      setQueryInput('');
      await loadFolders({ nextPage: 1, nextQuery: '' });
    } catch (error) {
      message.error(getAreaErrorMessage(error, '目录整理失败'));
    } finally {
      setCleanupExecuting(false);
    }
  };

  const columns = [
    {
      title: '数据目录',
      dataIndex: 'folder_name',
      ellipsis: true,
      render: (value) => (
        <Space>
          <FolderOpenOutlined className="area-muted-icon" />
          <Typography.Text strong>{value}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 200,
      render: formatAreaDateTime,
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      align: 'right',
      render: (_, row) => (
        <Space size={4} onClick={(event) => event.stopPropagation()}>
          <Button type="text" icon={<EyeOutlined />} onClick={() => openPreview(row)}>预览</Button>
          <Dropdown
            trigger={['click']}
            menu={{
              items: [
                { key: 'cleanup', label: '整理目录', icon: <DeleteOutlined />, danger: true },
              ],
              onClick: ({ key }) => {
                if (key === 'cleanup') openCleanup(row);
              },
            }}
          >
            <Button type="text" icon={<MoreOutlined />} title="更多操作" />
          </Dropdown>
        </Space>
      ),
    },
  ];

  const submitSearch = () => {
    setQuery(queryInput.trim());
    setPage(1);
  };

  return (
    <div className="area-page">
      <div className="area-page-toolbar">
        <Space.Compact>
          <Input
            value={queryInput}
            prefix={<SearchOutlined />}
            placeholder="搜索编号或文件夹名称"
            allowClear
            onChange={(event) => setQueryInput(event.target.value)}
            onPressEnter={submitSearch}
            style={{ width: 340 }}
          />
          <Button onClick={submitSearch}>搜索</Button>
        </Space.Compact>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => loadFolders()}>
          刷新
        </Button>
      </div>

      {errorText ? <Alert type="error" showIcon message={errorText} className="area-system-alert" /> : null}

      <div className="area-table-surface">
        <Table
          rowKey="folder_name"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={query ? false : {
            current: page,
            pageSize: 20,
            total,
            showSizeChanger: false,
            showTotal: (value) => `共 ${value} 个目录`,
            onChange: (nextPage) => loadFolders({ nextPage }),
          }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={query ? '没有匹配的目录' : '暂无可用目录'} /> }}
          onRow={(row) => ({ onDoubleClick: () => openPreview(row) })}
        />
      </div>

      <Drawer
        open={Boolean(previewFolder)}
        width="min(920px, calc(100vw - 24px))"
        title={previewFolder ? `${previewFolder.folder_name} · ${previewTotal} 张图片` : '图片预览'}
        onClose={() => setPreviewFolder(null)}
      >
        <Spin spinning={previewLoading}>
          {previewItems.length ? (
            <Image.PreviewGroup>
              <List
                className="area-folder-gallery"
                grid={{ gutter: 12, xs: 2, sm: 3, md: 4, lg: 4, xl: 5, xxl: 5 }}
                dataSource={previewItems}
                renderItem={(item) => (
                  <List.Item>
                    <div className="area-folder-image">
                      <Image
                        src={areaApi.getFolderImageUrl(previewFolder.folder_name, item.name)}
                        alt={item.name}
                        loading="lazy"
                      />
                      <Typography.Text ellipsis={{ tooltip: item.name }}>{item.name}</Typography.Text>
                    </div>
                  </List.Item>
                )}
              />
            </Image.PreviewGroup>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="目录中没有图片" />}
        </Spin>
        {previewTotal > 50 ? (
          <Pagination
            current={previewPage}
            pageSize={50}
            total={previewTotal}
            showSizeChanger={false}
            onChange={(nextPage) => loadPreviewImages(previewFolder.folder_name, nextPage)}
          />
        ) : null}
      </Drawer>

      <Modal
        open={Boolean(cleanupFolder)}
        title="整理采集目录"
        okText="确认整理"
        okButtonProps={{ danger: true, loading: cleanupExecuting, disabled: cleanupLoading || cleanupPreview?.rename_target_exists }}
        cancelText="取消"
        onCancel={() => setCleanupFolder(null)}
        onOk={executeCleanup}
      >
        <Alert
          type="warning"
          showIcon
          message="此操作会移动目录中的原始图片"
          description="非 *_i.jpg / *_i.jpeg 图片将移入同级 .recycle 目录，保留的采集图片不会被删除。"
        />
        <Spin spinning={cleanupLoading}>
          <div className="area-cleanup-summary">
            <Typography.Text>将移动</Typography.Text>
            <Typography.Title level={3}>{Number(cleanupPreview?.move_count || 0)}</Typography.Title>
            <Typography.Text type="secondary">个文件</Typography.Text>
            <Typography.Text>保留 {Number(cleanupPreview?.keep_count || 0)} 个采集文件</Typography.Text>
          </div>
        </Spin>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Checkbox checked={renameEnabled} onChange={(event) => setRenameEnabled(event.target.checked)}>
            整理后重命名文件夹
          </Checkbox>
          {renameEnabled ? (
            <Input
              status={cleanupPreview?.rename_target_exists ? 'error' : undefined}
              value={newFolderName}
              onChange={(event) => setNewFolderName(event.target.value)}
              placeholder="新的文件夹名称"
            />
          ) : null}
          {cleanupPreview?.rename_target_exists ? <Typography.Text type="danger">目标文件夹已经存在</Typography.Text> : null}
        </Space>
      </Modal>
    </div>
  );
}

export default AreaFolders;
