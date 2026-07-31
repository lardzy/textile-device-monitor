using System;
using System.IO;
using System.Reflection;

namespace LegacyFibreCheckRunner
{
    /// <summary>
    /// 从旧程序自己的 SystemData 类读取连接串（含其硬编码回退值）。
    /// 凭据只存在于旧程序 DLL 内，不复制到本项目的任何文件。
    /// </summary>
    internal static class SystemDataConnection
    {
        private const string EntitiesAssemblyName = "Toone.FibreCheck.Entites.CommonEntities.dll";
        private const string SystemDataTypeName = "Toone.FibreCheck.Entites.CommonEntities.SystemData";

        private static string _probeDir;

        public static void InstallAssemblyResolver(string fibreCheckDir)
        {
            _probeDir = fibreCheckDir;
            AppDomain.CurrentDomain.AssemblyResolve += ProbeAssembly;
        }

        public static string Read(string fibreCheckDir, string propertyName)
        {
            string assemblyPath = Path.Combine(fibreCheckDir, EntitiesAssemblyName);
            if (!File.Exists(assemblyPath))
            {
                throw new FileNotFoundException("找不到旧程序实体程序集 " + EntitiesAssemblyName);
            }
            Assembly assembly = Assembly.LoadFrom(assemblyPath);
            Type type = assembly.GetType(SystemDataTypeName, true);
            object instance = type.GetProperty("Instance", BindingFlags.Public | BindingFlags.Static).GetValue(null, null);
            PropertyInfo property = type.GetProperty(propertyName, BindingFlags.Public | BindingFlags.Instance);
            if (property == null)
            {
                throw new InvalidOperationException("SystemData 缺少属性 " + propertyName);
            }
            return property.GetValue(instance, null) as string;
        }

        private static Assembly ProbeAssembly(object sender, ResolveEventArgs args)
        {
            if (string.IsNullOrEmpty(_probeDir))
            {
                return null;
            }
            string candidate = Path.Combine(_probeDir, new AssemblyName(args.Name).Name + ".dll");
            return File.Exists(candidate) ? Assembly.LoadFrom(candidate) : null;
        }
    }
}
