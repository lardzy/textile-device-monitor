import {
  ClearOutlined,
  EyeOutlined,
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
  List,
  message,
  Pagination,
  Space,
  Spin,
  Table,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { areaApi } from '../../api/area';
import AreaFolderCleanupModal from './AreaFolderCleanupModal';
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
  const previewRequestIdRef = useRef(0);

  const [cleanupFolder, setCleanupFolder] = useState(null);

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
    const requestId = previewRequestIdRef.current + 1;
    previewRequestIdRef.current = requestId;
    setPreviewLoading(true);
    try {
      const payload = await areaApi.listFolderImages(folderName, { page: nextPage, page_size: 50 });
      if (previewRequestIdRef.current !== requestId) return;
      setPreviewItems(payload?.items || []);
      setPreviewTotal(Number(payload?.total || 0));
      setPreviewPage(Number(payload?.page || nextPage));
    } catch (error) {
      if (previewRequestIdRef.current !== requestId) return;
      message.error(getAreaErrorMessage(error, '图片加载失败'));
      setPreviewItems([]);
      setPreviewTotal(0);
    } finally {
      if (previewRequestIdRef.current === requestId) setPreviewLoading(false);
    }
  }, []);

  const openPreview = (folder) => {
    setPreviewFolder(folder);
    setPreviewPage(1);
    loadPreviewImages(folder.folder_name, 1);
  };

  const closePreview = useCallback(() => {
    previewRequestIdRef.current += 1;
    setPreviewFolder(null);
    setPreviewItems([]);
    setPreviewTotal(0);
    setPreviewPage(1);
    setPreviewLoading(false);
  }, []);

  useEffect(() => () => {
    previewRequestIdRef.current += 1;
  }, []);

  const handleCleanupCompleted = async () => {
    if (query) {
      setQuery('');
      setQueryInput('');
      setPage(1);
    } else {
      await loadFolders({ nextPage: 1, nextQuery: '' });
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
          <Tooltip title="整理目录：删除多余图片并可重命名">
            <Button
              type="text"
              icon={<ClearOutlined />}
              aria-label={`整理目录：${row.folder_name}`}
              onClick={() => setCleanupFolder(row)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const submitSearch = () => {
    setQuery(queryInput.trim());
    setPage(1);
  };

  const previewFolderName = previewFolder?.folder_name || '';

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
        title={previewFolderName ? `${previewFolderName} · ${previewTotal} 张图片` : '图片预览'}
        onClose={closePreview}
      >
        <Spin spinning={previewLoading}>
          {previewFolderName && previewItems.length ? (
            <Image.PreviewGroup>
              <List
                className="area-folder-gallery"
                grid={{ gutter: 12, xs: 2, sm: 3, md: 4, lg: 4, xl: 5, xxl: 5 }}
                dataSource={previewItems}
                renderItem={(item) => (
                  <List.Item>
                    <div className="area-folder-image">
                      <Image
                        src={areaApi.getFolderImageUrl(previewFolderName, item.name)}
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
        {previewFolderName && previewTotal > 50 ? (
          <Pagination
            current={previewPage}
            pageSize={50}
            total={previewTotal}
            showSizeChanger={false}
            onChange={(nextPage) => loadPreviewImages(previewFolderName, nextPage)}
          />
        ) : null}
      </Drawer>

      <AreaFolderCleanupModal
        folder={cleanupFolder}
        onClose={() => setCleanupFolder(null)}
        onCompleted={handleCleanupCompleted}
      />
    </div>
  );
}

export default AreaFolders;
