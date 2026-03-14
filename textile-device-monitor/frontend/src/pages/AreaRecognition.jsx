import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Form,
  Image,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd';
import { DownloadOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { areaApi } from '../api/area';

const POLL_INTERVAL_MS = 2000;

const statusColorMap = {
  queued: 'default',
  running: 'processing',
  succeeded: 'success',
  succeeded_with_errors: 'warning',
  failed: 'error',
};

function AreaRecognition() {
  const [configLoading, setConfigLoading] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [jobCreating, setJobCreating] = useState(false);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [rootPath, setRootPath] = useState('');
  const [mappingRows, setMappingRows] = useState([]);
  const [folderName, setFolderName] = useState('');
  const [modelName, setModelName] = useState('');
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState('');
  const [resultSummary, setResultSummary] = useState([]);
  const [resultDetails, setResultDetails] = useState([]);
  const [imageItems, setImageItems] = useState([]);
  const [imagesTotal, setImagesTotal] = useState(0);
  const [imagePage, setImagePage] = useState(1);
  const [imagePageSize, setImagePageSize] = useState(20);

  const modelOptions = useMemo(
    () => mappingRows.map((item) => item.model_name).filter(Boolean),
    [mappingRows],
  );

  const runningJobs = useMemo(
    () => jobs.filter((item) => ['queued', 'running'].includes(item.status)),
    [jobs],
  );

  const selectedJob = useMemo(
    () => jobs.find((item) => item.job_id === selectedJobId) || null,
    [jobs, selectedJobId],
  );

  const fetchConfig = async () => {
    setConfigLoading(true);
    try {
      const data = await areaApi.getConfig();
      setRootPath(data.root_path || '');
      const rows = Object.entries(data.model_mapping || {}).map(([name, file]) => ({
        key: name,
        model_name: name,
        model_file: file,
      }));
      setMappingRows(rows);
      if (!modelName && rows.length > 0) {
        setModelName(rows[0].model_name);
      }
    } catch (error) {
      message.error(error.message || '读取配置失败');
    } finally {
      setConfigLoading(false);
    }
  };

  const fetchJobs = async (keepSelection = true) => {
    setJobsLoading(true);
    try {
      const data = await areaApi.listJobs({ limit: 200 });
      setJobs(Array.isArray(data) ? data : []);
      if (!keepSelection && Array.isArray(data) && data.length > 0) {
        setSelectedJobId(data[0].job_id);
      }
    } catch (error) {
      message.error(error.message || '读取任务列表失败');
    } finally {
      setJobsLoading(false);
    }
  };

  const fetchJobDetails = async (jobId, page = imagePage, pageSize = imagePageSize) => {
    if (!jobId) {
      setResultSummary([]);
      setResultDetails([]);
      setImageItems([]);
      setImagesTotal(0);
      return;
    }
    setDetailsLoading(true);
    try {
      let summary = [];
      let details = [];
      try {
        const result = await areaApi.getResult(jobId);
        summary = result.summary || [];
        details = result.per_image || [];
      } catch (error) {
        // Ignore until task completed.
      }
      const images = await areaApi.getImages(jobId, { page, page_size: pageSize });
      setResultSummary(summary);
      setResultDetails(details);
      setImageItems(images.items || []);
      setImagesTotal(images.total || 0);
      setImagePage(images.page || page);
      setImagePageSize(images.page_size || pageSize);
    } catch (error) {
      message.error(error.message || '读取任务详情失败');
    } finally {
      setDetailsLoading(false);
    }
  };

  useEffect(() => {
    fetchConfig();
    fetchJobs(false);
  }, []);

  useEffect(() => {
    fetchJobDetails(selectedJobId, imagePage, imagePageSize);
  }, [selectedJobId]);

  useEffect(() => {
    if (!runningJobs.length) return undefined;
    const timer = setInterval(() => {
      fetchJobs(true);
      if (selectedJobId) {
        fetchJobDetails(selectedJobId, imagePage, imagePageSize);
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [runningJobs.length, selectedJobId, imagePage, imagePageSize]);

  const handleSaveConfig = async () => {
    const modelMapping = {};
    for (const row of mappingRows) {
      const modelKey = String(row.model_name || '').trim();
      const modelFile = String(row.model_file || '').trim();
      if (!modelKey || !modelFile) continue;
      modelMapping[modelKey] = modelFile;
    }
    if (!rootPath.trim()) {
      message.error('根路径不能为空');
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
        model_mapping: modelMapping,
      });
      message.success('配置已保存');
      await fetchConfig();
    } catch (error) {
      message.error(error.message || '保存配置失败');
    } finally {
      setConfigSaving(false);
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
      setFolderName('');
      await fetchJobs(true);
      setSelectedJobId(job.job_id);
      setImagePage(1);
      await fetchJobDetails(job.job_id, 1, imagePageSize);
    } catch (error) {
      message.error(error.message || '创建任务失败');
    } finally {
      setJobCreating(false);
    }
  };

  const handleImagePageChange = async (page, pageSize) => {
    setImagePage(page);
    setImagePageSize(pageSize);
    if (selectedJobId) {
      await fetchJobDetails(selectedJobId, page, pageSize);
    }
  };

  const mappingColumns = [
    {
      title: '模型名称',
      dataIndex: 'model_name',
      width: 240,
      render: (_, row, index) => (
        <Input
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
          value={row.model_file}
          onChange={(event) => {
            const next = [...mappingRows];
            next[index] = { ...next[index], model_file: event.target.value };
            setMappingRows(next);
          }}
        />
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
          <Button size="small" onClick={() => setSelectedJobId(row.job_id)}>查看</Button>
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
    { title: '类别', dataIndex: 'class_name' },
    { title: '总面积(px)', dataIndex: 'total_area_px', width: 120 },
    { title: '面积占比(%)', dataIndex: 'ratio_percent', width: 140 },
    { title: '命中图片数', dataIndex: 'image_count', width: 120 },
  ];

  const detailColumns = [
    { title: '图像', dataIndex: 'image_name', width: 200 },
    { title: '类别', dataIndex: 'class_name', width: 150 },
    { title: '实例数', dataIndex: 'instance_count', width: 100 },
    { title: '面积(px)', dataIndex: 'area_px', width: 120 },
    { title: '占比(%)', dataIndex: 'ratio_percent', width: 120 },
    { title: '错误', dataIndex: 'error', ellipsis: true },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="全局配置（对所有用户生效）"
        extra={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={fetchConfig} loading={configLoading}>刷新</Button>
            <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveConfig} loading={configSaving}>保存配置</Button>
          </Space>
        )}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Input
            addonBefore="根路径"
            value={rootPath}
            onChange={(event) => setRootPath(event.target.value)}
            placeholder="输入服务器可访问的根路径"
          />
          <Table
            rowKey={(row) => row.key}
            columns={mappingColumns}
            dataSource={mappingRows}
            pagination={false}
            size="small"
          />
        </Space>
      </Card>

      <Card title="创建任务">
        <Space wrap>
          <Input
            style={{ width: 280 }}
            placeholder="文件夹名称（精确匹配）"
            value={folderName}
            onChange={(event) => setFolderName(event.target.value)}
          />
          <Select
            style={{ width: 260 }}
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
        extra={<Button icon={<ReloadOutlined />} onClick={() => fetchJobs(true)} loading={jobsLoading}>刷新</Button>}
      >
        <Table
          rowKey={(row) => row.job_id}
          columns={jobColumns}
          dataSource={jobs}
          loading={jobsLoading}
          size="small"
          pagination={{ pageSize: 8 }}
        />
      </Card>

      <Card title={`任务详情 ${selectedJob ? `(${selectedJob.job_id})` : ''}`} loading={detailsLoading}>
        <Row gutter={[16, 16]}>
          <Col span={24}>
            <Table
              rowKey={(row) => row.class_name}
              size="small"
              columns={summaryColumns}
              dataSource={resultSummary}
              pagination={false}
            />
          </Col>
          <Col span={24}>
            <Table
              rowKey={(row, index) => `${row.image_name}-${row.class_name}-${index}`}
              size="small"
              columns={detailColumns}
              dataSource={resultDetails}
              pagination={{ pageSize: 10 }}
              scroll={{ x: 900 }}
            />
          </Col>
          <Col span={24}>
            <Card
              type="inner"
              title="分割叠加图预览"
              extra={(
                <Space>
                  <span>总数: {imagesTotal}</span>
                  <InputNumber
                    min={1}
                    max={200}
                    value={imagePageSize}
                    onChange={(value) => setImagePageSize(value || 20)}
                  />
                  <Button
                    onClick={() => handleImagePageChange(1, imagePageSize)}
                    disabled={!selectedJobId}
                  >
                    应用分页
                  </Button>
                </Space>
              )}
            >
              <Row gutter={[12, 12]}>
                {imageItems.map((item) => (
                  <Col key={`${item.image_name}-${item.overlay_filename}`} xs={24} sm={12} md={8} lg={6}>
                    <Card size="small" title={item.image_name}>
                      <Image
                        width="100%"
                        src={areaApi.getImageUrl(selectedJobId, item.overlay_filename)}
                        alt={item.image_name}
                      />
                    </Card>
                  </Col>
                ))}
              </Row>
              <Space style={{ marginTop: 12 }}>
                <Button
                  disabled={imagePage <= 1}
                  onClick={() => handleImagePageChange(imagePage - 1, imagePageSize)}
                >
                  上一页
                </Button>
                <span>第 {imagePage} 页</span>
                <Button
                  disabled={imagePage * imagePageSize >= imagesTotal}
                  onClick={() => handleImagePageChange(imagePage + 1, imagePageSize)}
                >
                  下一页
                </Button>
              </Space>
            </Card>
          </Col>
        </Row>
      </Card>
    </Space>
  );
}

export default AreaRecognition;
