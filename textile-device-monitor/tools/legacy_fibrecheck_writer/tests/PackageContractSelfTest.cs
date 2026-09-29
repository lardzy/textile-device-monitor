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
        string readableName = "薄膜正面-微观形貌-原始记录.xls";
        summary["target_filename"] = "260221991-" + readableName;
        var file = (Dictionary<string, object>)((List<object>)summary["files"])[0];
        file["filename"] = readableName;
        Assert(check(), null);
        // Physical names can remain immutable hashes while the upload name is
        // readable. Exercise the installed Writer's actual source reader.
        string sourceRoot = Path.Combine(Path.GetTempPath(), "ReadableXls-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(sourceRoot);
        byte[] bytes = new byte[] { 1, 2, 3 };
        File.WriteAllBytes(Path.Combine(sourceRoot, "immutable-hash.xls"), bytes);
        file["artifact_id"] = "fixture";
        file["relative_path"] = "immutable-hash.xls";
        file["size_bytes"] = bytes.Length;
        using (var sha = System.Security.Cryptography.SHA256.Create())
            file["content_sha256"] = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        var read = validate.DeclaringType.GetMethod("TryReadSource", BindingFlags.NonPublic | BindingFlags.Static);
        object[] readArgs = { summary, sourceRoot, null, null };
        if (!(bool)read.Invoke(null, readArgs)) throw new InvalidOperationException("Source read failed: " + readArgs[3]);
        Assert((string)readArgs[2].GetType().GetField("FileName", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(readArgs[2]), readableName);
        summary["profile"] = "special_wool_qualitative_upload_v1";
        business["fiber_category"] = "棉再生纤";
        business["inspection_method"] = "定量";
        business["inspection_item"] = business["review_item"] = "棉再生纤定性";
        business["file_type"] = null;
        Assert(check(), null);
        Console.WriteLine("Writer package contract self-test: 8 passed; no remote access; fixtures retained");
    }

    private static void Assert(string actual, string expected)
    {
        if (actual != expected) throw new InvalidOperationException("Expected " + expected + ", got " + actual);
    }
}
