import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
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
  CopyOutlined,
  DownloadOutlined,
  InboxOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { saveAs } from 'file-saver';
import dayjs from 'dayjs';
import { ocrApi } from '../api/ocr';

const { Dragger } = Upload;
const { Paragraph, Text } = Typography;

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

const formatDateTime = (value) => {
  if (!value) return '-';
  const m = dayjs(value);
  return m.isValid() ? m.format('YYYY-MM-DD HH:mm:ss') : '-';
};

const formatDuration = (job) => {
  if (!job?.started_at || !job?.finished_at) return '-';
  const start = dayjs(job.started_at);
  const end = dayjs(job.finished_at);
  if (!start.isValid() || !end.isValid()) return '-';
  const ms = Math.max(0, end.diff(start));
  return `${(ms / 1000).toFixed(1)} 秒`;
};

function OcrTool() {
  const [fileList, setFileList] = useState([]);
  const [pageRange, setPageRange] = useState('');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [polling, setPolling] = useState(false);
  const [jobs, setJobs] = useState([]);
  const [resultsByJobId, setResultsByJobId] = useState({});
  const [activeJobId, setActiveJobId] = useState(null);

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

  useEffect(() => {
    if (!jobs.length) {
      setActiveJobId(null);
      return;
    }
    if (!activeJobId || !jobs.some((item) => item.job_id === activeJobId)) {
      setActiveJobId(jobs[0].job_id);
    }
  }, [jobs, activeJobId]);

  const activeJob = useMemo(
    () => jobs.find((item) => item.job_id === activeJobId) || null,
    [jobs, activeJobId],
  );
  const activeResult = activeJob ? resultsByJobId[activeJob.job_id] : null;
  const statusConfig = STATUS_MAP[activeJob?.status] || STATUS_MAP.queued;
  const markdownText = activeResult?.markdown_text || '';
  const jsonText = useMemo(
    () => JSON.stringify(activeResult?.json_data ?? {}, null, 2),
    [activeResult],
  );

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
      setActiveJobId(createdJobs[0]?.job_id || null);
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
    if (!markdownText) return;
    try {
      await navigator.clipboard.writeText(markdownText);
      messageApi.success('Markdown 已复制');
    } catch (error) {
      messageApi.error('复制失败，请手动复制');
    }
  };

  const handleDownload = async (kind) => {
    if (!activeJob?.job_id) return;
    try {
      const blob = await ocrApi.downloadArtifact(activeJob.job_id, kind);
      const ext = kind === 'md' ? 'md' : 'json';
      saveAs(blob, `${activeJob.job_id}.${ext}`);
    } catch (error) {
      messageApi.error(getErrorMessage(error.message));
    }
  };

  const jobColumns = [
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
              <Input value="Markdown + JSON" disabled />
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
        {!jobs.length ? (
          <Alert type="info" message="尚未提交任务" showIcon />
        ) : (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Table
              size="small"
              rowKey="job_id"
              columns={jobColumns}
              dataSource={jobs}
              pagination={false}
              rowClassName={(record) => (record.job_id === activeJobId ? 'ant-table-row-selected' : '')}
              onRow={(record) => ({
                onClick: () => setActiveJobId(record.job_id),
              })}
            />

            {activeJob ? (
              <Descriptions bordered size="small" column={1}>
                <Descriptions.Item label="任务ID">
                  <Text copyable>{activeJob.job_id}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="当前文件">
                  {activeJob.original_filename || '-'}
                </Descriptions.Item>
                <Descriptions.Item label="状态">
                  <Tag color={statusConfig.color}>{statusConfig.text}</Tag>
                  {polling ? <Text type="secondary">（自动轮询中）</Text> : null}
                </Descriptions.Item>
                <Descriptions.Item label="排队位置">
                  {activeJob.queue_position == null ? '-' : activeJob.queue_position}
                </Descriptions.Item>
                <Descriptions.Item label="创建时间">
                  {formatDateTime(activeJob.created_at)}
                </Descriptions.Item>
                <Descriptions.Item label="开始时间">
                  {formatDateTime(activeJob.started_at)}
                </Descriptions.Item>
                <Descriptions.Item label="完成时间">
                  {formatDateTime(activeJob.finished_at)}
                </Descriptions.Item>
                <Descriptions.Item label="耗时">
                  {formatDuration(activeJob)}
                </Descriptions.Item>
                <Descriptions.Item label="失败原因">
                  {activeJob.error_code || activeJob.error_message
                    ? getErrorMessage(pickJobError(activeJob))
                    : '-'}
                </Descriptions.Item>
              </Descriptions>
            ) : null}
          </Space>
        )}
      </Card>

      <Card title="识别结果">
        {!activeJob ? (
          <Alert type="info" message="请选择任务查看结果" showIcon />
        ) : activeJob.status !== 'succeeded' ? (
          <Alert type="info" message="当前任务尚未成功，完成后会在此显示 Markdown 和 JSON 结果" showIcon />
        ) : !activeResult ? (
          <Alert type="info" message="结果加载中，请稍候或点击刷新状态" showIcon />
        ) : (
          <Tabs
            items={[
              {
                key: 'markdown',
                label: 'Markdown',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space>
                      <Button icon={<CopyOutlined />} onClick={handleCopyMarkdown}>
                        复制
                      </Button>
                      <Button icon={<DownloadOutlined />} onClick={() => handleDownload('md')}>
                        下载 .md
                      </Button>
                    </Space>
                    <Paragraph style={{ maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                      {markdownText || '(空)'}
                    </Paragraph>
                  </Space>
                ),
              },
              {
                key: 'json',
                label: 'JSON',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Button icon={<DownloadOutlined />} onClick={() => handleDownload('json')}>
                      下载 .json
                    </Button>
                    <Paragraph style={{ maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                      {jsonText}
                    </Paragraph>
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
