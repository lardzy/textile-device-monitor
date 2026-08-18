import { useEffect, useMemo } from 'react';
import {
  Alert,
  AutoComplete,
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
  const checkCount = Number(selectedProject?.check_count);
  const identityCountMismatch = identities.length > 0
    && Number.isInteger(checkCount)
    && checkCount >= 1
    && identities.length !== checkCount;

  useEffect(() => {
    if (projects.length === 1 && form.getFieldValue('selected_project_key') !== projects[0]._key) {
      form.setFieldValue('selected_project_key', projects[0]._key);
    }
  }, [form, projects]);

  useEffect(() => {
    const currentSampleName = form.getFieldValue('sample_name');
    const currentIdentity = form.getFieldValue('sample_identity');
    const currentBasis = form.getFieldValue('judge_basis');
    const currentJudgement = form.getFieldValue('judgement');
    const currentIndicator = form.getFieldValue('indicator_requirement');
    const currentResult = form.getFieldValue('test_result');
    const currentRemark = form.getFieldValue('remark');
    form.setFieldsValue({
      sample_name: currentSampleName || rawSampleName || undefined,
      sample_identity: identities.length === 1
        ? identities[0]
        : identities.includes(currentIdentity) ? currentIdentity : undefined,
      judge_basis: judgementRequired
        ? currentBasis || (basisOptions.length === 1 ? basisOptions[0] : undefined)
        : undefined,
      indicator_requirement: judgementRequired
        ? currentIndicator || selectedProject?.indicator_requirement || undefined
        : undefined,
      test_result: judgementRequired
        ? currentResult || selectedProject?.test_result || undefined
        : undefined,
      judgement: judgementRequired && judgementOptions.includes(currentJudgement)
        ? currentJudgement
        : undefined,
      remark: currentRemark ?? selectedProject?.remark ?? undefined,
    });
  }, [
    basisOptions,
    form,
    identities,
    judgementOptions,
    judgementRequired,
    rawSampleName,
    selectedProjectKey,
  ]);

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
        <div className="execution-microscopy-record__section">
          <Descriptions size="small" bordered column={1}>
            <Descriptions.Item label="样品识别">
              {identities[0]}
              <Tag color="green" className="execution-microscopy-record__automatic">已自动填入</Tag>
            </Descriptions.Item>
          </Descriptions>
          <Form.Item name="sample_identity" hidden>
            <Input />
          </Form.Item>
        </div>
      ) : identities.length > 1 ? (
        <Form.Item
          name="sample_identity"
          label="样品识别"
          extra="可从任务单候选中选择，也可输入后核对；提交值必须与旧系统下拉框严格一致。"
          rules={[{ required: true, whitespace: true, message: '请选择或填写样品识别' }]}
        >
          <AutoComplete
            disabled={disabled}
            options={identities.map(value => ({ value }))}
            filterOption={(value, option) => String(option?.value || '').includes(value)}
            placeholder="请选择或填写当前录入的样品识别"
          />
        </Form.Item>
      ) : null}

      {identityCountMismatch && (
        <Alert
          showIcon
          type="warning"
          message="任务单份数与样品识别数量不一致"
          description={`检测份数为 ${checkCount}，样品识别拆分后为 ${identities.length} 项；请核对后继续，本提示不会终止流程。`}
        />
      )}

      {judgementRequired && (
        <div className="execution-microscopy-record__section is-judgement">
          <div className="execution-microscopy-record__heading">
            <div>
              <strong>判定信息</strong>
              <Text type="secondary">任务单已要求判定</Text>
            </div>
          </div>
          {basisOptions.length > 0 ? (
            <Form.Item
              name="judge_basis"
              label="判定依据"
              extra={basisOptions.length === 1
                ? '任务单唯一判定依据已自动填入，可按需修改。'
                : '可选择任务单候选，也可直接修改填写。'}
              rules={[{ required: true, whitespace: true, message: '请填写判定依据' }]}
            >
              <AutoComplete
                disabled={disabled}
                options={basisOptions.map(value => ({ value, label: value }))}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="judge_basis"
              label="判定依据"
              extra="任务单未提供可选值，请核对任务单后填写。"
              rules={[{ required: true, whitespace: true, message: '请填写判定依据' }]}
            >
              <Input disabled={disabled} />
            </Form.Item>
          )}
          <Form.Item
            name="indicator_requirement"
            label="指标要求"
            rules={[{ required: true, whitespace: true, message: '请填写指标要求' }]}
          >
            <Input.TextArea disabled={disabled} rows={2} />
          </Form.Item>
          <Form.Item
            name="test_result"
            label="测试结果"
            rules={[{ required: true, whitespace: true, message: '请填写测试结果' }]}
          >
            <Input.TextArea disabled={disabled} rows={2} />
          </Form.Item>
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
        </div>
      )}

      <Form.Item name="remark" label="备注">
        <Input.TextArea disabled={disabled} rows={2} placeholder="无备注可留空" />
      </Form.Item>
    </section>
  );
};

const MicroscopyPrintConfirmation = ({
  form,
  inputData,
  disabled,
  legacyPrintContract,
}) => {
  const input = inputData?.print_context || inputData || {};
  const artifact = input.artifact || {};
  const downloadUrl = artifact.download_url || artifact.preview_url;
  const previewUrl = artifact.preview_url || downloadUrl;
  const sha256 = String(artifact.sha256 || artifact.content_sha256 || '').trim();
  const fileName = artifact.name || artifact.filename || '纤维微观形貌原始记录';
  const printDecision = Form.useWatch('print_decision', form);
  const effectivePrintDecision = legacyPrintContract ? 'print' : printDecision;

  useEffect(() => {
    if (sha256 && form.getFieldValue('artifact_sha256') !== sha256) {
      form.setFieldValue('artifact_sha256', sha256);
    }
  }, [form, sha256]);

  useEffect(() => {
    if (
      effectivePrintDecision !== 'print'
      && form.getFieldValue('print_completed') !== undefined
    ) {
      form.setFieldValue('print_completed', undefined);
    }
  }, [effectivePrintDecision, form]);

  const openWorkbook = () => {
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
          <Text type="secondary">工作表：微观形貌 · 默认打印机 · 模板默认打印区域</Text>
        </div>
      </div>
      {sha256 ? (
        <Descriptions size="small" bordered column={1}>
          <Descriptions.Item label="文件 SHA-256">
            <Text code copyable>{sha256}</Text>
          </Descriptions.Item>
        </Descriptions>
      ) : (
        <Alert showIcon type="error" message="文件缺少 SHA-256，暂不能继续" />
      )}
      {legacyPrintContract ? (
        <Alert
          showIcon
          type="warning"
          message="此任务由旧版本流程创建"
          description="旧任务仅支持完成打印后确认；新创建的任务可以选择暂不打印。"
        />
      ) : (
        <Form.Item
          name="print_decision"
          label="是否打印原始记录"
          rules={[{ required: true, message: '请选择打印原始记录或暂不打印' }]}
        >
          <Radio.Group
            disabled={disabled || !sha256}
            className="execution-microscopy-print__decisions"
          >
            <Radio value="print">
              <span>
                <strong>打印</strong>
                <Text type="secondary">
                  下载或打开工作簿后，在 Excel 中使用默认打印机打印“微观形貌”工作表的默认打印区域
                </Text>
              </span>
            </Radio>
            <Radio value="skip">
              <span>
                <strong>暂不打印</strong>
                <Text type="secondary">保留生成的工作簿并继续后续流程</Text>
              </span>
            </Radio>
          </Radio.Group>
        </Form.Item>
      )}
      {effectivePrintDecision === 'print' && (
        <div className="execution-microscopy-print__manual-actions">
          <Alert
            showIcon
            type="info"
            message="打印需要在 Excel 中手动完成"
            description="网页不会自动打印。请打开工作簿，切换到“微观形貌”工作表，使用默认打印机打印模板中已经设置好的默认打印区域。"
          />
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
              onClick={openWorkbook}
            >
              打开工作簿（手动打印）
            </Button>
          </Space>
          <Paragraph type="secondary" className="execution-microscopy-print__hint">
            打开或下载工作簿不会被系统自动记为已打印；请在实际打印完成后手动确认。
          </Paragraph>
          <Form.Item
            name="print_completed"
            valuePropName="checked"
            rules={[{
              validator: (_, value) => (
                value === true && Boolean(sha256)
                  ? Promise.resolve()
                  : Promise.reject(new Error('请确认已在 Excel 中完成打印'))
              ),
            }]}
          >
            <Checkbox disabled={disabled || !sha256}>
              我确认已在 Excel 中完成打印
            </Checkbox>
          </Form.Item>
        </div>
      )}
      {effectivePrintDecision === 'skip' && (
        <Alert
          showIcon
          type="success"
          message="本次暂不打印"
          description="提交后将直接继续后续流程；生成的工作簿仍可下载。"
        />
      )}
      <Form.Item
        name="artifact_sha256"
        hidden
        rules={[
          { required: true, message: '生成文件缺少校验和' },
          { pattern: /^[0-9a-f]{64}$/i, message: '生成文件校验和格式无效' },
        ]}
      >
        <Input />
      </Form.Item>
    </section>
  );
};

export default function MicroscopyRecordHumanTask({
  taskKind,
  form,
  inputData,
  legacyPrintContract = false,
  disabled = false,
}) {
  if (taskKind === 'microscopy_record_input') {
    return <MicroscopyRecordInput form={form} inputData={inputData} disabled={disabled} />;
  }
  if (taskKind === 'microscopy_print_confirmation') {
    return (
      <MicroscopyPrintConfirmation
        form={form}
        inputData={inputData}
        disabled={disabled}
        legacyPrintContract={legacyPrintContract}
      />
    );
  }
  return null;
}
