import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CloudDownloadOutlined,
  CompressOutlined,
  SearchOutlined,
  StopOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import { Button, Card, Empty, Input, Modal, Progress, Spin, Tooltip, message } from 'antd';

import { FixedSizeGrid as Grid } from 'react-window';
import { resultsApi } from '../api/results';
import './ResultsImages.css';

const MIN_COLUMN_WIDTH = 190;
const ROW_HEIGHT = 190;
const SCROLLBAR_GUTTER = 16;
const PAGE_SIZE = 400;
const MAX_CONCURRENT = 4;
const PREFETCH_ROWS = 4;
const IMAGE_TIMEOUT_MS = 12000;

function ResultsImages({
  deviceId: propDeviceId,
  folder: propFolder,
  embedded = false,
  layoutVersion = 0,
}) {
  const params = new URLSearchParams(window.location.search);
  const deviceId = propDeviceId ?? params.get('device_id');
  const requestedFolder = propFolder ?? params.get('folder');

  const containerRef = useRef(null);
  const thumbCacheRef = useRef(new Map());
  const pendingRef = useRef(new Set());
  const queueRef = useRef([]);
  const retryRef = useRef(new Map());
  const activeRef = useRef(0);
  const controllersRef = useRef(new Map());
  const desiredKeysRef = useRef(new Set());
  const visibleCenterRef = useRef(0);
  const failedRef = useRef(new Set());
  const [, setCacheTick] = useState(0);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState([]);
  const [searchText, setSearchText] = useState('');
  const [previewItem, setPreviewItem] = useState(null);
  const [zoom, setZoom] = useState(1);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [folder, setFolder] = useState(null);
  const [loadingAllMeta, setLoadingAllMeta] = useState(false);
  const [loadAllActive, setLoadAllActive] = useState(false);
  const [loadAllProgress, setLoadAllProgress] = useState(0);
  const [loadedCount, setLoadedCount] = useState(0);
  const [failedCount, setFailedCount] = useState(0);
  const [loadAllDuration, setLoadAllDuration] = useState(null);
  const [gridHeight, setGridHeight] = useState(600);
  const previewContainerRef = useRef(null);
  const loadAllStartRef = useRef(null);
  const loadAllCancelledRef = useRef(false);

  const [containerWidth, setContainerWidth] = useState(0);
  const [columns, setColumns] = useState(1);

  const filteredItems = useMemo(() => {
    if (!searchText) return items;
    const query = searchText.trim().toLowerCase();
    if (!query) return items;
    return items.filter(item => item.name.toLowerCase().includes(query));
  }, [items, searchText]);
  const hasVisibleItems = filteredItems.length > 0;
  const rows = Math.ceil(filteredItems.length / columns);

  const buildImageUrls = useCallback((name, targetFolder) => {
    return {
      fullUrl: resultsApi.getImageUrl(deviceId, name, targetFolder),
      thumbUrl: resultsApi.getThumbUrl(deviceId, name, targetFolder),
    };
  }, [deviceId]);


  const processQueue = useCallback(() => {
    while (activeRef.current < MAX_CONCURRENT && queueRef.current.length > 0) {
      queueRef.current.sort((a, b) => a.priority - b.priority);
      const item = queueRef.current.shift();
      if (!item) continue;
      const { cacheKey, url } = item;
      if (thumbCacheRef.current.has(cacheKey)) {
        pendingRef.current.delete(cacheKey);
        continue;
      }
      activeRef.current += 1;
      const controller = new AbortController();
      controllersRef.current.set(cacheKey, controller);
      const timeoutId = setTimeout(() => controller.abort(), IMAGE_TIMEOUT_MS);
      fetch(url, { cache: 'force-cache', signal: controller.signal })
        .then(response => {
          if (!response.ok) {
            throw new Error('load_failed');
          }
          return response.blob();
        })
        .then(blob => {
          if (!thumbCacheRef.current.has(cacheKey)) {
            const blobUrl = URL.createObjectURL(blob);
            thumbCacheRef.current.set(cacheKey, blobUrl);
            setLoadedCount(prev => prev + 1);
            setCacheTick(tick => tick + 1);
          }
          retryRef.current.delete(cacheKey);
          pendingRef.current.delete(cacheKey);
        })
        .catch((error) => {
          if (error?.name === 'AbortError') {
            pendingRef.current.delete(cacheKey);
            return;
          }
          const retries = retryRef.current.get(cacheKey) || 0;
          pendingRef.current.delete(cacheKey);
          if (retries < 2) {
            retryRef.current.set(cacheKey, retries + 1);
            queueRef.current.push({ cacheKey, url, priority: item.priority + 1 });
          } else if (!failedRef.current.has(cacheKey)) {
            failedRef.current.add(cacheKey);
            setFailedCount(prev => prev + 1);
          }
        })
        .finally(() => {
          clearTimeout(timeoutId);
          controllersRef.current.delete(cacheKey);
          activeRef.current -= 1;
          processQueue();
        });
    }
  }, []);

  const enqueueImage = useCallback((item, priority = 0) => {
    if (!item) return;
    const cacheKey = item.cacheKey;
    if (
      thumbCacheRef.current.has(cacheKey)
      || pendingRef.current.has(cacheKey)
      || failedRef.current.has(cacheKey)
    ) {
      if (pendingRef.current.has(cacheKey)) {
        const queued = queueRef.current.find(entry => entry.cacheKey === cacheKey);
        if (queued) {
          queued.priority = Math.min(queued.priority, priority);
        }
      }
      return;
    }
    pendingRef.current.add(cacheKey);
    queueRef.current.push({ cacheKey, url: item.thumbUrl, priority });
    processQueue();
  }, [processQueue]);

  const pruneQueue = useCallback((desiredKeys) => {
    queueRef.current = queueRef.current.filter(item => {
      if (desiredKeys.has(item.cacheKey)) {
        return true;
      }
      pendingRef.current.delete(item.cacheKey);
      return false;
    });
    controllersRef.current.forEach((controller, key) => {
      if (desiredKeys.has(key) || thumbCacheRef.current.has(key)) {
        return;
      }
      controller.abort();
      controllersRef.current.delete(key);
      pendingRef.current.delete(key);
    });
  }, []);

  const scheduleVisibleImages = useCallback((startRow, endRow) => {
    if (!filteredItems.length) return;
    const startIndex = Math.max(0, startRow * columns);
    const endIndex = Math.min(
      filteredItems.length - 1,
      (endRow + 1) * columns - 1
    );
    const desiredKeys = new Set();
    const centerIndex = Math.floor((startIndex + endIndex) / 2);
    visibleCenterRef.current = centerIndex;
    for (let index = startIndex; index <= endIndex; index += 1) {
      const item = filteredItems[index];
      if (!item) continue;
      desiredKeys.add(item.cacheKey);
      const priority = Math.abs(index - centerIndex);
      enqueueImage(item, priority);
    }
    desiredKeysRef.current = desiredKeys;
    if (!loadAllActive) {
      pruneQueue(desiredKeys);
    }
  }, [columns, enqueueImage, filteredItems, loadAllActive, pruneQueue]);

  const loadPage = async (pageToLoad, reset = false) => {
    if (!deviceId) return null;
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
        const urls = buildImageUrls(item.name, nextFolder);
        return {
          ...item,
          cacheKey,
          folder: nextFolder || null,
          fullUrl: urls.fullUrl,
          thumbUrl: urls.thumbUrl,
        };
      });
      setFolder(nextFolder || null);
      setTotal(data.total || 0);
      setItems(prev => reset ? newItems : [...prev, ...newItems]);
      setPage(pageToLoad);
      return { newItemsCount: newItems.length, total: data.total || 0 };
    } catch (error) {
      message.error('图片加载失败');
      return null;
    } finally {
      setLoading(false);
    }
  };

  const formatDuration = (durationMs) => {
    if (durationMs == null) return '';
    const totalSeconds = Math.max(0, durationMs / 1000);
    if (totalSeconds < 60) {
      return `${totalSeconds.toFixed(1)}s`;
    }
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.round(totalSeconds % 60);
    return `${minutes}m${String(seconds).padStart(2, '0')}s`;
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
    thumbCacheRef.current.forEach(url => URL.revokeObjectURL(url));
    thumbCacheRef.current.clear();
    pendingRef.current.clear();
    retryRef.current.clear();
    failedRef.current.clear();
    controllersRef.current.forEach(controller => controller.abort());
    controllersRef.current.clear();
    queueRef.current = [];
    activeRef.current = 0;
    setLoadedCount(0);
    setFailedCount(0);
    setLoadAllProgress(0);
    setLoadAllActive(false);
    setLoadingAllMeta(false);
    setLoadAllDuration(null);
    loadAllStartRef.current = null;
    loadAllCancelledRef.current = false;
    loadPage(1, true);

  }, [deviceId, requestedFolder]);

  useEffect(() => {
    if (!hasVisibleItems) return undefined;
    const element = containerRef.current;
    if (!element) return undefined;
    const updateSize = () => {
      // clientWidth/clientHeight are not affected by the modal's scale animation.
      const width = Math.floor(element.clientWidth || element.parentElement?.clientWidth || 0);
      const height = Math.floor(element.clientHeight || 0);
      if (width > 0) {
        const nextColumns = Math.max(
          1,
          Math.floor(Math.max(1, width - SCROLLBAR_GUTTER) / MIN_COLUMN_WIDTH)
        );
        setContainerWidth(previous => previous === width ? previous : width);
        setColumns(previous => previous === nextColumns ? previous : nextColumns);
      }
      if (height > 0) {
        setGridHeight(previous => previous === height ? previous : height);
      }
    };
    updateSize();
    const frameId = window.requestAnimationFrame(updateSize);
    const settledTimerId = window.setTimeout(updateSize, 350);
    let observer;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(updateSize);
      observer.observe(element);
    }
    window.addEventListener('resize', updateSize);
    return () => {
      window.cancelAnimationFrame(frameId);
      window.clearTimeout(settledTimerId);
      observer?.disconnect();
      window.removeEventListener('resize', updateSize);
    };
  }, [hasVisibleItems, layoutVersion]);

  const onItemsRendered = ({ visibleRowStartIndex, visibleRowStopIndex }) => {
    if (rows <= 0) return;
    const startRow = Math.max(0, visibleRowStartIndex - PREFETCH_ROWS);
    const endRow = Math.min(rows - 1, visibleRowStopIndex + PREFETCH_ROWS);
    scheduleVisibleImages(startRow, endRow);
    if (loadingAllMeta) return;
    const loadedRows = Math.ceil(items.length / columns);
    if (!loading && items.length < total && visibleRowStopIndex >= loadedRows - 6) {
      loadPage(page + 1);
    }
  };

  const handleLoadAll = async () => {
    if (loadingAllMeta || loading) return;
    setLoadAllActive(true);
    setLoadingAllMeta(true);
    setLoadAllProgress(0);
    setLoadAllDuration(null);
    loadAllStartRef.current = Date.now();
    loadAllCancelledRef.current = false;
    let currentPage = page;
    let loadedMetaCount = items.length;
    let totalCount = total;

    try {
      if (loadedMetaCount === 0) {
        const result = await loadPage(1, true);
        if (!result) {
          message.error('加载全部失败，请重试。');
          setLoadAllActive(false);
          return;
        }
        currentPage = 1;
        loadedMetaCount = result.newItemsCount;
        totalCount = result.total;
      }

      if (!totalCount) {
        setLoadAllProgress(0);
        setLoadAllActive(false);
        return;
      }

      while (loadedMetaCount < totalCount) {
        if (loadAllCancelledRef.current) {
          break;
        }
        const result = await loadPage(currentPage + 1);
        if (!result) {
          message.error('加载全部失败，请重试。');
          setLoadAllActive(false);
          break;
        }
        currentPage += 1;
        loadedMetaCount += result.newItemsCount;
        totalCount = result.total;
        if (!totalCount || result.newItemsCount === 0) {
          break;
        }
      }
    } finally {
      setLoadingAllMeta(false);
    }
  };

  const handleStopLoadAll = () => {
    loadAllCancelledRef.current = true;
    setLoadAllActive(false);
    setLoadingAllMeta(false);
    setLoadAllDuration(null);
    loadAllStartRef.current = null;
    pruneQueue(desiredKeysRef.current);
  };

  useEffect(() => {
    if (!loadAllActive) return;
    if (!total) {
      setLoadAllProgress(0);
      return;
    }
    const completed = loadedCount + failedCount;
    const progress = Math.min(100, Math.round((completed / total) * 100));
    setLoadAllProgress(progress);
    if (completed >= total) {
      if (loadAllStartRef.current) {
        setLoadAllDuration(Date.now() - loadAllStartRef.current);
        loadAllStartRef.current = null;
      }
      setLoadAllActive(false);
    }
  }, [failedCount, loadAllActive, loadedCount, total]);

  useEffect(() => {
    if (!loadAllActive || items.length === 0) return;
    const desiredKeys = new Set();
    const centerIndex = visibleCenterRef.current;
    items.forEach((item, index) => {
      if (!item) return;
      desiredKeys.add(item.cacheKey);
      const priority = Math.abs(index - centerIndex) + PREFETCH_ROWS * columns;
      enqueueImage(item, priority);
    });
    desiredKeysRef.current = desiredKeys;
  }, [columns, enqueueImage, items, loadAllActive]);

  const Cell = ({ columnIndex, rowIndex, style }) => {
    const index = rowIndex * columns + columnIndex;
    const item = filteredItems[index];

    if (!item) return <div style={style} />;
    const cachedUrl = thumbCacheRef.current.get(item.cacheKey);
    const loadFailed = failedRef.current.has(item.cacheKey);
    if (!cachedUrl) {
      enqueueImage(item);
    }
    return (
      <div className="results-images-cell" style={style}>
        <button
          type="button"
          className="results-images-tile"
          title={item.name}
          onClick={() => setPreviewItem(item)}
        >
          <span className="results-images-tile__preview">
            {cachedUrl ? (
              <img
                src={cachedUrl}
                alt={item.name}
                loading="lazy"
                decoding="async"
              />
            ) : loadFailed ? (
              <span className="results-images-tile__error">缩略图加载失败</span>
            ) : (
              <Spin size="small" />
            )}
          </span>
          <span className="results-images-tile__name">{item.name}</span>
        </button>
      </div>
    );
  };

  const usableGridWidth = Math.max(1, containerWidth - SCROLLBAR_GUTTER);
  const columnWidth = Math.max(1, Math.floor(usableGridWidth / columns));
  const gridWidth = containerWidth;
  const gridHeightValue = Math.max(1, gridHeight);

  useEffect(() => {
    return () => {
      thumbCacheRef.current.forEach(url => URL.revokeObjectURL(url));
      thumbCacheRef.current.clear();
      pendingRef.current.clear();
      retryRef.current.clear();
      failedRef.current.clear();
      controllersRef.current.forEach(controller => controller.abort());
      controllersRef.current.clear();
      queueRef.current = [];
      activeRef.current = 0;
    };
  }, []);

  const previewUrl = previewItem ? previewItem.fullUrl : null;

  const handleZoomIn = () => setZoom(prev => Math.min(prev + 0.2, 3));
  const handleZoomOut = () => setZoom(prev => Math.max(prev - 0.2, 0.4));
  const handleZoomReset = () => setZoom(1);
  const handleWheelZoom = useCallback((event) => {
    const element = previewContainerRef.current;
    if (!element || !element.contains(event.target)) return;
    event.preventDefault();
    const direction = event.deltaY > 0 ? -0.1 : 0.1;
    setZoom(prev => {
      const next = prev + direction;
      return Math.min(Math.max(next, 0.4), 3);
    });
  }, []);

  useEffect(() => {
    if (!previewItem) return;
    window.addEventListener('wheel', handleWheelZoom, { passive: false });
    return () => window.removeEventListener('wheel', handleWheelZoom);
  }, [handleWheelZoom, previewItem]);

  useEffect(() => {
    if (!previewItem) return;
    const handleKeyDown = (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
      }
      const currentIndex = filteredItems.findIndex(item => item.cacheKey === previewItem.cacheKey);
      if (currentIndex < 0) return;
      const nextIndex = event.key === 'ArrowRight' ? currentIndex + 1 : currentIndex - 1;
      if (nextIndex < 0 || nextIndex >= filteredItems.length) return;
      const nextItem = filteredItems[nextIndex];
      setPreviewItem(nextItem);
      setZoom(1);
      enqueueImage(nextItem);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [enqueueImage, filteredItems, previewItem]);

  const summaryText = searchText.trim()
    ? `匹配 ${filteredItems.length} / ${items.length} 张`
    : `已载入 ${items.length} / ${total || items.length} 张`;

  const toolbar = (
    <div className="results-images-toolbar">
      <span className="results-images-toolbar__summary">{summaryText}</span>
      <div className="results-images-toolbar__controls">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索已载入文件"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          className="results-images-toolbar__search"
        />
        <Button
          icon={<CloudDownloadOutlined />}
          onClick={handleLoadAll}
          loading={loadingAllMeta}
          disabled={loading || loadAllActive}
        >
          加载全部
        </Button>
        {loadAllActive && (
          <Button danger icon={<StopOutlined />} onClick={handleStopLoadAll}>
            停止加载
          </Button>
        )}
      </div>
    </div>
  );

  const loadStatus = (loadAllActive || loadAllDuration != null) && (
    <div className="results-images-load-status">
      {loadAllActive && <Progress percent={loadAllProgress} size="small" />}
      <div className="results-images-load-status__detail">
        元数据 {Math.min(items.length, total)} / {total || 0}
        {'，'}缩略图 {Math.min(loadedCount, total)} / {total || 0}
        {failedCount ? `，失败 ${failedCount}` : ''}
        {loadAllDuration != null ? `，耗时 ${formatDuration(loadAllDuration)}` : ''}
      </div>
    </div>
  );

  const galleryContent = (
    <div className="results-images-content">
      {loading && items.length === 0 ? (
        <div className="results-images-state">
          <Spin />
        </div>
      ) : items.length === 0 ? (
        <div className="results-images-state">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={folder ? `结果目录 ${folder} 中没有图片` : '当前结果目录没有图片'}
          />
        </div>
      ) : filteredItems.length === 0 ? (
        <div className="results-images-state">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的图片" />
        </div>
      ) : (
        <div ref={containerRef} className="results-images-grid-shell">
          {containerWidth > 0 ? (
            <Grid
              key={`${layoutVersion}-${columns}-${columnWidth}`}
              className="results-images-grid"
              columnCount={columns}
              columnWidth={columnWidth}
              height={gridHeightValue}
              rowCount={rows}
              rowHeight={ROW_HEIGHT}
              width={gridWidth}
              onItemsRendered={onItemsRendered}
              style={{ overflowX: 'hidden', overflowY: 'auto' }}
            >
              {Cell}
            </Grid>
          ) : (
            <div className="results-images-state">
              <Spin />
            </div>
          )}
        </div>
      )}
    </div>
  );

  return (
    <div className={`results-images-root results-images-root--${embedded ? 'embedded' : 'standalone'}`}>
      {embedded ? (
        <>
          {toolbar}
          {loadStatus}
          {galleryContent}
        </>
      ) : (
        <Card title="结果图片" bodyStyle={{ padding: 0 }}>
          {toolbar}
          {loadStatus}
          {galleryContent}
        </Card>
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
        bodyStyle={{ textAlign: 'center', padding: 16, height: '80vh', display: 'flex', flexDirection: 'column' }}
        destroyOnClose
      >
        <div className="results-images-preview">
          <div className="results-images-preview__toolbar">
            <Tooltip title="缩小">
              <Button aria-label="缩小" icon={<ZoomOutOutlined />} onClick={handleZoomOut} />
            </Tooltip>
            <Tooltip title="复位">
              <Button aria-label="复位" icon={<CompressOutlined />} onClick={handleZoomReset} />
            </Tooltip>
            <span className="results-images-preview__zoom">{Math.round(zoom * 100)}%</span>
            <Tooltip title="放大">
              <Button aria-label="放大" icon={<ZoomInOutlined />} onClick={handleZoomIn} />
            </Tooltip>
          </div>
          {previewUrl ? (
            <div ref={previewContainerRef} className="results-images-preview__canvas">
              <img
                src={previewUrl}
                alt={previewItem?.name || 'preview'}
                decoding="async"
                style={{ transform: `scale(${zoom})` }}
              />
            </div>
          ) : (
            <div className="results-images-state">图片加载中...</div>
          )}
        </div>
      </Modal>
    </div>
  );
}

export default ResultsImages;
