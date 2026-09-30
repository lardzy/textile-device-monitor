import { Button, List, Space, Tag, Typography } from 'antd';
import { DownloadOutlined, FileExcelOutlined } from '@ant-design/icons';
import { executionArtifactDownloadUrl } from '../../api/execution';

export const collectExecutionArtifacts = (artifacts = [], groups = []) => [
  ...artifacts,
  ...groups.flatMap(group => (group.artifacts || []).map(artifact => ({
    ...artifact, groupLabel: group.context?.label,
  }))),
];

export default function ExecutionArtifactDownloads({ artifacts = [], groups = [], workbooksOnly = false }) {
  const files = collectExecutionArtifacts(artifacts, groups)
    .filter(artifact => !workbooksOnly || /\.xlsx?$/i.test(artifact.filename || ''));
  if (workbooksOnly && !files.length) return null;

  return (
    <List
      className={`execution-artifact-downloads${workbooksOnly ? ' execution-workbook-downloads' : ''}`}
      bordered={workbooksOnly}
      header={workbooksOnly && <Space direction="vertical" size={2}>
        <Typography.Text strong>文件下载与打印</Typography.Text>
        <Typography.Text type="secondary">下载 XLS / XLSX 后用 Excel 打开，按 Ctrl+P（Mac：⌘P）打印。</Typography.Text>
      </Space>}
      locale={{ emptyText: '暂无制品' }}
      dataSource={files}
      rowKey="id"
      renderItem={artifact => <List.Item actions={[
        <Button key="download" icon={<DownloadOutlined />}
          href={executionArtifactDownloadUrl(artifact.id)} download={artifact.filename}>下载文件</Button>,
      ]}>
        <List.Item.Meta avatar={<FileExcelOutlined />} title={artifact.filename || artifact.name || artifact.relative_path}
          description={artifact.groupLabel ? <Tag>{artifact.groupLabel}</Tag> : artifact.media_type} />
      </List.Item>}
    />
  );
}
