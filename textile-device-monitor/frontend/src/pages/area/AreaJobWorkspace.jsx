import {
  ArrowLeftOutlined,
  CheckOutlined,
  CompressOutlined,
  DeleteOutlined,
  DownloadOutlined,
  DragOutlined,
  EditOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  LeftOutlined,
  PlusOutlined,
  RedoOutlined,
  ReloadOutlined,
  RightOutlined,
  SaveOutlined,
  SearchOutlined,
  UndoOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Divider,
  Empty,
  Input,
  message,
  Modal,
  Progress,
  Segmented,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { areaApi } from '../../api/area';
import { getOrCreateQueueUserId } from '../../utils/localStorage';
import {
  ACTIVE_JOB_STATUSES,
  EDITABLE_JOB_STATUSES,
  formatAreaDateTime,
  getAreaErrorMessage,
  JOB_STATUS_META,
  makeClientInstanceId,
  polygonArea,
  polygonBbox,
} from './areaUtils';

const COLORS = ['#1677ff', '#13a8a8', '#52c41a', '#722ed1', '#fa8c16', '#eb2f96'];
const MIN_SCALE = 0.05;
const MAX_SCALE = 12;

const cloneInstances = (items) => items.map((item) => ({
  ...item,
  polygon: Array.isArray(item.polygon) ? item.polygon.map((point) => [...point]) : [],
  bbox: Array.isArray(item.bbox) ? [...item.bbox] : [],
}));

const getInstanceKey = (item) => String(item.instance_id || item.client_id || '');

function AreaJobWorkspace() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedImageId = Number(searchParams.get('image') || 0) || null;
  const queueUserId = useRef(getOrCreateQueueUserId());

  const [job, setJob] = useState(null);
  const [jobLoading, setJobLoading] = useState(true);
  const [images, setImages] = useState([]);
  const [imagesTotal, setImagesTotal] = useState(0);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [imageQueryInput, setImageQueryInput] = useState('');
  const [imageQuery, setImageQuery] = useState('');
  const [imageFilter, setImageFilter] = useState('all');

  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [instances, setInstances] = useState([]);
  const [selectedInstanceKey, setSelectedInstanceKey] = useState('');
  const [history, setHistory] = useState([]);
  const [future, setFuture] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const [tool, setTool] = useState('select');
  const [viewMode, setViewMode] = useState('overlay');
  const [showFill, setShowFill] = useState(true);
  const [maskOpacity, setMaskOpacity] = useState(0.28);
  const [drawClass, setDrawClass] = useState('');
  const [draftPoints, setDraftPoints] = useState([]);
  const [baseImageFailed, setBaseImageFailed] = useState(false);
  const [overlayImageFailed, setOverlayImageFailed] = useState(false);

  const canvasRef = useRef(null);
  const dragRef = useRef(null);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const [viewport, setViewport] = useState({ scale: 1, x: 0, y: 0 });
  const [manualViewport, setManualViewport] = useState(false);

  const classNames = detail?.class_names || [];
  const imageMeta = detail?.image || null;
  const naturalWidth = Number(imageMeta?.width || 0);
  const naturalHeight = Number(imageMeta?.height || 0);
  const selectedInstance = instances.find((item) => getInstanceKey(item) === selectedInstanceKey) || null;
  const selectedImageIndex = images.findIndex((item) => Number(item.image_id) === Number(selectedImageId));
  const sourceImageUrl = imageMeta?.source_url || (selectedImageId ? areaApi.getSourceImageUrl(jobId, selectedImageId) : '');
  const overlayImageUrl = imageMeta?.overlay_url || '';
  const activeImageUrl = !baseImageFailed && sourceImageUrl
    ? sourceImageUrl
    : (!overlayImageFailed ? overlayImageUrl : '');

  const applyDetail = useCallback((payload) => {
    const nextInstances = cloneInstances(payload?.instances || []);
    setDetail(payload);
    setInstances(nextInstances);
    setSelectedInstanceKey(nextInstances[0] ? getInstanceKey(nextInstances[0]) : '');
    setHistory([]);
    setFuture([]);
    setDirty(false);
    setDraftPoints([]);
    setTool('select');
    setBaseImageFailed(false);
    setOverlayImageFailed(false);
    const nextClasses = payload?.class_names || [];
    setDrawClass((current) => (nextClasses.includes(current) ? current : (nextClasses[0] || '')));
    setManualViewport(false);
  }, []);

  const loadJob = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setJobLoading(true);
    try {
      const payload = await areaApi.getJob(jobId);
      setJob(payload);
      return payload;
    } catch (error) {
      message.error(getAreaErrorMessage(error, '任务加载失败'));
      return null;
    } finally {
      if (!quiet) setJobLoading(false);
    }
  }, [jobId]);

  const loadImages = useCallback(async ({ preserveSelection = true } = {}) => {
    setImagesLoading(true);
    try {
      const payload = await areaApi.getEditorImages(jobId, {
        page: 1,
        page_size: 100,
        q: imageQuery || undefined,
        state: imageFilter,
      });
      const nextItems = payload?.items || [];
      setImages(nextItems);
      setImagesTotal(Number(payload?.total || 0));
      const selectionStillVisible = nextItems.some((item) => Number(item.image_id) === Number(selectedImageId));
      if ((!preserveSelection || !selectionStillVisible) && nextItems[0]) {
        setSearchParams({ image: String(nextItems[0].image_id) }, { replace: true });
      }
      if (!nextItems.length) setDetail(null);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '图片列表加载失败'));
      setImages([]);
      setImagesTotal(0);
    } finally {
      setImagesLoading(false);
    }
  }, [imageFilter, imageQuery, jobId, selectedImageId, setSearchParams]);

  const loadDetail = useCallback(async (imageId) => {
    if (!imageId) return;
    setDetailLoading(true);
    try {
      const payload = await areaApi.getEditorImage(jobId, imageId);
      applyDetail(payload);
    } catch (error) {
      setDetail(null);
      setInstances([]);
      message.error(getAreaErrorMessage(error, '图片详情加载失败'));
    } finally {
      setDetailLoading(false);
    }
  }, [applyDetail, jobId]);

  useEffect(() => {
    loadJob();
  }, [loadJob]);

  useEffect(() => {
    if (!job || !EDITABLE_JOB_STATUSES.includes(job.status)) return;
    loadImages();
  }, [job?.status, imageFilter, imageQuery]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selectedImageId || !job || !EDITABLE_JOB_STATUSES.includes(job.status)) return;
    loadDetail(selectedImageId);
  }, [job?.status, loadDetail, selectedImageId]);

  useEffect(() => {
    if (!job || !ACTIVE_JOB_STATUSES.includes(job.status)) return undefined;
    const timer = window.setInterval(() => loadJob({ quiet: true }), 2500);
    return () => window.clearInterval(timer);
  }, [job, loadJob]);

  useEffect(() => {
    const node = canvasRef.current;
    if (!node) return undefined;
    const updateSize = () => {
      const rect = node.getBoundingClientRect();
      setCanvasSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(node);
    return () => observer.disconnect();
  }, [detail]);

  const fitViewport = useCallback(() => {
    if (!canvasSize.width || !canvasSize.height || !naturalWidth || !naturalHeight) return;
    const scale = Math.min(
      (canvasSize.width - 32) / naturalWidth,
      (canvasSize.height - 32) / naturalHeight,
    );
    const boundedScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
    setViewport({
      scale: boundedScale,
      x: (canvasSize.width - naturalWidth * boundedScale) / 2,
      y: (canvasSize.height - naturalHeight * boundedScale) / 2,
    });
    setManualViewport(false);
  }, [canvasSize.height, canvasSize.width, naturalHeight, naturalWidth]);

  useEffect(() => {
    if (!manualViewport) fitViewport();
  }, [fitViewport, manualViewport]);

  useEffect(() => {
    const handleBeforeUnload = (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [dirty]);

  const commitInstances = useCallback((nextInstances, nextSelectedKey = selectedInstanceKey) => {
    setHistory((current) => [...current.slice(-49), cloneInstances(instances)]);
    setFuture([]);
    setInstances(cloneInstances(nextInstances));
    setSelectedInstanceKey(nextSelectedKey);
    setDirty(true);
  }, [instances, selectedInstanceKey]);

  const undo = useCallback(() => {
    if (!history.length) return;
    const previous = history[history.length - 1];
    setHistory((current) => current.slice(0, -1));
    setFuture((current) => [cloneInstances(instances), ...current].slice(0, 50));
    setInstances(cloneInstances(previous));
    setDirty(true);
  }, [history, instances]);

  const redo = useCallback(() => {
    if (!future.length) return;
    const next = future[0];
    setHistory((current) => [...current.slice(-49), cloneInstances(instances)]);
    setFuture((current) => current.slice(1));
    setInstances(cloneInstances(next));
    setDirty(true);
  }, [future, instances]);

  const guardDirty = useCallback((action) => {
    if (!dirty) {
      action();
      return;
    }
    Modal.confirm({
      title: '放弃未保存的修改？',
      content: '当前图片的轮廓和类别修改尚未保存。',
      okText: '放弃修改',
      okButtonProps: { danger: true },
      cancelText: '继续编辑',
      onOk: action,
    });
  }, [dirty]);

  const selectImage = useCallback((imageId) => {
    if (Number(imageId) === Number(selectedImageId)) return;
    guardDirty(() => setSearchParams({ image: String(imageId) }));
  }, [guardDirty, selectedImageId, setSearchParams]);

  const finishDraft = useCallback((points = draftPoints) => {
    if (points.length < 3 || !drawClass) {
      message.warning('至少需要三个顶点并选择一个类别');
      return;
    }
    const clientId = makeClientInstanceId();
    const next = {
      instance_id: null,
      client_id: clientId,
      class_name: drawClass,
      source: 'manual',
      score: null,
      polygon: points.map((point) => point.map(Math.round)),
      bbox: polygonBbox(points),
      area_px: polygonArea(points),
      is_deleted: false,
      sort_index: instances.length,
    };
    commitInstances([...instances, next], clientId);
    setDraftPoints([]);
    setTool('select');
  }, [commitInstances, draftPoints, drawClass, instances]);

  const handleKeyDown = useCallback((event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
      return;
    }
    if (event.key === 'Escape') {
      setDraftPoints([]);
      setTool('select');
      return;
    }
    if (event.key === 'Enter' && tool === 'draw') {
      event.preventDefault();
      finishDraft();
      return;
    }
    if (event.key === 'ArrowLeft' && tool !== 'draw' && selectedImageIndex > 0) {
      event.preventDefault();
      selectImage(images[selectedImageIndex - 1].image_id);
    }
    if (event.key === 'ArrowRight' && tool !== 'draw' && selectedImageIndex >= 0 && selectedImageIndex < images.length - 1) {
      event.preventDefault();
      selectImage(images[selectedImageIndex + 1].image_id);
    }
  }, [finishDraft, images, redo, selectImage, selectedImageIndex, tool, undo]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  const pointFromEvent = (event) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect || !viewport.scale) return null;
    return {
      x: Math.max(0, Math.min(naturalWidth, (event.clientX - rect.left - viewport.x) / viewport.scale)),
      y: Math.max(0, Math.min(naturalHeight, (event.clientY - rect.top - viewport.y) / viewport.scale)),
    };
  };

  const handleCanvasPointerDown = (event) => {
    if (event.button !== 0 && event.button !== 1) return;
    if (tool === 'draw' && event.button === 0) {
      const point = pointFromEvent(event);
      if (!point) return;
      const nextPoints = [...draftPoints, [point.x, point.y]];
      if (event.detail >= 2 && nextPoints.length >= 3) finishDraft(nextPoints);
      else setDraftPoints(nextPoints);
      return;
    }
    if (tool === 'pan' || event.button === 1) {
      event.preventDefault();
      canvasRef.current?.setPointerCapture?.(event.pointerId);
      dragRef.current = {
        type: 'pan',
        startX: event.clientX,
        startY: event.clientY,
        origin: { ...viewport },
      };
      return;
    }
    setSelectedInstanceKey('');
  };

  const startInstanceDrag = (event, item, type, vertexIndex = null) => {
    if (tool !== 'select' || item.is_deleted || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const point = pointFromEvent(event);
    if (!point) return;
    const key = getInstanceKey(item);
    setSelectedInstanceKey(key);
    canvasRef.current?.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      type,
      key,
      vertexIndex,
      startPoint: point,
      originalInstances: cloneInstances(instances),
    };
  };

  const handleCanvasPointerMove = (event) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.type === 'pan') {
      setViewport({
        ...drag.origin,
        x: drag.origin.x + event.clientX - drag.startX,
        y: drag.origin.y + event.clientY - drag.startY,
      });
      setManualViewport(true);
      return;
    }
    const point = pointFromEvent(event);
    if (!point) return;
    const dx = point.x - drag.startPoint.x;
    const dy = point.y - drag.startPoint.y;
    const next = cloneInstances(drag.originalInstances).map((item) => {
      if (getInstanceKey(item) !== drag.key) return item;
      const polygon = item.polygon.map((vertex, index) => {
        if (drag.type === 'vertex' && index !== drag.vertexIndex) return vertex;
        return [
          Math.max(0, Math.min(naturalWidth, vertex[0] + dx)),
          Math.max(0, Math.min(naturalHeight, vertex[1] + dy)),
        ];
      });
      return { ...item, polygon, bbox: polygonBbox(polygon), area_px: polygonArea(polygon) };
    });
    setInstances(next);
  };

  const handleCanvasPointerUp = () => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.type !== 'pan') {
      setHistory((current) => [...current.slice(-49), cloneInstances(drag.originalInstances)]);
      setFuture([]);
      setDirty(true);
    }
    dragRef.current = null;
  };

  const handleWheel = (event) => {
    if (!naturalWidth || !naturalHeight) return;
    event.preventDefault();
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    const oldScale = viewport.scale;
    const nextScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, oldScale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    const imageX = (cursorX - viewport.x) / oldScale;
    const imageY = (cursorY - viewport.y) / oldScale;
    setViewport({
      scale: nextScale,
      x: cursorX - imageX * nextScale,
      y: cursorY - imageY * nextScale,
    });
    setManualViewport(true);
  };

  const zoomBy = (factor) => {
    const nextScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, viewport.scale * factor));
    const centerX = canvasSize.width / 2;
    const centerY = canvasSize.height / 2;
    const imageX = (centerX - viewport.x) / viewport.scale;
    const imageY = (centerY - viewport.y) / viewport.scale;
    setViewport({ scale: nextScale, x: centerX - imageX * nextScale, y: centerY - imageY * nextScale });
    setManualViewport(true);
  };

  const updateSelectedInstance = (updates) => {
    if (!selectedInstance) return;
    const next = instances.map((item) => (
      getInstanceKey(item) === selectedInstanceKey ? { ...item, ...updates } : item
    ));
    commitInstances(next);
  };

  const saveEditor = async () => {
    if (!detail || !selectedImageId) return;
    setSaving(true);
    try {
      const result = await areaApi.saveEditorImage(jobId, selectedImageId, {
        edited_by_id: queueUserId.current,
        expected_edit_version: Number(detail.image?.edit_version || 0),
        instances: instances.map((item) => ({
          instance_id: item.instance_id || null,
          client_id: item.client_id || null,
          class_name: item.class_name,
          polygon: item.polygon,
          bbox: polygonBbox(item.polygon),
          is_deleted: Boolean(item.is_deleted),
        })),
      });
      if (result?.detail) applyDetail(result.detail);
      else await loadDetail(selectedImageId);
      await loadImages({ preserveSelection: true });
      message.success('当前图片已保存');
    } catch (error) {
      if (error.status === 409 && error.message === 'edit_version_conflict') {
        const editorText = error.detail?.edited_by_id ? `，更新终端：${error.detail.edited_by_id}` : '';
        Modal.confirm({
          title: '图片结果已被更新',
          content: `服务器已有更新版本${editorText}。加载最新版本会放弃当前未保存修改。`,
          okText: '加载最新版本',
          okButtonProps: { danger: true },
          cancelText: '保留当前修改',
          onOk: () => loadDetail(selectedImageId),
        });
      } else {
        message.error(getAreaErrorMessage(error, '保存失败'));
      }
    } finally {
      setSaving(false);
    }
  };

  const resetEditor = () => {
    Modal.confirm({
      title: '恢复这张图片的初始识别结果？',
      content: '人工新增实例将被移除，轮廓、类别和弃用状态都会恢复。',
      okText: '确认恢复',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          const result = await areaApi.resetEditorImage(jobId, selectedImageId, {
            edited_by_id: queueUserId.current,
            expected_edit_version: Number(detail?.image?.edit_version || 0),
          });
          if (result?.detail) applyDetail(result.detail);
          else await loadDetail(selectedImageId);
          await loadImages({ preserveSelection: true });
          message.success('已恢复初始识别结果');
        } catch (error) {
          if (error.status === 409) {
            message.error('图片已被其他终端更新，请先加载最新版本');
          } else {
            message.error(getAreaErrorMessage(error, '恢复失败'));
          }
          throw error;
        }
      },
    });
  };

  const retryJob = async () => {
    try {
      const created = await areaApi.retryJob(jobId);
      message.success('重试任务已提交');
      navigate(`/tools/area/jobs/${created.job_id}`);
    } catch (error) {
      message.error(getAreaErrorMessage(error, '重试失败'));
    }
  };

  const summaryRows = useMemo(() => {
    const totals = new Map(classNames.map((name) => [name, { class_name: name, area_px: 0, count: 0 }]));
    instances.forEach((item) => {
      if (item.is_deleted) return;
      const current = totals.get(item.class_name) || { class_name: item.class_name, area_px: 0, count: 0 };
      current.area_px += Number(item.area_px || polygonArea(item.polygon));
      current.count += 1;
      totals.set(item.class_name, current);
    });
    return Array.from(totals.values());
  }, [classNames, instances]);

  const statusMeta = JOB_STATUS_META[job?.status] || { label: job?.status || '-', color: 'default' };
  const totalImages = Number(job?.total_images || 0);
  const processedImages = Number(job?.processed_images || 0);
  const progressPercent = totalImages ? Math.round((processedImages / totalImages) * 100) : 0;

  if (jobLoading) {
    return <div className="area-workspace-loading"><Spin size="large" /></div>;
  }

  if (!job) {
    return <Alert type="error" showIcon message="任务不存在" action={<Button onClick={() => navigate('/tools/area')}>返回任务中心</Button>} />;
  }

  return (
    <div className="area-workspace">
      <header className="area-workspace-header">
        <Space size={10}>
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            title="返回任务中心"
            onClick={() => guardDirty(() => navigate('/tools/area'))}
          />
          <div className="area-workspace-title">
            <Space size={8}>
              <Typography.Text strong ellipsis={{ tooltip: job.folder_name }}>{job.folder_name}</Typography.Text>
              <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
              {dirty ? <Tag color="warning">未保存</Tag> : null}
            </Space>
            <Typography.Text type="secondary">{job.model_name} · {String(job.job_id).slice(0, 12)}</Typography.Text>
          </div>
        </Space>
        <Space>
          {selectedImageId ? (
            <Typography.Text type="secondary">
              {selectedImageIndex >= 0 ? selectedImageIndex + 1 : '-'} / {imagesTotal}
            </Typography.Text>
          ) : null}
          <Button
            icon={<DownloadOutlined />}
            href={EDITABLE_JOB_STATUSES.includes(job.status) ? areaApi.getExcelUrl(jobId) : undefined}
            target="_blank"
            disabled={!EDITABLE_JOB_STATUSES.includes(job.status)}
          >
            导出
          </Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} disabled={!dirty || !selectedImageId} onClick={saveEditor}>
            保存
          </Button>
        </Space>
      </header>

      {!EDITABLE_JOB_STATUSES.includes(job.status) ? (
        <main className="area-job-state-view">
          <div className="area-job-state-panel">
            <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
            <Typography.Title level={3}>{job.folder_name}</Typography.Title>
            <Progress percent={progressPercent} status={job.status === 'failed' ? 'exception' : 'active'} />
            <div className="area-job-state-metrics">
              <span>已处理 <strong>{processedImages}/{totalImages || '-'}</strong></span>
              <span>成功 <strong>{Number(job.succeeded_images || 0)}</strong></span>
              <span>失败 <strong>{Number(job.failed_images || 0)}</strong></span>
            </div>
            {job.error_code ? <Alert showIcon type="error" message={getAreaErrorMessage(job.error_code, job.error_message)} /> : null}
            {['failed', 'cancelled'].includes(job.status) ? <Button icon={<ReloadOutlined />} onClick={retryJob}>重新提交任务</Button> : null}
          </div>
        </main>
      ) : (
        <div className="area-workspace-grid">
          <aside className="area-image-rail">
            <div className="area-image-filterbar">
              <Input
                size="small"
                prefix={<SearchOutlined />}
                value={imageQueryInput}
                allowClear
                placeholder="搜索图片"
                onChange={(event) => setImageQueryInput(event.target.value)}
                onPressEnter={() => setImageQuery(imageQueryInput.trim())}
              />
              <Segmented
                size="small"
                block
                value={imageFilter}
                onChange={setImageFilter}
                options={[
                  { label: '全部', value: 'all' },
                  { label: '已编辑', value: 'edited' },
                  { label: '失败', value: 'failed' },
                ]}
              />
            </div>
            <Spin spinning={imagesLoading} wrapperClassName="area-image-list-spin">
              <div className="area-image-list">
                {images.map((item) => {
                  const active = Number(item.image_id) === Number(selectedImageId);
                  return (
                    <button
                      type="button"
                      key={item.image_id}
                      className={active ? 'area-image-item area-image-item--active' : 'area-image-item'}
                      onClick={() => selectImage(item.image_id)}
                    >
                      <span className="area-image-thumb">
                        {item.error ? (
                          <DeleteOutlined />
                        ) : (
                          <img src={item.source_url || areaApi.getSourceImageUrl(jobId, item.image_id)} alt="" loading="lazy" />
                        )}
                      </span>
                      <span className="area-image-copy">
                        <Typography.Text ellipsis={{ tooltip: item.image_name }}>{item.image_name}</Typography.Text>
                        <span>
                          {item.error ? <Tag color="error">失败</Tag> : null}
                          {item.edited_at ? <Tag color="blue">已编辑</Tag> : null}
                        </span>
                      </span>
                    </button>
                  );
                })}
                {!images.length && !imagesLoading ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的图片" /> : null}
              </div>
            </Spin>
          </aside>

          <main className="area-canvas-column">
            <div className="area-canvas-toolbar">
              <Space size={4}>
                <Tooltip title="选择和调整轮廓">
                  <Button type={tool === 'select' ? 'primary' : 'text'} icon={<EditOutlined />} onClick={() => { setTool('select'); setDraftPoints([]); }} />
                </Tooltip>
                <Tooltip title="平移画布">
                  <Button type={tool === 'pan' ? 'primary' : 'text'} icon={<DragOutlined />} onClick={() => { setTool('pan'); setDraftPoints([]); }} />
                </Tooltip>
                <Tooltip title="补画区域">
                  <Button type={tool === 'draw' ? 'primary' : 'text'} icon={<PlusOutlined />} onClick={() => setTool('draw')} />
                </Tooltip>
                {tool === 'draw' ? (
                  <>
                    <Select size="small" value={drawClass || undefined} options={classNames.map((name) => ({ value: name, label: name }))} onChange={setDrawClass} style={{ width: 120 }} />
                    <Button size="small" icon={<CheckOutlined />} disabled={draftPoints.length < 3} onClick={() => finishDraft()}>完成轮廓</Button>
                  </>
                ) : null}
              </Space>
              <Space size={4}>
                <Tooltip title="撤销 Ctrl+Z"><Button type="text" icon={<UndoOutlined />} disabled={!history.length} onClick={undo} /></Tooltip>
                <Tooltip title="重做 Ctrl+Shift+Z"><Button type="text" icon={<RedoOutlined />} disabled={!future.length} onClick={redo} /></Tooltip>
                <Divider type="vertical" />
                <Segmented size="small" value={viewMode} onChange={setViewMode} options={[{ label: '叠加', value: 'overlay' }, { label: '原图', value: 'source' }]} />
                <Tooltip title={showFill ? '隐藏遮罩填充' : '显示遮罩填充'}>
                  <Button type="text" icon={showFill ? <EyeOutlined /> : <EyeInvisibleOutlined />} disabled={viewMode === 'source'} onClick={() => setShowFill((value) => !value)} />
                </Tooltip>
                <Slider className="area-opacity-slider" min={0.05} max={0.7} step={0.05} value={maskOpacity} disabled={!showFill || viewMode === 'source'} onChange={setMaskOpacity} />
                <Tooltip title="缩小"><Button type="text" icon={<ZoomOutOutlined />} onClick={() => zoomBy(1 / 1.2)} /></Tooltip>
                <Typography.Text className="area-zoom-label">{Math.round(viewport.scale * 100)}%</Typography.Text>
                <Tooltip title="放大"><Button type="text" icon={<ZoomInOutlined />} onClick={() => zoomBy(1.2)} /></Tooltip>
                <Tooltip title="适应窗口"><Button type="text" icon={<CompressOutlined />} onClick={fitViewport} /></Tooltip>
              </Space>
            </div>

            <div
              ref={canvasRef}
              className={`area-editor-canvas area-editor-canvas--${tool}`}
              onPointerDown={handleCanvasPointerDown}
              onPointerMove={handleCanvasPointerMove}
              onPointerUp={handleCanvasPointerUp}
              onPointerCancel={handleCanvasPointerUp}
              onWheel={handleWheel}
            >
              {detailLoading ? <div className="area-canvas-state"><Spin size="large" /></div> : null}
              {!detailLoading && imageMeta?.error ? <div className="area-canvas-state"><Alert type="error" showIcon message={getAreaErrorMessage(imageMeta.error)} /></div> : null}
              {!detailLoading && activeImageUrl && naturalWidth && naturalHeight ? (
                <div
                  className="area-canvas-stage"
                  style={{
                    width: naturalWidth,
                    height: naturalHeight,
                    transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.scale})`,
                  }}
                >
                  <img
                    src={activeImageUrl}
                    alt={imageMeta?.image_name || ''}
                    draggable={false}
                    onLoad={() => { if (!manualViewport) fitViewport(); }}
                    onError={() => {
                      if (!baseImageFailed && sourceImageUrl) setBaseImageFailed(true);
                      else setOverlayImageFailed(true);
                    }}
                  />
                  <svg width={naturalWidth} height={naturalHeight} viewBox={`0 0 ${naturalWidth} ${naturalHeight}`}>
                    {viewMode === 'overlay' ? instances.map((item, index) => {
                      const key = getInstanceKey(item);
                      const selected = key === selectedInstanceKey;
                      const colorIndex = Math.max(0, classNames.indexOf(item.class_name));
                      const color = COLORS[(colorIndex >= 0 ? colorIndex : index) % COLORS.length];
                      const points = item.polygon.map((point) => point.join(',')).join(' ');
                      return (
                        <g key={key}>
                          <polygon
                            points={points}
                            fill={showFill && !item.is_deleted ? color : 'transparent'}
                            fillOpacity={showFill ? maskOpacity : 0}
                            stroke={item.is_deleted ? '#ff4d4f' : (selected ? '#ffffff' : color)}
                            strokeWidth={(selected ? 3 : 1.5) / viewport.scale}
                            strokeDasharray={item.is_deleted ? `${8 / viewport.scale} ${5 / viewport.scale}` : undefined}
                            onPointerDown={(event) => startInstanceDrag(event, item, 'polygon')}
                            onClick={(event) => { event.stopPropagation(); setSelectedInstanceKey(key); }}
                          />
                          {selected && tool === 'select' && !item.is_deleted ? item.polygon.map((point, vertexIndex) => (
                            <circle
                              key={`${key}-${vertexIndex}`}
                              cx={point[0]}
                              cy={point[1]}
                              r={6 / viewport.scale}
                              fill="#1677ff"
                              stroke="#fff"
                              strokeWidth={2 / viewport.scale}
                              onPointerDown={(event) => startInstanceDrag(event, item, 'vertex', vertexIndex)}
                            />
                          )) : null}
                        </g>
                      );
                    }) : null}
                    {draftPoints.length ? (
                      <polyline
                        points={draftPoints.map((point) => point.join(',')).join(' ')}
                        fill="rgba(22,119,255,0.15)"
                        stroke="#1677ff"
                        strokeWidth={2 / viewport.scale}
                        strokeDasharray={`${6 / viewport.scale} ${4 / viewport.scale}`}
                      />
                    ) : null}
                  </svg>
                </div>
              ) : null}
              {!detailLoading && (!activeImageUrl || !naturalWidth || !naturalHeight) ? (
                <div className="area-canvas-state"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前图片不可显示" /></div>
              ) : null}
            </div>

            <div className="area-canvas-pager">
              <Button type="text" icon={<LeftOutlined />} disabled={selectedImageIndex <= 0} onClick={() => selectImage(images[selectedImageIndex - 1]?.image_id)} />
              <Typography.Text ellipsis={{ tooltip: imageMeta?.image_name }}>{imageMeta?.image_name || '-'}</Typography.Text>
              <Button type="text" icon={<RightOutlined />} disabled={selectedImageIndex < 0 || selectedImageIndex >= images.length - 1} onClick={() => selectImage(images[selectedImageIndex + 1]?.image_id)} />
            </div>
          </main>

          <aside className="area-inspector">
            <Tabs
              defaultActiveKey="instances"
              items={[
                {
                  key: 'instances',
                  label: `实例 ${instances.filter((item) => !item.is_deleted).length}`,
                  children: (
                    <div className="area-inspector-content">
                      {selectedInstance ? (
                        <div className="area-instance-properties">
                          <div className="area-property-row"><span>实例编号</span><Typography.Text>{selectedInstance.instance_id || '新增'}</Typography.Text></div>
                          <label>
                            <span>类别</span>
                            <Select value={selectedInstance.class_name} options={classNames.map((name) => ({ value: name, label: name }))} disabled={selectedInstance.is_deleted} onChange={(value) => updateSelectedInstance({ class_name: value })} />
                          </label>
                          <div className="area-property-row"><span>面积</span><Typography.Text>{Number(selectedInstance.area_px || polygonArea(selectedInstance.polygon)).toLocaleString()} px</Typography.Text></div>
                          <div className="area-property-row"><span>置信度</span><Typography.Text>{selectedInstance.score == null ? '-' : `${(Number(selectedInstance.score) * 100).toFixed(1)}%`}</Typography.Text></div>
                          <div className="area-property-row">
                            <span>启用</span>
                            <Switch checked={!selectedInstance.is_deleted} onChange={(checked) => updateSelectedInstance({ is_deleted: !checked, area_px: checked ? polygonArea(selectedInstance.polygon) : 0 })} />
                          </div>
                        </div>
                      ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一个实例" />}
                      <Divider />
                      <div className="area-instance-list">
                        {instances.map((item, index) => {
                          const key = getInstanceKey(item);
                          const active = key === selectedInstanceKey;
                          return (
                            <button type="button" key={key} className={active ? 'area-instance-item area-instance-item--active' : 'area-instance-item'} onClick={() => setSelectedInstanceKey(key)}>
                              <span className="area-instance-color" style={{ background: COLORS[Math.max(0, classNames.indexOf(item.class_name)) % COLORS.length] }} />
                              <span><strong>{item.class_name}</strong><small>{Number(item.area_px || polygonArea(item.polygon)).toLocaleString()} px</small></span>
                              {item.source === 'manual' ? <Tag color="blue">人工</Tag> : null}
                              {item.is_deleted ? <Tag>弃用</Tag> : null}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  ),
                },
                {
                  key: 'summary',
                  label: '当前汇总',
                  children: (
                    <div className="area-summary-list">
                      {summaryRows.map((row, index) => (
                        <div key={row.class_name}>
                          <span className="area-instance-color" style={{ background: COLORS[index % COLORS.length] }} />
                          <Typography.Text>{row.class_name}</Typography.Text>
                          <Typography.Text type="secondary">{row.count} 个</Typography.Text>
                          <Typography.Text strong>{row.area_px.toLocaleString()} px</Typography.Text>
                        </div>
                      ))}
                    </div>
                  ),
                },
              ]}
            />
            <div className="area-inspector-footer">
              <Typography.Text type="secondary">
                版本 {Number(imageMeta?.edit_version || 0)} · {imageMeta?.edited_at ? formatAreaDateTime(imageMeta.edited_at) : '未编辑'}
              </Typography.Text>
              <Button type="text" danger icon={<ReloadOutlined />} disabled={!selectedImageId} onClick={resetEditor}>恢复初始结果</Button>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

export default AreaJobWorkspace;
