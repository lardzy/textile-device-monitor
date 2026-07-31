using System;
using System.Security.Cryptography;
using System.Text;

namespace LegacyFibreCheckRunner
{
    /// <summary>输出脱敏：与只读探针同一套规则（ID 散列、登录名掩码、中文名保留）。</summary>
    internal static class Redact
    {
        public static string HashId(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return null;
            }
            using (SHA256 sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(value));
                var builder = new StringBuilder("sha256:");
                for (int i = 0; i < 8; i++)
                {
                    builder.Append(digest[i].ToString("x2"));
                }
                return builder.ToString();
            }
        }

        public static string MaskLogin(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return "";
            }
            if (value.Length == 1)
            {
                return "*";
            }
            if (value.Length == 2)
            {
                return value[0] + "*";
            }
            int stars = Math.Min(value.Length - 2, 6);
            return value[0] + new string('*', stars) + value[value.Length - 1];
        }

        /// <summary>与旧系统 CommonHelper.EncryptByMD5 完全一致：Encoding.Default 字节、大写 X2。</summary>
        public static string EncryptByMd5(string password)
        {
            using (MD5 md5 = new MD5CryptoServiceProvider())
            {
                byte[] digest = md5.ComputeHash(Encoding.Default.GetBytes(password));
                var builder = new StringBuilder();
                foreach (byte b in digest)
                {
                    builder.Append(b.ToString("X2"));
                }
                return builder.ToString();
            }
        }

        /// <summary>与旧系统 CommonHelper.CheckPasswordIsOK 一致，仅用于报告，不触发任何重置。</summary>
        public static string CheckPasswordIsOk(string password)
        {
            if (string.IsNullOrWhiteSpace(password))
            {
                return "密码不能为空！";
            }
            if (password.Length < 6)
            {
                return "密码长度不能小于6位！";
            }
            bool hasLower = false, hasUpper = false, hasDigit = false;
            foreach (char c in password)
            {
                hasLower |= c >= 'a' && c <= 'z';
                hasUpper |= c >= 'A' && c <= 'Z';
                hasDigit |= c >= '0' && c <= '9';
            }
            if (!hasLower || !hasUpper || !hasDigit)
            {
                return "密码必须包含数字、大小写字母！";
            }
            return string.Empty;
        }
    }
}
