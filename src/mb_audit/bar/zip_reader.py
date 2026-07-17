"""Tolerant ZIP reader for Micro.blog BAR files.

Why this exists: BAR files exported by Micro.blog are valid ZIPs in every
respect except one — when the archive grows past 4 GB, the EOCD's
``cd_offset`` field is left as the ZIP64 sentinel ``0xFFFFFFFF`` but no
ZIP64 EOCD record is written to back it up. System ``unzip`` recovers by
computing ``cd_offset = file_size - 22 - cd_size``; Python's ``zipfile``
does not, and as a result every member offset it reports is wrong.

This module reads the central directory directly with the unzip-style
recovery, then exposes the entries we need (name, size, local-header
offset) and decompresses members on demand.

Only the subset of ZIP that BARs actually use is supported:
- compression methods 0 (stored) and 8 (deflate)
- filenames in ASCII / UTF-8
- both regular and ZIP64-extra-field offsets in CD entries (read if present)
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

# Signatures
_EOCD_SIG = b"PK\x05\x06"
_EOCD64_SIG = b"PK\x06\x06"
_EOCD64_LOCATOR_SIG = b"PK\x06\x07"
_CD_ENTRY_SIG = b"PK\x01\x02"
_LOCAL_FILE_SIG = b"PK\x03\x04"

# Sizes
_EOCD_SIZE = 22
_EOCD_SCAN_WINDOW = 65557  # max comment length + 22

_UINT32_MAX = 0xFFFFFFFF
_UINT16_MAX = 0xFFFF


@dataclass(frozen=True, slots=True)
class ZipEntry:
    name: str
    compressed_size: int
    uncompressed_size: int
    compress_method: int
    local_header_offset: int
    is_dir: bool


class BarZipError(Exception):
    pass


class BarZipReader:
    """Read entries from a BAR file (ZIP archive, possibly ZIP64-defective).

    The file is held open for the lifetime of this reader. Use as a
    context manager to close it deterministically.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fp: BinaryIO = path.open("rb")
        self._size = path.stat().st_size
        try:
            self._entries: dict[str, ZipEntry] = {}
            self._load_central_directory()
        except Exception:
            self._fp.close()
            raise

    # ----- Public API -----

    def __enter__(self) -> BarZipReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()

    @property
    def entries(self) -> dict[str, ZipEntry]:
        return self._entries

    def has(self, name: str) -> bool:
        return name in self._entries

    def get(self, name: str) -> ZipEntry:
        return self._entries[name]

    def iter_prefix(self, prefix: str) -> Iterator[ZipEntry]:
        for e in self._entries.values():
            if e.name.startswith(prefix) and not e.is_dir:
                yield e

    def read(self, name: str) -> bytes:
        entry = self._entries[name]
        return self._read_entry(entry)

    def open_stream(self, name: str) -> io.BufferedReader:
        # Returned bytes wrapped as a BufferedReader for json.load convenience.
        return io.BufferedReader(io.BytesIO(self.read(name)))

    # ----- Internal: directory parsing -----

    def _load_central_directory(self) -> None:
        eocd_off, eocd = self._find_eocd()
        (
            _sig,
            _disk,
            _disk_with_cd,
            _entries_on_disk,
            entries_total,
            cd_size,
            cd_off_recorded,
            comment_len,
        ) = struct.unpack("<IHHHHIIH", eocd[:_EOCD_SIZE])

        cd_size_real = cd_size
        cd_off_real = cd_off_recorded
        entries_total_real = entries_total

        # Try ZIP64 EOCD if present (standards-compliant case).
        if cd_off_recorded == _UINT32_MAX or cd_size == _UINT32_MAX or entries_total == _UINT16_MAX:
            cd_off_real, cd_size_real, entries_total_real = self._read_zip64_eocd(
                eocd_off, cd_off_recorded, cd_size, entries_total
            )
            if cd_off_real == _UINT32_MAX:
                # Defective: sentinel without ZIP64 record. Recover by
                # placing the CD immediately before the EOCD.
                cd_off_real = eocd_off - cd_size_real

        # Parse the central directory.
        self._fp.seek(cd_off_real)
        cd = self._fp.read(cd_size_real)
        if len(cd) != cd_size_real:
            raise BarZipError(f"truncated central directory: wanted {cd_size_real}, got {len(cd)}")

        pos = 0
        seen = 0
        while pos + 46 <= len(cd):
            if cd[pos : pos + 4] != _CD_ENTRY_SIG:
                # Some archivers pad. Skip a byte and keep scanning, but bail
                # if we're losing the structure entirely.
                pos += 1
                continue
            (
                _sig,
                _ver_made,
                _ver_need,
                _flags,
                comp,
                _mtime,
                _mdate,
                _crc,
                csize,
                usize,
                fname_len,
                extra_len,
                comment_len_e,
                _disk_no,
                _int_attr,
                _ext_attr,
                local_off,
            ) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[pos : pos + 46])

            name_b = cd[pos + 46 : pos + 46 + fname_len]
            extra_b = cd[pos + 46 + fname_len : pos + 46 + fname_len + extra_len]

            # ZIP64 extra-field overrides for any sentinel values.
            if csize == _UINT32_MAX or usize == _UINT32_MAX or local_off == _UINT32_MAX:
                csize, usize, local_off = self._apply_zip64_extra(extra_b, csize, usize, local_off)

            name = name_b.decode("utf-8", errors="replace")
            self._entries[name] = ZipEntry(
                name=name,
                compressed_size=csize,
                uncompressed_size=usize,
                compress_method=comp,
                local_header_offset=local_off,
                is_dir=name.endswith("/"),
            )

            pos += 46 + fname_len + extra_len + comment_len_e
            seen += 1

        if entries_total_real and seen < entries_total_real:
            # Not fatal — some BARs may have padding that throws off the count.
            # Surface this would be useful but we keep it quiet here.
            pass

    def _find_eocd(self) -> tuple[int, bytes]:
        scan_size = min(_EOCD_SCAN_WINDOW, self._size)
        self._fp.seek(self._size - scan_size)
        tail = self._fp.read(scan_size)
        i = tail.rfind(_EOCD_SIG)
        if i < 0:
            raise BarZipError("end-of-central-directory record not found")
        eocd_off = self._size - scan_size + i
        return eocd_off, tail[i:]

    def _read_zip64_eocd(
        self,
        eocd_off: int,
        cd_off_recorded: int,
        cd_size_recorded: int,
        entries_total_recorded: int,
    ) -> tuple[int, int, int]:
        # The ZIP64 EOCD locator sits 20 bytes before the regular EOCD.
        loc_off = eocd_off - 20
        if loc_off < 0:
            return cd_off_recorded, cd_size_recorded, entries_total_recorded
        self._fp.seek(loc_off)
        loc = self._fp.read(20)
        if len(loc) < 20 or loc[:4] != _EOCD64_LOCATOR_SIG:
            return cd_off_recorded, cd_size_recorded, entries_total_recorded
        (_sig, _disk, eocd64_off, _total_disks) = struct.unpack("<IIQI", loc)
        self._fp.seek(eocd64_off)
        eocd64 = self._fp.read(56)
        if len(eocd64) < 56 or eocd64[:4] != _EOCD64_SIG:
            return cd_off_recorded, cd_size_recorded, entries_total_recorded
        (
            _sig2,
            _size_of_record,
            _ver_made,
            _ver_need,
            _disk2,
            _disk_with_cd2,
            _entries_on_disk,
            entries_total,
            cd_size,
            cd_off,
        ) = struct.unpack("<IQHHIIQQQQ", eocd64)
        return cd_off, cd_size, entries_total

    @staticmethod
    def _apply_zip64_extra(
        extra: bytes, csize: int, usize: int, local_off: int
    ) -> tuple[int, int, int]:
        e = 0
        while e + 4 <= len(extra):
            tag, esz = struct.unpack("<HH", extra[e : e + 4])
            payload = extra[e + 4 : e + 4 + esz]
            if tag == 0x0001:
                p = 0
                if usize == _UINT32_MAX and p + 8 <= len(payload):
                    usize = struct.unpack_from("<Q", payload, p)[0]
                    p += 8
                if csize == _UINT32_MAX and p + 8 <= len(payload):
                    csize = struct.unpack_from("<Q", payload, p)[0]
                    p += 8
                if local_off == _UINT32_MAX and p + 8 <= len(payload):
                    local_off = struct.unpack_from("<Q", payload, p)[0]
                    p += 8
                break
            e += 4 + esz
        return csize, usize, local_off

    # ----- Internal: data extraction -----

    def _read_entry(self, entry: ZipEntry) -> bytes:
        self._fp.seek(entry.local_header_offset)
        head = self._fp.read(30)
        if len(head) < 30 or head[:4] != _LOCAL_FILE_SIG:
            raise BarZipError(
                f"bad local header for {entry.name!r} at offset {entry.local_header_offset}"
            )
        fname_len, extra_len = struct.unpack("<HH", head[26:30])
        # Skip filename + extra
        self._fp.seek(fname_len + extra_len, io.SEEK_CUR)
        compressed = self._fp.read(entry.compressed_size)
        if len(compressed) != entry.compressed_size:
            raise BarZipError(
                f"short read for {entry.name!r}: wanted "
                f"{entry.compressed_size}, got {len(compressed)}"
            )
        if entry.compress_method == 0:
            return compressed
        if entry.compress_method == 8:
            return zlib.decompress(compressed, -zlib.MAX_WBITS)
        raise BarZipError(
            f"unsupported compression method {entry.compress_method} for {entry.name!r}"
        )
