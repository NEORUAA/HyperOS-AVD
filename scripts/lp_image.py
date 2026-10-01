#!/usr/bin/env python3
"""Read/repack AVD GPT and liblp raw images using the AOSP on-disk format."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

SECTOR = 512
ALIGN = 1024 * 1024


def align(n, size=ALIGN):
    return (n + size - 1) // size * size


def read_gpt(path):
    with open(path, "rb") as f:
        f.seek(SECTOR)
        header = f.read(SECTOR)
        assert header[:8] == b"EFI PART"
        table_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
        f.seek(table_lba * SECTOR)
        entries = [f.read(entry_size) for _ in range(count)]
    return [(e[56:128].decode("utf-16le").rstrip("\0"), *struct.unpack_from("<QQ", e, 32))
            for e in entries if any(e[:16])]


def read_lp(path):
    super_start = next(first * SECTOR for name, first, _ in read_gpt(path) if name == "super")
    with open(path, "rb") as f:
        f.seek(super_start + 4096)
        geo = bytearray(f.read(52))
        assert struct.unpack_from("<I", geo)[0] == 0x616c4467
        checksum = bytes(geo[8:40])
        geo[8:40] = bytes(32)
        assert hashlib.sha256(geo).digest() == checksum
        max_size, slots, block_size = struct.unpack_from("<III", geo, 40)
        f.seek(super_start + 12288)
        data = f.read(max_size)
    magic, major, minor, header_size = struct.unpack_from("<IHHI", data)
    assert magic == 0x414c5030 and major == 10
    header = bytearray(data[:header_size])
    checksum = bytes(header[12:44])
    header[12:44] = bytes(32)
    assert hashlib.sha256(header).digest() == checksum
    table_size = struct.unpack_from("<I", header, 44)[0]
    tables = data[header_size:header_size + table_size]
    assert hashlib.sha256(tables).digest() == header[48:80]
    descriptors = [struct.unpack_from("<III", header, 80 + i * 12) for i in range(4)]
    entries = [[tables[off + j * size:off + (j + 1) * size] for j in range(count)]
               for off, count, size in descriptors]
    extents = [struct.unpack("<QIQI", e) for e in entries[1]]
    partitions = []
    for e in entries[0]:
        name, attr, first, count, group = struct.unpack("<36sIIII", e)
        ex = extents[first:first + count]
        partitions.append({"name": name.split(b"\0")[0].decode(), "attributes": attr,
                           "size": sum(x[0] * SECTOR for x in ex), "extents": ex})
    return super_start, partitions


def copy_range(source, dest, offset, size):
    source.seek(offset)
    while size:
        data = source.read(min(size, 8 * ALIGN))
        if not data:
            raise EOFError("Image is truncated")
        dest.write(data)
        size -= len(data)


def unpack(path, output):
    output.mkdir(parents=True, exist_ok=True)
    base, parts = read_lp(path)
    with open(path, "rb") as source:
        for p in parts:
            with open(output / (p["name"] + ".img"), "wb") as dest:
                for length, typ, start, device in p["extents"]:
                    assert device == 0
                    if typ == 0:
                        copy_range(source, dest, base + start * SECTOR, length * SECTOR)
                    else:
                        assert typ == 1
                        dest.seek(length * SECTOR, 1)
                dest.truncate(p["size"])
    print(json.dumps(parts, indent=2))


def pack(template, output, inputs):
    inputs = [(name, Path(path)) for name, path in inputs]
    sizes = [align(p.stat().st_size, 4096) for _, p in inputs]
    offsets = []
    cursor = ALIGN
    for size in sizes:
        offsets.append(cursor)
        cursor = align(cursor + size)
    super_size = cursor + ALIGN
    max_metadata, slots = 65536, 2
    parts = b"".join(struct.pack("<36sIIII", name.encode(), 1, i, 1, 1)
                     for i, (name, _) in enumerate(inputs))
    extents = b"".join(struct.pack("<QIQI", size // SECTOR, 0, off // SECTOR, 0)
                       for size, off in zip(sizes, offsets))
    groups = struct.pack("<36sIQ", b"default", 0, 0) + struct.pack(
        "<36sIQ", b"emulator_dynamic_partitions", 0, super_size - ALIGN)
    devices = struct.pack("<QIIQ36sI", ALIGN // SECTOR, ALIGN, 0, super_size, b"super", 0)
    tables = parts + extents + groups + devices
    header = bytearray(struct.pack("<IHHI32sI32s", 0x414c5030, 10, 0, 128,
                                  bytes(32), len(tables), hashlib.sha256(tables).digest()))
    off = 0
    for table, count, entry_size in [(parts, len(inputs), 52), (extents, len(inputs), 24),
                                     (groups, 2, 48), (devices, 1, 64)]:
        header.extend(struct.pack("<III", off, count, entry_size))
        off += len(table)
    header[12:44] = hashlib.sha256(header).digest()
    metadata = (header + tables).ljust(max_metadata, b"\0")
    geometry = bytearray(struct.pack("<II32sIII", 0x616c4467, 52, bytes(32),
                                    max_metadata, slots, 4096))
    geometry[8:40] = hashlib.sha256(geometry).digest()
    geometry = geometry.ljust(4096, b"\0")
    total_size = align(2 * ALIGN + super_size + 33 * SECTOR)
    last_lba = total_size // SECTOR - 1
    entries = bytearray(128 * 128)
    with open(template, "rb") as src:
        src.seek(SECTOR)
        old_header = src.read(SECTOR)
        src.seek(1024)
        old_entries = src.read(256)
        src.seek(ALIGN)
        vbmeta = bytearray(src.read(ALIGN))
    for i, (name, first, last) in enumerate([("vbmeta", 2048, 4095),
                                           ("super", 4096, 4096 + super_size // SECTOR - 1)]):
        entry = bytearray(old_entries[i * 128:(i + 1) * 128])
        struct.pack_into("<QQ", entry, 32, first, last)
        entries[i * 128:(i + 1) * 128] = entry
    assert vbmeta[:4] == b"AVB0"
    struct.pack_into(">I", vbmeta, 120, 3)
    def gpt_header(current, backup, table_lba):
        h = bytearray(old_header)
        struct.pack_into("<QQQQ", h, 24, current, backup, 34, last_lba - 33)
        struct.pack_into("<Q", h, 72, table_lba)
        struct.pack_into("<I", h, 88, zlib.crc32(entries))
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, zlib.crc32(h[:92]))
        return h
    mbr = bytearray(SECTOR)
    mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\0\x02\0", 0xee, b"\xff\xff\xff",
                               1, min(last_lba, 0xffffffff))
    mbr[510:] = b"\x55\xaa"
    with open(output, "wb") as dest:
        dest.truncate(total_size)
        dest.write(mbr)
        dest.write(gpt_header(1, last_lba, 2))
        dest.write(entries)
        dest.seek(ALIGN)
        dest.write(vbmeta)
        dest.seek(2 * ALIGN + 4096)
        dest.write(geometry * 2)
        dest.write(metadata * (slots * 2))
        for (name, path), offset in zip(inputs, offsets):
            dest.seek(2 * ALIGN + offset)
            with path.open("rb") as src:
                copy_range(src, dest, 0, path.stat().st_size)
        dest.seek((last_lba - 32) * SECTOR)
        dest.write(entries)
        dest.write(gpt_header(last_lba, 1, last_lba - 32))
    print(json.dumps({"output": str(output), "size": total_size,
                      "partitions": read_lp(output)[1]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inspect", "unpack", "pack"])
    parser.add_argument("image", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("partitions", nargs="*")
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(read_lp(args.image)[1], indent=2))
    elif args.action == "unpack":
        unpack(args.image, args.output)
    else:
        pack(args.image, args.output, [x.split("=", 1) for x in args.partitions])
