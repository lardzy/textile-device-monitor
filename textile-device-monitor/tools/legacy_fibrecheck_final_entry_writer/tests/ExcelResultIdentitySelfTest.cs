using System;
using System.Collections.Generic;

namespace LegacyFibreCheckFinalEntryWriter
{
    internal static class ExcelResultIdentitySelfTest
    {
        private static int passed;
        private static void Check(bool value, string name)
        {
            if (!value) throw new InvalidOperationException(name);
            passed++;
        }
        private static void Main()
        {
            string missing = ExcelResultIdentity.Key("record", "类别", null);
            Check(missing == ExcelResultIdentity.Key("record", "类别", DBNull.Value),
                "collector nullable sequence matches Oracle NULL");
            Check(missing != ExcelResultIdentity.Key("record", "类别", 0),
                "an omitted sequence is not zero");
            Check(ExcelResultIdentity.Key("record", "类别", 3)
                == ExcelResultIdentity.Key("record", "类别", 3m),
                "collector integer matches Oracle number");
            Check(missing != ExcelResultIdentity.Key("record", "另一组", null),
                "two configuration groups may both omit a sequence");
            Check(missing != ExcelResultIdentity.Key("other", "类别", null),
                "separate workbooks retain separate keys");
            var first = new Dictionary<string, string> {
                {missing, "正面"}, {ExcelResultIdentity.Key("record", "另一组", 7), "反面"}};
            var proof = new Dictionary<string, string> {
                {ExcelResultIdentity.Key("record", "另一组", 7m), "反面"}, {missing, "正面"}};
            Check(ExcelResultIdentity.Same(first, proof), "readback ignores query order");
            proof[missing] = "反面";
            Check(!ExcelResultIdentity.Same(first, proof), "changed identity is rejected");
            proof.Remove(missing);
            Check(!ExcelResultIdentity.Same(first, proof), "missing result is rejected");
            proof[ExcelResultIdentity.Key("record", "类别", 1)] = "正面";
            Check(!ExcelResultIdentity.Same(first, proof), "invented one-based sequence is rejected");
            Console.WriteLine("Excel result identity self-test: " + passed + " passed");
        }
    }
}
