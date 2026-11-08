"""Minimal lossless FIT reader/writer.

fit_tool only understands profile fields it knows about and silently drops the
rest (a Garmin watch file loses thousands of proprietary fields on a round trip).
This codec works on raw bytes instead: every definition and data message is kept
byte-for-byte, and callers can overwrite or append individual fields. Only the
definitions that gain fields are re-emitted.

FIT layout reference: https://developer.garmin.com/fit/protocols/
"""

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Base types: (code, size, struct format char, invalid value)
BASE_TYPES = {
    0x00: (1, 'B', 0xFF),           # enum
    0x01: (1, 'b', 0x7F),           # sint8
    0x02: (1, 'B', 0xFF),           # uint8
    0x83: (2, 'h', 0x7FFF),         # sint16
    0x84: (2, 'H', 0xFFFF),         # uint16
    0x85: (4, 'i', 0x7FFFFFFF),     # sint32
    0x86: (4, 'I', 0xFFFFFFFF),     # uint32
    0x07: (1, 's', 0x00),           # string
    0x88: (4, 'f', None),           # float32
    0x89: (8, 'd', None),           # float64
    0x0A: (1, 'B', 0x00),           # uint8z
    0x8B: (2, 'H', 0x0000),         # uint16z
    0x8C: (4, 'I', 0x00000000),     # uint32z
    0x0D: (1, 'B', 0xFF),           # byte
    0x8E: (8, 'q', 0x7FFFFFFFFFFFFFFF),   # sint64
    0x8F: (8, 'Q', 0xFFFFFFFFFFFFFFFF),   # uint64
    0x90: (8, 'Q', 0x0000000000000000),   # uint64z
}

SINT32, UINT32, UINT16, UINT8, ENUM = 0x85, 0x86, 0x84, 0x02, 0x00

TIMESTAMP_FIELD = 253
FIT_EPOCH_OFFSET = 631065600  # seconds between 1970-01-01 and 1989-12-31

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


@dataclass
class FieldDef:
    num: int
    size: int
    base_type: int


@dataclass
class Definition:
    local_type: int
    global_num: int
    big_endian: bool
    fields: List[FieldDef]
    dev_fields: List[Tuple[int, int, int]] = field(default_factory=list)  # (num, size, dev_index)

    @property
    def endian(self) -> str:
        return '>' if self.big_endian else '<'

    @property
    def data_size(self) -> int:
        return sum(f.size for f in self.fields) + sum(d[1] for d in self.dev_fields)

    def encode(self) -> bytes:
        header = 0x40 | (0x20 if self.dev_fields else 0) | self.local_type
        out = bytearray([header, 0, 1 if self.big_endian else 0])
        out += struct.pack(self.endian + 'HB', self.global_num, len(self.fields))
        for f in self.fields:
            out += bytes([f.num, f.size, f.base_type])
        if self.dev_fields:
            out.append(len(self.dev_fields))
            for num, size, idx in self.dev_fields:
                out += bytes([num, size, idx])
        return bytes(out)


class DataMessage:
    """A data message with raw field bytes; values are decoded on demand."""

    def __init__(self, definition: Definition, header: int, payload: bytes, timestamp: Optional[int]):
        self.definition = definition
        self.header = header          # original record header (kept for compressed timestamps)
        self.timestamp = timestamp    # FIT timestamp (explicit or derived from compressed header)
        self._raw: Dict[int, bytes] = {}
        offset = 0
        for f in definition.fields:
            self._raw[f.num] = payload[offset:offset + f.size]
            offset += f.size
        self.dev_payload = payload[offset:]
        self._extra: List[FieldDef] = []  # fields appended to the original definition

    @property
    def global_num(self) -> int:
        return self.definition.global_num

    def _field_def(self, num: int) -> Optional[FieldDef]:
        for f in self.definition.fields + self._extra:
            if f.num == num:
                return f
        return None

    def get_string(self, num: int) -> Optional[str]:
        """Decode a string field (NUL-terminated UTF-8), or None if absent/empty."""
        fdef = self._field_def(num)
        if fdef is None or fdef.base_type != 0x07:
            return None
        text = self._raw[num].split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        return text or None

    def dev_value(self, dev_num: int, dev_index: int, fmt: str):
        """Decode a scalar developer field (struct format char `fmt`), or None if absent."""
        offset = 0
        for num, size, idx in self.definition.dev_fields:
            if (num, idx) == (dev_num, dev_index):
                raw = self.dev_payload[offset:offset + size]
                if len(raw) != struct.calcsize(fmt):
                    return None
                return struct.unpack(self.definition.endian + fmt, raw)[0]
            offset += size
        return None

    def get(self, num: int):
        """Return the decoded scalar value of a field, or None if absent/invalid."""
        fdef = self._field_def(num)
        if fdef is None or fdef.base_type not in BASE_TYPES:
            return None
        size, fmt, invalid = BASE_TYPES[fdef.base_type]
        raw = self._raw[num]
        if fmt == 's' or len(raw) != size:
            return None  # strings / arrays are not needed here
        value = struct.unpack(self.definition.endian + fmt, raw)[0]
        return None if value == invalid else value

    def set(self, num: int, base_type: int, value: Optional[int]) -> None:
        """Set a scalar field (raw, unscaled value); None writes the invalid value.

        If the field is not in the definition it is appended, and the writer emits
        an extended definition message.
        """
        size, fmt, invalid = BASE_TYPES[base_type]
        fdef = self._field_def(num)
        if fdef is None:
            fdef = FieldDef(num, size, base_type)
            self._extra.append(fdef)
        elif fdef.size != size:
            raise ValueError(f"Field {num} has size {fdef.size}, cannot write as {size}-byte value")
        fmt = self.definition.endian + BASE_TYPES[fdef.base_type][1]
        self._raw[num] = struct.pack(fmt, BASE_TYPES[fdef.base_type][2] if value is None else value)

    def output_definition(self) -> Definition:
        if not self._extra:
            return self.definition
        d = self.definition
        return Definition(d.local_type, d.global_num, d.big_endian, d.fields + self._extra, d.dev_fields)

    def encode_payload(self) -> bytes:
        out = bytearray()
        for f in self.definition.fields + self._extra:
            out += self._raw[f.num]
        return bytes(out) + self.dev_payload


@dataclass
class FitData:
    header: bytes            # original file header (12 or 14 bytes)
    records: list            # Definition and DataMessage objects in file order
    trailing: bytes = b''    # anything after the first FIT file (chained files), copied as-is

    def messages(self, global_num: Optional[int] = None) -> List[DataMessage]:
        return [r for r in self.records if isinstance(r, DataMessage)
                and (global_num is None or r.global_num == global_num)]

    def developer_field(self, name: str) -> Optional[Tuple[int, int, int]]:
        """(field number, developer index, base type) of a developer field by name.

        Field numbers are not stable between files, so look them up in the
        field_description messages (global 206).
        """
        for desc in self.messages(206):
            if desc.get_string(3) == name:
                return desc.get(1), desc.get(0), desc.get(2)
        return None


def read_fit(path: str) -> FitData:
    with open(path, 'rb') as f:
        data = f.read()
    header_size = data[0]
    if data[8:12] != b'.FIT':
        raise ValueError(f"{path} is not a FIT file")
    data_size = struct.unpack('<I', data[4:8])[0]
    end = header_size + data_size
    pos = header_size
    definitions: Dict[int, Definition] = {}
    records = []
    last_ts: Optional[int] = None

    while pos < end:
        header = data[pos]
        pos += 1
        if header & 0x80:  # compressed timestamp header -> data message
            local = (header >> 5) & 0x3
            offset = header & 0x1F
            if last_ts is None:
                raise ValueError("Compressed timestamp before any timestamp")
            ts = (last_ts & ~0x1F) + offset
            if offset < (last_ts & 0x1F):
                ts += 0x20
            last_ts = ts
            definition = definitions[local]
            payload = data[pos:pos + definition.data_size]
            pos += definition.data_size
            records.append(DataMessage(definition, header, payload, ts))
        elif header & 0x40:  # definition message
            local = header & 0x0F
            big = data[pos + 1] == 1
            endian = '>' if big else '<'
            global_num, nfields = struct.unpack(endian + 'HB', data[pos + 2:pos + 5])
            pos += 5
            fields = [FieldDef(*data[pos + 3 * i:pos + 3 * i + 3]) for i in range(nfields)]
            pos += 3 * nfields
            dev_fields = []
            if header & 0x20:
                ndev = data[pos]
                pos += 1
                dev_fields = [tuple(data[pos + 3 * i:pos + 3 * i + 3]) for i in range(ndev)]
                pos += 3 * ndev
            definition = Definition(local, global_num, big, fields, dev_fields)
            definitions[local] = definition
            records.append(definition)
        else:  # normal data message
            local = header & 0x0F
            definition = definitions[local]
            payload = data[pos:pos + definition.data_size]
            pos += definition.data_size
            msg = DataMessage(definition, header, payload, None)
            ts = msg.get(TIMESTAMP_FIELD)
            if ts is not None:
                last_ts = ts
            msg.timestamp = ts
            records.append(msg)

    return FitData(header=data[:header_size], records=records, trailing=data[end + 2:])


def write_fit(fit: FitData, path: str) -> None:
    body = bytearray()
    active: Dict[int, bytes] = {}  # local type -> encoded definition currently in effect

    for record in fit.records:
        if isinstance(record, Definition):
            # Emitted lazily before the first data message that uses it, so an
            # extended definition can replace it without a redundant original.
            continue
        definition = record.output_definition()
        encoded = definition.encode()
        if active.get(definition.local_type) != encoded:
            body += encoded
            active[definition.local_type] = encoded
        body.append(record.header)
        body += record.encode_payload()

    header = bytearray(fit.header)
    header[4:8] = struct.pack('<I', len(body))
    if len(header) >= 14:
        header[12:14] = struct.pack('<H', fit_crc(bytes(header[:12])))
    out = bytes(header) + bytes(body)
    out += struct.pack('<H', fit_crc(out))
    with open(path, 'wb') as f:
        f.write(out + fit.trailing)
