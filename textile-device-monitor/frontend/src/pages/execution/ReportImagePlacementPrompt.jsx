import { Alert, Descriptions, List, Tag, Typography } from 'antd';

const { Text } = Typography;

const formatSize = value => {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return null;
  }
  if (size >= 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  if (size >= 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${size} B`;
};

// 报告上传图片放置节点的冲突询问区：展示可复制的目标文件夹路径、
// 拟放置清单和同名冲突，配合服务端下发的 placement_action 表单项。
export default function ReportImagePlacementPrompt({ plan }) {
  if (!plan) {
    return null;
  }
  const conflicts = new Set(
    Array.isArray(plan.conflicts) ? plan.conflicts : [],
  );
  const files = Array.isArray(plan.files) ? plan.files : [];
  return (
    <section className="execution-human-task__placement">
      <Alert
        showIcon
        type="warning"
        message={`目标文件夹中已存在 ${conflicts.size} 个同名图片`}
        description="可先把下方路径复制到 Windows 资源管理器中查看现有文件；确认覆盖后，目标文件夹中所有同名文件都会被替换，选择取消则不放置任何图片。"
        style={{ marginBottom: 12 }}
      />
      <Descriptions
        size="small"
        bordered
        column={1}
        style={{ marginBottom: 12 }}
        items={[
          {
            key: 'directory',
            label: '目标文件夹',
            children: (
              <Text
                code
                copyable={{
                  text: plan.display_directory || '',
                  tooltips: ['复制路径', '已复制'],
                }}
              >
                {plan.display_directory || '—'}
              </Text>
            ),
          },
          {
            key: 'count',
            label: '拟放置图片',
            children: `${plan.image_count ?? files.length} 张（命名：编号-样品识别-序号）`,
          },
        ]}
      />
      <List
        size="small"
        bordered
        header="本次将放置的图片"
        dataSource={files}
        renderItem={item => {
          const isConflict = conflicts.has(item?.target_filename);
          const sizeText = formatSize(item?.size_bytes);
          return (
            <List.Item>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span>
                  <Text strong={isConflict}>{item?.target_filename || '—'}</Text>
                  {sizeText && (
                    <Text type="secondary" style={{ marginLeft: 8 }}>
                      {sizeText}
                    </Text>
                  )}
                </span>
                {item?.source_name && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    源文件：{item.source_name}
                  </Text>
                )}
              </div>
              {isConflict ? (
                <Tag color="error">同名已存在</Tag>
              ) : (
                <Tag color="success">新增</Tag>
              )}
            </List.Item>
          );
        }}
      />
    </section>
  );
}
