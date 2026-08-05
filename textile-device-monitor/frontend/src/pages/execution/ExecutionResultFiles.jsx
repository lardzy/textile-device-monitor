import { useMemo, useState } from 'react';
import {
  Button,
  Checkbox,
  Empty,
  Image,
  Modal,
  Radio,
  Space,
  Tag,
  Typography,
} from 'antd';
import {
  FileImageOutlined,
  FileTextOutlined,
  StarFilled,
  StarOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { executionArtifactPreviewUrl } from '../../api/execution';

const { Text } = Typography;

export const resultFileId = file => (
  file?.file_index_entry_id
  || file?.id
  || file?.artifact?.id
  || file?.artifact_id
);

const resultOf = file => (
  file?.result && typeof file.result === 'object' ? file.result : file || {}
);

export const qualitativeResultOf = (file) => {
  const result = resultOf(file);
  const value = result.qualitative_result
    ?? result.w32_value
    ?? file?.qualitative_result;
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  return normalized || null;
};

export const isPaperQualitativeResultFile = (file) => {
  const result = resultOf(file);
  return qualitativeResultOf(file) !== null
    && String(result.worksheet || file?.worksheet || '') === 'Sheet1'
    && String(result.cell || file?.result_cell || '') === 'W32';
};

const partName = part => (
  part?.name
  ?? part?.part_name
  ?? part?.position
  ?? part?.display_name
  ?? part?.label
  ?? null
);

const componentsOf = part => (
  Array.isArray(part?.components)
    ? part.components
    : Array.isArray(part?.results)
      ? part.results
      : []
);

const componentName = component => (
  component?.name
  ?? component?.component
  ?? component?.fiber_name
  ?? '未命名成分'
);

const componentContent = (component) => {
  const value = component?.content
    ?? component?.percentage
    ?? component?.value;
  if (value === null || value === undefined || value === '') {
    return '—';
  }
  if (typeof value === 'number') {
    return `${value}%`;
  }
  const text = String(value).trim();
  return text.endsWith('%') ? text : `${text}%`;
};

const fileNameOf = file => (
  file?.name
  || file?.filename
  || file?.artifact?.filename
  || String(file?.relative_path || '').split(/[\\/]/).pop()
  || '未命名文件'
);

const filePathOf = file => (
  file?.relative_path
  || file?.artifact?.relative_path
  || ''
);

const inspectorNameOf = (file) => {
  const inspector = resultOf(file)?.inspector;
  const value = inspector && typeof inspector === 'object'
    ? inspector.name
    : inspector;
  return value === null || value === undefined
    ? ''
    : String(value).trim();
};

const imageNameOf = (image, index) => (
  image?.name
  || image?.filename
  || image?.artifact?.filename
  || `插图 ${index + 1}`
);

const imageSourceOf = (image) => {
  if (typeof image === 'string') {
    return /^(?:data:|blob:|https?:\/\/|\/)/.test(image) ? image : null;
  }
  const explicit = image?.preview_url
    || image?.url
    || image?.src
    || image?.artifact?.preview_url;
  if (explicit) {
    return explicit;
  }
  const artifactId = image?.artifact_id || image?.artifact?.id;
  return artifactId ? executionArtifactPreviewUrl(artifactId) : null;
};

const imageItemsOf = (file) => {
  const result = resultOf(file);
  return Array.isArray(result.images)
    ? result.images
    : Array.isArray(file?.images)
      ? file.images
      : [];
};

const remarkTextOf = remark => (
  remark && typeof remark === 'object'
    ? remark.text ?? remark.value ?? remark.content ?? ''
    : remark
);

const warningMessageOf = warning => (
  warning && typeof warning === 'object'
    ? warning.message ?? warning.code ?? '工作簿读取时出现异常'
    : String(warning || '')
);

const warningDetailsOf = warning => (
  warning?.details && typeof warning.details === 'object'
    ? JSON.stringify(warning.details)
    : warning?.details
      ? String(warning.details)
      : ''
);

const isImageWarning = warning => (
  /image|插图|图片/i.test(
    `${warning?.code || ''} ${warningMessageOf(warning)}`,
  )
);

const normalizeResultFiles = value => (
  Array.isArray(value)
    ? value.filter(file => file && typeof file === 'object')
    : []
);

const looksLikeResultFiles = (value) => (
  Array.isArray(value)
  && value.some(file => (
    file
    && typeof file === 'object'
    && (
      file.result
      || Array.isArray(file.parts)
      || Array.isArray(file.remarks)
      || Array.isArray(file.images)
      || file.read_status
    )
  ))
);

export const extractExecutionResultFiles = (value, depth = 0) => {
  if (!value || depth > 6) {
    return [];
  }
  if (looksLikeResultFiles(value)) {
    return normalizeResultFiles(value);
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const nested = extractExecutionResultFiles(item, depth + 1);
      if (nested.length) {
        return nested;
      }
    }
    return [];
  }
  if (typeof value !== 'object') {
    return [];
  }
  for (const key of ['files', 'result_files', 'selected_files', 'results', 'result', 'selection']) {
    if (Object.hasOwn(value, key)) {
      const nested = extractExecutionResultFiles(value[key], depth + 1);
      if (nested.length) {
        return nested;
      }
    }
  }
  return [];
};

export const extractPrimaryFileId = (value, depth = 0) => {
  if (!value || typeof value !== 'object' || depth > 6) {
    return null;
  }
  const direct = value.primary_file_id
    || resultFileId(value.primary_file);
  if (direct) {
    return String(direct);
  }
  for (const key of ['result', 'selection', 'output']) {
    const nested = extractPrimaryFileId(value[key], depth + 1);
    if (nested) {
      return nested;
    }
  }
  return null;
};

function ResultImageModal({ file, open, onClose }) {
  const images = useMemo(() => imageItemsOf(file), [file]);

  return (
    <Modal
      title={`${fileNameOf(file)} · 表格插图（${images.length}）`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={920}
      destroyOnHidden
    >
      {images.length ? (
        <Image.PreviewGroup>
          <div className="execution-result-images">
            {images.map((image, index) => {
              const source = imageSourceOf(image);
              const name = imageNameOf(image, index);
              return (
                <figure key={image?.artifact_id || image?.id || `${name}-${index}`}>
                  {source ? (
                    <Image
                      src={source}
                      alt={name}
                      fallback="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='160'%3E%3Crect width='100%25' height='100%25' fill='%23f3f5f8'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='middle' text-anchor='middle' fill='%23808a9b' font-size='14'%3E%E5%9B%BE%E7%89%87%E6%97%A0%E6%B3%95%E5%8A%A0%E8%BD%BD%3C/text%3E%3C/svg%3E"
                    />
                  ) : (
                    <div className="execution-result-images__unavailable">
                      <FileImageOutlined />
                      <span>暂无在线预览地址</span>
                    </div>
                  )}
                  <figcaption title={name}>{name}</figcaption>
                </figure>
              );
            })}
          </div>
        </Image.PreviewGroup>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此表格没有可查看的插图" />
      )}
    </Modal>
  );
}

export default function ExecutionResultFiles({
  files,
  selectable = false,
  selectedIds = [],
  primaryId = null,
  onSelectedIdsChange,
  onPrimaryIdChange,
  disabled = false,
  selectionMode = 'multiple',
  showPrimary = true,
}) {
  const [imageFile, setImageFile] = useState(null);
  const normalizedFiles = normalizeResultFiles(files);
  const normalizedSelected = selectedIds.map(String);
  const selectedSet = new Set(normalizedSelected);
  const normalizedPrimary = primaryId ? String(primaryId) : null;

  const toggleSelected = (fileId, checked) => {
    const id = String(fileId);
    const next = checked
      ? [...new Set([...normalizedSelected, id])]
      : normalizedSelected.filter(value => value !== id);
    onSelectedIdsChange?.(next);
    if (checked && !normalizedPrimary) {
      onPrimaryIdChange?.(id);
    } else if (!checked && normalizedPrimary === id) {
      onPrimaryIdChange?.(next[0] || null);
    }
  };

  const selectPrimary = (fileId) => {
    const id = String(fileId);
    if (!selectedSet.has(id)) {
      onSelectedIdsChange?.([...new Set([...normalizedSelected, id])]);
    }
    onPrimaryIdChange?.(id);
  };

  const selectOnly = (fileId) => {
    const id = String(fileId);
    onSelectedIdsChange?.([id]);
    onPrimaryIdChange?.(id);
  };

  if (!normalizedFiles.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可展示的文件结果" />;
  }

  return (
    <>
      <div className="execution-result-files">
        {normalizedFiles.map((file, index) => {
          const id = resultFileId(file);
          const stringId = id ? String(id) : `result-file-${index}`;
          const result = resultOf(file);
          const qualitativeResult = qualitativeResultOf(file);
          const isQualitativeResult = isPaperQualitativeResultFile(file);
          const qualitativeUnit = String(result.unit || file?.unit || '');
          const parts = Array.isArray(result.parts) ? result.parts : [];
          const remarks = Array.isArray(result.remarks)
            ? result.remarks.map(remarkTextOf).filter(Boolean)
            : [];
          const images = imageItemsOf(file);
          const inspectorName = inspectorNameOf(file);
          const warnings = Array.isArray(result.warnings)
            ? result.warnings.filter(Boolean)
            : [];
          const hasImageWarning = warnings.some(isImageWarning);
          const isSelected = selectable
            ? selectedSet.has(stringId)
            : file.selected !== false;
          const isPrimary = normalizedPrimary === stringId || file.is_primary === true;
          const readFailed = file.read_status && file.read_status !== 'succeeded';

          return (
            <article
              key={stringId}
              className={[
                'execution-result-file',
                isSelected ? 'is-selected' : '',
                isPrimary ? 'is-primary' : '',
              ].filter(Boolean).join(' ')}
            >
              <header className="execution-result-file__header">
                <div className="execution-result-file__identity">
                  <FileTextOutlined />
                  <div>
                    <strong title={fileNameOf(file)}>{fileNameOf(file)}</strong>
                    {filePathOf(file) && (
                      <small title={filePathOf(file)}>{filePathOf(file)}</small>
                    )}
                  </div>
                </div>
                <Space size={4} wrap>
                  {isPrimary && (
                    <Tag
                      color={isQualitativeResult ? 'blue' : 'gold'}
                      icon={isQualitativeResult ? undefined : <StarFilled />}
                    >
                      {isQualitativeResult ? '已选文件' : '主单'}
                    </Tag>
                  )}
                  {inspectorName && <Tag>检验员：{inspectorName}</Tag>}
                  {file.read_status && (
                    <Tag color={readFailed ? 'error' : 'success'}>
                      {readFailed ? '读取失败' : '读取完成'}
                    </Tag>
                  )}
                </Space>
              </header>

              {readFailed ? (
                <div className="execution-result-file__error">
                  {file.error?.message || file.error_message || '该文件的结果读取失败'}
                </div>
              ) : (
                <>
                  {isQualitativeResult && (
                    <section className="execution-result-file__qualitative">
                      <div>
                        <Text type="secondary">读取单元格</Text>
                        <Text code>
                          {result.worksheet || 'Sheet1'}!{result.cell || 'W32'}
                        </Text>
                      </div>
                      <strong>{qualitativeResult}</strong>
                      {qualitativeUnit && <Tag color="blue">单位：{qualitativeUnit}</Tag>}
                    </section>
                  )}
                  {!isQualitativeResult && (
                    <div className="execution-result-file__parts">
                      {parts.length ? parts.map((part, partIndex) => (
                        <section key={`${partName(part) || 'default'}-${partIndex}`}>
                          <Text type="secondary">
                            {partName(part) || (parts.length === 1
                              ? '检测结果'
                              : `结果 ${partIndex + 1}`)}
                          </Text>
                          <div className="execution-result-file__components">
                            {componentsOf(part).map((component, componentIndex) => (
                              <span key={`${componentName(component)}-${componentIndex}`}>
                                <strong>{componentName(component)}</strong>
                                <b>{componentContent(component)}</b>
                              </span>
                            ))}
                          </div>
                        </section>
                      )) : (
                        <Text type="secondary">未读取到成分结果</Text>
                      )}
                    </div>
                  )}

                  {remarks.length > 0 && (
                    <div className="execution-result-file__remarks">
                      <Text type="secondary">备注</Text>
                      {remarks.map((remark, remarkIndex) => (
                        <p key={`${remark}-${remarkIndex}`}>{String(remark)}</p>
                      ))}
                    </div>
                  )}
                  {warnings.length > 0 && (
                    <div className="execution-result-file__warnings" role="status">
                      <WarningOutlined />
                      <div>
                        <Text strong>读取提示</Text>
                        {warnings.map((warning, warningIndex) => (
                          <div
                            className="execution-result-file__warning-item"
                            key={`${warning?.code || warningMessageOf(warning)}-${warningIndex}`}
                          >
                            <p>
                              {warningMessageOf(warning)}
                              {warning?.code && <Text code>{warning.code}</Text>}
                            </p>
                            {warningDetailsOf(warning) && (
                              <small>{warningDetailsOf(warning)}</small>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}

              <footer className="execution-result-file__footer">
                <Space size={8} wrap>
                  {images.length > 0 && (
                    <Button
                      size="small"
                      icon={<FileImageOutlined />}
                      onClick={() => setImageFile(file)}
                    >
                      查看图片（{images.length}）
                    </Button>
                  )}
                  {!readFailed && !isQualitativeResult && images.length === 0 && (
                    <Text
                      className={[
                        'execution-result-file__image-state',
                        hasImageWarning ? 'is-warning' : '',
                      ].filter(Boolean).join(' ')}
                    >
                      {hasImageWarning ? '图片读取异常' : '未读取到表格插图'}
                    </Text>
                  )}
                  {selectable && id && selectionMode === 'single' && (
                    <Radio
                      aria-label={`选择文件：${fileNameOf(file)}`}
                      checked={selectedSet.has(stringId)}
                      disabled={disabled || readFailed}
                      onChange={() => selectOnly(stringId)}
                    >
                      选择此文件
                    </Radio>
                  )}
                  {selectable && id && selectionMode !== 'single' && (
                    <>
                      <Checkbox
                        checked={selectedSet.has(stringId)}
                        disabled={disabled || readFailed}
                        onChange={event => toggleSelected(stringId, event.target.checked)}
                      >
                        需要
                      </Checkbox>
                      {showPrimary && (
                        <Radio
                          checked={normalizedPrimary === stringId}
                          disabled={disabled || !selectedSet.has(stringId)}
                          onClick={() => selectPrimary(stringId)}
                        >
                          <StarOutlined /> 设为主单
                        </Radio>
                      )}
                    </>
                  )}
                </Space>
              </footer>
            </article>
          );
        })}
      </div>
      <ResultImageModal
        file={imageFile}
        open={Boolean(imageFile)}
        onClose={() => setImageFile(null)}
      />
    </>
  );
}
