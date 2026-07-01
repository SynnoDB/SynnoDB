"""Read a shared object's GNU build-id (NT_GNU_BUILD_ID) without loading it.

Mirrors the C++ ``Plugin::file_build_id_for`` (plugin.hpp) so the Python side can
detect a ``libloader.so`` source change the same way the C++ loader detects a
``libbuilder.so`` change. A loader-source change makes the engine's in-RAM Arrow
input stale, so the whole engine must restart; this reader is how run.py notices.

Returns the lowercase-hex build-id, or ``None`` when the file is missing,
unreadable, not a 64-bit ELF, or carries no build-id note - callers treat
``None`` as "no change signal" and leave the runner as-is.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional, Union

_ELF_MAGIC = b"\x7fELF"
_PT_NOTE = 4
_NT_GNU_BUILD_ID = 3


def read_build_id(path: Union[str, Path]) -> Optional[str]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    # Elf64_Ehdr is 64 bytes; reject anything that cannot hold one.
    if len(data) < 64 or data[:4] != _ELF_MAGIC:
        return None
    ei_class, ei_data = data[4], data[5]
    if ei_class != 2:  # ELFCLASS64 only - the project's plugins are 64-bit
        return None
    endian = "<" if ei_data == 1 else ">"
    # Elf64_Ehdr: e_phoff (u64) @32, e_phentsize (u16) @54, e_phnum (u16) @56.
    (e_phoff,) = struct.unpack_from(endian + "Q", data, 32)
    e_phentsize, e_phnum = struct.unpack_from(endian + "HH", data, 54)
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 56 > len(data):
            break
        # Elf64_Phdr: p_type (u32) @0, p_offset (u64) @8, p_filesz (u64) @32.
        (p_type,) = struct.unpack_from(endian + "I", data, off)
        if p_type != _PT_NOTE:
            continue
        (p_offset,) = struct.unpack_from(endian + "Q", data, off + 8)
        (p_filesz,) = struct.unpack_from(endian + "Q", data, off + 32)
        build_id = _scan_notes(data, p_offset, p_filesz, endian)
        if build_id is not None:
            return build_id
    return None


def _scan_notes(data: bytes, start: int, size: int, endian: str) -> Optional[str]:
    pos = start
    end = min(start + size, len(data))
    while pos + 12 <= end:
        n_namesz, n_descsz, n_type = struct.unpack_from(endian + "III", data, pos)
        pos += 12
        name = data[pos : pos + n_namesz]
        pos += (n_namesz + 3) & ~3  # 4-byte aligned
        desc_start = pos
        pos += (n_descsz + 3) & ~3
        if n_type == _NT_GNU_BUILD_ID and name.rstrip(b"\x00") == b"GNU":
            desc = data[desc_start : desc_start + n_descsz]
            if desc:
                return desc.hex()
    return None
