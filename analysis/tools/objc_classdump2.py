#!/usr/bin/env python3
"""ObjC classdump for the dumped (chained-fixup encoded) SIMPLEQR arm64 binary.

Pointer encoding in this dumped binary: chained-fixup entries whose low 43 bits
hold the plain VM address (bit 51+ carries the chain "next" offset, bit 52 set on
every entry in the dense pointer tables).  => P(v) = v & 0x7FFFFFFFFFF.
"""
import lief, struct as st, json, sys, re

BIN = 'SimpleQR'
b = lief.parse(BIN)
raw = open(BIN, 'rb').read()
segs = [(s.name, s.virtual_address, s.virtual_size, s.file_offset) for s in b.segments]
secs = {s.name: s for s in b.sections}

def va2off(va):
    for (n, a, sz, fo) in segs:
        if a <= va < a + sz:
            return int(fo + (va - a))
    return None

def rd(va, n):
    o = va2off(va)
    if o is None or o + n > len(raw):
        return None
    return raw[o:o + n]

def cstr(va, ml=200):
    if not va: return None
    o = va2off(va)
    if o is None: return None
    e = raw.find(b'\x00', o)
    if e == -1 or e - o > ml: return None
    return raw[o:e].decode('utf-8', 'ignore')

def P(v):
    """chained-fixup entry -> plain VM address"""
    return v & 0x7FFFFFFFFFF

def PA(v):
    """same, but for struct pointers: low 3 bits carry flag/marker bits in this
    dumped binary (e.g. class_ro 0x...d9c04a -> real 0x...d9c048)."""
    return (v & 0x7FFFFFFFFFF) & ~0x7

def u64p(va):
    d = rd(va, 8)
    return P(st.unpack('<Q', d)[0]) if d else 0

def parse_method_list(ml_va, out):
    if not ml_va: return
    mlh = rd(ml_va, 8)
    if not mlh: return
    entsize, count = st.unpack('<II', mlh)
    if count == 0 or count > 20000: return
    rel = bool(entsize & 0x80000000)
    es = entsize & 0x7FFFFFFF
    if es == 0: return
    for i in range(count):
        eva = ml_va + 8 + i * es
        if rel:
            e = rd(eva, 12)
            if not e: break
            name_off, types_off, imp_off = st.unpack('<iii', e)
            slot_va = eva + name_off
            sel = cstr(u64p(slot_va))
            out.append(sel if sel else f'<sel@{hex(slot_va)}>')
        else:
            e = rd(eva, 24)
            if not e: break
            name_va, types_va, imp_va = st.unpack('<QQQ', e)
            out.append(cstr(P(name_va)) or f'<sel@{hex(name_va)}>')

def parse_class(cls_va):
    hdr = rd(cls_va, 40)
    if not hdr: return None
    isa, sup, cache, vt, classro = st.unpack('<QQQQQ', hdr)
    ro_va = PA(classro)
    ro = rd(ro_va, 72)
    if not ro: return None
    (flags, iS, iSz, resv, ivlay, name_va, base_methods, base_protos,
     ivars, wivar, bprops) = st.unpack('<IIIIQQQQQQQ', ro)
    name = cstr(P(name_va)) if name_va else None
    if not name: return None
    methods = []
    parse_method_list(P(base_methods), methods)
    return {'name': name, 'super': cstr(P(sup)) if sup else None,
            'cls_va': hex(cls_va), 'ro_va': hex(ro_va), 'flags': hex(flags),
            'methods': methods}

def parse_category(cat_va):
    hdr = rd(cat_va, 32)
    if not hdr: return None
    isa, name_va, m_cls, m_meta = st.unpack('<QQQQ', hdr)
    nm = cstr(P(name_va))
    if not nm: return None
    ms, cms = [], []
    parse_method_list(P(m_cls), ms)
    parse_method_list(P(m_meta), cms)
    return {'name': nm, 'cls_methods': ms, 'meta_methods': cms}

def dump_list(secname):
    s = secs.get(secname)
    if not s: return []
    d = rd(s.virtual_address, s.size)
    if not d: return []
    n = s.size // 8
    ptrs = st.unpack(f'<{n}Q', d)
    return [P(p) for p in ptrs if P(p)]

classes = []
for cva in dump_list('__objc_classlist'):
    try:
        info = parse_class(cva)
        if info: classes.append(info)
    except Exception:
        pass

cats = []
for cva in dump_list('__objc_catlist'):
    try:
        info = parse_category(cva)
        if info: cats.append(info)
    except Exception:
        pass

print(f'[*] classes parsed: {len(classes)}   categories: {len(cats)}', file=sys.stderr)
json.dump(classes, open('objc_classes.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
json.dump(cats, open('objc_cats.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

KW = re.compile(r'GAD|AdMob|AdManager|AdLoader|AdView|Vungle|PAG|Pangle|BUC|CSJ|Moloco|InMobi|SKAd|Interstit|Banner|Rewarded|AppOpen|OpenAd|StoreKit|Purchas|IAP|Subscri|Paywall|RemoveAd|AdFree|Premium|BuyManager|RevenueCat|Advert|^Ad[A-Z]|ADS|ADS|Ads', re.I)

def match(c): return bool(KW.search(c.get('name') or ''))

sel_ads = [c for c in classes if match(c)]
cat_ads = [c for c in cats if match(c)]

lines = []
for c in sel_ads:
    lines.append(f"=== [CLASS] {c['name']} : {c['super']}  ({c['cls_va']}) ===")
    for m in c['methods']:
        lines.append('   - ' + m)
for c in cat_ads:
    lines.append(f"=== [CATEGORY] {c['name']} ===")
    for m in c['cls_methods']:
        lines.append('   - ' + m)

open('ad_classes.txt', 'w', encoding='utf-8').write('\n'.join(lines))
print(f'[*] ad-related: {len(sel_ads)} classes, {len(cat_ads)} categories', file=sys.stderr)
print('\n'.join(lines))
