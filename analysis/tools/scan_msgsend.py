#!/usr/bin/env python3
"""反汇编 App 自身方法里的“消息发送”目标：
解析相对方法列表拿到 IMP，再解码 ARM64 的 ADRP/ADD/LDR，
把引用的 __objc_selrefs / __objc_classrefs 槽位解析成 selector / 类名。
"""
import lief, struct as st, sys

BIN = 'SimpleQR'
b = lief.parse(BIN)
raw = open(BIN, 'rb').read()
segs = [(s.name, s.virtual_address, s.virtual_size, s.file_offset) for s in b.segments]
secs = {s.name: s for s in b.sections}


def va2off(va):
    for (n, a, sz, fo) in segs:
        if a <= va < a + sz:
            return int(fo + (va - a))


def rd(va, n):
    o = va2off(va)
    if o is None or o + n > len(raw):
        return None
    return raw[o:o + n]


def cstr(va, ml=220):
    if not va:
        return None
    o = va2off(va)
    if o is None:
        return None
    e = raw.find(b'\x00', o)
    if e == -1 or e - o > ml:
        return None
    return raw[o:e].decode('utf-8', 'ignore')


def P(v):
    return v & 0x7FFFFFFFFFF


def PA(v):
    return (v & 0x7FFFFFFFFFF) & ~0x7


def u64(va):
    d = rd(va, 8)
    return st.unpack('<Q', d)[0] if d else 0


# ---- 索引：selref 槽 -> selector 名；classref 槽 -> 类名 ----
sel_map, cls_map = {}, {}
sr = secs['__objc_selrefs']
for i in range(sr.size // 8):
    va = sr.virtual_address + i * 8
    sel_map[va] = cstr(P(u64(va)))
cr = secs['__objc_classrefs']
for i in range(cr.size // 8):
    va = cr.virtual_address + i * 8
    tgt = PA(u64(va))
    ro = rd(tgt + 32, 8) or b'\x00' * 8
    ro_va = PA(st.unpack('<Q', ro)[0])
    nm = None
    if ro_va:
        d = rd(ro_va + 24, 8)
        if d:
            nm = cstr(P(st.unpack('<Q', d)[0]))
    cls_map[va] = nm
print(f'[*] selrefs={len(sel_map)} classrefs={len(cls_map)}', file=sys.stderr)


def methods_of_class(cls_va):
    """返回 [(sel, imp_va), ...]"""
    hdr = rd(cls_va, 40)
    isa, sup, cache, vt, classro = st.unpack('<QQQQQ', hdr)
    ro = rd(PA(classro), 72)
    (fl, iS, iSz, r, ivl, nv, bm, bp, ivars, wiv, bpr) = st.unpack('<IIIIQQQQQQQ', ro)
    out = []
    ml = P(bm)
    if not ml:
        return out
    es, cnt = st.unpack('<II', rd(ml, 8))
    if not cnt or cnt > 5000:
        return out
    rel = bool(es & 0x80000000)
    es &= 0x7FFFFFFF
    for i in range(cnt):
        eva = ml + 8 + i * es
        if rel:
            no, to, io = st.unpack('<iii', rd(eva, 12))
            sel = cstr(P(u64(eva + no)))
            out.append((sel, eva + 8 + io))
        else:
            nv2, tv2, iv2 = st.unpack('<QQQ', rd(eva, 24))
            out.append((cstr(P(nv2)), P(iv2)))
    return out


def find_class(name):
    cl = secs['__objc_classlist']
    d = rd(cl.virtual_address, cl.size)
    for p in st.unpack(f'<{cl.size // 8}Q', d):
        cva = P(p)
        ro = rd(PA(st.unpack('<Q', rd(cva + 32, 8))[0]), 72)
        if not ro:
            continue
        nm = cstr(P(st.unpack('<Q', ro[24:32])[0]))
        if nm == name:
            return cva


def scan(imp_va, end_va):
    """解码 ADRP/ADD/LDR，产出该函数内引用的 selector / 类。
    end_va = 下一个方法的 IMP（或 imp+0x400），遇到 ret 立即停止。"""
    page = {}
    hits = []
    i = 0
    pc = imp_va
    while pc < end_va and i < 400:
        d = rd(pc, 4)
        if not d:
            break
        w = st.unpack('<I', d)[0]
        i += 1
        if w == 0xD65F03C0:                       # ret
            break
        # ADRP
        if (w & 0x9F000000) == 0x90000000:
            immlo = (w >> 29) & 3
            immhi = (w >> 5) & 0x7FFFF
            imm = (immhi << 2) | immlo
            if imm & (1 << 20):
                imm -= 1 << 21
            page[w & 0x1F] = ((pc & ~0xFFF) + (imm << 12)) & 0xFFFFFFFFFFFF
        # ADD imm (64-bit)
        elif (w & 0xFF800000) == 0x91000000:
            rd_n, rn = w & 0x1F, (w >> 5) & 0x1F
            if rn in page and not ((w >> 22) & 1):
                page[rd_n] = page[rn] + ((w >> 10) & 0xFFF)
            else:
                page.pop(rd_n, None)
        # LDR x, [x, #uimm]  -> 解引用
        elif (w & 0xFFC00000) == 0xF9400000:
            rn, rt = (w >> 5) & 0x1F, w & 0x1F
            if rn in page:
                tgt = page[rn] + ((w >> 10) & 0xFFF) * 8
                if tgt in sel_map and sel_map[tgt]:
                    hits.append(('sel', sel_map[tgt]))
                elif tgt in cls_map and cls_map[tgt]:
                    hits.append(('cls', cls_map[tgt]))
            page.pop(rt, None)
        elif (w & 0x3B000000) == 0x39000000:      # 其它访存：粗粒度失效
            pass
        else:
            # 非以上形式：保守地把可能的写目标寄存器失效
            pass
        pc += 4
    # 去重保序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


targets = sys.argv[1:] or [
    '_TtC8SimpleQR24CodeDetailViewController',
    '_TtC8SimpleQR25HistoryListViewController',
    '_TtC8SimpleQR11AppDelegate',
]
for cname in targets:
    cva = find_class(cname)
    if not cva:
        print(f'!! class not found: {cname}')
        continue
    print(f'\n############ {cname} ############')
    ms = [(s, i) for (s, i) in methods_of_class(cva) if s]
    imps = sorted({i for _, i in ms})
    for sel, imp in ms:
        if not any(k in sel for k in ('reloadBanner', 'viewDidLoad', 'viewWillAppear',
                                      'didFinishLaunching', 'reload', 'Ad', 'ad')):
            continue
        nxt = [x for x in imps if x > imp]
        end = nxt[0] if nxt else imp + 0x400
        print(f'\n--- -[{cname} {sel}]  imp={hex(imp)} .. {hex(end)} ---')
        for kind, name in scan(imp, end):
            print(f'    {kind:4s} {name}')
