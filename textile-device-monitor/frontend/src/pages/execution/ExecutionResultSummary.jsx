import { Button, Collapse, Tag, Typography } from 'antd';
import {
  CheckCircleFilled,
  CopyOutlined,
  DownloadOutlined,
  FileExcelOutlined,
} from '@ant-design/icons';

const { Text } = Typography;

// 顶层输出键的中文标签与展示顺序；未收录的键归入“其它输出”。
const SECTION_DEFS = [
  { key: 'original_record', label: '生成原始记录' },
  { key: 'upload', label: '旧系统上传' },
  { key: 'review', label: '旧系统复核' },
  { key: 'judgement', label: '判定信息' },
  { key: 'registration_decision', label: '登记决策' },
  { key: 'registration_workbook', label: '登记工作簿' },
  { key: 'final_entry', label: '检验记录登记' },
];

const STATUS_TAG = {
  completed: { color: 'success', text: '已完成' },
  succeeded: { color: 'success', text: '已完成' },
  failed: { color: 'error', text: '失败' },
  cancelled: { color: 'warning', text: '已取消' },
  skipped: { color: 'default', text: '已跳过' },
};

const formatSize = (bytes) => {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${Math.max(1, Math.round(value / 1024))} KB`;
};

const statusTagOf = (status) => {
  const meta = STATUS_TAG[String(status || '').trim()];
  if (!meta) {
    return null;
  }
  return <Tag color={meta.color}>{meta.text}</Tag>;
};

const targetNumberOf = value => (
  value?.target_sample_number
  || value?.receipt?.target_sample_number
  || null
);

function ResultRow({ label, children }) {
  if (children === null || children === undefined || children === '') {
    return null;
  }
  return (
    <div className="execution-result-row">
      <span className="execution-result-row__label">{label}</span>
      <span className="execution-result-row__value">{children}</span>
    </div>
  );
}

function Section({ label, tag, children }) {
  return (
    <section className="execution-result-section">
      <header className="execution-result-section__head">
        <strong>{label}</strong>
        {tag}
      </header>
      <div className="execution-result-section__body">{children}</div>
    </section>
  );
}

function FileSection({ label, file }) {
  const filename = file?.filename || file?.name || '未命名文件';
  const size = formatSize(file?.size_bytes);
  const downloadUrl = file?.download_url;
  return (
    <Section
      label={label}
      tag={<FileExcelOutlined className="execution-result-section__file-icon" />}
    >
      <div className="execution-result-file-line">
        <strong title={filename}>{filename}</strong>
        {size && <Text type="secondary">{size}</Text>}
      </div>
      {downloadUrl && (
        <Button
          size="small"
          icon={<DownloadOutlined />}
          href={downloadUrl}
          target="_blank"
        >
          下载
        </Button>
      )}
    </Section>
  );
}

function OperationSection({ label, value }) {
  return (
    <Section label={label} tag={statusTagOf(value?.status)}>
      <ResultRow label="目标编号">{targetNumberOf(value)}</ResultRow>
    </Section>
  );
}

function JudgementSection({ value }) {
  const required = value?.judgement_required === true;
  if (!required) {
    return (
      <Section label="判定信息">
        <Text type="secondary" className="execution-result-muted">
          任务单未要求判定，未写入判定字段
        </Text>
      </Section>
    );
  }
  const judgement = String(value?.judgement || '').trim();
  return (
    <Section label="判定信息">
      <ResultRow label="样品识别">{value?.sample_identity}</ResultRow>
      <ResultRow label="判定依据">{value?.judge_basis}</ResultRow>
      <ResultRow label="标准值与允差">{value?.standard_value}</ResultRow>
      {judgement && (
        <ResultRow label="判定结果">
          <Tag color={judgement === '符合' ? 'success' : 'error'}>{judgement}</Tag>
        </ResultRow>
      )}
    </Section>
  );
}

function RegistrationDecisionSection({ value }) {
  const cancelled = value?.registration_cancelled === true
    || value?.existing_record_action === 'cancel';
  if (cancelled) {
    return (
      <Section label="登记决策" tag={<Tag color="warning">已取消录入</Tag>}>
        <Text type="secondary" className="execution-result-muted">
          用户在登记前选择取消，本次未写入检验记录
        </Text>
      </Section>
    );
  }
  const action = value?.existing_record_action;
  const project = value?.selected_project || {};
  const existing = Number(value?.expected_existing_register_count);
  return (
    <Section label="登记决策">
      <ResultRow label="检测项目">{project.check_item_name}</ResultRow>
      <ResultRow label="样品识别">{project.sample_identify}</ResultRow>
      <ResultRow label="处理方式">
        {action === 'append' ? '确认后直接新增' : '自动继续'}
      </ResultRow>
      {Number.isFinite(existing) && (
        <ResultRow label="登记前已有">{`${existing} 条`}</ResultRow>
      )}
    </Section>
  );
}

function FinalEntrySection({ value }) {
  const receipt = value?.receipt || {};
  const detail = receipt.final_entry || {};
  const expected = Number(detail.expected_existing_register_count);
  const resulting = Number(detail.resulting_register_count);
  const hasCounts = Number.isFinite(expected) && Number.isFinite(resulting);
  return (
    <Section label="检验记录登记" tag={statusTagOf(value?.status)}>
      <ResultRow label="目标编号">{targetNumberOf(value)}</ResultRow>
      {hasCounts && (
        <ResultRow label="登记条数">{`${expected} → ${resulting} 条`}</ResultRow>
      )}
      {detail.proofed !== undefined && detail.proofed !== null && (
        <ResultRow label="校对状态">
          {detail.proofed
            ? (
              <Tag icon={<CheckCircleFilled />} color="success">已校对</Tag>
            ) : (
              <Tag>仅保存（按流程要求不校对）</Tag>
            )}
        </ResultRow>
      )}
    </Section>
  );
}

const renderSection = (key, value) => {
  if (value === null || value === undefined) {
    return null;
  }
  switch (key) {
    case 'original_record':
      return <FileSection key={key} label="生成原始记录" file={value} />;
    case 'registration_workbook':
      return <FileSection key={key} label="登记工作簿" file={value} />;
    case 'upload':
      return <OperationSection key={key} label="旧系统上传" value={value} />;
    case 'review':
      return <OperationSection key={key} label="旧系统复核" value={value} />;
    case 'judgement':
      return <JudgementSection key={key} value={value} />;
    case 'registration_decision':
      return <RegistrationDecisionSection key={key} value={value} />;
    case 'final_entry':
      return <FinalEntrySection key={key} value={value} />;
    default:
      return null;
  }
};

const formatPrimitive = (value) => {
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'boolean') {
    return value ? '是' : '否';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
};

export default function ExecutionResultSummary({ outputs, skipKeys }) {
  const entries = Object.entries(outputs || {});
  const skipped = skipKeys || new Set();
  const knownKeys = new Set(SECTION_DEFS.map(def => def.key));
  const knownSections = SECTION_DEFS
    .filter(def => !skipped.has(def.key) && outputs?.[def.key] != null)
    .map(def => renderSection(def.key, outputs[def.key]))
    .filter(Boolean);
  const otherEntries = entries.filter(
    ([key]) => !knownKeys.has(key) && !skipped.has(key),
  );
  const rawJson = JSON.stringify(outputs || {}, null, 2);

  if (!entries.length) {
    return null;
  }

  return (
    <div className="execution-result-summary">
      {knownSections}
      {otherEntries.length > 0 && (
        <section className="execution-result-section">
          <header className="execution-result-section__head">
            <strong>其它输出</strong>
          </header>
          <div className="execution-result-section__body">
            {otherEntries.map(([key, value]) => (
              <div className="execution-result-other" key={key}>
                <Text type="secondary">{key}</Text>
                <pre>{formatPrimitive(value)}</pre>
              </div>
            ))}
          </div>
        </section>
      )}
      <Collapse
        ghost
        className="execution-result-raw"
        items={[{
          key: 'raw',
          label: '原始输出数据（JSON）',
          extra: (
            <Typography.Text
              copyable={{ text: rawJson, icon: [<CopyOutlined key="a" />, <CopyOutlined key="b" />] }}
              onClick={event => event.stopPropagation()}
            />
          ),
          children: <pre>{rawJson}</pre>,
        }]}
      />
    </div>
  );
}
