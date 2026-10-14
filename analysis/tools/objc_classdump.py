#!/usr/bin/env python3
"""Parse __objc_classlist of an ARM64 Mach-O and dump class -> methods mapping.
Focus on ad-related classes for hook targeting (Plan C)."""
import lief, struct, json, sys, re

BIN = 'SimpleQR'
b = lief.parse(BIN)
raw = open(BIN, 'rb').read()

segments = [(s.virtual_address, s.virtual_size, s.file_offset) for s in b.segments]

def va2off(va):
    for (vaddr, vsize, foff) in segments:
        if vaddr <= va < vaddr + vsize:
            return int(foff + (va - vaddr))
    return None

def read_va(va, n):
    off = va2off(va)
    if off is None: return None
    return raw[off:off+n]

def cstr(va, maxlen=200):
    off = va2off(va)
    if off is None: return None
    end = raw.find(b'\x00', off)
    if end == -1: return None
    return raw[off:min(end, off+maxlen)].decode('utf-8', errors='ignore')

def u64(va): return struct.unpack('<Q', read_va(va, 8))[0]
def u32(va): return struct.unpack('<I', read_va(va, 4))[0]

def parse_method_list(ml_va, methods_out):
    """ARM64 method_list_t: {u32 entsize, u32 count}. Relative lists flagged by 0x80000000."""
    if ml_va == 0: return
    hdr = read_va(ml_va, 8)
    if hdr is None: return
    entsize, count = struct.unpack('<II', hdr)
    if count > 5000: return
    relative = bool(entsize & 0x80000000)
    entsize &= 0x7FFFFFFF
    if entsize == 0: return
    for i in range(count):
        e_va = ml_va + 8 + i * entsize
        if relative:
            e = read_va(e_va, 12)
            if e is None or len(e) < 12: continue
            name_off, types_off, imp_off = struct.unpack('<iii', e)
            name_va = e_va + name_off
            # name points to a location in __objc_selrefs (a pointer to SEL string)
            try:
                sel_ptr = struct.unpack('<Q', read_va(name_va, 8) or b'\x00\x00\x00\x00\x00\x00\x00\x00')[0]
                name = cstr(sel_ptr)
            except Exception:
                name = None
            if not name:
                # some toolchains point directly at the methname string
                name = cstr(name_va)
            methods_out.append(name or f'<sel@{hex(name_va)}>')
        else:
            e = read_va(e_va, 24)
            if e is None or len(e) < 24: continue
            name_va, types_va, imp_va = struct.unpack('<QQQ', e)
            name = cstr(name_va)
            methods_out.append(name or f'<sel@{hex(name_va)}>')

def parse_class(cls_va):
    """class_t: isa, superclass, cache, vtable, classro (each u64; low bits may carry flags)."""
    hdr = read_va(cls_va, 40)
    if hdr is None or len(hdr) < 40: return None
    isa, superclass, cache, vtable, classro = struct.unpack('<QQQQQ', hdr)
    # mask off arm64e /RO flag bits (on plain arm64 nothing is set, but be safe)
    ro_va = classro & ~0x7
    ro = read_va(ro_va, 48)
    if ro is None or len(ro) < 48: return None
    flags, inststart, instsize, reserved, ivalayout, name_va, base_methods, base_protos, ivars, weak_ivar, base_props = \
        struct.unpack('<IIIIQQQQQQQ', ro + b'\x00'*0)
    name = cstr(name_va & ~0x7) if name_va else None
    methods = []
    parse_method_list(base_methods & ~0x7 if base_methods else 0, methods)
    return {
        'name': name, 'cls_va': hex(cls_va), 'super': cstr(superclass & ~0x7) if superclass else None,
        'methods': methods, 'flags': flags,
    }

secs = {s.name: s for s in b.sections}
cl = secs['__objc_classlist']
n = cl.size // 8
data = read_va(cl.virtual_address, cl.size)
ptrs = struct.unpack(f'<{n}Q', data)
print(f'[*] {n} classes in __objc_classlist', file=sys.stderr)

classes = []
for i, p in enumerate(ptrs):
    try:
        info = parse_class(p)
        if info: classes.append(info)
    except Exception as e:
        print(f'[!] class {i} @ {hex(p)}: {e}', file=sys.stderr)

print(f'[*] parsed {len(classes)} classes OK', file=sys.stderr)

with open('objc_classes.json', 'w', encoding='utf-8') as f:
    json.dump(classes, f, ensure_ascii=False, indent=1)

# ad-related keywords
KW = re.compile(r'GAD|GoogleMobileAds|Ad[Nn]|AdMob|Ads|Vungle|VungleAds|PAG|Pangle|BUC|CSJAds|Moloco|InMobi|SKAd|Interstit|Banner|Rewarded|AppOpen|OpenAd|Store[Kk]it|Purchase|IAP|Subscription|Paywall|RemoveAd|AdFree|Pro|Premium|Buy', re.I)

ad_classes = [c for c in classes if c['name'] and KW.search(c['name'])]
print(f'[*] {len(ad_classes)} ad/commerce-related classes', file=sys.stderr)
with open('ad_classes.json', 'w', encoding='utf-8') as f:
    json.dump(ad_classes, f, ensure_ascii=False, indent=1)

for c in ad_classes:
    print(f"\n=== {c['name']} : {c['super']} ===")
    for m in c['methods']:
        print('  -', m)
