import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Card,
  Checkbox,
  Collapse,
  Col,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Pagination,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
} from '@ant-design/icons';

import { areaApi } from '../api/area';
import { getOrCreateQueueUserId } from '../utils/localStorage';

const POLL_INTERVAL_MS = 2000;
const AUTO_REFRESH_MS = 30000;

const statusColorMap = {
  queued: 'default',
  running: 'processing',
  succeeded: 'success',
  succeeded_with_errors: 'warning',
  failed: 'error',
};

const DEFAULT_INFERENCE_OPTIONS = {
  threshold_bias: 0,
  mask_mode: 'auto',
  smooth_min_neighbors: 3,
  min_pixels: 64,
  overlay_alpha: 0.45,
  score_threshold: 0.15,
  top_k: 200,
  nms_top_k: 200,
  nms_conf_thresh: 0.05,
  nms_thresh: 0.5,
};

const LABEL_ALIAS = {
  粘: '粘纤',
  莱: '莱赛尔',
  莫: '莫代尔',
};

const invalidFolderNamePattern = /[\\/:*?"<>|]/;

const getCreateJobErrorMessage = (raw) => {
  const code = String(raw || '').trim();
  if (code === 'folder_not_found') return '文件夹不存在，请检查根路径和文件夹名称';
  if (code === 'root_path_not_found') return '根路径不可访问，请检查本地路径或局域网路径配置';
  if (code === 'empty_image_list') return '该文件夹没有可处理的 jpg/png 图片';
  if (code === 'invalid_model_name') return '模型名称无效，请重新选择';
  if (code === 'invalid_folder_name') return '文件夹名称无效，请仅填写目录名';
  if (code === 'invalid_root_path') return '根路径无效，请先保存正确的全局配置';
  if (code === 'invalid_inference_options') return '推理参数无效，请检查全局配置中的参数';
  if (code === 'infer_service_unavailable') return '原生推理服务不可用，请稍后重试或联系管理员';
  if (code === 'infer_timeout') return '原生推理服务超时，请稍后重试';
  if (code === 'infer_model_load_failed') return '模型加载失败，请检查权重文件与模型映射';
  if (code === 'infer_bad_response') return '原生推理服务返回异常，请联系管理员排查';
  return code || '创建任务失败';
};

const parseModelClasses = (modelName) => {
  const deduped = [];
  String(modelName || '').split('-').forEach((item) => {
    const token = String(item || '').trim();
    if (!token) return;
    const mapped = LABEL_ALIAS[token] || token;
    if (!deduped.includes(mapped)) deduped.push(mapped);
  });
  return deduped;
};

const getColorByIndex = (idx, alpha = 0.35) => {
  const palette = [
    [255, 87, 34],
    [30, 136, 229],
    [67, 160, 71],
    [142, 36, 170],
    [255, 179, 0],
    [0, 172, 193],
    [94, 53, 177],
    [216, 27, 96],
  ];
  const color = palette[idx % palette.length];
  return `rgba(${color[0]}, ${color[1]}, ${color[2]}, ${alpha})`;
};

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

const computeBBoxFromPolygon = (polygon, fallback = []) => {
  if (!Array.isArray(polygon) || polygon.length < 3) return Array.isArray(fallback) ? fallback : [];
  const xs = [];
  const ys = [];
  polygon.forEach((p) => {
    if (!Array.isArray(p) || p.length !== 2) return;
    const x = Number(p[0]);
    const y = Number(p[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      xs.push(Math.round(x));
      ys.push(Math.round(y));
    }
  });
  if (xs.length < 3 || ys.length < 3) return Array.isArray(fallback) ? fallback : [];
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};

function AreaRecognition() {
  const queueUserIdRef = useRef(getOrCreateQueueUserId());
  const editorCanvasRef = useRef(null);

  const [configLoading, setConfigLoading] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [jobCreating, setJobCreating] = useState(false);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [folderLoading, setFolderLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [editorImagesLoading, setEditorImagesLoading] = useState(false);
  const [editorImageLoading, setEditorImageLoading] = useState(false);
  const [editorSaving, setEditorSaving] = useState(false);

  const [rootPath, setRootPath] = useState('');
  const [oldRootPath, setOldRootPath] = useState('');
  const [resultOutputRoot, setResultOutputRoot] = useState('/tmp/area_outputs');
  const [mappingRows, setMappingRows] = useState([]);
  const [configInferenceOptions, setConfigInferenceOptions] = useState(DEFAULT_INFERENCE_OPTIONS);
  const [archiveStatus, setArchiveStatus] = useState(null);
  const [configOpenKeys, setConfigOpenKeys] = useState([]);

  const [folderLimit, setFolderLimit] = useState(5);
  const [folderQueryInput, setFolderQueryInput] = useState('');
  const [folderQueryUsed, setFolderQueryUsed] = useState('');
  const [folderItems, setFolderItems] = useState([]);
  const [folderTotal, setFolderTotal] = useState(0);
  const [folderPage, setFolderPage] = useState(1);

  const [folderName, setFolderName] = useState('');
  const [modelName, setModelName] = useState('');

  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobSearchInput, setJobSearchInput] = useState('');
  const [jobSearchQuery, setJobSearchQuery] = useState('');
  const [jobPage, setJobPage] = useState(1);
  const [jobPageSize, setJobPageSize] = useState(5);
  const [selectedJobId, setSelectedJobId] = useState('');

  const [resultSummary, setResultSummary] = useState([]);
  const [editorImages, setEditorImages] = useState([]);
  const [editorImagesTotal, setEditorImagesTotal] = useState(0);
  const [editorImagePage, setEditorImagePage] = useState(1);
  const [editorImagePageSize, setEditorImagePageSize] = useState(5);
  const [selectedEditorImageId, setSelectedEditorImageId] = useState(null);
  const [editorDetail, setEditorDetail] = useState(null);
  const [workingInstances, setWorkingInstances] = useState([]);
  const [selectedInstanceId, setSelectedInstanceId] = useState(null);
  const [isEditing, setIsEditing] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [dragState, setDragState] = useState(null);
  const [imageNaturalSize, setImageNaturalSize] = useState({ width: 0, height: 0 });
  const [imageDisplaySize, setImageDisplaySize] = useState({ width: 0, height: 0 });

  const [folderImagesModal, setFolderImagesModal] = useState({
    open: false,
    folderName: '',
    items: [],
    total: 0,
    page: 1,
    pageSize: 24,
    currentIndex: 0,
    loading: false,
  });

  const [cleanupModal, setCleanupModal] = useState({
    open: false,
    folderName: '',
    renameEnabled: false,
    newFolderName: '',
    submitting: false,
  });

  const modelOptions = useMemo(
    () => mappingRows.map((item) => item.model_name).filter(Boolean),
    [mappingRows],
  );

  const selectedJob = useMemo(
    () => jobs.find((item) => item.job_id === selectedJobId) || null,
    [jobs, selectedJobId],
  );

  const runningJobs = useMemo(
    () => jobs.filter((item) => ['queued', 'running'].includes(item.status)),
    [jobs],
  );

  const classNames = useMemo(() => parseModelClasses(selectedJob?.model_name), [selectedJob?.model_name]);

  const selectedInstance = useMemo(
    () => workingInstances.find((item) => item.instance_id === selectedInstanceId) || null,
    [workingInstances, selectedInstanceId],
  );

  const updateDisplaySize = useCallback(() => {
    if (!editorCanvasRef.current) return;
    const imageEl = editorCanvasRef.current.querySelector('img');
    if (!imageEl) return;
    setImageDisplaySize({
      width: imageEl.clientWidth,
      height: imageEl.clientHeight,
    });
  }, []);

  useEffect(() => {
    window.addEventListener('resize', updateDisplaySize);
    return () => window.removeEventListener('resize', updateDisplaySize);
  }, [updateDisplaySize]);

  const pointFromEvent = useCallback((event) => {
    if (!editorCanvasRef.current) return null;
    const imageEl = editorCanvasRef.current.querySelector('img');
    if (!imageEl) return null;
    const rect = imageEl.getBoundingClientRect();
    if (!rect.width || !rect.height || !imageNaturalSize.width || !imageNaturalSize.height) return null;
    const x = (event.clientX - rect.left) * (imageNaturalSize.width / rect.width);
    const y = (event.clientY - rect.top) * (imageNaturalSize.height / rect.height);
    return {
      x: clamp(Math.round(x), 0, imageNaturalSize.width - 1),
      y: clamp(Math.round(y), 0, imageNaturalSize.height - 1),
      rect,
    };
  }, [imageNaturalSize.height, imageNaturalSize.width]);

  const updateInstanceById = useCallback((instanceId, updater) => {
    setWorkingInstances((prev) => prev.map((item) => {
      if (item.instance_id !== instanceId) return item;
      const next = updater(item);
      return next;
    }));
    setDirty(true);
  }, []);

  useEffect(() => {
    if (!dragState) return undefined;
    const onMove = (event) => {
      const p = pointFromEvent(event);
      if (!p) return;
      if (dragState.type === 'vertex') {
        updateInstanceById(dragState.instanceId, (item) => {
          const polygon = Array.isArray(item.polygon) ? item.polygon.map((point) => [...point]) : [];
          if (dragState.vertexIndex < 0 || dragState.vertexIndex >= polygon.length) return item;
          polygon[dragState.vertexIndex] = [p.x, p.y];
          return {
            ...item,
            polygon,
            bbox: computeBBoxFromPolygon(polygon, item.bbox),
          };
        });
      } else if (dragState.type === 'polygon') {
        const start = dragState.startPoint;
        const dx = p.x - start.x;
        const dy = p.y - start.y;
        updateInstanceById(dragState.instanceId, (item) => {
          const base = dragState.originPolygon || item.polygon || [];
          const polygon = base.map((point) => [
            clamp(Math.round(point[0] + dx), 0, imageNaturalSize.width - 1),
            clamp(Math.round(point[1] + dy), 0, imageNaturalSize.height - 1),
          ]);
          return {
            ...item,
            polygon,
            bbox: computeBBoxFromPolygon(polygon, item.bbox),
          };
        });
      }
    };
    const onUp = () => setDragState(null);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
  }, [dragState, imageNaturalSize.height, imageNaturalSize.width, pointFromEvent, updateInstanceById]);

  const withDiscardConfirm = useCallback((action) => {
    if (!dirty) {
      action();
      return;
    }
    Modal.confirm({
      title: '存在未保存修改',
      content: '当前结果有未保存修改，是否放弃并继续？',
      okText: '放弃修改',
      cancelText: '取消',
      onOk: () => {
        setDirty(false);
        setIsEditing(false);
        action();
      },
    });
  }, [dirty]);

  const fetchConfig = useCallback(async () => {
    setConfigLoading(true);
    try {
      const data = await areaApi.getConfig();
      setRootPath(data.root_path || '');
      setOldRootPath(data.old_root_path || '');
      setResultOutputRoot(data.result_output_root || '/tmp/area_outputs');
      const rows = Object.entries(data.model_mapping || {}).map(([name, file]) => ({
        key: name,
        model_name: name,
        model_file: file,
      }));
      setMappingRows(rows);
      setConfigInferenceOptions({
        ...DEFAULT_INFERENCE_OPTIONS,
        ...(data.inference_defaults || {}),
      });
      if (!modelName && rows.length > 0) {
        setModelName(rows[0].model_name);
      }
      const archive = await areaApi.getArchiveStatus();
      setArchiveStatus(archive);
    } catch (error) {
      message.error(error.message || '读取配置失败');
    } finally {
      setConfigLoading(false);
    }
  }, [modelName]);

  const fetchFolders = useCallback(async ({ query, page, resetPage = false } = {}) => {
    const nextPage = resetPage ? 1 : (page || folderPage);
    const limit = clamp(Number(folderLimit || 5), 1, 100);
    setFolderLoading(true);
    try {
      const normalizedQuery = String(query != null ? query : folderQueryUsed || '').trim();
      if (normalizedQuery) {
        const data = await areaApi.searchFolders({ q: normalizedQuery, limit });
        const items = Array.isArray(data.items) ? data.items : [];
        setFolderItems(items);
        setFolderTotal(items.length);
        setFolderPage(1);
        setFolderQueryUsed(normalizedQuery);
      } else {
        const data = await areaApi.listRecentFolders({ limit, page: nextPage, page_size: 5 });
        setFolderItems(Array.isArray(data.items) ? data.items : []);
        setFolderTotal(Number(data.total || 0));
        setFolderPage(Number(data.page || nextPage));
        setFolderQueryUsed('');
      }
    } catch (error) {
      message.error(error.message || '读取文件夹列表失败');
    } finally {
      setFolderLoading(false);
    }
  }, [folderLimit, folderPage, folderQueryUsed]);

  const fetchJobs = useCallback(async ({ page, keepSelection = true, query } = {}) => {
    setJobsLoading(true);
    try {
      const nextPage = page || jobPage;
      const currentQuery = query != null ? query : jobSearchQuery;
      const data = await areaApi.listJobs({
        limit: 500,
        page: nextPage,
        page_size: jobPageSize,
        q: currentQuery || undefined,
      });
      const items = Array.isArray(data.items) ? data.items : [];
      setJobs(items);
      setJobsTotal(Number(data.total || 0));
      setJobPage(Number(data.page || nextPage));

      if (!keepSelection && items.length > 0) {
        setSelectedJobId(items[0].job_id);
      }
      if (keepSelection && selectedJobId && !items.some((item) => item.job_id === selectedJobId)) {
        setSelectedJobId('');
      }
    } catch (error) {
      message.error(error.message || '读取任务列表失败');
    } finally {
      setJobsLoading(false);
    }
  }, [jobPage, jobPageSize, jobSearchQuery, selectedJobId]);

  const fetchEditorImages = useCallback(async (jobId, page = 1, pageSize = editorImagePageSize) => {
    if (!jobId) {
      setEditorImages([]);
      setEditorImagesTotal(0);
      setSelectedEditorImageId(null);
      return;
    }
    setEditorImagesLoading(true);
    try {
      const data = await areaApi.getEditorImages(jobId, { page, page_size: pageSize });
      const items = Array.isArray(data.items) ? data.items : [];
      setEditorImages(items);
      setEditorImagesTotal(Number(data.total || 0));
      setEditorImagePage(Number(data.page || page));
      setEditorImagePageSize(Number(data.page_size || pageSize));
      if (items.length > 0) {
        const stillExists = items.some((item) => item.image_id === selectedEditorImageId);
        if (!stillExists) {
          setSelectedEditorImageId(items[0].image_id);
        }
      } else {
        setSelectedEditorImageId(null);
      }
    } catch (error) {
      message.error(error.message || '读取结果编辑图片列表失败');
    } finally {
      setEditorImagesLoading(false);
    }
  }, [editorImagePageSize, selectedEditorImageId]);

  const fetchResultSummary = useCallback(async (jobId) => {
    if (!jobId) {
      setResultSummary([]);
      return;
    }
    try {
      const data = await areaApi.getResult(jobId);
      setResultSummary(Array.isArray(data.summary) ? data.summary : []);
    } catch (_error) {
      setResultSummary([]);
    }
  }, []);

  const fetchEditorImageDetail = useCallback(async (jobId, imageId) => {
    if (!jobId || !imageId) {
      setEditorDetail(null);
      setWorkingInstances([]);
      setSelectedInstanceId(null);
      setDirty(false);
      setIsEditing(false);
      return;
    }
    setEditorImageLoading(true);
    try {
      const data = await areaApi.getEditorImage(jobId, imageId);
      setEditorDetail(data);
      const instances = Array.isArray(data.instances) ? data.instances : [];
      setWorkingInstances(instances.map((item) => ({ ...item })));
      setSelectedInstanceId(instances.length > 0 ? instances[0].instance_id : null);
      setDirty(false);
      setIsEditing(false);
    } catch (error) {
      message.error(error.message || '读取图片编辑详情失败');
      setEditorDetail(null);
      setWorkingInstances([]);
      setSelectedInstanceId(null);
      setDirty(false);
      setIsEditing(false);
    } finally {
      setEditorImageLoading(false);
    }
  }, []);

  const refreshAll = useCallback(async ({ resetFolderLimit = false } = {}) => {
    if (resetFolderLimit) {
      setFolderLimit(5);
      await fetchFolders({ query: '', page: 1, resetPage: true });
    } else {
      await fetchFolders({ query: folderQueryUsed || '', page: folderPage });
    }
    await fetchJobs({ keepSelection: true });
    if (selectedJobId) {
      await fetchEditorImages(selectedJobId, editorImagePage, editorImagePageSize);
      await fetchResultSummary(selectedJobId);
      if (selectedEditorImageId) {
        await fetchEditorImageDetail(selectedJobId, selectedEditorImageId);
      }
    }
    try {
      const archive = await areaApi.getArchiveStatus();
      setArchiveStatus(archive);
    } catch (_error) {
      // ignore
    }
  }, [editorImagePage, editorImagePageSize, fetchEditorImageDetail, fetchEditorImages, fetchFolders, fetchJobs, fetchResultSummary, folderPage, folderQueryUsed, selectedEditorImageId, selectedJobId]);

  useEffect(() => {
    const initialize = async () => {
      await fetchConfig();
      await fetchFolders({ query: '', page: 1, resetPage: true });
      await fetchJobs({ keepSelection: false, page: 1 });
    };
    initialize();
  }, [fetchConfig, fetchFolders, fetchJobs]);

  useEffect(() => {
    if (!selectedJobId) {
      setEditorImages([]);
      setEditorImagesTotal(0);
      setSelectedEditorImageId(null);
      setEditorDetail(null);
      setWorkingInstances([]);
      setResultSummary([]);
      return;
    }
    fetchResultSummary(selectedJobId);
    fetchEditorImages(selectedJobId, 1, editorImagePageSize);
  }, [editorImagePageSize, fetchEditorImages, fetchResultSummary, selectedJobId]);

  useEffect(() => {
    if (!selectedJobId || !selectedEditorImageId) {
      setEditorDetail(null);
      setWorkingInstances([]);
      setSelectedInstanceId(null);
      return;
    }
    fetchEditorImageDetail(selectedJobId, selectedEditorImageId);
  }, [fetchEditorImageDetail, selectedEditorImageId, selectedJobId]);

  useEffect(() => {
    if (!runningJobs.length) return undefined;
    const timer = setInterval(() => {
      fetchJobs({ keepSelection: true });
      if (selectedJobId) {
        fetchEditorImages(selectedJobId, editorImagePage, editorImagePageSize);
        fetchResultSummary(selectedJobId);
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [editorImagePage, editorImagePageSize, fetchEditorImages, fetchJobs, fetchResultSummary, runningJobs.length, selectedJobId]);

  useEffect(() => {
    const timer = setInterval(() => {
      fetchFolders({ query: folderQueryUsed || '', page: folderPage });
      fetchJobs({ keepSelection: true });
    }, AUTO_REFRESH_MS);
    return () => clearInterval(timer);
  }, [fetchFolders, fetchJobs, folderPage, folderQueryUsed]);

  useEffect(() => {
    if (!folderImagesModal.open) return undefined;
    const onKeyDown = (event) => {
      if (!folderImagesModal.items.length) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) return;
      }
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        setFolderImagesModal((prev) => ({
          ...prev,
          currentIndex: Math.max(0, prev.currentIndex - 1),
        }));
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        setFolderImagesModal((prev) => ({
          ...prev,
          currentIndex: Math.min(Math.max(prev.items.length - 1, 0), prev.currentIndex + 1),
        }));
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [folderImagesModal.items.length, folderImagesModal.open]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (folderImagesModal.open) return;
      if (!selectedJobId) return;
      if (event.target && ['INPUT', 'TEXTAREA'].includes(event.target.tagName)) return;

      if (event.code === 'Space') {
        event.preventDefault();
        if (!selectedInstance || selectedInstance.is_deleted) return;
        setIsEditing((prev) => !prev);
        return;
      }
      if (event.key.toLowerCase() === 'd') {
        event.preventDefault();
        if (!selectedInstance) return;
        updateInstanceById(selectedInstance.instance_id, (item) => ({
          ...item,
          is_deleted: !item.is_deleted,
        }));
        return;
      }
      if (!isEditing && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        if (!editorImages.length || !selectedEditorImageId) return;
        const idx = editorImages.findIndex((item) => item.image_id === selectedEditorImageId);
        if (idx < 0) return;
        const nextIdx = event.key === 'ArrowLeft' ? idx - 1 : idx + 1;
        if (nextIdx < 0 || nextIdx >= editorImages.length) return;
        withDiscardConfirm(() => {
          setSelectedEditorImageId(editorImages[nextIdx].image_id);
        });
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [editorImages, folderImagesModal.open, isEditing, selectedEditorImageId, selectedInstance, selectedJobId, updateInstanceById, withDiscardConfirm]);

  const handleSaveConfig = async () => {
    const modelMapping = {};
    mappingRows.forEach((row) => {
      const key = String(row.model_name || '').trim();
      const value = String(row.model_file || '').trim();
      if (!key || !value) return;
      modelMapping[key] = value;
    });
    if (!rootPath.trim()) {
      message.error('根路径不能为空');
      return;
    }
    if (!oldRootPath.trim()) {
      message.error('旧文件路径不能为空');
      return;
    }
    if (!resultOutputRoot.trim()) {
      message.error('结果输出路径不能为空');
      return;
    }
    if (Object.keys(modelMapping).length <= 0) {
      message.error('模型映射不能为空');
      return;
    }

    setConfigSaving(true);
    try {
      await areaApi.updateConfig({
        root_path: rootPath.trim(),
        old_root_path: oldRootPath.trim(),
        result_output_root: resultOutputRoot.trim(),
        model_mapping: modelMapping,
        inference_defaults: {
          threshold_bias: Number(configInferenceOptions.threshold_bias ?? 0),
          mask_mode: String(configInferenceOptions.mask_mode || 'auto'),
          smooth_min_neighbors: Number(configInferenceOptions.smooth_min_neighbors ?? 3),
          min_pixels: Number(configInferenceOptions.min_pixels ?? 64),
          overlay_alpha: Number(configInferenceOptions.overlay_alpha ?? 0.45),
          score_threshold: Number(configInferenceOptions.score_threshold ?? 0.15),
          top_k: Number(configInferenceOptions.top_k ?? 200),
          nms_top_k: Number(configInferenceOptions.nms_top_k ?? 200),
          nms_conf_thresh: Number(configInferenceOptions.nms_conf_thresh ?? 0.05),
          nms_thresh: Number(configInferenceOptions.nms_thresh ?? 0.5),
        },
      });
      message.success('配置已保存（全局生效）');
      await fetchConfig();
    } catch (error) {
      message.error(error.message || '保存配置失败');
    } finally {
      setConfigSaving(false);
    }
  };

  const handleManualArchive = async () => {
    try {
      const data = await areaApi.runArchive();
      message.success(`归档完成，移动目录 ${data.moved_count || 0} 个`);
      const next = await areaApi.getArchiveStatus();
      setArchiveStatus(next);
      fetchFolders({ query: folderQueryUsed || '', page: folderPage });
    } catch (error) {
      message.error(error.message || '归档失败');
    }
  };

  const handleSearchFolders = async () => {
    if (!folderQueryInput.trim()) {
      setFolderQueryUsed('');
      await fetchFolders({ query: '', page: 1, resetPage: true });
      return;
    }
    await fetchFolders({ query: folderQueryInput.trim(), page: 1, resetPage: true });
  };

  const handleRefreshFolders = async () => {
    setFolderLimit(5);
    setFolderQueryInput('');
    setFolderQueryUsed('');
    await fetchFolders({ query: '', page: 1, resetPage: true });
  };

  const openFolderImagesModal = async (targetFolderName, page = 1) => {
    setFolderImagesModal((prev) => ({
      ...prev,
      open: true,
      folderName: targetFolderName,
      loading: true,
      page,
      currentIndex: 0,
    }));
    try {
      const data = await areaApi.listFolderImages(targetFolderName, {
        page,
        page_size: folderImagesModal.pageSize,
      });
      setFolderImagesModal((prev) => ({
        ...prev,
        open: true,
        folderName: targetFolderName,
        loading: false,
        items: Array.isArray(data.items) ? data.items : [],
        total: Number(data.total || 0),
        page: Number(data.page || page),
        currentIndex: 0,
      }));
    } catch (error) {
      setFolderImagesModal((prev) => ({ ...prev, loading: false }));
      message.error(error.message || '读取图片列表失败');
    }
  };

  const openCleanupModal = (targetFolderName) => {
    const fallbackName = String(targetFolderName || '').trim();
    const defaultRename = fallbackName.split('_')[0] || fallbackName;
    setCleanupModal({
      open: true,
      folderName: fallbackName,
      renameEnabled: false,
      newFolderName: defaultRename,
      submitting: false,
    });
  };

  const handleCleanupSubmit = async () => {
    const folder = String(cleanupModal.folderName || '').trim();
    if (!folder) return;
    const renameName = String(cleanupModal.newFolderName || '').trim();
    if (cleanupModal.renameEnabled) {
      if (!renameName) {
        message.error('新文件夹名称不能为空');
        return;
      }
      if (renameName === '.' || renameName === '..' || invalidFolderNamePattern.test(renameName)) {
        message.error('新文件夹名称不合法，不能包含 \\ / : * ? " < > |');
        return;
      }
    }

    setCleanupModal((prev) => ({ ...prev, submitting: true }));
    try {
      const data = await areaApi.cleanupFolder(folder, {
        rename_enabled: cleanupModal.renameEnabled,
        new_folder_name: cleanupModal.renameEnabled ? renameName : undefined,
      });
      const moved = data?.moved ?? 0;
      if (cleanupModal.renameEnabled && data?.renamed) {
        message.success(`已移动 ${moved} 张图片，文件夹已重命名为 ${data?.new_folder || renameName}`);
      } else {
        message.success(`已移动 ${moved} 张图片`);
      }
      setCleanupModal((prev) => ({ ...prev, open: false, submitting: false }));
      fetchFolders({ query: folderQueryUsed || '', page: folderPage });
    } catch (error) {
      setCleanupModal((prev) => ({ ...prev, submitting: false }));
      const msg = error?.message || '';
      if (msg.includes('folder_not_found')) {
        message.error('目标文件夹不存在');
      } else if (msg.includes('output_parent_missing')) {
        message.error('目标文件夹父路径不存在，无法清理');
      } else if (msg.includes('rename_name_empty')) {
        message.error('新文件夹名称不能为空');
      } else if (msg.includes('rename_invalid_name')) {
        message.error('新文件夹名称不合法');
      } else if (msg.includes('rename_target_exists')) {
        message.error('目标文件夹已存在，请更换名称');
      } else {
        message.error('删图/重命名文件夹失败');
      }
    }
  };

  const handleCreateJob = async () => {
    if (!folderName.trim()) {
      message.error('请输入文件夹名称');
      return;
    }
    if (!modelName) {
      message.error('请选择模型');
      return;
    }
    setJobCreating(true);
    try {
      const job = await areaApi.createJob({
        folder_name: folderName.trim(),
        model_name: modelName,
      });
      message.success(`任务已创建: ${job.job_id}`);
      await fetchJobs({ keepSelection: true, page: 1 });
      withDiscardConfirm(() => {
        setSelectedJobId(job.job_id);
      });
    } catch (error) {
      message.error(getCreateJobErrorMessage(error.message));
    } finally {
      setJobCreating(false);
    }
  };

  const handleSelectJob = (jobId) => {
    withDiscardConfirm(() => {
      setSelectedJobId(jobId);
    });
  };

  const handleToggleDelete = () => {
    if (!selectedInstance) return;
    updateInstanceById(selectedInstance.instance_id, (item) => ({
      ...item,
      is_deleted: !item.is_deleted,
    }));
  };

  const handleSaveEditor = async () => {
    if (!selectedJobId || !selectedEditorImageId) {
      message.warning('请先选择任务和图片');
      return;
    }
    setEditorSaving(true);
    try {
      await areaApi.saveEditorImage(selectedJobId, selectedEditorImageId, {
        edited_by_id: queueUserIdRef.current,
        instances: workingInstances.map((item) => ({
          instance_id: item.instance_id,
          class_name: item.class_name,
          is_deleted: !!item.is_deleted,
          polygon: Array.isArray(item.polygon) ? item.polygon : [],
          bbox: computeBBoxFromPolygon(item.polygon, item.bbox),
        })),
      });
      message.success('当前图片修改已保存');
      await fetchEditorImageDetail(selectedJobId, selectedEditorImageId);
      await fetchEditorImages(selectedJobId, editorImagePage, editorImagePageSize);
      await fetchResultSummary(selectedJobId);
      await fetchJobs({ keepSelection: true, page: jobPage });
    } catch (error) {
      message.error(error.message || '保存失败');
    } finally {
      setEditorSaving(false);
    }
  };

  const handleResetEditor = () => {
    if (!selectedJobId || !selectedEditorImageId) return;
    Modal.confirm({
      title: '确认重置',
      content: '将当前图片恢复到初始推理结果，是否继续？',
      okText: '重置',
      cancelText: '取消',
      onOk: async () => {
        try {
          await areaApi.resetEditorImage(selectedJobId, selectedEditorImageId, {
            edited_by_id: queueUserIdRef.current,
          });
          message.success('已重置当前图片');
          await fetchEditorImageDetail(selectedJobId, selectedEditorImageId);
          await fetchEditorImages(selectedJobId, editorImagePage, editorImagePageSize);
          await fetchResultSummary(selectedJobId);
        } catch (error) {
          message.error(error.message || '重置失败');
        }
      },
    });
  };

  const folderColumns = [
    {
      title: '文件夹名称',
      dataIndex: 'folder_name',
      key: 'folder_name',
      render: (_, row) => (
        <Space>
          <Typography.Text>{row.folder_name}</Typography.Text>
          <Tooltip title="复制文件夹名称">
            <Button
              size="small"
              type="text"
              icon={<CopyOutlined />}
              onClick={(event) => {
                event.stopPropagation();
                navigator.clipboard.writeText(String(row.folder_name || '')).then(() => {
                  message.success('已复制文件夹名称');
                }).catch(() => {
                  message.warning('复制失败，请手动复制');
                });
              }}
            />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '图片数',
      dataIndex: 'image_count',
      key: 'image_count',
      width: 90,
      align: 'center',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 200,
      render: (value) => (value ? String(value).replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, row) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={(e) => { e.stopPropagation(); openFolderImagesModal(row.folder_name, 1); }}>
            查看图片
          </Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={(e) => { e.stopPropagation(); openCleanupModal(row.folder_name); }}>
            删除/重命名文件夹
          </Button>
        </Space>
      ),
    },
  ];

  const jobColumns = [
    {
      title: '任务ID',
      dataIndex: 'job_id',
      width: 220,
      ellipsis: true,
    },
    {
      title: '文件夹',
      dataIndex: 'folder_name',
      width: 160,
    },
    {
      title: '模型',
      dataIndex: 'model_name',
      width: 180,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 160,
      render: (value) => <Tag color={statusColorMap[value] || 'default'}>{value}</Tag>,
    },
    {
      title: '进度',
      key: 'progress',
      width: 120,
      render: (_, row) => `${row.processed_images || 0}/${row.total_images || 0}`,
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => handleSelectJob(row.job_id)}>查看</Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            disabled={!['succeeded', 'succeeded_with_errors'].includes(row.status)}
            href={areaApi.getExcelUrl(row.job_id)}
            target="_blank"
          >
            Excel
          </Button>
        </Space>
      ),
    },
  ];

  const summaryColumns = [
    { title: '类别', dataIndex: 'class_name', width: 140 },
    { title: '总面积(px)', dataIndex: 'total_area_px', width: 120 },
    { title: '面积占比(%)', dataIndex: 'ratio_percent', width: 140 },
    { title: '命中图片数', dataIndex: 'image_count', width: 120 },
  ];

  const editorImageColumns = [
    { title: '图像', dataIndex: 'image_name', key: 'image_name' },
    {
      title: '编辑时间',
      dataIndex: 'edited_at',
      key: 'edited_at',
      width: 180,
      render: (value) => (value ? String(value).replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: '编辑人ID',
      dataIndex: 'edited_by_id',
      key: 'edited_by_id',
      width: 130,
      render: (value) => {
        if (!value) return '-';
        const text = String(value);
        return (
          <Tooltip title={text}>
            <span>{text.length > 10 ? text.slice(0, 8) : text}</span>
          </Tooltip>
        );
      },
    },
    {
      title: '版本',
      dataIndex: 'edit_version',
      key: 'edit_version',
      width: 70,
      align: 'center',
    },
  ];

  const onCanvasImageLoad = (event) => {
    const target = event.target;
    const width = target.naturalWidth || 0;
    const height = target.naturalHeight || 0;
    setImageNaturalSize({ width, height });
    setImageDisplaySize({ width: target.clientWidth, height: target.clientHeight });
  };

  const displayPolygon = (polygon) => {
    if (!Array.isArray(polygon) || !polygon.length || !imageNaturalSize.width || !imageNaturalSize.height || !imageDisplaySize.width || !imageDisplaySize.height) {
      return '';
    }
    const sx = imageDisplaySize.width / imageNaturalSize.width;
    const sy = imageDisplaySize.height / imageNaturalSize.height;
    return polygon.map((point) => `${Math.round(point[0] * sx)},${Math.round(point[1] * sy)}`).join(' ');
  };

  const displayPoint = (point) => {
    if (!Array.isArray(point) || point.length !== 2 || !imageNaturalSize.width || !imageNaturalSize.height || !imageDisplaySize.width || !imageDisplaySize.height) {
      return { x: 0, y: 0 };
    }
    const sx = imageDisplaySize.width / imageNaturalSize.width;
    const sy = imageDisplaySize.height / imageNaturalSize.height;
    return {
      x: Math.round(point[0] * sx),
      y: Math.round(point[1] * sy),
    };
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title="文件夹检索">
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Input
              style={{ width: 320 }}
              placeholder="搜索编号（模糊匹配文件夹名称）"
              value={folderQueryInput}
              onChange={(event) => setFolderQueryInput(event.target.value)}
              onPressEnter={handleSearchFolders}
            />
            <Button icon={<SearchOutlined />} onClick={handleSearchFolders}>搜索</Button>
            <Button type="primary" icon={<ReloadOutlined />} onClick={handleRefreshFolders}>刷新</Button>
            <InputNumber
              style={{ width: 170 }}
              addonBefore="显示数量"
              min={1}
              max={100}
              value={folderLimit}
              onChange={(value) => setFolderLimit(clamp(Number(value || 5), 1, 100))}
            />
          </Space>
        </Space>

        <Table
          rowKey={(row) => row.folder_name}
          style={{ marginTop: 12 }}
          columns={folderColumns}
          dataSource={folderItems}
          loading={folderLoading}
          size="small"
          pagination={folderQueryUsed ? false : {
            current: folderPage,
            pageSize: 5,
            total: folderTotal,
            onChange: (page) => fetchFolders({ query: '', page }),
            showSizeChanger: false,
          }}
          onRow={(record) => ({
            onClick: () => setFolderName(record.folder_name || ''),
            onDoubleClick: () => setFolderName(record.folder_name || ''),
          })}
        />
      </Card>

      <Card title="创建任务">
        <Space wrap>
          <Input
            style={{ width: 280 }}
            placeholder="文件夹名称"
            value={folderName}
            onChange={(event) => setFolderName(event.target.value)}
          />
          <Select
            style={{ width: 280 }}
            value={modelName || undefined}
            onChange={setModelName}
            options={modelOptions.map((item) => ({ value: item, label: item }))}
            placeholder="选择模型"
          />
          <Button type="primary" onClick={handleCreateJob} loading={jobCreating}>开始处理</Button>
        </Space>
      </Card>

      <Card
        title="任务列表"
        extra={(
          <Space>
            <Input
              style={{ width: 220 }}
              placeholder="搜索任务ID/文件夹/模型"
              value={jobSearchInput}
              onChange={(event) => setJobSearchInput(event.target.value)}
              onPressEnter={() => {
                setJobSearchQuery(jobSearchInput.trim());
                fetchJobs({ keepSelection: true, page: 1, query: jobSearchInput.trim() });
              }}
            />
            <Button icon={<SearchOutlined />} onClick={() => {
              setJobSearchQuery(jobSearchInput.trim());
              fetchJobs({ keepSelection: true, page: 1, query: jobSearchInput.trim() });
            }}>搜索</Button>
            <Button icon={<ReloadOutlined />} onClick={() => fetchJobs({ keepSelection: true, page: jobPage })} loading={jobsLoading}>刷新</Button>
          </Space>
        )}
      >
        <Table
          rowKey={(row) => row.job_id}
          columns={jobColumns}
          dataSource={jobs}
          loading={jobsLoading}
          size="small"
          pagination={{
            current: jobPage,
            pageSize: jobPageSize,
            total: jobsTotal,
            onChange: (page, pageSize) => {
              setJobPage(page);
              setJobPageSize(pageSize);
              fetchJobs({ keepSelection: true, page });
            },
            showSizeChanger: false,
          }}
          onRow={(record) => ({
            onDoubleClick: () => handleSelectJob(record.job_id),
          })}
        />
      </Card>

      <Card title="结果编辑栏" loading={detailsLoading || editorImagesLoading || editorImageLoading}>
        {!selectedJobId ? (
          <Typography.Text type="secondary">请在任务列表中点击“查看”或双击任务行。</Typography.Text>
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Card size="small" type="inner">
              <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space wrap>
                  <Typography.Text>创建日期：{selectedJob?.created_at ? String(selectedJob.created_at).replace('T', ' ').slice(0, 19) : '-'}</Typography.Text>
                  <Typography.Text>编辑日期：{editorDetail?.image?.edited_at ? String(editorDetail.image.edited_at).replace('T', ' ').slice(0, 19) : '-'}</Typography.Text>
                  <Typography.Text>ID：{editorDetail?.image?.edited_by_id || '-'}</Typography.Text>
                </Space>
                <Space>
                  <Button type="primary" icon={<SaveOutlined />} loading={editorSaving} onClick={handleSaveEditor} disabled={!selectedEditorImageId}>保存</Button>
                  <Button icon={<DownloadOutlined />} href={areaApi.getExcelUrl(selectedJobId)} target="_blank">导出Excel</Button>
                </Space>
              </Space>
            </Card>

            <Row gutter={12}>
              <Col span={8}>
                <Card size="small" title="图片列表" bodyStyle={{ padding: 8 }}>
                  <Table
                    rowKey={(row) => row.image_id}
                    columns={editorImageColumns}
                    dataSource={editorImages}
                    size="small"
                    pagination={{
                      current: editorImagePage,
                      pageSize: editorImagePageSize,
                      total: editorImagesTotal,
                      onChange: (page) => {
                        fetchEditorImages(selectedJobId, page, editorImagePageSize);
                      },
                      showSizeChanger: false,
                    }}
                    onRow={(record) => ({
                      onClick: () => {
                        withDiscardConfirm(() => {
                          setSelectedEditorImageId(record.image_id);
                        });
                      },
                      onDoubleClick: () => {
                        withDiscardConfirm(() => {
                          setSelectedEditorImageId(record.image_id);
                        });
                      },
                    })}
                    rowClassName={(row) => (row.image_id === selectedEditorImageId ? 'ant-table-row-selected' : '')}
                  />
                </Card>

                <Card size="small" title="汇总" style={{ marginTop: 12 }} bodyStyle={{ padding: 8 }}>
                  <Table
                    rowKey={(row) => row.class_name}
                    size="small"
                    columns={summaryColumns}
                    dataSource={resultSummary}
                    pagination={false}
                  />
                </Card>
              </Col>

              <Col span={16}>
                <Card
                  size="small"
                  title={`编辑画布 ${editorDetail?.image?.image_name ? `(${editorDetail.image.image_name})` : ''}`}
                  extra={(
                    <Space direction="vertical" align="end" size={8}>
                      <Button onClick={handleToggleDelete} disabled={!selectedInstance}>弃用/加回</Button>
                      <Button icon={<EditOutlined />} onClick={() => setIsEditing((prev) => !prev)} disabled={!selectedInstance || selectedInstance?.is_deleted}>编辑</Button>
                      <Button onClick={handleResetEditor} disabled={!selectedEditorImageId}>重置</Button>
                    </Space>
                  )}
                >
                  <div style={{ marginBottom: 8, color: '#666', fontSize: 12 }}>
                    快捷键：`Space` 编辑模式，`D` 弃用/加回，`←/→` 切换图片（仅非编辑状态）
                  </div>
                  <div
                    ref={editorCanvasRef}
                    style={{
                      position: 'relative',
                      width: '100%',
                      border: '1px solid #e5e5e5',
                      borderRadius: 6,
                      minHeight: 300,
                      overflow: 'hidden',
                      background: '#fafafa',
                    }}
                  >
                    {editorDetail?.image?.overlay_filename ? (
                      <img
                        src={areaApi.getImageUrl(selectedJobId, editorDetail.image.overlay_filename)}
                        alt={editorDetail.image.image_name || 'overlay'}
                        style={{ width: '100%', display: 'block' }}
                        onLoad={onCanvasImageLoad}
                      />
                    ) : (
                      <div style={{ padding: 24, color: '#999' }}>当前图片无叠加图可编辑</div>
                    )}
                    {imageDisplaySize.width > 0 && imageDisplaySize.height > 0 && (
                      <svg
                        width={imageDisplaySize.width}
                        height={imageDisplaySize.height}
                        style={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          pointerEvents: 'auto',
                        }}
                      >
                        {workingInstances.map((item, idx) => {
                          const colorIdx = Math.max(0, classNames.indexOf(item.class_name));
                          const fill = getColorByIndex(colorIdx >= 0 ? colorIdx : idx, item.is_deleted ? 0.05 : 0.32);
                          const stroke = getColorByIndex(colorIdx >= 0 ? colorIdx : idx, item.is_deleted ? 0.25 : 0.85);
                          const points = displayPolygon(item.polygon);
                          const selected = item.instance_id === selectedInstanceId;
                          return (
                            <g key={item.instance_id}>
                              {points ? (
                                <polygon
                                  points={points}
                                  fill={fill}
                                  stroke={stroke}
                                  strokeWidth={selected ? 2 : 1}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setSelectedInstanceId(item.instance_id);
                                  }}
                                  onMouseDown={(event) => {
                                    if (!isEditing || item.is_deleted || !selected) return;
                                    event.preventDefault();
                                    const p = pointFromEvent(event);
                                    if (!p) return;
                                    setDragState({
                                      type: 'polygon',
                                      instanceId: item.instance_id,
                                      startPoint: { x: p.x, y: p.y },
                                      originPolygon: Array.isArray(item.polygon) ? item.polygon.map((point) => [...point]) : [],
                                    });
                                  }}
                                />
                              ) : null}

                              {isEditing && selected && !item.is_deleted && Array.isArray(item.polygon)
                                ? item.polygon.map((point, vertexIdx) => {
                                  const display = displayPoint(point);
                                  return (
                                    <circle
                                      key={`${item.instance_id}-${vertexIdx}`}
                                      cx={display.x}
                                      cy={display.y}
                                      r={4}
                                      fill={stroke}
                                      stroke="#fff"
                                      strokeWidth={1}
                                      onMouseDown={(event) => {
                                        event.preventDefault();
                                        event.stopPropagation();
                                        setDragState({
                                          type: 'vertex',
                                          instanceId: item.instance_id,
                                          vertexIndex: vertexIdx,
                                        });
                                      }}
                                    />
                                  );
                                })
                                : null}
                            </g>
                          );
                        })}
                      </svg>
                    )}
                  </div>

                  <Table
                    style={{ marginTop: 12 }}
                    rowKey={(row) => row.instance_id}
                    size="small"
                    columns={[
                      { title: '实例ID', dataIndex: 'instance_id', width: 90 },
                      { title: '类别', dataIndex: 'class_name', width: 130 },
                      { title: '面积(px)', dataIndex: 'area_px', width: 110 },
                      {
                        title: '状态',
                        key: 'state',
                        width: 90,
                        render: (_, row) => (row.is_deleted ? <Tag color="default">已弃用</Tag> : <Tag color="success">启用</Tag>),
                      },
                    ]}
                    dataSource={workingInstances}
                    pagination={{ pageSize: 6 }}
                    onRow={(record) => ({
                      onClick: () => setSelectedInstanceId(record.instance_id),
                    })}
                    rowClassName={(row) => (row.instance_id === selectedInstanceId ? 'ant-table-row-selected' : '')}
                  />
                </Card>
              </Col>
            </Row>
          </Space>
        )}
      </Card>

      <Card title="全局配置（对所有用户生效）" size="small">
        <Collapse
          activeKey={configOpenKeys}
          onChange={(keys) => {
            const nextKeys = Array.isArray(keys) ? keys : [keys];
            const opening = nextKeys.includes('global') && !configOpenKeys.includes('global');
            if (opening) {
              Modal.warning({
                title: '修改风险提示',
                content: '你正在修改全局配置。此变更会立即影响所有用户，并可能影响模型准确性与产出路径。',
                okText: '我已知晓',
              });
            }
            setConfigOpenKeys(nextKeys);
          }}
          items={[
            {
              key: 'global',
              label: '展开全局配置（谨慎修改）',
              children: (
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  <Space wrap>
                    <Button size="small" icon={<ReloadOutlined />} onClick={fetchConfig} loading={configLoading}>刷新配置</Button>
                    <Button size="small" type="primary" icon={<SaveOutlined />} onClick={handleSaveConfig} loading={configSaving}>保存配置</Button>
                    <Button size="small" onClick={handleManualArchive}>立即归档旧文件</Button>
                  </Space>

                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Input
                      addonBefore="根路径"
                      value={rootPath}
                      onChange={(event) => setRootPath(event.target.value)}
                      placeholder="支持本地路径和局域网路径"
                    />
                    <Input
                      addonBefore="旧文件路径"
                      value={oldRootPath}
                      onChange={(event) => setOldRootPath(event.target.value)}
                      placeholder="默认：\\\\192.168.105.82\\材料检测中心\\10特纤\\02-检验"
                    />
                    <Input
                      addonBefore="结果输出路径"
                      value={resultOutputRoot}
                      onChange={(event) => setResultOutputRoot(event.target.value)}
                      placeholder="例如：/tmp/area_outputs 或局域网挂载路径"
                    />
                    <Typography.Text type="secondary">
                      归档状态：上次执行 {archiveStatus?.last_run_at ? String(archiveStatus.last_run_at).replace('T', ' ').slice(0, 19) : '未执行'}，
                      当前 {archiveStatus?.is_due ? '已到执行窗口' : '未到执行窗口'}。
                    </Typography.Text>
                  </Space>

                  <Table
                    rowKey={(row) => row.key}
                    size="small"
                    pagination={false}
                    columns={[
                      {
                        title: '模型名称',
                        dataIndex: 'model_name',
                        width: 260,
                        render: (_, row, index) => (
                          <Input
                            size="small"
                            value={row.model_name}
                            onChange={(event) => {
                              const next = [...mappingRows];
                              next[index] = { ...next[index], model_name: event.target.value };
                              setMappingRows(next);
                            }}
                          />
                        ),
                      },
                      {
                        title: '权重文件名',
                        dataIndex: 'model_file',
                        render: (_, row, index) => (
                          <Input
                            size="small"
                            value={row.model_file}
                            onChange={(event) => {
                              const next = [...mappingRows];
                              next[index] = { ...next[index], model_file: event.target.value };
                              setMappingRows(next);
                            }}
                          />
                        ),
                      },
                    ]}
                    dataSource={mappingRows}
                  />

                  <Card size="small" type="inner" title="模型测试参数（全局）">
                    <div style={{ marginBottom: 8, color: '#555', fontSize: 12 }}>当前重建参数组</div>
                    <Space wrap size={12} style={{ marginBottom: 10 }}>
                      <Select
                        size="small"
                        style={{ width: 170 }}
                        value={configInferenceOptions.mask_mode}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, mask_mode: value }))}
                        options={[
                          { value: 'auto', label: '前景模式: 自动' },
                          { value: 'dark', label: '前景模式: 深色' },
                          { value: 'light', label: '前景模式: 浅色' },
                        ]}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 160 }}
                        addonBefore="阈值偏移"
                        min={-128}
                        max={128}
                        value={configInferenceOptions.threshold_bias}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, threshold_bias: value ?? 0 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 160 }}
                        addonBefore="最小像素"
                        min={1}
                        max={100000}
                        value={configInferenceOptions.min_pixels}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, min_pixels: value ?? 64 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 170 }}
                        addonBefore="平滑邻域阈值"
                        min={1}
                        max={5}
                        value={configInferenceOptions.smooth_min_neighbors}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, smooth_min_neighbors: value ?? 3 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 190 }}
                        addonBefore="叠加透明度"
                        min={0.05}
                        max={0.95}
                        step={0.05}
                        value={configInferenceOptions.overlay_alpha}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, overlay_alpha: value ?? 0.45 }))}
                      />
                    </Space>
                    <div style={{ marginBottom: 8, color: '#555', fontSize: 12 }}>
                      原命名参数（近似映射）
                    </div>
                    <Space wrap size={12}>
                      <InputNumber
                        size="small"
                        style={{ width: 210 }}
                        addonBefore="score_threshold"
                        min={0}
                        max={1}
                        step={0.01}
                        value={configInferenceOptions.score_threshold}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, score_threshold: value ?? 0.15 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 160 }}
                        addonBefore="top_k"
                        min={1}
                        max={1000}
                        value={configInferenceOptions.top_k}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, top_k: value ?? 200 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 170 }}
                        addonBefore="nms_top_k"
                        min={1}
                        max={1000}
                        value={configInferenceOptions.nms_top_k}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, nms_top_k: value ?? 200 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 220 }}
                        addonBefore="nms_conf_thresh"
                        min={0}
                        max={1}
                        step={0.01}
                        value={configInferenceOptions.nms_conf_thresh}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, nms_conf_thresh: value ?? 0.05 }))}
                      />
                      <InputNumber
                        size="small"
                        style={{ width: 180 }}
                        addonBefore="nms_thresh"
                        min={0}
                        max={1}
                        step={0.01}
                        value={configInferenceOptions.nms_thresh}
                        onChange={(value) => setConfigInferenceOptions((prev) => ({ ...prev, nms_thresh: value ?? 0.5 }))}
                      />
                    </Space>
                  </Card>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={`查看图片 - ${folderImagesModal.folderName || ''}`}
        open={folderImagesModal.open}
        footer={null}
        onCancel={() => setFolderImagesModal((prev) => ({ ...prev, open: false, currentIndex: 0 }))}
        width="90vw"
        style={{ top: 20 }}
        bodyStyle={{ height: '80vh', padding: 12, width: '100%', overflow: 'hidden' }}
        destroyOnClose
      >
        <div style={{ height: '100%', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {folderImagesModal.loading ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Spin />
            </div>
          ) : folderImagesModal.items.length ? (
            <>
              <div
                style={{
                  flex: 1,
                  minHeight: 0,
                  borderRadius: 8,
                  background: '#f5f5f5',
                  overflow: 'hidden',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <img
                  src={areaApi.getFolderImageUrl(
                    folderImagesModal.folderName,
                    folderImagesModal.items[folderImagesModal.currentIndex]?.name || '',
                  )}
                  alt={folderImagesModal.items[folderImagesModal.currentIndex]?.name || 'preview'}
                  style={{ width: '100%', height: '100%', objectFit: 'contain' }}
                />
              </div>
              <Typography.Text style={{ textAlign: 'center' }}>
                {folderImagesModal.items[folderImagesModal.currentIndex]?.name || ''}
              </Typography.Text>
              <div style={{ display: 'flex', justifyContent: 'center', gap: 8 }}>
                <Button
                  onClick={() => {
                    setFolderImagesModal((prev) => ({ ...prev, currentIndex: Math.max(0, prev.currentIndex - 1) }));
                  }}
                  disabled={folderImagesModal.currentIndex <= 0}
                >
                  上一张
                </Button>
                <Button
                  onClick={() => {
                    setFolderImagesModal((prev) => ({
                      ...prev,
                      currentIndex: Math.min(Math.max(prev.items.length - 1, 0), prev.currentIndex + 1),
                    }));
                  }}
                  disabled={folderImagesModal.currentIndex >= folderImagesModal.items.length - 1}
                >
                  下一张
                </Button>
              </div>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
                  gap: 8,
                  maxHeight: 180,
                  overflowY: 'auto',
                }}
              >
                {folderImagesModal.items.map((item, index) => {
                  const active = index === folderImagesModal.currentIndex;
                  return (
                    <button
                      key={item.name}
                      type="button"
                      onClick={() => setFolderImagesModal((prev) => ({ ...prev, currentIndex: index }))}
                      style={{
                        border: active ? '2px solid #1677ff' : '1px solid #d9d9d9',
                        borderRadius: 8,
                        padding: 6,
                        background: '#fff',
                        cursor: 'pointer',
                        textAlign: 'left',
                      }}
                    >
                      <Image
                        preview={false}
                        src={areaApi.getFolderImageUrl(folderImagesModal.folderName, item.name)}
                        alt={item.name}
                        style={{ width: '100%', height: 90, objectFit: 'cover', borderRadius: 4 }}
                      />
                      <Typography.Text
                        style={{
                          display: 'block',
                          marginTop: 6,
                          fontSize: 12,
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                        title={item.name}
                      >
                        {item.name}
                      </Typography.Text>
                    </button>
                  );
                })}
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#999' }}>
              当前文件夹暂无可预览图片
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'center' }}>
            <Pagination
              current={folderImagesModal.page}
              pageSize={folderImagesModal.pageSize}
              total={folderImagesModal.total}
              onChange={(page) => openFolderImagesModal(folderImagesModal.folderName, page)}
              showSizeChanger={false}
              size="small"
            />
          </div>
        </div>
      </Modal>

      <Modal
        title="删除/重命名文件夹"
        open={cleanupModal.open}
        onCancel={() => {
          if (cleanupModal.submitting) return;
          setCleanupModal((prev) => ({ ...prev, open: false }));
        }}
        onOk={handleCleanupSubmit}
        okText="确认"
        cancelText="取消"
        confirmLoading={cleanupModal.submitting}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Typography.Text type="secondary">源文件夹：</Typography.Text>
            <Typography.Text copyable={{ text: cleanupModal.folderName || '-' }} style={{ marginLeft: 8 }}>
              {cleanupModal.folderName || '-'}
            </Typography.Text>
          </div>
          <Checkbox
            checked={cleanupModal.renameEnabled}
            onChange={(event) => {
              const checked = event.target.checked;
              setCleanupModal((prev) => ({
                ...prev,
                renameEnabled: checked,
                newFolderName: checked && !prev.newFolderName
                  ? String(prev.folderName || '').split('_')[0] || prev.folderName
                  : prev.newFolderName,
              }));
            }}
          >
            同时重命名文件夹
          </Checkbox>
          <Form.Item label="新文件夹名称" style={{ marginBottom: 0 }}>
            <Input
              disabled={!cleanupModal.renameEnabled}
              value={cleanupModal.newFolderName}
              onChange={(event) => setCleanupModal((prev) => ({ ...prev, newFolderName: event.target.value }))}
            />
          </Form.Item>
        </Space>
      </Modal>
    </Space>
  );
}

export default AreaRecognition;
