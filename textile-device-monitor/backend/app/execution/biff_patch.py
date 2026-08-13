"""Surgical BIFF8 workbook patcher for legacy CheckRecord templates.

The legacy collector reads raw cell values from the registration workbook, and
the desktop client keeps every template formula with a corrected cached result.
Re-saving the template through a general-purpose office suite normalizes fonts
and drops formulas, so this module instead rewrites only the affected BIFF
records inside the OLE ``Workbook`` stream and leaves every other byte of the
file untouched:

- literal cells (sample number, hidden feed cells, optional text inputs) are
  replaced in place, preserving each cell's XF index;
- formula cells keep their expression bytes and only receive an updated cached
  result (numeric or string), exactly like a desktop recalculation;
- cells stored inside a MULBLANK range make the range split around the written
  cell, which inherits exactly the XF the range assigned to that column;
- truly empty cells (no record at the address) get a LABELSST record inserted,
  with the XF inherited exactly the way Excel renders an empty cell: the row's
  default format first, then the column's COLINFO format, finally the built-in
  default cell XF;
- new strings are appended to the SST, which is grown in place.

Only the ``Workbook`` stream bytes change.  Summary streams, fonts, formats,
names, print settings and every untouched record remain byte-identical.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


# --- BIFF8 record ids -------------------------------------------------------
BOF_ID = 0x0809
SST_ID = 0x00FC
CONTINUE_ID = 0x003C
LABELSST_ID = 0x00FD
BLANK_ID = 0x0201
NUMBER_ID = 0x0203
RK_ID = 0x027E
MULRK_ID = 0x00BD
MULBLANK_ID = 0x00BE
FORMULA_ID = 0x0006
STRING_ID = 0x0207
LABEL_ID = 0x0204
BOOLERR_ID = 0x0205

BOUNDSHEET_ID = 0x0085
INDEX_ID = 0x020B
DBCELL_ID = 0x00D7
ROW_ID = 0x0208
COLINFO_ID = 0x007D

# BIFF8 built-in default cell XF, used only when neither the row nor the
# column declares a default format.
DEFAULT_CELL_XF = 15

CELL_RECORD_IDS = (
    LABELSST_ID,
    BLANK_ID,
    NUMBER_ID,
    RK_ID,
    FORMULA_ID,
    LABEL_ID,
    BOOLERR_ID,
    MULRK_ID,
    MULBLANK_ID,
)

# --- OLE2 compound document constants ---------------------------------------
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SECTOR_SIZE = 512
END_OF_CHAIN = 0xFFFFFFFE
FREE_SECTOR = 0xFFFFFFFF
FAT_SECTOR_TYPE = 0xFFFFFFFD
DIFAT_SECTOR_TYPE = 0xFFFFFFFC


class BiffPatchError(RuntimeError):
    pass


def _sector_offset(sector_id: int) -> int:
    return SECTOR_SIZE + sector_id * SECTOR_SIZE


@dataclass
class OleStream:
    name: str
    stream_type: int
    data: bytes
    entry_index: int = -1
    chain: list[int] | None = None
    mini: bool = False
    original_data: bytes = b""


@dataclass
class OleFile:
    streams: list[OleStream]
    fat_sector: int
    dir_sector: int
    source: bytes = b""
    fat_sector_ids: list[int] | None = None
    dir_chain: list[int] | None = None


def _read_chain(data: bytes, fat: tuple[int, ...], start: int) -> list[int]:
    chain = []
    sector = start
    while sector not in (END_OF_CHAIN, FREE_SECTOR):
        if sector >= 0xFFFFFFF0 or sector >= len(fat):
            raise BiffPatchError("ole_chain_broken")
        chain.append(sector)
        sector = fat[sector]
    return chain


def ole_read(path) -> OleFile:
    data = open(path, "rb").read()
    if len(data) < SECTOR_SIZE or data[:8] != OLE_MAGIC:
        raise BiffPatchError("ole_header_invalid")
    sector_shift = struct.unpack_from("<H", data, 0x1E)[0]
    if sector_shift != 9:
        raise BiffPatchError("ole_sector_size_unsupported")
    dir_start = struct.unpack_from("<I", data, 0x30)[0]
    mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
    mini_fat_start, mini_fat_count = struct.unpack_from("<II", data, 0x3C)
    difat = list(struct.unpack_from("<109I", data, 0x4C))
    fat_sector_ids = [sid for sid in difat if sid != FREE_SECTOR]
    if not fat_sector_ids:
        raise BiffPatchError("ole_fat_missing")
    fat: list[int] = []
    for sid in fat_sector_ids:
        fat.extend(struct.unpack_from("<128I", data, _sector_offset(sid)))

    dir_chain = _read_chain(data, tuple(fat), dir_start)
    directory = b"".join(
        data[_sector_offset(s) : _sector_offset(s) + SECTOR_SIZE] for s in dir_chain
    )
    streams: list[OleStream] = []
    root_ministream: bytes | None = None
    for index in range(0, len(directory), 128):
        entry = directory[index : index + 128]
        name_len = struct.unpack_from("<H", entry, 64)[0]
        if name_len == 0:
            continue
        name = entry[: name_len - 2].decode("utf-16le", "replace")
        stream_type = entry[66]
        start = struct.unpack_from("<I", entry, 116)[0]
        size = struct.unpack_from("<Q", entry, 120)[0]
        if stream_type == 5:
            if start != END_OF_CHAIN and size:
                root_chain = _read_chain(data, tuple(fat), start)
                root_ministream = b"".join(
                    data[_sector_offset(s) : _sector_offset(s) + SECTOR_SIZE]
                    for s in root_chain
                )[:size]
            continue
        if stream_type != 2:
            continue
        if size >= mini_cutoff or mini_fat_count == 0:
            chain = _read_chain(data, tuple(fat), start)
            payload = b"".join(
                data[_sector_offset(s) : _sector_offset(s) + SECTOR_SIZE]
                for s in chain
            )[:size]
            streams.append(
                OleStream(
                    name=name,
                    stream_type=stream_type,
                    data=payload,
                    entry_index=index // 128,
                    chain=chain,
                    mini=False,
                    original_data=payload,
                )
            )
        else:
            if root_ministream is None:
                raise BiffPatchError("ole_ministream_missing")
            mini_fat_chain = _read_chain(data, tuple(fat), mini_fat_start)
            mini_fat: list[int] = []
            for sector in mini_fat_chain:
                mini_fat.extend(
                    struct.unpack_from("<128I", data, _sector_offset(sector))
                )
            mini_chain = _read_chain(root_ministream, tuple(mini_fat), start)
            payload = b"".join(
                root_ministream[s * 64 : s * 64 + 64] for s in mini_chain
            )[:size]
            streams.append(
                OleStream(
                    name=name,
                    stream_type=stream_type,
                    data=payload,
                    entry_index=index // 128,
                    chain=None,
                    mini=True,
                    original_data=payload,
                )
            )
    return OleFile(
        streams=streams,
        fat_sector=fat_sector_ids[0],
        dir_sector=dir_start,
        source=data,
        fat_sector_ids=fat_sector_ids,
        dir_chain=dir_chain,
    )


def ole_write(ole: OleFile, path) -> None:
    """Rewrite the compound file preserving the original layout byte-for-byte.

    Only the sectors of streams whose payload actually changed are rewritten,
    and each changed stream keeps its original sector chain (fresh sectors
    are appended at the end of the file only when the stream outgrows its
    original chain).  Header, directory tree links, FAT ordering and every
    untouched stream stay byte-identical to the source.  The legacy NPOI
    build rejects from-scratch container trees (it throws
    IndexOutOfRangeException while walking the Workbook stream), so the
    original Excel-written container structure must survive the patch.
    """
    if not ole.source or not ole.fat_sector_ids or not ole.dir_chain:
        raise BiffPatchError("ole_layout_missing")
    image = bytearray(ole.source)
    fat: list[int] = []
    for sid in ole.fat_sector_ids:
        fat.extend(struct.unpack_from("<128I", ole.source, _sector_offset(sid)))
    fat_dirty = False
    dir_patches: list[tuple[int, int, int | None]] = []  # entry, size, start

    for stream in ole.streams:
        if stream.data == stream.original_data:
            continue
        if stream.mini or stream.chain is None or stream.entry_index < 0:
            raise BiffPatchError("ole_mini_streams_not_supported_for_write")
        required = -(-len(stream.data) // SECTOR_SIZE)
        chain = list(stream.chain)
        start_override: int | None = None
        if required > len(chain):
            extra = required - len(chain)
            next_sector = (len(image) - SECTOR_SIZE) // SECTOR_SIZE
            if next_sector + extra > len(fat):
                raise BiffPatchError("ole_fat_capacity_exceeded")
            image.extend(b"\x00" * (extra * SECTOR_SIZE))
            new_sectors = list(range(next_sector, next_sector + extra))
            if chain:
                fat[chain[-1]] = new_sectors[0]
            else:
                start_override = new_sectors[0]
            for previous, following in zip(new_sectors, new_sectors[1:]):
                fat[previous] = following
            fat[new_sectors[-1]] = END_OF_CHAIN
            chain.extend(new_sectors)
            fat_dirty = True
        padded = stream.data
        remainder = len(padded) % SECTOR_SIZE
        if remainder:
            padded += b"\x00" * (SECTOR_SIZE - remainder)
        for index, sector in enumerate(chain[:required]):
            offset = _sector_offset(sector)
            image[offset : offset + SECTOR_SIZE] = padded[
                index * SECTOR_SIZE : (index + 1) * SECTOR_SIZE
            ]
        dir_patches.append((stream.entry_index, len(stream.data), start_override))

    for entry_index, size, start_override in dir_patches:
        byte_offset = entry_index * 128
        sector = ole.dir_chain[byte_offset // SECTOR_SIZE]
        base = _sector_offset(sector) + byte_offset % SECTOR_SIZE
        if start_override is not None:
            struct.pack_into("<I", image, base + 116, start_override)
        struct.pack_into("<Q", image, base + 120, size)

    if fat_dirty:
        for index, sid in enumerate(ole.fat_sector_ids):
            offset = _sector_offset(sid)
            image[offset : offset + SECTOR_SIZE] = struct.pack(
                "<128I", *fat[index * 128 : (index + 1) * 128]
            )

    with open(path, "wb") as handle:
        handle.write(bytes(image))


# --- BIFF record layer ------------------------------------------------------


@dataclass
class BiffRecord:
    offset: int
    record_id: int
    payload: bytes

    @property
    def end(self) -> int:
        return self.offset + 4 + len(self.payload)


def iter_records(data: bytes) -> list[BiffRecord]:
    """Parse BIFF records, ignoring zero padding after the last record."""
    records, tail = split_records(data)
    if tail.strip(b"\x00"):
        raise BiffPatchError("biff_trailing_bytes")
    return records


def split_records(data: bytes) -> tuple[list[BiffRecord], bytes]:
    """Parse BIFF records, tolerating zero padding after the last one."""
    records = []
    pos = 0
    while pos + 4 <= len(data):
        record_id, length = struct.unpack_from("<HH", data, pos)
        payload = data[pos + 4 : pos + 4 + length]
        if len(payload) != length:
            raise BiffPatchError("biff_record_truncated")
        records.append(BiffRecord(pos, record_id, payload))
        pos += 4 + length
    return records, data[pos:]


def serialize_records(records: list[BiffRecord]) -> bytes:
    parts = []
    for record in records:
        parts.append(struct.pack("<HH", record.record_id, len(record.payload)))
        parts.append(record.payload)
    return b"".join(parts)


def _cell_address(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 6:
        return None
    row, column = struct.unpack_from("<HH", payload, 0)
    return row, column


def _cell_xf(payload: bytes) -> int:
    return struct.unpack_from("<H", payload, 4)[0]


def _encode_xl_unicode_string(text: str) -> bytes:
    # flags=0x01: 16-bit characters, no rich text, no phonetic block.
    encoded = text.encode("utf-16le")
    return struct.pack("<HB", len(text), 0x01) + encoded


def _decode_xl_unicode_string(data: bytes, offset: int) -> tuple[str, int]:
    """XLUnicodeRichExtendedString: cch, flags, [cRun], [cbExtRst], chars,
    [formatting runs], [phonetic data].  Returns (text, next_offset)."""
    length, flags = struct.unpack_from("<HB", data, offset)
    pos = offset + 3
    run_count = 0
    phonetic_size = 0
    if flags & 0x08:
        run_count = struct.unpack_from("<H", data, pos)[0]
        pos += 2
    if flags & 0x04:
        phonetic_size = struct.unpack_from("<I", data, pos)[0]
        pos += 4
    byte_length = length * (2 if flags & 0x01 else 1)
    text = data[pos : pos + byte_length].decode(
        "utf-16le" if flags & 0x01 else "latin1"
    )
    end = pos + byte_length + run_count * 4 + phonetic_size
    return text, end


def _raw_xl_unicode_string(data: bytes, offset: int) -> int:
    """Byte length of one XLUnicodeRichExtendedString starting at offset."""
    _, end = _decode_xl_unicode_string(data, offset)
    return end


@dataclass
class SstTable:
    record_index: int
    total_count: int
    strings: list[str]
    raws: list[bytes]


def parse_sst(records: list[BiffRecord]) -> SstTable:
    for index, record in enumerate(records):
        if record.record_id != SST_ID:
            continue
        blob = bytearray(record.payload)
        cursor = index + 1
        while cursor < len(records) and records[cursor].record_id == CONTINUE_ID:
            blob += records[cursor].payload
            cursor += 1
        total, unique = struct.unpack_from("<II", blob, 0)
        strings: list[str] = []
        raws: list[bytes] = []
        offset = 8
        for _ in range(unique):
            end = _raw_xl_unicode_string(bytes(blob), offset)
            text, _ = _decode_xl_unicode_string(bytes(blob), offset)
            strings.append(text)
            raws.append(bytes(blob[offset:end]))
            offset = end
        return SstTable(
            record_index=index, total_count=total, strings=strings, raws=raws
        )
    raise BiffPatchError("biff_sst_missing")


def _set_formula_cached_string(records: list[BiffRecord], record: BiffRecord, text: str) -> list[BiffRecord]:
    if len(record.payload) < 22:
        raise BiffPatchError("biff_formula_payload_invalid")
    index = records.index(record)
    result = b"\x00" * 6 + b"\xff\xff"
    payload = record.payload[:6] + result + record.payload[14:]
    records[index] = BiffRecord(record.offset, FORMULA_ID, payload)
    # The cached string lives in a STRING record immediately after the formula.
    follower = records[index + 1]
    string_payload = _encode_xl_unicode_string(text)
    if follower.record_id == STRING_ID:
        records[index + 1] = BiffRecord(follower.offset, STRING_ID, string_payload)
    else:
        records.insert(index + 1, BiffRecord(record.offset, STRING_ID, string_payload))
    return records


def _make_labelsst(row: int, column: int, xf: int, sst_index: int) -> bytes:
    return struct.pack("<HHHI", row, column, xf, sst_index)


def _make_number(row: int, column: int, xf: int, value: float) -> bytes:
    return struct.pack("<HHHd", row, column, xf, value)


def _make_blank(row: int, column: int, xf: int) -> bytes:
    return struct.pack("<HHH", row, column, xf)


def _mul_bounds(record: BiffRecord) -> tuple[int, int, int]:
    """(row, col_first, col_last) of a MULRK/MULBLANK record; colLast is the
    two-byte trailer, not the last payload byte."""
    row, col_first = struct.unpack_from("<HH", record.payload, 0)
    col_last = struct.unpack_from("<H", record.payload, len(record.payload) - 2)[0]
    return row, col_first, col_last


def _mul_entry_xf(record: BiffRecord, column: int) -> int:
    """XF a MULBLANK range assigns to one covered column."""
    _, col_first, _ = _mul_bounds(record)
    return struct.unpack_from("<H", record.payload, 4 + 2 * (column - col_first))[0]


def _mul_segment(record: BiffRecord, seg_first: int, seg_last: int) -> BiffRecord:
    """Re-emit a column sub-range of a MULBLANK, preserving each column's XF.
    A single-column remainder becomes a plain BLANK record."""
    row, _, _ = _mul_bounds(record)
    xfs = [
        _mul_entry_xf(record, column)
        for column in range(seg_first, seg_last + 1)
    ]
    if len(xfs) == 1:
        return BiffRecord(0, BLANK_ID, _make_blank(row, seg_first, xfs[0]))
    payload = struct.pack("<HH", row, seg_first)
    payload += b"".join(struct.pack("<H", xf) for xf in xfs)
    payload += struct.pack("<H", seg_last)
    return BiffRecord(0, MULBLANK_ID, payload)


@dataclass
class CellEdit:
    row: int
    column: int
    kind: str  # "text" | "number" | "blank" | "cached_string" | "cached_number"
    value: object


def patch_workbook_stream(data: bytes, edits: list[CellEdit]) -> bytes:
    """Apply cell edits to the BIFF Workbook stream, preserving all other bytes."""
    records, tail = split_records(data)
    sst = parse_sst(records)
    strings = list(sst.strings)
    appended: list[str] = []

    def intern(text: str) -> tuple[int, int]:
        # Returns (sst_index, appended_count_delta).
        if text in strings:
            return strings.index(text), 0
        strings.append(text)
        appended.append(text)
        return len(strings) - 1, 1

    # Index existing cell records by address; remember ROW boundaries for inserts.
    cell_records: dict[tuple[int, int], int] = {}
    for index, record in enumerate(records):
        if record.record_id in (MULRK_ID, MULBLANK_ID):
            continue
        if record.record_id not in CELL_RECORD_IDS:
            continue
        address = _cell_address(record.payload)
        if address is not None:
            cell_records[address] = index

    formula_string_followers: set[int] = set()
    for index, record in enumerate(records[:-1]):
        if record.record_id == FORMULA_ID and records[index + 1].record_id == STRING_ID:
            formula_string_followers.add(index)

    # XF fallback for cells that have no record of their own, resolved exactly
    # the way Excel renders an empty cell: the row's default format wins when
    # the ROW record declares one (flags bit 0x0080), then the column's
    # COLINFO format, and finally the built-in default cell XF.  A hardcoded
    # index would import whatever fill/font that XF happens to carry in the
    # template (e.g. Z7 picking up a grey fill from an unrelated XF).
    row_default_xfs: dict[int, int] = {}
    column_default_xfs: list[tuple[int, int, int]] = []
    for record in records:
        if record.record_id == ROW_ID and len(record.payload) >= 16:
            row_index = struct.unpack_from("<H", record.payload, 0)[0]
            row_flags = struct.unpack_from("<H", record.payload, 12)[0]
            if row_flags & 0x0080:
                row_default_xfs[row_index] = struct.unpack_from(
                    "<H", record.payload, 14
                )[0]
        elif record.record_id == COLINFO_ID and len(record.payload) >= 8:
            col_first, col_last = struct.unpack_from("<HH", record.payload, 0)
            column_default_xfs.append(
                (
                    col_first,
                    col_last,
                    struct.unpack_from("<H", record.payload, 6)[0],
                )
            )

    def empty_cell_xf(row: int, column: int) -> int:
        if row in row_default_xfs:
            return row_default_xfs[row]
        for col_first, col_last, col_xf in column_default_xfs:
            if col_first <= column <= col_last:
                return col_xf
        return DEFAULT_CELL_XF

    new_string_refs = 0
    # Descending address order: inserts only shift records after the edit
    # point, which have already been processed, so every cell_records index
    # stays valid for the remaining edits.
    for edit in sorted(edits, key=lambda item: (item.row, item.column), reverse=True):
        address = (edit.row, edit.column)
        record_index = cell_records.get(address)
        record = records[record_index] if record_index is not None else None
        mul_index: int | None = None
        if record is None:
            for index, candidate in enumerate(records):
                if candidate is None or candidate.record_id not in (
                    MULRK_ID,
                    MULBLANK_ID,
                ):
                    continue
                mul_row, mul_first, mul_last = _mul_bounds(candidate)
                if mul_row == edit.row and mul_first <= edit.column <= mul_last:
                    # Splitting a numeric MULRK range is not supported; the
                    # registration templates only store blank runs there.
                    if candidate.record_id == MULRK_ID:
                        raise BiffPatchError("biff_target_inside_mul_record")
                    mul_index = index
                    break
        if record is not None:
            xf = _cell_xf(record.payload)
        elif mul_index is not None:
            # The template holds this cell inside a MULBLANK range: the new
            # record inherits exactly the XF the range assigns to this column,
            # and the range is re-emitted around the written cell.  Inserting
            # a duplicate record with a foreign XF would let Excel override
            # the template fill/border (e.g. Z7 losing its background).
            xf = _mul_entry_xf(records[mul_index], edit.column)
        else:
            xf = empty_cell_xf(edit.row, edit.column)

        def place(new_record: BiffRecord) -> None:
            if record is not None:
                # Drop a string-cached formula's STRING follower when replacing.
                if (
                    record.record_id == FORMULA_ID
                    and record_index in formula_string_followers
                ):
                    records[record_index + 1] = None
                records[record_index] = new_record
                return
            if mul_index is not None:
                mul_record = records[mul_index]
                _, mul_first, mul_last = _mul_bounds(mul_record)
                segments: list[BiffRecord] = []
                if mul_first < edit.column:
                    segments.append(
                        _mul_segment(mul_record, mul_first, edit.column - 1)
                    )
                segments.append(new_record)
                if edit.column < mul_last:
                    segments.append(
                        _mul_segment(mul_record, edit.column + 1, mul_last)
                    )
                records[mul_index : mul_index + 1] = segments
                return
            # Insert at the end of the target row's cell run: immediately
            # before the first cell record with a higher address, which is
            # where the row's run ends in stream order.  Non-cell records
            # (ROW blocks and the like) are not valid anchors.
            insert_at = None
            for index, candidate in enumerate(records):
                if candidate is None:
                    continue
                if candidate.record_id in (MULRK_ID, MULBLANK_ID):
                    anchor_row, anchor_first, _ = _mul_bounds(candidate)
                    if (anchor_row, anchor_first) > address:
                        insert_at = index
                        break
                    continue
                if candidate.record_id not in CELL_RECORD_IDS:
                    continue
                addr = _cell_address(candidate.payload)
                if addr is not None and addr > address:
                    insert_at = index
                    break
            if insert_at is None:
                # No later cell exists: append after the last cell record,
                # ahead of DBCELL or the sheet EOF.
                for index, candidate in enumerate(records):
                    if candidate.record_id == DBCELL_ID:
                        insert_at = index
                        break
                if insert_at is None:
                    insert_at = len(records)
            records.insert(insert_at, new_record)
            cell_records[address] = insert_at

        if edit.kind == "cached_string":
            if record is not None and record.record_id == FORMULA_ID:
                _set_formula_cached_string(records, record, str(edit.value))
                continue
            # Some template revisions ship literals instead of formulas in the
            # mirror cells: write the value as a plain cell then.
            text = str(edit.value)
            sst_index, _ = intern(text)
            new_string_refs += 1
            payload = _make_labelsst(edit.row, edit.column, xf, sst_index)
            place(BiffRecord(record.offset if record else 0, LABELSST_ID, payload))
            continue
        if edit.kind == "cached_number":
            if record is None or record.record_id != FORMULA_ID:
                raise BiffPatchError("biff_cached_number_requires_formula")
            payload = record.payload[:6] + struct.pack("<d", float(edit.value)) + record.payload[14:]
            records[record_index] = BiffRecord(record.offset, FORMULA_ID, payload)
            continue
        if edit.kind == "number":
            payload = _make_number(edit.row, edit.column, xf, float(edit.value))
            new_record = BiffRecord(record.offset if record else 0, NUMBER_ID, payload)
        elif edit.kind == "blank":
            payload = _make_blank(edit.row, edit.column, xf)
            new_record = BiffRecord(record.offset if record else 0, BLANK_ID, payload)
        elif edit.kind == "text":
            text = str(edit.value)
            sst_index, _ = intern(text)
            new_string_refs += 1
            payload = _make_labelsst(edit.row, edit.column, xf, sst_index)
            new_record = BiffRecord(record.offset if record else 0, LABELSST_ID, payload)
        else:
            raise BiffPatchError("biff_edit_kind_unknown")
        place(new_record)

    records = [record for record in records if record is not None]

    # Rebuild the SST record: originals keep their exact bytes (rich-text
    # runs and phonetic data included), new strings are appended plainly.
    total = sst.total_count + new_string_refs
    unique = len(strings)
    blob = struct.pack("<II", total, unique)
    for raw in sst.raws:
        blob += raw
    for text in appended:
        blob += _encode_xl_unicode_string(text)
    records[sst.record_index] = BiffRecord(
        records[sst.record_index].offset, SST_ID, blob
    )
    # Drop any now-dangling CONTINUE records of the old SST only if they were
    # SST continuations; keep every other record untouched.
    result = []
    for index, record in enumerate(records):
        if record is None:
            continue
        result.append(record)
    return serialize_records(_relocate_index_records(result)) + tail


def _relocate_index_records(records: list[BiffRecord]) -> list[BiffRecord]:
    """Repair stream-position pointers embedded in BIFF records.

    BOUNDSHEET carries the absolute stream position of the worksheet BOF,
    INDEX carries absolute DBCELL positions, and DBCELL carries the relative
    offsets of each row's cell run.  Insertions shift every later record, so
    all three are recomputed against the patched layout.
    """
    # Assign patched stream offsets.
    offset = 0
    new_positions: list[int] = []
    for record in records:
        new_positions.append(offset)
        offset += 4 + len(record.payload)

    # old record offset -> new record offset (matched by list position).
    offset_map: dict[int, int] = {}
    for record, new_offset in zip(records, new_positions):
        offset_map[record.offset] = new_offset

    def remap(position: int) -> int:
        if position in offset_map:
            return offset_map[position]
        # Fall back to the delta of the closest record after the position.
        later = [off for off in offset_map if off >= position]
        if later:
            nearest = min(later)
            return position + (offset_map[nearest] - nearest)
        return position

    # Locate worksheet BOF, ROW records and each row's first cell record.
    worksheet_bof_new: int | None = None
    row_offsets: dict[int, int] = {}
    first_cell_of_row: dict[int, int] = {}
    dbcell_indices: list[int] = []
    for index, (record, new_offset) in enumerate(zip(records, new_positions)):
        if record.record_id == BOF_ID and worksheet_bof_new is None:
            if len(record.payload) >= 4 and struct.unpack_from("<H", record.payload, 2)[0] == 0x0010:
                worksheet_bof_new = new_offset
        elif record.record_id == ROW_ID:
            row = struct.unpack_from("<H", record.payload, 0)[0]
            row_offsets[row] = new_offset
        elif record.record_id in (MULRK_ID, MULBLANK_ID):
            row = struct.unpack_from("<H", record.payload, 0)[0]
            first_cell_of_row.setdefault(row, new_offset)
        elif record.record_id in CELL_RECORD_IDS and record.record_id not in (
            MULRK_ID, MULBLANK_ID
        ):
            address = _cell_address(record.payload)
            if address is not None:
                first_cell_of_row.setdefault(address[0], new_offset)
        if record.record_id == DBCELL_ID:
            dbcell_indices.append(index)

    sorted_rows = sorted(row_offsets)
    result = list(records)
    for index, record in enumerate(result):
        if record.record_id == BOUNDSHEET_ID and worksheet_bof_new is not None:
            payload = struct.pack("<I", worksheet_bof_new) + record.payload[4:]
            result[index] = BiffRecord(record.offset, record.record_id, payload)
        elif record.record_id == INDEX_ID and len(record.payload) >= 12:
            count = (len(record.payload) - 12) // 4
            payload = bytearray(record.payload)
            for entry in range(count):
                old = struct.unpack_from("<I", record.payload, 12 + entry * 4)[0]
                struct.pack_into("<I", payload, 12 + entry * 4, remap(old))
            result[index] = BiffRecord(record.offset, record.record_id, bytes(payload))
        elif record.record_id == DBCELL_ID and sorted_rows:
            dbcell_new = new_positions[index]
            first_row_new = row_offsets[sorted_rows[0]]
            dbrtrw = dbcell_new - first_row_new
            entries = bytearray(record.payload[4:])
            # rgib[i] is the byte span of the previous row's cell run; the
            # first entry covers the block header region through to row 0's
            # first cell record.
            previous_anchor = (
                row_offsets[sorted_rows[1]]
                if len(sorted_rows) > 1
                else first_row_new
            )
            out_index = 0
            for position, row in enumerate(sorted_rows):
                first_cell = first_cell_of_row.get(row)
                if first_cell is None or out_index * 2 + 2 > len(entries):
                    continue
                span = first_cell - previous_anchor
                struct.pack_into("<H", entries, out_index * 2, span & 0xFFFF)
                previous_anchor = first_cell
                out_index += 1
            payload = struct.pack("<I", dbrtrw) + bytes(entries)
            result[index] = BiffRecord(record.offset, record.record_id, payload)
    return result


def patch_workbook_file(source_path, target_path, edits: list[CellEdit]) -> None:
    ole = ole_read(source_path)
    replaced = False
    for stream in ole.streams:
        if stream.name == "Workbook":
            stream.data = patch_workbook_stream(stream.data, edits)
            replaced = True
    if not replaced:
        raise BiffPatchError("workbook_stream_missing")
    ole_write(ole, target_path)


# --- verification helpers ---------------------------------------------------


def workbook_record_map(data: bytes) -> dict[tuple[int, int], tuple[int, bytes]]:
    """Map (record offset, record id) -> full record bytes for byte-diff checks."""
    return {
        (record.offset, record.record_id): (record.record_id, record.payload)
        for record in iter_records(data)
    }


def formula_cells(data: bytes) -> dict[tuple[int, int], tuple[bytes, str | None]]:
    """Return {address: (rpn_bytes, cached_string_or_none)} for every formula."""
    records = iter_records(data)
    result: dict[tuple[int, int], tuple[bytes, str | None]] = {}
    for index, record in enumerate(records):
        if record.record_id != FORMULA_ID:
            continue
        address = _cell_address(record.payload)
        if address is None:
            continue
        cce = struct.unpack_from("<H", record.payload, 20)[0]
        rpn = record.payload[22 : 22 + cce]
        cached = None
        if index + 1 < len(records) and records[index + 1].record_id == STRING_ID:
            payload = records[index + 1].payload
            cached, _ = _decode_xl_unicode_string(payload, 0)
        result[address] = (rpn, cached)
    return result


POINTER_RECORD_IDS = (BOUNDSHEET_ID, INDEX_ID, DBCELL_ID, SST_ID)
# Records a patch may add: the written cell itself plus the MULBLANK/BLANK
# segments re-emitted around a cell that was covered by a MULBLANK range.
# Numeric record kinds stay forbidden, so no undeclared value can appear.
INSERTABLE_RECORD_IDS = (LABELSST_ID, STRING_ID, MULBLANK_ID, BLANK_ID)


def verify_patch_scope(
    before: bytes, after: bytes, *, max_changed_records: int
) -> dict[str, Any]:
    """Prove the patch only touched its declared neighbourhood.

    Aligns the two record sequences.  An ``after`` record pairs with the next
    ``before`` record when they share a record id; a payload difference then
    counts as an in-place update (cell/formula rewrite or pointer relocation).
    Records of the insertable kinds (LABELSST/STRING plus the MULBLANK/BLANK
    segments re-emitted around a cell that was covered by a MULBLANK range)
    may appear between pairings; numeric kinds stay forbidden as inserts, so
    no undeclared value can appear.  Remaining id mismatches and removals are
    violations.
    """
    before_records = iter_records(before)
    after_records = iter_records(after)
    changed: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    before_index = 0
    after_index = 0
    while after_index < len(after_records):
        record = after_records[after_index]
        if before_index >= len(before_records):
            if record.record_id not in INSERTABLE_RECORD_IDS:
                violations.append({"after_index": after_index, "kind": "unexpected_append"})
            else:
                changed.append({"after_index": after_index, "kind": "inserted"})
            after_index += 1
            continue
        previous = before_records[before_index]
        if record.record_id == previous.record_id:
            if record.payload != previous.payload:
                kind = (
                    "pointer_relocated"
                    if previous.record_id in POINTER_RECORD_IDS
                    else "cell_or_formula_updated"
                )
                changed.append(
                    {
                        "before_index": before_index,
                        "after_index": after_index,
                        "before_id": previous.record_id,
                        "after_id": record.record_id,
                        "kind": kind,
                    }
                )
            before_index += 1
            after_index += 1
            continue
        # A record kind the patch may add without consuming its before peer.
        if record.record_id in INSERTABLE_RECORD_IDS:
            changed.append({"after_index": after_index, "kind": "inserted"})
            after_index += 1
            continue
        entry = {
            "before_index": before_index,
            "after_index": after_index,
            "before_id": previous.record_id,
            "after_id": record.record_id,
        }
        if previous.record_id not in CELL_RECORD_IDS or record.record_id not in CELL_RECORD_IDS:
            violations.append(entry)
        else:
            changed.append({**entry, "kind": "cell_or_formula_updated"})
        before_index += 1
        after_index += 1
    while before_index < len(before_records):
        violations.append({"before_index": before_index, "kind": "unexpected_removal"})
        before_index += 1
    return {
        "before_records": len(before_records),
        "after_records": len(after_records),
        "changed_records": len(changed),
        "violation_count": len(violations),
        "within_bound": len(changed) <= max_changed_records and not violations,
        "changed": changed,
        "violations": violations,
    }
