import {
  CheckCircleFilled,
  FolderOpenOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Image,
  Input,
  message,
  Select,
  Space,
  Spin,
  Table,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { areaApi } from '../../api/area';
import { formatAreaDateTime, getAreaErrorMessage } from './areaUtils';

function NewAreaJobDrawer({ open, modelOptions, onClose, onCreated }) {
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [folders, setFolders] = useState([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [folderError, setFolderError] = useState('');
  const [selectedFolder, setSelectedFolder] = useState(null);
  const [modelName, setModelName] = useState('');
  const [previewItems, setPreviewItems] = useState([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [creating, setCreating] = useState(false);

  const loadFolders = useCallback(async (nextQuery = '') => {
    setFoldersLoading(true);
    setFolderError('');
    try {
      const payload = nextQuery
        ? await areaApi.searchFolders({ q: nextQuery, limit: 50 })
        : await areaApi.listRecentFolders({ page: 1, page_size: 50, limit: 100 });
      setFolders(payload?.items || []);
    } catch (error) {
      setFolders([]);
      setFolderError(getAreaErrorMessage(error, '目录加载失败'));
    } finally {
      setFoldersLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setQueryInput('');
    setQuery('');
    setSelectedFolder(null);
    setPreviewItems([]);
    setModelName(modelOptions?.[0] || '');
    loadFolders('');
  }, [loadFolders, modelOptions, open]);

  useEffect(() => {
    if (!selectedFolder?.folder_name) {
      setPreviewItems([]);
      return;
    }
    let active = true;
    setPreviewLoading(true);
    areaApi.listFolderImages(selectedFolder.folder_name, { page: 1, page_size: 6 })
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
      message.success('任务已提交');
      onCreated(job);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '任务创建失败'));
    } finally {
      setCreating(false);
    }
  };

  const columns = useMemo(() => [
    {
      title: '数据目录',
      dataIndex: 'folder_name',
      ellipsis: true,
      render: (value, row) => (
        <Space>
          {selectedFolder?.folder_name === row.folder_name
            ? <CheckCircleFilled className="area-selected-icon" />
            : <FolderOpenOutlined className="area-muted-icon" />}
          <Typography.Text strong={selectedFolder?.folder_name === row.folder_name}>
            {value}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '图片',
      dataIndex: 'image_count',
      width: 80,
      align: 'right',
      render: (value) => `${Number(value || 0)} 张`,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      render: formatAreaDateTime,
    },
  ], [selectedFolder]);

  return (
    <Drawer
      open={open}
      title="新建识别任务"
      width="min(760px, calc(100vw - 24px))"
      destroyOnClose
      onClose={onClose}
      footer={(
        <div className="area-drawer-footer">
          <Typography.Text type="secondary">
            {selectedFolder
              ? `${selectedFolder.folder_name} · ${Number(selectedFolder.image_count || 0)} 张图片`
              : '尚未选择数据目录'}
          </Typography.Text>
          <Space>
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" loading={creating} onClick={handleCreate}>
              提交任务
            </Button>
          </Space>
        </div>
      )}
    >
      <div className="area-form-section">
        <Typography.Title level={5}>选择数据目录</Typography.Title>
        <Space.Compact block>
          <Input
            value={queryInput}
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索编号或文件夹名称"
            onChange={(event) => setQueryInput(event.target.value)}
            onPressEnter={submitSearch}
          />
          <Button onClick={submitSearch}>搜索</Button>
          <Button
            icon={<ReloadOutlined />}
            title="刷新目录"
            onClick={() => loadFolders(query)}
          />
        </Space.Compact>
        {folderError ? <Alert type="error" showIcon message={folderError} /> : null}
        <Table
          className="area-select-table"
          rowKey="folder_name"
          size="small"
          loading={foldersLoading}
          columns={columns}
          dataSource={folders}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={query ? '没有匹配的目录' : '暂无可用目录'} /> }}
          rowClassName={(row) => (selectedFolder?.folder_name === row.folder_name ? 'area-row-selected' : '')}
          onRow={(row) => ({
            onClick: () => setSelectedFolder(row),
          })}
        />
      </div>

      <div className="area-form-section">
        <Typography.Title level={5}>识别模型</Typography.Title>
        <Select
          value={modelName || undefined}
          placeholder="选择模型"
          options={(modelOptions || []).map((name) => ({ value: name, label: name }))}
          onChange={setModelName}
          style={{ width: '100%' }}
        />
      </div>

      {selectedFolder ? (
        <div className="area-form-section">
          <div className="area-section-heading">
            <Typography.Title level={5}>图片预览</Typography.Title>
            <Typography.Text type="secondary">{Number(selectedFolder.image_count || 0)} 张</Typography.Text>
          </div>
          <Spin spinning={previewLoading}>
            <Image.PreviewGroup>
              <div className="area-preview-strip">
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
          </Spin>
        </div>
      ) : null}
    </Drawer>
  );
}

export default NewAreaJobDrawer;
