#!/usr/bin/env python3
"""Probe dyld_chained_fixups page_start layout by validating decoded class pointers."""
import lief, struct as st

b = lief.parse('SimpleQR')
raw = open('SimpleQR', 'rb').read()
segs = [(s.name, s.virtual_address, s.virtual_size, s.file_offset) for s in b.segments]

def va2off(va):
    for (n, a, sz, fo) in segs:
        if a <= va < a + sz:
            return int(fo + (va - a))

def in_range(va, lo, hi):
    return lo <= va < hi

anchor_objc_data = (0x100e9daf8, 0x100e9daf8 + 0x28dd8)      # __objc_data
anchor_classname = (0x100c50788, 0x100c50788 + 0xd88f)        # __objc_classname
CL_VA = 0x100d944e8
CL_SIZE = 0x3df0

base = 0xee8000
blob = raw[base + 32:]
seg_count = st.unpack('<I', blob[:4])[0]
seg_info = st.unpack(f'<{seg_count}I', blob[4:4 + 4 * seg_count])

def decode_segment(seg_idx, soff, arr_off, npages):
    seg = segs[seg_idx]
    si = blob[soff:]
    page_size = st.unpack('<H', si[4:6])[0] or 4096
    pfmt = st.unpack('<H', si[6:8])[0]
    if npages is None:
        npages = 0
    ps = st.unpack(f'<{npages}H', si[arr_off:arr_off + 2 * npages])
    out = {}
    for pi, start in enumerate(ps):
        if start == 0xFFFF:
            continue
        eva = seg[1] + pi * page_size + start
        guard = 0
        while guard < 100000:
            guard += 1
            o = va2off(eva)
            if o is None or o + 8 > len(raw):
                break
            v = st.unpack('<Q', raw[o:o + 8])[0]
            bind = (v >> 62) & 1
            nxt = (v >> 51) & 0x7FF
            if bind:
                break
            tgt = v & ((1 << 43) - 1)
            h8 = (v >> 43) & 0xFF
            final = 0x100000000 + (h8 << 43 | tgt)
            out[eva] = final
            if nxt == 0:
                break
            eva += nxt * 8
    return out

def validate(res):
    """Check classlist pointers decode into __objc_data with sane class names."""
    good = 0
    total = 0
    for i in range(0, CL_SIZE, 8):
        va = CL_VA + i
        if va not in res:
            continue
        total += 1
        cv = res[va]
        if not in_range(cv, *anchor_objc_data):
            continue
        # read class_t at cv (already in file — class_t itself is in __objc_data, plain bytes)
        o = va2off(cv)
        if o is None:
            continue
        hdr = raw[o:o + 40]
        if len(hdr) < 40:
            continue
        isa, sup, cache, vt, classro = st.unpack('<QQQQQ', hdr)
        ro_o = va2off(classro & ~7)
        if ro_o is None:
            continue
        ro = raw[ro_o:ro_o + 40]
        if len(ro) < 40:
            continue
        name_va = st.unpack('<Q', ro[24:32])[0]
        # name pointer is itself in __objc_const (chained, decode needed) OR plain
        name_off = va2off(name_va)
        if name_off is None:
            name_off = va2off(name_va & ~7)
        if name_off is None:
            continue
        end = raw.find(b'\x00', name_off)
        s = raw[name_off:min(end, name_off + 60)]
        if 3 < len(s) < 60 and s.isidentifier() or b'_' in s[:2]:
            good += 1
    return good, total

for seg_idx, soff in enumerate(seg_info):
    if soff == 0 or seg_idx not in (2, 3):
        continue
    si = blob[soff:]
    size = st.unpack('<I', si[:4])[0]
    field20 = st.unpack('<I', si[20:24])[0]
    field24 = st.unpack('<I', si[24:28])[0]
    cands = [
        ('arr@24 count@20', 24, field20),
        ('arr@24 count@24', 24, field24),
        ('arr@24 count=(size-24)/2', 24, (size - 24) // 2),
        ('arr@32 count@20', 32, field20),
        ('arr@32 count=(size-32)/2', 32, (size - 32) // 2),
    ]
    print(f'=== seg{seg_idx} {segs[seg_idx][0]} size={size} f20={field20} f24={f24 if False else field24}')
    for label, ao, np in cands:
        try:
            res = decode_segment(seg_idx, soff, ao, np)
            good, total = validate(res)
            print(f'  {label:28s} -> decoded={len(res):6d} classlist-hit={good}/{total}')
        except Exception as e:
            print(f'  {label:28s} -> ERROR {e}')
