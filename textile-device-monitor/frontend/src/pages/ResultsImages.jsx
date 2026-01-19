import { useEffect, useRef, useState } from 'react';
import { Button, Card, Spin, message } from 'antd';
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

  const containerRef = useRef(null);
  const cacheRef = useRef(new Map());
  const pendingRef = useRef(new Set());
  const queueRef = useRef([]);
  const retryRef = useRef(new Map());
  const activeRef = useRef(0);
  const [cacheTick, setCacheTick] = useState(0);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [folder, setFolder] = useState(null);
  const [containerWidth, setContainerWidth] = useState(window.innerWidth);
  const [columns, setColumns] = useState(Math.max(1, Math.floor(window.innerWidth / COLUMN_WIDTH)));

  const rows = Math.ceil(items.length / columns);

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
      const data = await resultsApi.getImages(deviceId, { page: pageToLoad, page_size: PAGE_SIZE });
      const nextFolder = data.folder || folder;
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
    cacheRef.current.forEach(url => URL.revokeObjectURL(url));
    cacheRef.current.clear();
    pendingRef.current.clear();
    retryRef.current.clear();
    queueRef.current = [];
    activeRef.current = 0;
    loadPage(1, true);
  }, [deviceId]);

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
    const item = items[index];
    if (!item) return <div style={style} />;
    const cachedUrl = cacheRef.current.get(item.cacheKey);
    if (!cachedUrl) {
      enqueueImage(item);
    }
    return (
      <div style={{ ...style, padding: 6, boxSizing: 'border-box' }}>
        <div style={{ border: '1px solid #eee', padding: 6, borderRadius: 6 }}>
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

  return (
    <div style={{ padding: 24 }}>
      <Card title="结果图片" extra={<Button onClick={() => loadPage(1, true)}>刷新</Button>}>
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
      </Card>
    </div>
  );
}

export default ResultsImages;
