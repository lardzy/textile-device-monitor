import {
  Alert,
  AutoComplete,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Select,
  Typography,
} from 'antd';

const formItemRules = (name, schema, required = []) => {
  const rules = [];
  if (required.includes(name)) {
    rules.push({ required: true, message: `请填写${schema.title || name}` });
  }
  if (schema.pattern) {
    rules.push({
      pattern: new RegExp(schema.pattern),
      message: schema.validation_message || `${schema.title || name}格式不正确`,
    });
  }
  if (Object.prototype.hasOwnProperty.call(schema, 'const')) {
    rules.push({
      validator: async (_, value) => {
        if (value !== schema.const) {
          throw new Error(
            schema.validation_message
            || `${schema.title || name}必须为${String(schema.const)}`,
          );
        }
      },
    });
  }
  return rules;
};

// x-copy-sources：字段参考数据源（如任务单说明列、Sheet1!M32）。
// 文本可直接拖选复制，也可点复制图标或“填入”一键写入字段。
const CopySourceList = ({ sources, fieldName, disabled }) => {
  const form = Form.useFormInstance();
  return (
    <div style={{ marginBottom: 4 }}>
      {sources.map(source => (
        <div
          key={source.label}
          style={{
            display: 'flex', alignItems: 'baseline', gap: 4, flexWrap: 'wrap', lineHeight: '22px',
          }}
        >
          <Typography.Text type="secondary" style={{ flexShrink: 0 }}>
            {source.label}：
          </Typography.Text>
          <Typography.Text copyable={{ text: source.text, tooltips: ['复制', '已复制'] }}>
            {source.text}
          </Typography.Text>
          <Button
            size="small"
            type="link"
            style={{ padding: 0, height: 'auto' }}
            disabled={disabled}
            onClick={() => form?.setFieldValue(fieldName, source.text)}
          >
            填入
          </Button>
        </div>
      ))}
    </div>
  );
};

export default function SchemaFields({
  schema,
  disabled = false,
  namePrefix,
}) {
  const properties = schema?.properties || {};
  const required = schema?.required || [];

  const fields = Object.entries(properties).map(([name, field]) => {
    const fieldName = namePrefix ? [namePrefix, name] : name;
    const common = {
      disabled: disabled || field.readOnly,
      placeholder: field.placeholder,
    };
    const initialValue = field.default !== undefined ? field.default : undefined;
    const copySources = (
      Array.isArray(field['x-copy-sources']) ? field['x-copy-sources'] : []
    ).filter(source => source && source.text);

    let control;
    if (Array.isArray(field.enum)) {
      control = (
        <Select
          {...common}
          mode={field.type === 'array' ? 'multiple' : undefined}
          options={field.enum.map((value, index) => ({
            value,
            label: field.enumNames?.[index] || String(value),
          }))}
        />
      );
    } else if (field.type === 'boolean') {
      return (
        <Form.Item
          key={name}
          name={fieldName}
          valuePropName="checked"
          rules={formItemRules(name, field, required)}
        >
          <Checkbox disabled={common.disabled}>{field.title || name}</Checkbox>
        </Form.Item>
      );
    } else if (Array.isArray(field['x-suggestions'])) {
      control = (
        <AutoComplete
          {...common}
          options={field['x-suggestions'].map(value => ({ value }))}
          filterOption={(inputValue, option) => String(option?.value || '')
            .toLowerCase()
            .includes(String(inputValue || '').toLowerCase())}
        />
      );
    } else if (field.type === 'number' || field.type === 'integer') {
      control = (
        <InputNumber
          {...common}
          min={field.minimum}
          max={field.maximum}
          step={field.multipleOf}
          style={{ width: '100%' }}
        />
      );
    } else if (field.format === 'textarea' || field.multiline) {
      control = <Input.TextArea {...common} rows={3} />;
    } else {
      control = <Input {...common} />;
    }

    if (copySources.length > 0) {
      return (
        <Form.Item
          key={name}
          label={field.title || name}
          tooltip={field.description}
          required={required.includes(name)}
        >
          <CopySourceList
            sources={copySources}
            fieldName={fieldName}
            disabled={common.disabled}
          />
          <Form.Item
            name={fieldName}
            noStyle
            initialValue={initialValue}
            rules={formItemRules(name, field, required)}
          >
            {control}
          </Form.Item>
        </Form.Item>
      );
    }

    return (
      <Form.Item
        key={name}
        name={fieldName}
        label={field.title || name}
        tooltip={field.description}
        initialValue={initialValue}
        rules={formItemRules(name, field, required)}
      >
        {control}
      </Form.Item>
    );
  });

  return (
    <>
      {schema?.['x-warning'] && (
        <Alert
          showIcon
          type="warning"
          message="任务单份数与样品识别数量不一致"
          description={schema['x-warning']}
          style={{ marginBottom: 12 }}
        />
      )}
      {fields}
    </>
  );
}
