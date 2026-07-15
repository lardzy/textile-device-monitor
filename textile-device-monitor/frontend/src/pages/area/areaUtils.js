import dayjs from 'dayjs';

export const ACTIVE_JOB_STATUSES = ['queued', 'running', 'cancelling'];
export const EDITABLE_JOB_STATUSES = ['succeeded', 'succeeded_with_errors'];

export const JOB_STATUS_META = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '处理中', color: 'processing' },
  cancelling: { label: '正在取消', color: 'warning' },
  cancelled: { label: '已取消', color: 'default' },
  succeeded: { label: '已完成', color: 'success' },
  succeeded_with_errors: { label: '部分完成', color: 'warning' },
  failed: { label: '失败', color: 'error' },
};

const ERROR_MESSAGES = {
  root_path_not_found: '数据目录不可访问，请检查挂载路径或全局设置。',
  folder_not_found: '文件夹不存在或已经被移动。',
  empty_image_list: '所选文件夹中没有可处理的图片。',
  infer_service_unavailable: '面积识别服务不可用。',
  infer_model_load_failed: '模型加载失败，请检查模型映射和权重文件。',
  infer_timeout: '识别服务响应超时。',
  infer_bad_response: '识别服务返回了无效结果。',
  all_images_failed: '任务中的图片全部处理失败。',
  service_restarted: '服务重启导致任务中断，请重试任务。',
  job_not_cancellable: '当前任务状态不允许取消。',
  job_not_retryable: '当前任务状态不允许重试。',
  job_not_editable: '任务尚未完成，暂不能修改结果。',
  invalid_polygon: '轮廓至少需要三个有效顶点。',
  invalid_class_name: '所选类别不属于当前模型。',
  source_image_not_found: '原图不可访问，可能已被移动或删除。',
  edit_version_conflict: '该图片已被其他终端更新，请加载最新版本。',
  output_root_unavailable: '结果输出目录不可写。',
  model_weights_missing: '存在缺失的模型权重文件。',
};

export function getAreaErrorMessage(error, fallback = '操作失败') {
  const code = typeof error === 'string' ? error : error?.message;
  return ERROR_MESSAGES[code] || code || fallback;
}

export function formatAreaDateTime(value) {
  if (!value) return '-';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm:ss') : '-';
}

export function formatJobDuration(job) {
  const start = job?.started_at || job?.created_at;
  const end = job?.finished_at || (ACTIVE_JOB_STATUSES.includes(job?.status) ? dayjs() : null);
  if (!start || !end) return '-';
  const seconds = Math.max(0, dayjs(end).diff(dayjs(start), 'second'));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

export function makeClientInstanceId() {
  return `manual-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function polygonArea(points) {
  if (!Array.isArray(points) || points.length < 3) return 0;
  let total = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    total += Number(current?.[0] || 0) * Number(next?.[1] || 0);
    total -= Number(next?.[0] || 0) * Number(current?.[1] || 0);
  }
  return Math.round(Math.abs(total) / 2);
}

export function polygonBbox(points) {
  if (!Array.isArray(points) || points.length < 3) return [];
  const xs = points.map((point) => Number(point[0] || 0));
  const ys = points.map((point) => Number(point[1] || 0));
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)].map(Math.round);
}
