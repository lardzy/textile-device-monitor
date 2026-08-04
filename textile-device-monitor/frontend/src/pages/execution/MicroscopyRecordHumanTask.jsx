import { useEffect, useMemo } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import {
  DownloadOutlined,
  FileExcelOutlined,
  PrinterOutlined,
} from '@ant-design/icons';

const { Paragraph, Text } = Typography;

const compactStrings = values => Array.from(new Set(
  (Array.isArray(values) ? values : [values])
    .flatMap((value) => {
      if (value === null || value === undefined) return [];
      if (typeof value === 'object') {
        const resolved = value.value
          ?? value.text
          ?? value.name
          ?? value.label
          ?? value.display_name;
        return resolved === null || resolved === undefined ? [] : [resolved];
      }
      return [value];
    })
    .map(value => String(value).trim())
    .filter(Boolean),
));

export const splitSampleIdentities = value => Array.from(new Set(
  (Array.isArray(value) ? value : [value])
    .flatMap(item => (
      typeof item === 'string' ? item.split(/[，,、]/) : [item]
    ))
    .map(item => String(item ?? '').trim())
    .filter(Boolean),
));

const projectKey = (project, index) => String(
  project?.project_key
  ?? project?.key
  ?? project?.task_check_item_id
  ?? project?.id
  ?? `project-${index + 1}`,
);

const projectName = project => (
  project?.project_name
  || project?.check_item_name
  || project?.item_name
  || project?.name
  || '未命名检测项目'
);

const projectMethod = project => (
  project?.test_method
  || project?.check_method
  || project?.check_basis
  || project?.method
  || ''
);

const hasJudgement = value => {
  if (value === null || value === undefined || value === false || value === 0) return false;
  const normalized = String(value).trim().toLowerCase();
  return !['', '0', 'false', 'no', '否', '否定'].includes(normalized);
};

const projectIdentitySource = (project, input) => (
  project?.sample_identify
  ?? project?.sample_identity
  ?? project?.sample_identification
  ?? project?.sample_identify_options
  ?? input?.sample_identify
  ?? input?.sample_identity
  ?? input?.sample_identify_options
  ?? []
);

const projectBasisSource = (project, input) => (
  project?.check_basis_options
  ?? project?.judge_basis_options
  ?? project?.judgement_basis_options
  ?? input?.check_basis_options
  ?? []
);

const candidateNameValues = analysis => {
  const source = analysis?.candidates
    ?? analysis?.options
    ?? analysis?.suggestions
    ?? analysis?.segments
    ?? analysis?.tokens
    ?? [];
  return compactStrings(source);
};

const originalSampleName = (analysis, input) => (
  analysis?.original
  ?? analysis?.original_text
  ?? analysis?.source
  ?? input?.sample_name
  ?? ''
);

export const microscopyTaskKind = (task, nodeRun) => (
  task?.task_kind
  || nodeRun?.input_data?.task_kind
  || nodeRun?.input_data?.record_context?.task_kind
  || ''
);

const MicroscopyRecordInput = ({ form, inputData, disabled }) => {
  const input = inputData?.record_context || inputData || {};
  const projects = useMemo(() => (
    Array.isArray(input.projects)
      ? input.projects.map((project, index) => ({
        ...project,
        _key: projectKey(project, index),
      }))
      : []
  ), [input.projects]);
  const selectedProjectKey = Form.useWatch('selected_project_key', form);
  const selectedProject = projects.find(project => project._key === selectedProjectKey)
    || (projects.length === 1 ? projects[0] : null);
  const identities = useMemo(
    () => splitSampleIdentities(projectIdentitySource(selectedProject, input)),
    [input, selectedProject],
  );
  const basisOptions = useMemo(
    () => compactStrings(projectBasisSource(selectedProject, input)),
    [input, selectedProject],
  );
  const judgementRequired = hasJudgement(
    selectedProject?.give_judgement
    ?? selectedProject?.giveJudgement
    ?? selectedProject?.judge_flag
    ?? input.give_judgement,
  );
  const analysis = input.sample_name_analysis || {};
  const rawSampleName = String(originalSampleName(analysis, input) || '').trim();
  const nameCandidates = candidateNameValues(analysis);
  const selectedImageCount = Array.isArray(input.selected_image_ids)
    ? input.selected_image_ids.length
    : Number(input.selected_image_count || 0);
  const judgementOptions = compactStrings(
    selectedProject?.judgement_options
    ?? input.judgement_options
    ?? ['符合', '不符合'],
  );

  useEffect(() => {
    if (projects.length === 1 && form.getFieldValue('selected_project_key') !== projects[0]._key) {
      form.setFieldValue('selected_project_key', projects[0]._key);
    }
  }, [form, projects]);

  useEffect(() => {
    const currentIdentity = form.getFieldValue('sample_identity');
    const currentBasis = form.getFieldValue('judge_basis');
    const currentJudgement = form.getFieldValue('judgement');
    form.setFieldsValue({
      sample_identity: identities.length === 1
        ? identities[0]
        : identities.includes(currentIdentity) ? currentIdentity : undefined,
      judge_basis: judgementRequired && basisOptions.length === 1
        ? basisOptions[0]
        : basisOptions.includes(currentBasis) ? currentBasis : undefined,
      judgement: judgementRequired && judgementOptions.includes(currentJudgement)
        ? currentJudgement
        : undefined,
    });
  }, [basisOptions, form, identities, judgementOptions, judgementRequired, selectedProjectKey]);

  const projectOptions = projects.map(project => ({
    value: project._key,
    label: projectMethod(project)
      ? `${projectName(project)} · ${projectMethod(project)}`
      : projectName(project),
  }));

  return (
    <section className="execution-microscopy-record">
      <div className="execution-microscopy-record__summary">
        <div>
          <Text type="secondary">将写入原始记录</Text>
          <strong>{input.inspection_number || input.sample_number || '当前检验编号'}</strong>
        </div>
        <div>
          <Text type="secondary">已选图片</Text>
          <strong>{selectedImageCount} 张</strong>
        </div>
      </div>

      {projects.length === 0 ? (
        <Alert
          showIcon
          type="error"
          message="未读取到可用的检测项目"
          description="请刷新旧系统任务单后再处理；本任务暂不能提交。"
        />
      ) : projects.length === 1 ? (
        <Descriptions size="small" bordered column={1}>
          <Descriptions.Item label="检测项目">
            {projectOptions[0].label}
            <Tag color="blue" className="execution-microscopy-record__automatic">已自动选用</Tag>
          </Descriptions.Item>
        </Descriptions>
      ) : (
        <Form.Item
          name="selected_project_key"
          label="检测项目"
          rules={[{ required: true, message: '请选择本次使用的检测项目' }]}
        >
          <Select disabled={disabled} options={projectOptions} placeholder="请选择检测项目" />
        </Form.Item>
      )}
      {projects.length <= 1 && (
        <Form.Item
          name="selected_project_key"
          hidden
          rules={[{ required: true, message: '未找到可用的检测项目' }]}
        >
          <Input />
        </Form.Item>
      )}

      <div className="execution-microscopy-record__section">
        <div className="execution-microscopy-record__heading">
          <div>
            <strong>样品名称</strong>
            <Text type="secondary">选择分词建议后仍可手工修正</Text>
          </div>
          {rawSampleName && <Tag>任务单原文</Tag>}
        </div>
        {rawSampleName && (
          <Paragraph className="execution-microscopy-record__source-name" copyable>
            {rawSampleName}
          </Paragraph>
        )}
        {nameCandidates.length > 0 ? (
          <div className="execution-microscopy-record__suggestions" aria-label="样品名称分词建议">
            {nameCandidates.map(candidate => (
              <Button
                key={candidate}
                size="small"
                disabled={disabled}
                onClick={() => form.setFieldValue('sample_name', candidate)}
              >
                {candidate}
              </Button>
            ))}
          </div>
        ) : (
          <Text type="secondary">暂无可用的分词建议，请手工填写。</Text>
        )}
        <Form.Item
          name="sample_name"
          label="写入样品名称"
          rules={[{ required: true, whitespace: true, message: '请选择或填写样品名称' }]}
        >
          <Input disabled={disabled} placeholder="可选择上方建议，也可直接修正" />
        </Form.Item>
      </div>

      {identities.length === 1 ? (
        <Descriptions size="small" bordered column={1}>
          <Descriptions.Item label="样品识别">
            {identities[0]}
            <Tag color="green" className="execution-microscopy-record__automatic">已自动选用</Tag>
          </Descriptions.Item>
        </Descriptions>
      ) : identities.length > 1 ? (
        <Form.Item
          name="sample_identity"
          label="样品识别"
          rules={[{ required: true, message: '请选择样品识别' }]}
        >
          <Radio.Group disabled={disabled} options={identities} />
        </Form.Item>
      ) : null}

      {judgementRequired && (
        <div className="execution-microscopy-record__section is-judgement">
          <div className="execution-microscopy-record__heading">
            <div>
              <strong>判定信息</strong>
              <Text type="secondary">任务单已要求判否</Text>
            </div>
          </div>
          {basisOptions.length === 1 ? (
            <Descriptions size="small" bordered column={1}>
              <Descriptions.Item label="判定依据">
                {basisOptions[0]}
                <Tag color="green" className="execution-microscopy-record__automatic">已自动选用</Tag>
              </Descriptions.Item>
            </Descriptions>
          ) : basisOptions.length > 1 ? (
            <Form.Item
              name="judge_basis"
              label="判定依据"
              rules={[{ required: true, message: '请选择判定依据' }]}
            >
              <Select
                disabled={disabled}
                options={basisOptions.map(value => ({ value, label: value }))}
              />
            </Form.Item>
          ) : (
            <Alert showIcon type="warning" message="任务单未提供判定依据" />
          )}
          <Form.Item
            name="judgement"
            label="判定"
            rules={[{ required: true, message: '请选择判定结果' }]}
          >
            <Radio.Group
              disabled={disabled}
              optionType="button"
              buttonStyle="solid"
              options={judgementOptions}
            />
          </Form.Item>
          <Alert
            showIcon
            type="info"
            message="指标要求与测试结果将在后续版本完善"
            description="本轮不要求录入，也不会写入模板。"
          />
        </div>
      )}

      <Alert
        showIcon
        type="info"
        message="备注字段将在后续版本完善"
        description="本轮不要求录入，也不会写入模板。"
      />
    </section>
  );
};

const MicroscopyPrintConfirmation = ({ form, inputData, disabled }) => {
  const input = inputData?.print_context || inputData || {};
  const artifact = input.artifact || {};
  const downloadUrl = artifact.download_url || artifact.preview_url;
  const previewUrl = artifact.preview_url || downloadUrl;
  const sha256 = String(artifact.sha256 || artifact.content_sha256 || '').trim();
  const fileName = artifact.name || artifact.filename || '纤维微观形貌原始记录';

  useEffect(() => {
    if (sha256 && form.getFieldValue('artifact_sha256') !== sha256) {
      form.setFieldValue('artifact_sha256', sha256);
    }
  }, [form, sha256]);

  const openPrintFile = () => {
    if (previewUrl) {
      window.open(previewUrl, '_blank', 'noopener,noreferrer');
    }
  };

  return (
    <section className="execution-microscopy-print">
      <div className="execution-microscopy-print__file">
        <FileExcelOutlined />
        <div>
          <strong>{fileName}</strong>
          <Text type="secondary">工作表：微观形貌 · 使用模板默认打印区域</Text>
        </div>
      </div>
      {sha256 ? (
        <Descriptions size="small" bordered column={1}>
          <Descriptions.Item label="文件 SHA-256">
            <Text code copyable>{sha256}</Text>
          </Descriptions.Item>
        </Descriptions>
      ) : (
        <Alert showIcon type="error" message="文件缺少 SHA-256，暂不能确认打印" />
      )}
      <Space wrap>
        {downloadUrl && (
          <Button
            icon={<DownloadOutlined />}
            href={downloadUrl}
            target="_blank"
            rel="noreferrer"
            disabled={disabled}
          >
            下载工作簿
          </Button>
        )}
        <Button
          type="primary"
          icon={<PrinterOutlined />}
          disabled={disabled || !previewUrl || !sha256}
          onClick={openPrintFile}
        >
          打开并打印
        </Button>
      </Space>
      <Paragraph type="secondary" className="execution-microscopy-print__hint">
        请在 Excel 中打开工作表“微观形貌”，按模板默认打印区域打印。
      </Paragraph>
      <Form.Item name="artifact_sha256" hidden>
        <Input />
      </Form.Item>
      <Form.Item
        name="printed"
        valuePropName="checked"
        rules={[{
          validator: (_, value) => (
            value === true && Boolean(sha256)
              ? Promise.resolve()
              : Promise.reject(new Error('请完成打印并确认文件校验和'))
          ),
        }]}
      >
        <Checkbox disabled={disabled || !sha256}>
          已完成打印，并确认文件校验和
          {sha256 ? ` ${sha256.slice(0, 12)}…` : ''}
          未变化
        </Checkbox>
      </Form.Item>
    </section>
  );
};

export default function MicroscopyRecordHumanTask({
  taskKind,
  form,
  inputData,
  disabled = false,
}) {
  if (taskKind === 'microscopy_record_input') {
    return <MicroscopyRecordInput form={form} inputData={inputData} disabled={disabled} />;
  }
  if (taskKind === 'microscopy_print_confirmation') {
    return (
      <MicroscopyPrintConfirmation form={form} inputData={inputData} disabled={disabled} />
    );
  }
  return null;
}
