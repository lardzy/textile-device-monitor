import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, Space, Tabs, Tag, Typography, Upload, message } from 'antd';
import { CopyOutlined, DownloadOutlined, InboxOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import { saveAs } from 'file-saver';
import dayjs from 'dayjs';
import { ocrApi } from '../api/ocr';

const { Dragger } = Upload;
const { Paragraph, Text } = Typography;

const POLLING_INTERVAL_MS = 2000;
const POLLING_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_UPLOAD_MB = 30;
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
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [polling, setPolling] = useState(false);
  const pollStartedAtRef = useRef(0);
  const pollingBusyRef = useRef(false);
  const [messageApi, contextHolder] = message.useMessage();

  const selectedFile = fileList[0]?.originFileObj || fileList[0] || null;
  const statusConfig = STATUS_MAP[job?.status] || STATUS_MAP.queued;
  const markdownText = result?.markdown_text || '';
  const jsonText = useMemo(
    () => JSON.stringify(result?.json_data ?? {}, null, 2),
    [result],
  );

  const refreshJobState = async (jobId) => {
    if (!jobId || pollingBusyRef.current) return;
    pollingBusyRef.current = true;
    try {
      const latestJob = await ocrApi.getJob(jobId);
      setJob(latestJob);

      if (latestJob.status === 'succeeded') {
        const latestResult = await ocrApi.getJobResult(jobId);
        setResult(latestResult);
        setPolling(false);
      } else if (latestJob.status === 'failed') {
        setPolling(false);
        messageApi.error(getErrorMessage(pickJobError(latestJob)));
      } else if (Date.now() - pollStartedAtRef.current > POLLING_TIMEOUT_MS) {
        setPolling(false);
        messageApi.error(getErrorMessage('ocr_timeout'));
      }
    } catch (error) {
      setPolling(false);
      messageApi.error(getErrorMessage(error.message));
    } finally {
      pollingBusyRef.current = false;
    }
  };

  useEffect(() => {
    if (!polling || !job?.job_id) return undefined;
    const currentJobId = job.job_id;
    refreshJobState(currentJobId);
    const timer = window.setInterval(() => {
      refreshJobState(currentJobId);
    }, POLLING_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [polling, job?.job_id]);

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

  const handleSubmit = async () => {
    if (!selectedFile) {
      messageApi.error('请先选择文件');
      return;
    }

    const formData = new FormData();
    formData.append('file', selectedFile);
    if (pageRange.trim()) formData.append('page_range', pageRange.trim());
    if (note.trim()) formData.append('note', note.trim());

    setSubmitting(true);
    try {
      const data = await ocrApi.createJob(formData);
      const initialJob = {
        job_id: data.job_id,
        status: data.status || 'queued',
        created_at: dayjs().toISOString(),
        started_at: null,
        finished_at: null,
        error_code: null,
        error_message: null,
        queue_position: null,
      };
      setJob(initialJob);
      setResult(null);
      setPolling(true);
      pollStartedAtRef.current = Date.now();
      messageApi.success('OCR任务已提交');
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
    if (!job?.job_id) return;
    try {
      const blob = await ocrApi.downloadArtifact(job.job_id, kind);
      const ext = kind === 'md' ? 'md' : 'json';
      saveAs(blob, `${job.job_id}.${ext}`);
    } catch (error) {
      messageApi.error(getErrorMessage(error.message));
    }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {contextHolder}

      <Card title="OCR识别">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Dragger
            fileList={fileList}
            multiple={false}
            maxCount={1}
            beforeUpload={validateFile}
            onChange={({ fileList: nextFileList }) => setFileList(nextFileList.slice(-1))}
            onRemove={() => setFileList([])}
            accept=".pdf,.png,.jpg,.jpeg,.webp"
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
            <p className="ant-upload-hint">支持 PDF/PNG/JPG/JPEG/WEBP，单文件，最大 30MB</p>
          </Dragger>

          <Form layout="vertical">
            <Form.Item label="输出格式">
              <Input value="Markdown + JSON" disabled />
            </Form.Item>
            <Form.Item label="页面范围（可选）">
              <Input
                value={pageRange}
                onChange={(event) => setPageRange(event.target.value)}
                placeholder="例如：1-3,5,8-10"
              />
            </Form.Item>
            <Form.Item label="任务备注（可选）">
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
              {job?.job_id ? (
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => refreshJobState(job.job_id)}
                >
                  刷新状态
                </Button>
              ) : null}
            </Space>
          </Form>
        </Space>
      </Card>

      <Card title="任务状态">
        {!job ? (
          <Alert type="info" message="尚未提交任务" showIcon />
        ) : (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="任务ID">
              <Text copyable>{job.job_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              <Tag color={statusConfig.color}>{statusConfig.text}</Tag>
              {polling ? <Text type="secondary">（自动轮询中）</Text> : null}
            </Descriptions.Item>
            <Descriptions.Item label="排队位置">
              {job.queue_position == null ? '-' : job.queue_position}
            </Descriptions.Item>
            <Descriptions.Item label="创建时间">
              {formatDateTime(job.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label="开始时间">
              {formatDateTime(job.started_at)}
            </Descriptions.Item>
            <Descriptions.Item label="完成时间">
              {formatDateTime(job.finished_at)}
            </Descriptions.Item>
            <Descriptions.Item label="耗时">
              {formatDuration(job)}
            </Descriptions.Item>
            <Descriptions.Item label="失败原因">
              {job.error_code || job.error_message
                ? getErrorMessage(pickJobError(job))
                : '-'}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card title="识别结果">
        {!result ? (
          <Alert type="info" message="任务成功后会在此显示 Markdown 和 JSON 结果" showIcon />
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
