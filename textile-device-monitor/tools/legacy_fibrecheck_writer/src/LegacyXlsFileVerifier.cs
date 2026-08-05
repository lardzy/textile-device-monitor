using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace LegacyFibreCheckWriter
{
    internal sealed class LegacyXlsFileVerification
    {
        internal bool Verified;
        internal string Error;
        internal string Mode;
        internal long SourceSize;
        internal string SourceSha256;
        internal long RemoteSize;
        internal string RemoteSha256;
        internal bool StreamPathsEqual;
        internal bool StreamSizesEqual;
        internal bool NonWorkbookStreamsEqual;
        internal bool BiffRecordBoundariesEqual;
        internal readonly List<string> ChangedRecordIds = new List<string>();
        internal int ChangedRecordCount;

        internal SortedDictionary<string, object> ToDocument()
        {
            return new SortedDictionary<string, object>
            {
                { "mode", Mode },
                { "source_size_bytes", SourceSize },
                { "source_content_sha256", SourceSha256 },
                { "remote_size_bytes", RemoteSize },
                { "remote_content_sha256", RemoteSha256 },
                { "stream_paths_equal", StreamPathsEqual },
                { "stream_sizes_equal", StreamSizesEqual },
                { "non_workbook_streams_equal", NonWorkbookStreamsEqual },
                { "biff_record_boundaries_equal", BiffRecordBoundariesEqual },
                { "changed_record_ids", new List<string>(ChangedRecordIds) },
                { "changed_record_count", ChangedRecordCount },
            };
        }
    }

    internal static class LegacyXlsFileVerifier
    {
        private const ushort WriteAccessRecord = 0x005C;

        internal static LegacyXlsFileVerification Verify(
            byte[] source,
            byte[] remote)
        {
            var result = NewResult(source, remote);
            if (BytesEqual(source, remote))
            {
                result.Verified = true;
                result.Mode = "exact_sha256";
                result.StreamPathsEqual = true;
                result.StreamSizesEqual = true;
                result.NonWorkbookStreamsEqual = true;
                result.BiffRecordBoundariesEqual = true;
                return result;
            }

            try
            {
                var sourceStreams = CfbDocument.ReadStreams(source);
                var remoteStreams = CfbDocument.ReadStreams(remote);
                result.StreamPathsEqual = SameKeys(sourceStreams, remoteStreams);
                if (!result.StreamPathsEqual)
                {
                    return Fail(result, "cfb_stream_paths_mismatch");
                }

                result.StreamSizesEqual = SameStreamSizes(
                    sourceStreams,
                    remoteStreams);
                if (!result.StreamSizesEqual)
                {
                    return Fail(result, "cfb_stream_sizes_mismatch");
                }

                string workbookPath = FindWorkbookPath(sourceStreams);
                if (workbookPath == null)
                {
                    return Fail(result, "cfb_workbook_stream_not_unique");
                }
                result.NonWorkbookStreamsEqual = OtherStreamsEqual(
                    sourceStreams,
                    remoteStreams,
                    workbookPath);
                if (!result.NonWorkbookStreamsEqual)
                {
                    return Fail(result, "cfb_non_workbook_stream_mismatch");
                }

                int changedCount;
                string biffError;
                result.BiffRecordBoundariesEqual =
                    BiffChangesAreWriteAccessOnly(
                        sourceStreams[workbookPath],
                        remoteStreams[workbookPath],
                        out changedCount,
                        out biffError);
                if (!result.BiffRecordBoundariesEqual)
                {
                    return Fail(result, biffError);
                }
                if (changedCount < 1)
                {
                    return Fail(result, "biff_writeaccess_change_missing");
                }
                result.ChangedRecordIds.Add("0x005C");
                result.ChangedRecordCount = changedCount;
                result.Mode = "cfb_biff_writeaccess_only";
                result.Verified = true;
                return result;
            }
            catch (Exception ex)
            {
                return Fail(
                    result,
                    "cfb_verify_failed:" + ex.GetType().Name);
            }
        }

        internal static bool BiffChangesAreWriteAccessOnly(
            byte[] source,
            byte[] remote,
            out int changedCount,
            out string error)
        {
            changedCount = 0;
            error = null;
            int sourceOffset = 0;
            int remoteOffset = 0;
            while (sourceOffset < source.Length || remoteOffset < remote.Length)
            {
                if (sourceOffset + 4 > source.Length
                    || remoteOffset + 4 > remote.Length)
                {
                    error = "biff_header_truncated";
                    return false;
                }
                ushort sourceId = UInt16(source, sourceOffset);
                ushort remoteId = UInt16(remote, remoteOffset);
                int sourceLength = UInt16(source, sourceOffset + 2);
                int remoteLength = UInt16(remote, remoteOffset + 2);
                if (sourceId != remoteId || sourceLength != remoteLength)
                {
                    error = "biff_record_boundary_mismatch";
                    return false;
                }
                int sourceEnd = sourceOffset + 4 + sourceLength;
                int remoteEnd = remoteOffset + 4 + remoteLength;
                if (sourceEnd > source.Length || remoteEnd > remote.Length)
                {
                    error = "biff_record_truncated";
                    return false;
                }
                if (!RangeEqual(
                    source,
                    sourceOffset + 4,
                    remote,
                    remoteOffset + 4,
                    sourceLength))
                {
                    if (sourceId != WriteAccessRecord)
                    {
                        error = "biff_record_content_mismatch";
                        return false;
                    }
                    changedCount++;
                }
                sourceOffset = sourceEnd;
                remoteOffset = remoteEnd;
            }
            if (sourceOffset != source.Length || remoteOffset != remote.Length)
            {
                error = "biff_stream_length_mismatch";
                return false;
            }
            return true;
        }

        private static LegacyXlsFileVerification NewResult(
            byte[] source,
            byte[] remote)
        {
            if (source == null) throw new ArgumentNullException("source");
            if (remote == null) throw new ArgumentNullException("remote");
            return new LegacyXlsFileVerification
            {
                SourceSize = source.LongLength,
                SourceSha256 = Sha256(source),
                RemoteSize = remote.LongLength,
                RemoteSha256 = Sha256(remote),
            };
        }

        private static LegacyXlsFileVerification Fail(
            LegacyXlsFileVerification result,
            string error)
        {
            result.Error = error;
            return result;
        }

        private static string Sha256(byte[] value)
        {
            using (SHA256 hash = SHA256.Create())
            {
                byte[] digest = hash.ComputeHash(value);
                var text = new StringBuilder(digest.Length * 2);
                foreach (byte item in digest)
                {
                    text.Append(item.ToString("x2"));
                }
                return text.ToString();
            }
        }

        private static bool SameKeys(
            Dictionary<string, byte[]> left,
            Dictionary<string, byte[]> right)
        {
            if (left.Count != right.Count) return false;
            foreach (string key in left.Keys)
            {
                if (!right.ContainsKey(key)) return false;
            }
            return true;
        }

        private static bool SameStreamSizes(
            Dictionary<string, byte[]> left,
            Dictionary<string, byte[]> right)
        {
            foreach (string key in left.Keys)
            {
                if (left[key].LongLength != right[key].LongLength) return false;
            }
            return true;
        }

        private static string FindWorkbookPath(
            Dictionary<string, byte[]> streams)
        {
            string result = null;
            foreach (string path in streams.Keys)
            {
                string name = path.Substring(path.LastIndexOf('/') + 1);
                if (!string.Equals(name, "Workbook", StringComparison.Ordinal)
                    && !string.Equals(name, "Book", StringComparison.Ordinal))
                {
                    continue;
                }
                if (result != null) return null;
                result = path;
            }
            return result;
        }

        private static bool OtherStreamsEqual(
            Dictionary<string, byte[]> left,
            Dictionary<string, byte[]> right,
            string workbookPath)
        {
            foreach (string path in left.Keys)
            {
                if (string.Equals(path, workbookPath, StringComparison.Ordinal))
                {
                    continue;
                }
                if (!BytesEqual(left[path], right[path])) return false;
            }
            return true;
        }

        private static bool BytesEqual(byte[] left, byte[] right)
        {
            return left != null
                && right != null
                && left.Length == right.Length
                && RangeEqual(left, 0, right, 0, left.Length);
        }

        private static bool RangeEqual(
            byte[] left,
            int leftOffset,
            byte[] right,
            int rightOffset,
            int length)
        {
            for (int index = 0; index < length; index++)
            {
                if (left[leftOffset + index] != right[rightOffset + index])
                {
                    return false;
                }
            }
            return true;
        }

        private static ushort UInt16(byte[] value, int offset)
        {
            return (ushort)(value[offset] | (value[offset + 1] << 8));
        }

        private sealed class CfbDocument
        {
            private const uint FreeSector = 0xFFFFFFFF;
            private const uint EndOfChain = 0xFFFFFFFE;
            private const uint FatSector = 0xFFFFFFFD;
            private const uint DifatSector = 0xFFFFFFFC;
            private static readonly byte[] Magic =
                { 0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1 };

            private readonly byte[] data;
            private readonly int sectorSize;
            private readonly int miniSectorSize;
            private readonly int sectorCount;
            private readonly uint miniStreamCutoff;
            private readonly List<uint> fat;
            private readonly List<uint> miniFat;
            private readonly List<DirectoryEntry> entries;
            private readonly byte[] rootMiniStream;

            private CfbDocument(byte[] value)
            {
                data = value;
                if (value.Length < 512 || !RangeEqual(value, 0, Magic, 0, Magic.Length))
                {
                    throw new InvalidDataException("not_a_cfb_file");
                }
                if (ReadUInt16(value, 26) != 3
                    || ReadUInt16(value, 28) != 0xFFFE
                    || ReadUInt16(value, 30) != 9
                    || ReadUInt16(value, 32) != 6
                    || value.Length % 512 != 0)
                {
                    throw new InvalidDataException("unsupported_cfb_layout");
                }
                sectorSize = 512;
                miniSectorSize = 64;
                sectorCount = value.Length / sectorSize - 1;
                uint fatCount = ReadUInt32(value, 44);
                uint firstDirectory = ReadUInt32(value, 48);
                miniStreamCutoff = ReadUInt32(value, 56);
                uint firstMiniFat = ReadUInt32(value, 60);
                uint miniFatCount = ReadUInt32(value, 64);
                uint firstDifat = ReadUInt32(value, 68);
                uint difatCount = ReadUInt32(value, 72);
                List<uint> difat = ReadDifat(
                    fatCount,
                    firstDifat,
                    difatCount);
                fat = ReadFat(difat);
                entries = ReadDirectory(firstDirectory);
                if (entries.Count == 0 || entries[0].ObjectType != 5)
                {
                    throw new InvalidDataException("cfb_root_entry_missing");
                }
                miniFat = ReadMiniFat(firstMiniFat, miniFatCount);
                rootMiniStream = ReadRegular(
                    entries[0].StartSector,
                    entries[0].Size);
            }

            internal static Dictionary<string, byte[]> ReadStreams(byte[] value)
            {
                return new CfbDocument(value).CollectStreams();
            }

            private List<uint> ReadDifat(
                uint fatCount,
                uint firstDifat,
                uint difatCount)
            {
                var result = new List<uint>();
                for (int index = 0; index < 109; index++)
                {
                    uint item = ReadUInt32(data, 76 + index * 4);
                    if (item != FreeSector) result.Add(item);
                }
                uint current = firstDifat;
                var seen = new HashSet<uint>();
                for (uint index = 0; index < difatCount; index++)
                {
                    if (IsSpecial(current) || !seen.Add(current))
                    {
                        throw new InvalidDataException("invalid_difat_chain");
                    }
                    byte[] sector = ReadSector(current);
                    for (int item = 0; item < sectorSize / 4 - 1; item++)
                    {
                        uint fatSector = ReadUInt32(sector, item * 4);
                        if (fatSector != FreeSector) result.Add(fatSector);
                    }
                    current = ReadUInt32(sector, sectorSize - 4);
                }
                if ((uint)result.Count < fatCount)
                {
                    throw new InvalidDataException("fat_sector_list_truncated");
                }
                if ((uint)result.Count > fatCount)
                {
                    result.RemoveRange((int)fatCount, result.Count - (int)fatCount);
                }
                return result;
            }

            private List<uint> ReadFat(List<uint> difat)
            {
                var result = new List<uint>(difat.Count * sectorSize / 4);
                foreach (uint sectorId in difat)
                {
                    byte[] sector = ReadSector(sectorId);
                    for (int offset = 0; offset < sector.Length; offset += 4)
                    {
                        result.Add(ReadUInt32(sector, offset));
                    }
                }
                return result;
            }

            private List<uint> ReadMiniFat(uint firstSector, uint count)
            {
                if (count == 0) return new List<uint>();
                List<uint> chain = ReadChain(firstSector, fat, "minifat_storage");
                if ((uint)chain.Count < count)
                {
                    throw new InvalidDataException("minifat_storage_truncated");
                }
                var result = new List<uint>((int)count * sectorSize / 4);
                for (int index = 0; index < (int)count; index++)
                {
                    byte[] sector = ReadSector(chain[index]);
                    for (int offset = 0; offset < sector.Length; offset += 4)
                    {
                        result.Add(ReadUInt32(sector, offset));
                    }
                }
                return result;
            }

            private List<DirectoryEntry> ReadDirectory(uint firstSector)
            {
                List<uint> chain = ReadChain(firstSector, fat, "directory");
                byte[] payload = JoinSectors(chain);
                var result = new List<DirectoryEntry>();
                for (int offset = 0; offset + 128 <= payload.Length; offset += 128)
                {
                    int nameLength = ReadUInt16(payload, offset + 64);
                    string name = string.Empty;
                    if (nameLength >= 2 && nameLength <= 64 && nameLength % 2 == 0)
                    {
                        name = Encoding.Unicode.GetString(
                            payload,
                            offset,
                            nameLength - 2);
                    }
                    result.Add(new DirectoryEntry
                    {
                        Name = name,
                        ObjectType = payload[offset + 66],
                        Left = ReadUInt32(payload, offset + 68),
                        Right = ReadUInt32(payload, offset + 72),
                        Child = ReadUInt32(payload, offset + 76),
                        StartSector = ReadUInt32(payload, offset + 116),
                        Size = ReadUInt32(payload, offset + 120),
                    });
                }
                return result;
            }

            private Dictionary<string, byte[]> CollectStreams()
            {
                var result = new Dictionary<string, byte[]>(StringComparer.Ordinal);
                var visited = new HashSet<uint>();
                WalkStorage(entries[0], string.Empty, result, visited);
                return result;
            }

            private void WalkStorage(
                DirectoryEntry storage,
                string parent,
                Dictionary<string, byte[]> result,
                HashSet<uint> visited)
            {
                var children = new List<uint>();
                CollectSiblings(storage.Child, new HashSet<uint>(), children);
                foreach (uint index in children)
                {
                    if (!visited.Add(index))
                    {
                        throw new InvalidDataException("duplicate_directory_entry");
                    }
                    DirectoryEntry entry = entries[(int)index];
                    if (entry.ObjectType == 0 || string.IsNullOrEmpty(entry.Name))
                    {
                        continue;
                    }
                    string path = parent + "/" + entry.Name;
                    if (entry.ObjectType == 1)
                    {
                        WalkStorage(entry, path, result, visited);
                    }
                    else if (entry.ObjectType == 2)
                    {
                        if (result.ContainsKey(path))
                        {
                            throw new InvalidDataException("duplicate_stream_path");
                        }
                        result[path] = ReadStream(entry);
                    }
                }
            }

            private void CollectSiblings(
                uint root,
                HashSet<uint> stack,
                List<uint> output)
            {
                if (root == FreeSector || root == EndOfChain) return;
                if (root >= (uint)entries.Count || !stack.Add(root))
                {
                    throw new InvalidDataException("invalid_directory_tree");
                }
                DirectoryEntry entry = entries[(int)root];
                CollectSiblings(entry.Left, new HashSet<uint>(stack), output);
                output.Add(root);
                CollectSiblings(entry.Right, new HashSet<uint>(stack), output);
            }

            private byte[] ReadStream(DirectoryEntry entry)
            {
                return entry.Size < miniStreamCutoff
                    ? ReadMini(entry.StartSector, entry.Size)
                    : ReadRegular(entry.StartSector, entry.Size);
            }

            private byte[] ReadRegular(uint start, long size)
            {
                if (size == 0) return new byte[0];
                List<uint> chain = ReadChain(start, fat, "fat");
                return Trim(JoinSectors(chain), size, "regular_stream_truncated");
            }

            private byte[] ReadMini(uint start, long size)
            {
                if (size == 0) return new byte[0];
                if (miniFat.Count == 0)
                {
                    throw new InvalidDataException("minifat_missing");
                }
                List<uint> chain = ReadChain(start, miniFat, "mini");
                using (var stream = new MemoryStream())
                {
                    foreach (uint sector in chain)
                    {
                        long offset = (long)sector * miniSectorSize;
                        if (offset < 0 || offset + miniSectorSize > rootMiniStream.Length)
                        {
                            throw new InvalidDataException("mini_sector_out_of_range");
                        }
                        stream.Write(rootMiniStream, (int)offset, miniSectorSize);
                    }
                    return Trim(stream.ToArray(), size, "mini_stream_truncated");
                }
            }

            private List<uint> ReadChain(
                uint start,
                List<uint> table,
                string label)
            {
                var result = new List<uint>();
                if (start == FreeSector || start == EndOfChain) return result;
                var seen = new HashSet<uint>();
                uint current = start;
                while (current != EndOfChain)
                {
                    if (current == FreeSector
                        || current == FatSector
                        || current == DifatSector
                        || current >= table.Count
                        || !seen.Add(current))
                    {
                        throw new InvalidDataException("invalid_" + label + "_chain");
                    }
                    result.Add(current);
                    current = table[(int)current];
                    if (result.Count > Math.Max(sectorCount + 1, table.Count + 1))
                    {
                        throw new InvalidDataException("runaway_" + label + "_chain");
                    }
                }
                return result;
            }

            private byte[] JoinSectors(List<uint> chain)
            {
                using (var stream = new MemoryStream(chain.Count * sectorSize))
                {
                    foreach (uint sector in chain)
                    {
                        byte[] value = ReadSector(sector);
                        stream.Write(value, 0, value.Length);
                    }
                    return stream.ToArray();
                }
            }

            private byte[] ReadSector(uint sector)
            {
                if (IsSpecial(sector) || sector >= (uint)sectorCount)
                {
                    throw new InvalidDataException("sector_out_of_range");
                }
                int offset = checked(((int)sector + 1) * sectorSize);
                var result = new byte[sectorSize];
                Buffer.BlockCopy(data, offset, result, 0, sectorSize);
                return result;
            }

            private static byte[] Trim(byte[] value, long size, string error)
            {
                if (size < 0 || size > int.MaxValue || value.LongLength < size)
                {
                    throw new InvalidDataException(error);
                }
                if (value.LongLength == size) return value;
                var result = new byte[(int)size];
                Buffer.BlockCopy(value, 0, result, 0, (int)size);
                return result;
            }

            private static bool IsSpecial(uint sector)
            {
                return sector == FreeSector
                    || sector == EndOfChain
                    || sector == FatSector
                    || sector == DifatSector;
            }

            private static ushort ReadUInt16(byte[] value, int offset)
            {
                return (ushort)(value[offset] | (value[offset + 1] << 8));
            }

            private static uint ReadUInt32(byte[] value, int offset)
            {
                return (uint)(
                    value[offset]
                    | (value[offset + 1] << 8)
                    | (value[offset + 2] << 16)
                    | (value[offset + 3] << 24));
            }

            private sealed class DirectoryEntry
            {
                internal string Name;
                internal byte ObjectType;
                internal uint Left;
                internal uint Right;
                internal uint Child;
                internal uint StartSector;
                internal long Size;
            }
        }
    }
}
