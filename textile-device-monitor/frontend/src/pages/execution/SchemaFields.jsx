import { Checkbox, Form, Input, InputNumber, Select } from 'antd';

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

export default function SchemaFields({
  schema,
  disabled = false,
  namePrefix,
}) {
  const properties = schema?.properties || {};
  const required = schema?.required || [];

  return Object.entries(properties).map(([name, field]) => {
    const fieldName = namePrefix ? [namePrefix, name] : name;
    const common = {
      disabled: disabled || field.readOnly,
      placeholder: field.placeholder,
    };

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

    return (
      <Form.Item
        key={name}
        name={fieldName}
        label={field.title || name}
        tooltip={field.description}
        rules={formItemRules(name, field, required)}
      >
        {control}
      </Form.Item>
    );
  });
}
