#!/usr/bin/env python3
"""Decode dyld chained fixups in a (dumped, unencrypted) ARM64 Mach-O and
write a shadow copy of the file with all chained pointers resolved to plain VM addresses.
Then dump ObjC class -> method mapping from __objc_classlist."""
import lief, struct, json, sys, re

BIN = 'SimpleQR'
b = lief.parse(BIN)
raw = bytearray(open(BIN, 'rb').read())

segments = []
for s in b.segments:
    segments.append((s.name, s.virtual_address, s.virtual_size, s.file_offset))

def va2off(va):
    for (_, a, sz, fo) in segments:
        if a <= va < a + sz:
            return int(fo + (va - a))
    return None

# ---- locate LC_DYLD_CHAINED_FIXUPS in __LINKEDIT ----
LINKEDIT = [s for s in b.segments if s.name == '__LINKEDIT'][0]
fix_cmd = None
for c in b.commands:
    if type(c).__name__ == 'DyldChainedFixups':
        fix_cmd = c
        break

# raw command bytes: walk load commands manually to get fixups data
import struct as st
LC_DYLD_CHAINED_FIXUPS = 0x80000034
ncmds = b.header.nb_cmds
off = 32  # mach_header_64 size
links = []
cur = off
for _ in range(ncmds):
    cmd, cmdsize = st.unpack('<II', raw[cur:cur+8])
    if cmd == LC_DYLD_CHAINED_FIXUPS:
        dataoff, datasize = st.unpack('<II', raw[cur+8:cur+16])
        print(f'[fixups] dataoff=0x{dataoff:x} datasize=0x{datasize:x}', file=sys.stderr)
        links = raw[dataoff:dataoff+datasize]
        break
    cur += cmdsize
assert links, 'no chained fixups command found'

# dyld_chained_fixups_header
fx_version, starts_offset, imports_count, imports_format, symbols_format = st.unpack('<IIIII', links[:20])
print(f'[fixups] version={fx_version} starts@{starts_offset} imports={imports_count} imports_format={imports_format}', file=sys.stderr)
starts = links[starts_offset:]
seg_count = st.unpack('<I', starts[:4])[0]
seg_info_offsets = st.unpack(f'<{seg_count}I', starts[4:4+4*seg_count])
print(f'[fixups] seg_count={seg_count} seg_info={seg_info_offsets}', file=sys.stderr)

imports = []
if imports_count:
    imp_start = 20
    for i in range(imports_count):
        if imports_format == 0:  # DYLD_CHAINED_IMPORT
            lib_ordinal, weak_import, name_offset = st.unpack('<IHH', links[imp_start:imp_start+8])
            imp_start += 8
        elif imports_format == 1:  # DYLD_CHAINED_IMPORT_ADDEND
            lib_ordinal, weak_import, name_offset = st.unpack('<IHH', links[imp_start:imp_start+12])
            imp_start += 12
        else:
            lib_ordinal, weak_import, name_offset = 0, 0, 0
        # name_offset relative to end of imports table
        imports.append((lib_ordinal & 0xFFFFFF, weak_import, name_offset))
    # symbol strings follow
    symstr_base = imp_start + 4 * (imports_count if imports_format == 2 else 0)
    # DYLD_CHAINED_IMPORT_ADDEND64 has addend
    # symbol pool begins after all import entries
    pool = links[imp_start:]
    imports = [(lo, wi, pool[no:].split(b'\x00')[0].decode('utf-8', 'replace')) for (lo, wi, no) in imports]
    print(f'[fixups] imports: {imports[:5]} ...', file=sys.stderr)

# decode each segment chain
resolved = {}   # slot va -> final value
for seg_idx, seg_info_off in enumerate(seg_info_offsets):
    if seg_info_off == 0:
        continue
    seg_info = starts[seg_info_off:]
    size, page_count, pointer_format = st.unpack('<IHH', seg_info[:8])
    page_starts = st.unpack(f'<{page_count}H', seg_info[8:8+2*page_count])
    seg_name, seg_va, seg_vsz, seg_fo = segments[seg_idx]
    print(f'[fixups] seg {seg_idx} {seg_name} fmt={pointer_format} pages={page_count}', file=sys.stderr)
    for page_idx, ps in enumerate(page_starts):
        if ps == 0xFFFF:
            continue
        page_va = seg_va + page_idx * 4096
        entry_va = page_va + ps
        while True:
            eo = va2off(entry_va)
            if eo is None:
                break
            value = st.unpack('<Q', raw[eo:eo+8])[0]
            if pointer_format in (2, 6):  # DYLD_CHAINED_PTR_64 (2) / _64_OFFSET (6)
                bind = (value >> 62) & 1
                nxt = (value >> 51) & 0x7FF
                if bind:
                    ord_n = (value >> 36) & 0xFFFF  # hmm variant
                    # DYLD_CHAINED_PTR_64_BIND: ordinal:16, addend:8? treat simply
                    break
                else:
                    target = value & ((1 << 43) - 1)
                    high8 = (value >> 43) & 0xFF
                    vm_off = target | (high8 << 43)
                    final = 0x100000000 + vm_off if pointer_format == 2 else vm_off  # assume base 0x100000000
                    resolved[entry_va] = final
            elif pointer_format in (1, 7, 8, 9, 10):  # arm64e variants
                bind = (value >> 63) & 1
                nxt = (value >> 51) & 0x3FF
                if bind:
                    break
                target = value & 0xFFFFFFFFF
                if pointer_format in (1, 7, 8):
                    final = 0x100000000 + target
                else:
                    final = target
                resolved[entry_va] = final
            else:
                break
            if nxt == 0:
                break
            entry_va += nxt * 4 if pointer_format in (1, 7, 8) else nxt * 8

print(f'[fixups] resolved {len(resolved)} pointer slots', file=sys.stderr)

# apply resolved pointers to shadow raw
for va, final in resolved.items():
    o = va2off(va)
    if o is not None:
        raw[o:o+8] = st.pack('<Q', final)

open('/tmp/SimpleQR.decoded', 'wb').write(bytes(raw))
print('[*] wrote /tmp/SimpleQR.decoded', file=sys.stderr)

# ---- now parse ObjC classes on decoded buffer ----
def read_va(va, n):
    o = va2off(va)
    return raw[o:o+n] if o is not None else None

def cstr(va, maxlen=200):
    o = va2off(va)
    if o is None: return None
    end = raw.find(b'\x00', o)
    if end == -1: return None
    try:
        return raw[o:min(end, o+maxlen)].decode('utf-8', 'ignore')
    except Exception:
        return None

def parse_method_list(ml_va, methods_out):
    if not ml_va: return
    hdr = read_va(ml_va, 8)
    if not hdr or len(hdr) < 8: return
    entsize, count = st.unpack('<II', hdr)
    if count > 20000: return
    relative = bool(entsize & 0x80000000)
    entsize &= 0x7FFFFFFF
    if entsize == 0: return
    for i in range(count):
        e_va = ml_va + 8 + i * entsize
        e = read_va(e_va, 12)
        if not e or len(e) < 12: continue
        if relative:
            name_off, types_off, imp_off = st.unpack('<iii', e)
            sel_slot_va = e_va + name_off
            slot = read_va(sel_slot_va, 8)
            name = None
            if slot and len(slot) == 8:
                sel_va = st.unpack('<Q', slot)[0]
                if sel_va:
                    name = cstr(sel_va)
            if not name:
                name = cstr(sel_slot_va)
            methods_out.append(name or f'<sel@{hex(sel_slot_va)}>')
        else:
            e24 = read_va(e_va, 24)
            if not e24 or len(e24) < 24: continue
            name_va, types_va, imp_va = st.unpack('<QQQ', e24)
            methods_out.append(cstr(name_va) or f'<sel@{hex(name_va)}>')

def parse_class(cls_va):
    hdr = read_va(cls_va, 40)
    if not hdr or len(hdr) < 40: return None
    isa, superclass, cache, vtable, classro = st.unpack('<QQQQQ', hdr)
    ro_va = classro & ~0x7
    ro = read_va(ro_va, 56)
    if not ro or len(ro) < 56: return None
    (flags, inst_start, inst_size, reserved, ivalayout,
     name_va, base_methods, base_protos, ivars, weak_ivar, base_props) = st.unpack('<IIIIQQQQQQQ', ro[:56])
    name = cstr(name_va) if name_va else None
    methods = []
    parse_method_list(base_methods, methods)
    super_name = cstr(superclass) if superclass else None
    return {'name': name, 'cls_va': hex(cls_va), 'super': super_name, 'methods': methods}

secs = {s.name: s for s in b.sections}
cl = secs['__objc_classlist']
d = read_va(cl.virtual_address, cl.size)
n = cl.size // 8
ptrs = st.unpack(f'<{n}Q', d)

classes = []
for p in ptrs:
    try:
        info = parse_class(p)
        if info and info['name']:
            classes.append(info)
    except Exception as e:
        pass

print(f'[*] parsed {len(classes)}/{n} classes', file=sys.stderr)
with open('objc_classes.json', 'w', encoding='utf-8') as f:
    json.dump(classes, f, ensure_ascii=False, indent=1)

# also parse categories
cat = secs.get('__objc_catlist')
if cat:
    d = read_va(cat.virtual_address, cat.size)
    ncat = cat.size // 8
    cptrs = st.unpack(f'<{ncat}Q', d)
    cats = []
    for p in cptrs:
        hdr = read_va(p, 32)
        if not hdr or len(hdr) < 32: continue
        isa, name_va = st.unpack('<QQ', hdr[:16])
        methods_cls, methods_meta = st.unpack('<QQ', hdr[16:32])
        nm = cstr(name_va)
        ms = []
        parse_method_list(methods_cls, ms)
        if nm:
            cats.append({'name': nm, 'methods': ms})
    with open('objc_cats.json', 'w', encoding='utf-8') as f:
        json.dump(cats, f, ensure_ascii=False, indent=1)
    print(f'[*] parsed {len(cats)} categories', file=sys.stderr)

KW = re.compile(r'GAD|AdMob|Ads$|^Ad|Ad[A-Z]|Vungle|PAG|Pangle|BUC|CSJ|Moloco|InMobi|SKAd|Interstit|Banner|Rewarded|AppOpen|StoreKit|Purchase|IAP|Subscri|Paywall|RemoveAd|AdFree|ProVersion|Premium|BuyManager|RevenueCat|Purchas', re.I)

ad_classes = [c for c in classes if c['name'] and KW.search(c['name'])]
print(f'[*] {len(ad_classes)} ad/commerce classes matched', file=sys.stderr)
with open('ad_classes.json', 'w', encoding='utf-8') as f:
    json.dump(ad_classes, f, ensure_ascii=False, indent=1)

out = []
for c in ad_classes:
    out.append(f"=== {c['name']} : {c['super']} (cls_va {c['cls_va']}) ===")
    for m in c['methods']:
        out.append('  - ' + str(m))
open('ad_classes.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
