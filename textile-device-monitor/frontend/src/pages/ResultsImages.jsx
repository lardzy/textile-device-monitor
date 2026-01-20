import { useEffect, useRef, useState } from 'react';
import { Button, Card, Input, Modal, Spin, message } from 'antd';

import { FixedSizeGrid as Grid } from 'react-window';
import { resultsApi } from '../api/results';

const COLUMN_WIDTH = 140;
const ROW_HEIGHT = 160;
const PAGE_SIZE = 400;
const MAX_CONCURRENT = 8;
const EMPTY_IMAGE =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';

function ResultsImages() {
  const params = new URLSearchParams(window.location.search);
  const deviceId = params.get('device_id');
  const requestedFolder = params.get('folder');

  const containerRef = useRef(null);
  const cacheRef = useRef(new Map());
  const pendingRef = useRef(new Set());
  const queueRef = useRef([]);
  const retryRef = useRef(new Map());
  const activeRef = useRef(0);
  const [cacheTick, setCacheTick] = useState(0);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState([]);
  const [searchText, setSearchText] = useState('');
  const [previewItem, setPreviewItem] = useState(null);
  const [zoom, setZoom] = useState(1);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [folder, setFolder] = useState(null);

  const [containerWidth, setContainerWidth] = useState(window.innerWidth);
  const [columns, setColumns] = useState(Math.max(1, Math.floor(window.innerWidth / COLUMN_WIDTH)));

  const filteredItems = searchText
    ? items.filter(item => item.name.toLowerCase().includes(searchText.trim().toLowerCase()))
    : items;
  const rows = Math.ceil(filteredItems.length / columns);


  const processQueue = () => {
    while (activeRef.current < MAX_CONCURRENT && queueRef.current.length > 0) {
      const item = queueRef.current.shift();
      if (!item) continue;
      const { cacheKey, url } = item;
      if (cacheRef.current.has(cacheKey)) {
        pendingRef.current.delete(cacheKey);
        continue;
      }
      activeRef.current += 1;
      fetch(url, { cache: 'force-cache' })
        .then(response => {
          if (!response.ok) {
            throw new Error('load_failed');
          }
          return response.blob();
        })
        .then(blob => {
          const blobUrl = URL.createObjectURL(blob);
          cacheRef.current.set(cacheKey, blobUrl);
          retryRef.current.delete(cacheKey);
          pendingRef.current.delete(cacheKey);
          setCacheTick(tick => tick + 1);
        })
        .catch(() => {
          const retries = retryRef.current.get(cacheKey) || 0;
          pendingRef.current.delete(cacheKey);
          if (retries < 2) {
            retryRef.current.set(cacheKey, retries + 1);
            queueRef.current.push({ cacheKey, url });
          }
        })
        .finally(() => {
          activeRef.current -= 1;
          processQueue();
        });
    }
  };

  const enqueueImage = (item) => {
    if (!item) return;
    const cacheKey = item.cacheKey;
    if (cacheRef.current.has(cacheKey) || pendingRef.current.has(cacheKey)) {
      return;
    }
    pendingRef.current.add(cacheKey);
    queueRef.current.push({ cacheKey, url: item.url });
    processQueue();
  };

  const loadPage = async (pageToLoad, reset = false) => {
    if (!deviceId) return;
    setLoading(true);
    try {
      const data = await resultsApi.getImages(deviceId, {
        page: pageToLoad,
        page_size: PAGE_SIZE,
        folder: requestedFolder || undefined,
      });
      const nextFolder = data.folder || requestedFolder || folder;
      const newItems = (data.items || []).map(item => {
        const cacheKey = `${nextFolder || 'latest'}/${item.name}`;
        return {
          ...item,
          cacheKey,
          url: resultsApi.getImageUrl(deviceId, item.name, nextFolder),
        };
      });
      setFolder(nextFolder || null);
      setTotal(data.total || 0);
      setItems(prev => reset ? newItems : [...prev, ...newItems]);
      setPage(pageToLoad);
      newItems.forEach(enqueueImage);
    } catch (error) {
      message.error('图片加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!deviceId) {
      message.error('缺少设备参数');
      return;
    }
    setFolder(null);
    setItems([]);
    setTotal(0);
    setPage(1);
    setPreviewItem(null);
    setZoom(1);
    cacheRef.current.forEach(url => URL.revokeObjectURL(url));
    cacheRef.current.clear();
    pendingRef.current.clear();
    retryRef.current.clear();
    queueRef.current = [];
    activeRef.current = 0;
    loadPage(1, true);

  }, [deviceId, requestedFolder]);

  useEffect(() => {
    const updateSize = () => {
      const width = containerRef.current?.clientWidth || window.innerWidth;
      setContainerWidth(width);
      setColumns(Math.max(1, Math.floor(width / COLUMN_WIDTH)));
    };
    updateSize();
    window.addEventListener('resize', updateSize);
    return () => window.removeEventListener('resize', updateSize);
  }, []);

  const onItemsRendered = ({ visibleRowStopIndex }) => {
    const loadedRows = Math.ceil(items.length / columns);
    if (!loading && items.length < total && visibleRowStopIndex >= loadedRows - 6) {
      loadPage(page + 1);
    }
  };

  const Cell = ({ columnIndex, rowIndex, style }) => {
    const index = rowIndex * columns + columnIndex;
    const item = filteredItems[index];

    if (!item) return <div style={style} />;
    const cachedUrl = cacheRef.current.get(item.cacheKey);
    if (!cachedUrl) {
      enqueueImage(item);
    }
    return (
      <div style={{ ...style, padding: 6, boxSizing: 'border-box' }}>
        <div
          style={{ border: '1px solid #eee', padding: 6, borderRadius: 6, cursor: 'pointer' }}
          onClick={() => setPreviewItem(item)}
        >
          <img
            src={cachedUrl || EMPTY_IMAGE}
            alt={item.name}
            style={{ width: '100%', height: 100, objectFit: 'contain', display: 'block', background: '#f6f6f6' }}
          />
          <div style={{ fontSize: 12, marginTop: 4, color: '#555', wordBreak: 'break-all' }}>{item.name}</div>
        </div>
      </div>

    );
  };

  const columnWidth = Math.max(120, Math.floor(containerWidth / columns));
  const gridWidth = containerWidth;

  useEffect(() => {
    return () => {
      cacheRef.current.forEach(url => URL.revokeObjectURL(url));
      cacheRef.current.clear();
      pendingRef.current.clear();
      retryRef.current.clear();
      queueRef.current = [];
      activeRef.current = 0;
    };
  }, []);

  const previewUrl = previewItem ? cacheRef.current.get(previewItem.cacheKey) || previewItem.url : null;

  const handleZoomIn = () => setZoom(prev => Math.min(prev + 0.2, 3));
  const handleZoomOut = () => setZoom(prev => Math.max(prev - 0.2, 0.4));
  const handleZoomReset = () => setZoom(1);
  const handleWheelZoom = (event) => {
    event.preventDefault();
    const direction = event.deltaY > 0 ? -0.1 : 0.1;
    setZoom(prev => {
      const next = prev + direction;
      return Math.min(Math.max(next, 0.4), 3);
    });
  };


  return (
    <div style={{ padding: 24 }}>
      <Card
        title="结果图片"
        extra={(
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <Input
              allowClear
              placeholder="模糊搜索文件名"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              style={{ width: 220 }}
            />
            <span style={{ fontSize: 12, color: '#999' }}>请将滚动条拉到底端后再进行搜索！</span>
            <Button onClick={() => loadPage(1, true)}>刷新</Button>
          </div>
        )}
      >
        {loading && items.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : (
          <div ref={containerRef} style={{ width: '100%' }}>
            <Grid
              columnCount={columns}
              columnWidth={columnWidth}
              height={600}
              rowCount={rows}
              rowHeight={ROW_HEIGHT}
              width={gridWidth}
              onItemsRendered={onItemsRendered}
            >
              {Cell}
            </Grid>
          </div>
        )}
        <Modal
          open={Boolean(previewItem)}
          title={previewItem?.name}
          footer={null}
          onCancel={() => {
            setPreviewItem(null);
            setZoom(1);
          }}
          width="80vw"
          style={{ top: 20 }}
          bodyStyle={{ textAlign: 'center', padding: 16 }}
          destroyOnClose
        >
          <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginBottom: 12 }}>
            <Button onClick={handleZoomOut}>缩小</Button>
            <Button onClick={handleZoomReset}>复位</Button>
            <Button onClick={handleZoomIn}>放大</Button>
          </div>
          {previewUrl ? (
            <div style={{ overflow: 'auto', maxHeight: '70vh' }} onWheel={handleWheelZoom}>
              <img
                src={previewUrl}
                alt={previewItem?.name || 'preview'}
                style={{
                  transform: `scale(${zoom})`,
                  transformOrigin: 'center center',
                  maxWidth: '100%',
                  maxHeight: '70vh',
                  objectFit: 'contain',
                  display: 'inline-block',
                }}
              />
            </div>
          ) : (
            <div style={{ padding: 24, color: '#999' }}>图片加载中...</div>
          )}
        </Modal>
      </Card>

    </div>
  );
}

export default ResultsImages;
