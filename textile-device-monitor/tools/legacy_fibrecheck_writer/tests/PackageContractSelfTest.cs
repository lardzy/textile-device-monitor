using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;

// Exercise the compiled package validator without login, copying, or a DB.
internal static class PackageContractSelfTest
{
    private static void Main(string[] args)
    {
        AppDomain.CurrentDomain.AssemblyResolve += (sender, value) =>
        {
            string path = Path.Combine(args[1], new AssemblyName(value.Name).Name + ".dll");
            return File.Exists(path) ? Assembly.LoadFrom(path) : null;
        };
        MethodInfo validate = Assembly.LoadFrom(args[0])
            .GetType("LegacyFibreCheckWriter.SpecialWoolExecutor", true)
            .GetMethod("ValidatePackageContract", BindingFlags.NonPublic | BindingFlags.Static);
        var business = new Dictionary<string, object>
        {
            { "fiber_category", "图片" }, { "inspection_method", "" },
            { "inspection_item", "图片" }, { "inspection_copies", 1 },
            { "review_item", "" }, { "review_copies", 1 },
        };
        var summary = new Dictionary<string, object>
        {
            { "schema_version", 1 }, { "profile", "original_record_upload_v1" },
            { "target_sample_number", "260221991" }, { "target_filename", "260221991-record.xls" },
            { "files", new List<object> { new Dictionary<string, object> { { "filename", "record.xls" } } } },
            { "business_fields", business },
            { "execution_capability", new Dictionary<string, object> { { "available", true } } },
            { "machine_contract", new Dictionary<string, object>
                {
                    { "schema_version", 1 }, { "read_only_probe_required", true },
                    { "observation_type", "legacy_special_wool_qualitative_upload_dry_run" },
                    { "receipt_type", "legacy_special_wool_qualitative_upload" },
                }
            },
        };
        var operation = new Dictionary<string, object> { { "id", "offline" }, { "payload_checksum", "offline" } };
        Func<string> check = () => (string)validate.Invoke(null, new object[]
            { operation, summary, "legacy_special_wool_qualitative_upload" });
        Assert(check(), "original_record_file_type_missing");
        foreach (string missing in new[] { null, "", "  " })
        {
            business["file_type"] = missing;
            Assert(check(), "original_record_file_type_missing");
        }
        business["file_type"] = "定量试验";
        Assert(check(), null);
        summary["profile"] = "special_wool_qualitative_upload_v1";
        business["fiber_category"] = "棉再生纤";
        business["inspection_method"] = "定量";
        business["inspection_item"] = business["review_item"] = "棉再生纤定性";
        business["file_type"] = null;
        Assert(check(), null);
        Console.WriteLine("Writer package contract self-test: 6 passed; no remote access");
    }

    private static void Assert(string actual, string expected)
    {
        if (actual != expected) throw new InvalidOperationException("Expected " + expected + ", got " + actual);
    }
}
