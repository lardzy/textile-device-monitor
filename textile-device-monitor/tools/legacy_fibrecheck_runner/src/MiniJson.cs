using System;
using System.Collections;
using System.Globalization;
using System.Text;

namespace LegacyFibreCheckRunner
{
    /// <summary>最小 JSON 写出器（.NET Framework 4.0 无内置 JSON），UTF-8 无 BOM。</summary>
    internal static class MiniJson
    {
        public static string Write(object value)
        {
            var builder = new StringBuilder();
            WriteValue(builder, value, 0);
            return builder.ToString();
        }

        private static void WriteValue(StringBuilder builder, object value, int indent)
        {
            if (value == null)
            {
                builder.Append("null");
                return;
            }
            if (value is string)
            {
                WriteString(builder, (string)value);
                return;
            }
            if (value is bool)
            {
                builder.Append((bool)value ? "true" : "false");
                return;
            }
            if (value is IDictionary)
            {
                WriteObject(builder, (IDictionary)value, indent);
                return;
            }
            if (value is IEnumerable)
            {
                WriteArray(builder, (IEnumerable)value, indent);
                return;
            }
            if (value is DateTime)
            {
                WriteString(builder, ((DateTime)value).ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
                return;
            }
            if (value is double || value is float || value is decimal)
            {
                builder.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
                return;
            }
            if (value is int || value is long || value is short || value is byte)
            {
                builder.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
                return;
            }
            WriteString(builder, value.ToString());
        }

        private static void WriteObject(StringBuilder builder, IDictionary map, int indent)
        {
            builder.Append('{');
            bool first = true;
            foreach (DictionaryEntry entry in map)
            {
                if (!first)
                {
                    builder.Append(',');
                }
                first = false;
                builder.Append('\n').Append(' ', indent + 2);
                WriteString(builder, Convert.ToString(entry.Key, CultureInfo.InvariantCulture));
                builder.Append(": ");
                WriteValue(builder, entry.Value, indent + 2);
            }
            if (!first)
            {
                builder.Append('\n').Append(' ', indent);
            }
            builder.Append('}');
        }

        private static void WriteArray(StringBuilder builder, IEnumerable items, int indent)
        {
            builder.Append('[');
            bool first = true;
            foreach (object item in items)
            {
                if (!first)
                {
                    builder.Append(',');
                }
                first = false;
                builder.Append('\n').Append(' ', indent + 2);
                WriteValue(builder, item, indent + 2);
            }
            if (!first)
            {
                builder.Append('\n').Append(' ', indent);
            }
            builder.Append(']');
        }

        private static void WriteString(StringBuilder builder, string value)
        {
            builder.Append('"');
            foreach (char c in value)
            {
                switch (c)
                {
                    case '"': builder.Append("\\\""); break;
                    case '\\': builder.Append("\\\\"); break;
                    case '\b': builder.Append("\\b"); break;
                    case '\f': builder.Append("\\f"); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            builder.Append("\\u").Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            builder.Append(c);
                        }
                        break;
                }
            }
            builder.Append('"');
        }
    }
}
