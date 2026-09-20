using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;
using LegacyFibreCheckRunner;
using LegacyFibreCheckFinalEntryWriter;

namespace LegacyFibreCheckFinalEntryWriter
{
    // Observation is also compiled independently of the official write DAL.
    internal sealed class PackageValidationException : Exception
    {
        public PackageValidationException(string code) : base(code) { }
    }
}

internal static class CheckRecordObservationSelfTest
{
    private static void Assert(bool value)
    {
        if (!value) throw new Exception("observation_assertion_failed");
    }

    public static int Main(string[] args)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        var json = new JavaScriptSerializer();
        Assert(CheckRecordObservation.Equal(json.DeserializeObject("{\"a\":1,\"b\":null}"),
                                             json.DeserializeObject("{\"b\":null,\"a\":1}")));
        Assert(!CheckRecordObservation.Equal(json.DeserializeObject("{\"a\":1}"), json.DeserializeObject("{\"a\":2}")));
        Assert(!CheckRecordObservation.Equal(null, ""));
        Assert(!CheckRecordObservation.Equal(json.DeserializeObject("[1,2]"), json.DeserializeObject("[1]")));
        Assert((string)CheckRecordObservation.PublicValue("ProofUser", "operator") == Redact.HashId("operator"));
        Assert((string)CheckRecordObservation.PublicValue("CreateTime", new DateTime(2026, 9, 20, 8, 0, 0)) == "2026-09-20T08:00:00");
        Assert(CheckRecordObservation.Equal(CheckRecordObservation.PublicValue("AttachInfo", "C:\\test\\a.xls"),
            json.DeserializeObject("{\"count\":1,\"items\":[{\"basename\":\"a.xls\",\"path_hash\":\""
                + Redact.HashId("C:\\test\\a.xls") + "\"}]}")));
        Assert(CheckRecordObservation.Equal(json.DeserializeObject("{\"content_fingerprint\":\"old\",\"v\":1}"),
                                             json.DeserializeObject("{\"v\":1}")));
        Console.WriteLine("Record observation self-test: 8 passed");
        if (args.Length == 2)
        {
            SystemDataConnection.InstallAssemblyResolver(args[0]);
            var before = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[1]));
            ReadOnline(args[0], before);
        }
        return 0;
    }

    private static void ReadOnline(string directory, Dictionary<string, object> before)
    {
        var register = (Dictionary<string, object>)before["register"];
        var measured = CheckRecordObservation.Read(SystemDataConnection.Read(directory, "DbConnString"),
            (string)register["SampleNo"], (string)before["record_ref"]);
        Assert(CheckRecordObservation.Equal(measured.Record, before));
        Console.WriteLine("Read-only record comparison: matched all observed fields");
    }
}
