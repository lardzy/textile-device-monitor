import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import {
  ClearOutlined,
  CopyOutlined,
  DownloadOutlined,
  FileWordOutlined,
  InboxOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { saveAs } from 'file-saver';
import dayjs from 'dayjs';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import { ocrApi } from '../api/ocr';

const { Dragger } = Upload;
const { Text } = Typography;

const POLLING_INTERVAL_MS = 2000;
const POLLING_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_UPLOAD_MB = 30;
const MAX_BATCH_FILES = 10;
const ALLOWED_EXTENSIONS = ['pdf', 'png', 'jpg', 'jpeg', 'webp'];

const ERROR_CODE_MESSAGES = {
  ocr_disabled: 'OCR服务未启用',
  invalid_file_type: '文件类型不支持，仅支持 PDF/PNG/JPG/JPEG/WEBP',
  invalid_pdf: 'PDF文件无效或已损坏，请检查后重试',
  invalid_page_range: '页面范围格式无效，请使用例如 1-3,5',
  page_range_out_of_bounds: '页面范围超出PDF总页数，请重新填写',
  pdf_page_limit_exceeded: '单次识别页数过多，请缩小页面范围后重试',
  pdf_processing_failed: 'PDF预处理失败，请稍后重试',
  file_too_large: `文件过大，请控制在 ${MAX_UPLOAD_MB}MB 以内`,
  too_many_files: `一次最多上传 ${MAX_BATCH_FILES} 个文件`,
  empty_file_list: '请先选择文件',
  ocr_timeout: 'OCR识别超时，请稍后重试',
  ocr_service_unreachable: 'OCR服务不可达，请检查 OCR 服务状态',
  ocr_inference_failed: 'OCR识别失败，请重试',
  oom: 'GPU显存不足，请减少页数或分批识别',
  job_not_completed: '任务尚未完成',
  job_not_found: '任务不存在或已被清理',
  result_not_found: '结果文件不存在或已被清理',
};

const STATUS_MAP = {
  queued: { color: 'default', text: '排队中' },
  running: { color: 'processing', text: '处理中' },
  succeeded: { color: 'success', text: '成功' },
  failed: { color: 'error', text: '失败' },
};

const getErrorMessage = (codeOrMessage) => {
  if (!codeOrMessage) return '请求失败';
  return ERROR_CODE_MESSAGES[codeOrMessage] || codeOrMessage;
};

const pickJobError = (job) => {
  if (!job) return '';
  if (job.error_message && job.error_message !== job.error_code) {
    return job.error_message;
  }
  return job.error_code || job.error_message || '';
};

const formatDuration = (job) => {
  if (!job?.started_at || !job?.finished_at) return '-';
  const start = dayjs(job.started_at);
  const end = dayjs(job.finished_at);
  if (!start.isValid() || !end.isValid()) return '-';
  const ms = Math.max(0, end.diff(start));
  return `${(ms / 1000).toFixed(1)} 秒`;
};

const isBBox = (value) => (
  Array.isArray(value)
  && value.length >= 4
  && value.slice(0, 4).every((item) => Number.isFinite(Number(item)))
);

const collectDetectionRows = (jsonData, ctx, rows) => {
  if (Array.isArray(jsonData)) {
    jsonData.forEach((item) => collectDetectionRows(item, ctx, rows));
    return;
  }
  if (!jsonData || typeof jsonData !== 'object') {
    return;
  }

  const pages = jsonData.pages;
  if (Array.isArray(pages)) {
    pages.forEach((pageObj, pageIndex) => {
      const pageNumber = Number(pageObj?.page_number) || pageIndex + 1;
      collectDetectionRows(pageObj?.data, { ...ctx, pageNumber }, rows);
    });
  }

  if (isBBox(jsonData.bbox_2d)) {
    const bbox = jsonData.bbox_2d.slice(0, 4).map((item) => Number(item));
    rows.push({
      key: `${ctx.jobId}-${ctx.fileIndex}-${rows.length}`,
      fileIndex: ctx.fileIndex,
      filename: ctx.filename,
      pageNumber: ctx.pageNumber || 1,
      label: jsonData.label || jsonData.native_label || '-',
      content: typeof jsonData.content === 'string' ? jsonData.content : '-',
      bboxText: `[${bbox.map((item) => Math.round(item)).join(', ')}]`,
    });
  }

  Object.values(jsonData).forEach((value) => {
    if (value !== pages) {
      collectDetectionRows(value, ctx, rows);
    }
  });
};

function OcrTool() {
  const [fileList, setFileList] = useState([]);
  const [pageRange, setPageRange] = useState('');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [exportingDocx, setExportingDocx] = useState(false);
  const [polling, setPolling] = useState(false);
  const [jobs, setJobs] = useState([]);
  const [resultsByJobId, setResultsByJobId] = useState({});

  const pollStartedAtRef = useRef(0);
  const pollingBusyRef = useRef(false);
  const failedNoticeRef = useRef(new Set());
  const jobsRef = useRef([]);
  const resultsRef = useRef({});
  const [messageApi, contextHolder] = message.useMessage();

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    resultsRef.current = resultsByJobId;
  }, [resultsByJobId]);

  const orderedJobs = useMemo(
    () => [...jobs].sort((a, b) => (a.upload_index || 0) - (b.upload_index || 0)),
    [jobs],
  );

  const summary = useMemo(() => {
    const total = orderedJobs.length;
    const succeeded = orderedJobs.filter((job) => job.status === 'succeeded').length;
    const failed = orderedJobs.filter((job) => job.status === 'failed').length;
    const running = orderedJobs.filter((job) => ['queued', 'running'].includes(job.status)).length;
    return { total, succeeded, failed, running };
  }, [orderedJobs]);

  const mergedResult = useMemo(() => {
    const markdownParts = [];
    const files = [];

    orderedJobs.forEach((job) => {
      const sectionTitle = `## ${job.upload_index || '-'} - ${job.original_filename || job.job_id}`;
      const result = resultsByJobId[job.job_id];
      const errorText = getErrorMessage(pickJobError(job));

      if (job.status === 'succeeded' && result) {
        markdownParts.push(sectionTitle);
        markdownParts.push('');
        markdownParts.push(result.markdown_text || '(空)');
      } else if (job.status === 'failed') {
        markdownParts.push(sectionTitle);
        markdownParts.push('');
        markdownParts.push(`> 识别失败：${errorText}`);
      } else {
        markdownParts.push(sectionTitle);
        markdownParts.push('');
        markdownParts.push('> 处理中...');
      }

      files.push({
        upload_index: job.upload_index,
        job_id: job.job_id,
        filename: job.original_filename,
        status: job.status,
        error: job.status === 'failed' ? errorText : null,
        json_data: result?.json_data ?? null,
      });
    });

    const mergedJson = {
      generated_at: dayjs().toISOString(),
      total_files: files.length,
      files,
    };

    const detectionRows = [];
    files.forEach((item, index) => {
      if (item.status === 'succeeded' && item.json_data != null) {
        collectDetectionRows(item.json_data, {
          fileIndex: item.upload_index || index + 1,
          filename: item.filename || '-',
          pageNumber: 1,
          jobId: item.job_id,
        }, detectionRows);
      }
    });

    return {
      markdown: markdownParts.join('\n\n').trim(),
      json: mergedJson,
      detectionRows,
    };
  }, [orderedJobs, resultsByJobId]);

  const refreshPendingJobs = async () => {
    if (pollingBusyRef.current) return;

    const snapshotJobs = jobsRef.current;
    const pendingJobs = snapshotJobs.filter((job) => ['queued', 'running'].includes(job.status));
    if (!pendingJobs.length) {
      setPolling(false);
      return;
    }

    if (Date.now() - pollStartedAtRef.current > POLLING_TIMEOUT_MS) {
      setPolling(false);
      messageApi.error(getErrorMessage('ocr_timeout'));
      return;
    }

    pollingBusyRef.current = true;
    try {
      const updatedMap = new Map();
      for (const pendingJob of pendingJobs) {
        try {
          const latestJob = await ocrApi.getJob(pendingJob.job_id);
          updatedMap.set(pendingJob.job_id, latestJob);
        } catch (error) {
          updatedMap.set(pendingJob.job_id, {
            ...pendingJob,
            status: 'failed',
            error_code: error.message,
            error_message: error.message,
          });
        }
      }

      const mergedJobs = snapshotJobs.map((job) => {
        const updated = updatedMap.get(job.job_id);
        if (!updated) return job;
        return { ...job, ...updated };
      });
      setJobs(mergedJobs);

      for (const job of mergedJobs) {
        if (job.status === 'failed' && !failedNoticeRef.current.has(job.job_id)) {
          failedNoticeRef.current.add(job.job_id);
          messageApi.error(`[${job.original_filename || job.job_id}] ${getErrorMessage(pickJobError(job))}`);
        }
        if (job.status === 'succeeded' && !resultsRef.current[job.job_id]) {
          try {
            const result = await ocrApi.getJobResult(job.job_id);
            setResultsByJobId((prev) => ({ ...prev, [job.job_id]: result }));
          } catch (error) {
            if (!failedNoticeRef.current.has(`${job.job_id}:result`)) {
              failedNoticeRef.current.add(`${job.job_id}:result`);
              messageApi.error(`[${job.original_filename || job.job_id}] ${getErrorMessage(error.message)}`);
            }
          }
        }
      }

      const stillPending = mergedJobs.some((job) => ['queued', 'running'].includes(job.status));
      setPolling(stillPending);
    } finally {
      pollingBusyRef.current = false;
    }
  };

  useEffect(() => {
    if (!polling) return undefined;
    refreshPendingJobs();
    const timer = window.setInterval(() => {
      refreshPendingJobs();
    }, POLLING_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [polling]);

  const validateFile = (file) => {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      messageApi.error(getErrorMessage('invalid_file_type'));
      return Upload.LIST_IGNORE;
    }
    const maxBytes = MAX_UPLOAD_MB * 1024 * 1024;
    if (file.size > maxBytes) {
      messageApi.error(getErrorMessage('file_too_large'));
      return Upload.LIST_IGNORE;
    }
    return false;
  };

  const handleFileChange = ({ fileList: nextFileList }) => {
    let next = nextFileList;
    if (next.length > MAX_BATCH_FILES) {
      messageApi.warning(getErrorMessage('too_many_files'));
      next = next.slice(0, MAX_BATCH_FILES);
    }
    setFileList(next);
  };

  const handleClearUploads = () => {
    setFileList([]);
  };

  const handleSubmit = async () => {
    if (!fileList.length) {
      messageApi.error('请先选择文件');
      return;
    }
    if (fileList.length > MAX_BATCH_FILES) {
      messageApi.error(getErrorMessage('too_many_files'));
      return;
    }

    const formData = new FormData();
    fileList.forEach((item) => {
      const rawFile = item.originFileObj || item;
      formData.append('files', rawFile);
    });
    if (pageRange.trim()) formData.append('page_range', pageRange.trim());
    if (note.trim()) formData.append('note', note.trim());

    setSubmitting(true);
    try {
      const data = await ocrApi.createBatchJobs(formData);
      const nowIso = dayjs().toISOString();
      const createdJobs = (data.jobs || []).map((job, index) => ({
        job_id: job.job_id,
        status: job.status || 'queued',
        original_filename: job.original_filename || fileList[index]?.name || '-',
        upload_index: job.upload_index || index + 1,
        created_at: nowIso,
        started_at: null,
        finished_at: null,
        error_code: null,
        error_message: null,
        queue_position: null,
      }));

      setJobs(createdJobs);
      setResultsByJobId({});
      failedNoticeRef.current.clear();

      const hasPending = createdJobs.some((job) => ['queued', 'running'].includes(job.status));
      setPolling(hasPending);
      pollStartedAtRef.current = Date.now();
      messageApi.success(`OCR任务已提交（共 ${createdJobs.length} 个文件）`);
    } catch (error) {
      messageApi.error(getErrorMessage(error.message));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCopyMarkdown = async () => {
    if (!mergedResult.markdown) return;
    try {
      await navigator.clipboard.writeText(mergedResult.markdown);
      messageApi.success('Markdown 已复制');
    } catch (error) {
      messageApi.error('复制失败，请手动复制');
    }
  };

  const handleDownloadMerged = (kind) => {
    if (!orderedJobs.length) return;
    const firstName = orderedJobs[0]?.original_filename || 'ocr-result';
    const baseName = firstName.replace(/\.[^.]+$/, '');
    if (kind === 'md') {
      const blob = new Blob([mergedResult.markdown || ''], { type: 'text/markdown;charset=utf-8' });
      saveAs(blob, `${baseName}-batch.md`);
      return;
    }
    const blob = new Blob([JSON.stringify(mergedResult.json, null, 2)], { type: 'application/json;charset=utf-8' });
    saveAs(blob, `${baseName}-batch.json`);
  };

  const handleExportDocx = async () => {
    const jobIds = orderedJobs
      .filter((job) => job.status === 'succeeded')
      .sort((a, b) => (a.upload_index || 0) - (b.upload_index || 0))
      .map((job) => job.job_id);

    if (!jobIds.length) {
      messageApi.error('暂无可导出的成功任务');
      return;
    }

    setExportingDocx(true);
    try {
      const blob = await ocrApi.exportBatchDocx(jobIds);
      const name = `ocr-batch-${dayjs().format('YYYYMMDD-HHmmss')}.docx`;
      saveAs(blob, name);
      messageApi.success('Word 导出成功');
    } catch (error) {
      messageApi.error(getErrorMessage(error.message));
    } finally {
      setExportingDocx(false);
    }
  };

  const statusColumns = [
    {
      title: '顺序',
      dataIndex: 'upload_index',
      key: 'upload_index',
      width: 72,
    },
    {
      title: '文件名',
      dataIndex: 'original_filename',
      key: 'original_filename',
      ellipsis: true,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status) => {
        const cfg = STATUS_MAP[status] || STATUS_MAP.queued;
        return <Tag color={cfg.color}>{cfg.text}</Tag>;
      },
    },
    {
      title: '排队位置',
      dataIndex: 'queue_position',
      key: 'queue_position',
      width: 94,
      render: (value) => (value == null ? '-' : value),
    },
    {
      title: '耗时',
      key: 'duration',
      width: 110,
      render: (_, record) => formatDuration(record),
    },
    {
      title: '失败原因',
      key: 'error',
      render: (_, record) => (record.status === 'failed' ? getErrorMessage(pickJobError(record)) : '-'),
    },
  ];

  const detectionColumns = [
    { title: '文件序号', dataIndex: 'fileIndex', key: 'fileIndex', width: 90 },
    { title: '文件名', dataIndex: 'filename', key: 'filename', width: 200, ellipsis: true },
    { title: '页码', dataIndex: 'pageNumber', key: 'pageNumber', width: 80 },
    { title: '标签', dataIndex: 'label', key: 'label', width: 130, ellipsis: true },
    { title: '坐标框', dataIndex: 'bboxText', key: 'bboxText', width: 180 },
    { title: '内容预览', dataIndex: 'content', key: 'content', ellipsis: true },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {contextHolder}

      <Card title="OCR识别（多文件顺序处理）">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Dragger
            fileList={fileList}
            multiple
            maxCount={MAX_BATCH_FILES}
            beforeUpload={validateFile}
            onChange={handleFileChange}
            onRemove={() => true}
            accept=".pdf,.png,.jpg,.jpeg,.webp"
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
            <p className="ant-upload-hint">
              支持 PDF/PNG/JPG/JPEG/WEBP，单文件最大 {MAX_UPLOAD_MB}MB，最多 {MAX_BATCH_FILES} 个文件，按上传顺序逐个处理
            </p>
          </Dragger>

          <Form layout="vertical">
            <Form.Item label="输出格式">
              <Input value="Markdown + JSON + Word 导出" disabled />
            </Form.Item>
            <Form.Item label="页面范围（可选，对本次所有文件生效）">
              <Input
                value={pageRange}
                onChange={(event) => setPageRange(event.target.value)}
                placeholder="例如：1-3,5,8-10"
              />
            </Form.Item>
            <Form.Item label="任务备注（可选，对本次所有文件生效）">
              <Input
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="例如：合同首页识别"
              />
            </Form.Item>
            <Space>
              <Button
                type="primary"
                icon={<UploadOutlined />}
                loading={submitting}
                onClick={handleSubmit}
              >
                提交识别
              </Button>
              <Button
                icon={<ClearOutlined />}
                disabled={!fileList.length || submitting}
                onClick={handleClearUploads}
              >
                清空上传
              </Button>
              {jobs.length ? (
                <Button icon={<ReloadOutlined />} onClick={refreshPendingJobs}>
                  刷新状态
                </Button>
              ) : null}
            </Space>
          </Form>
        </Space>
      </Card>

      <Card title="任务状态">
        {!orderedJobs.length ? (
          <Alert type="info" message="尚未提交任务" showIcon />
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color="blue">总数 {summary.total}</Tag>
              <Tag color="success">成功 {summary.succeeded}</Tag>
              <Tag color="error">失败 {summary.failed}</Tag>
              <Tag color="processing">处理中 {summary.running}</Tag>
              {polling ? <Text type="secondary">自动轮询中</Text> : null}
            </Space>
            <Table
              size="small"
              rowKey="job_id"
              columns={statusColumns}
              dataSource={orderedJobs}
              pagination={false}
            />
          </Space>
        )}
      </Card>

      <Card title="识别结果（按上传顺序拼接）">
        {!orderedJobs.length ? (
          <Alert type="info" message="提交任务后会在此显示合并结果" showIcon />
        ) : (
          <Tabs
            items={[
              {
                key: 'markdown',
                label: 'Markdown 渲染',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space>
                      <Button icon={<CopyOutlined />} onClick={handleCopyMarkdown}>复制</Button>
                      <Button icon={<DownloadOutlined />} onClick={() => handleDownloadMerged('md')}>下载 .md</Button>
                      <Button
                        type="primary"
                        icon={<FileWordOutlined />}
                        loading={exportingDocx}
                        onClick={handleExportDocx}
                      >
                        导出 Word
                      </Button>
                    </Space>
                    <div
                      style={{
                        maxHeight: 560,
                        overflow: 'auto',
                        border: '1px solid #f0f0f0',
                        borderRadius: 8,
                        padding: 16,
                        background: '#fff',
                      }}
                    >
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkMath]}
                        rehypePlugins={[rehypeKatex]}
                      >
                        {mergedResult.markdown || '(空)'}
                      </ReactMarkdown>
                    </div>
                  </Space>
                ),
              },
              {
                key: 'json',
                label: 'JSON 渲染',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space>
                      <Button icon={<DownloadOutlined />} onClick={() => handleDownloadMerged('json')}>下载 .json</Button>
                    </Space>
                    <Card size="small" title="结构化检测框视图">
                      <Table
                        size="small"
                        rowKey="key"
                        columns={detectionColumns}
                        dataSource={mergedResult.detectionRows}
                        pagination={{ pageSize: 10, showSizeChanger: false }}
                        locale={{ emptyText: '无可展示的检测框数据' }}
                        scroll={{ x: 980 }}
                      />
                    </Card>
                    <Card size="small" title="原始 JSON（合并）">
                      <pre style={{ maxHeight: 360, overflow: 'auto', margin: 0 }}>
                        {JSON.stringify(mergedResult.json, null, 2)}
                      </pre>
                    </Card>
                  </Space>
                ),
              },
            ]}
          />
        )}
      </Card>
    </Space>
  );
}

export default OcrTool;
