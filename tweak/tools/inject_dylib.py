#!/usr/bin/env python3
"""把一个 dylib 注入到 .app 里：拷贝 dylib + 给主程序加 LC_LOAD_DYLIB，并做完整性校验。

用 LIEF 直接改 Mach-O 的 load command（插进 header 与第一个 section 之间的空隙），
不搬运任何已有数据 —— 已实测：段表/文件偏移/代码区字节全部保持不变。

用法:
    python3 inject_dylib.py <app 目录> <dylib 路径> [install_name]

install_name 默认 @executable_path/<dylib 文件名>。
校验失败会以非 0 退出。
"""
import os
import shutil
import struct
import sys

import lief


def find_executable(app_dir: str) -> str:
    exe = None
    plist = os.path.join(app_dir, 'Info.plist')
    if os.path.exists(plist):
        try:
            with open(plist, 'rb') as f:
                head = f.read(8)
            if head.startswith(b'<?xml') or head.startswith(b'bplist'):
                import plistlib
                with open(plist, 'rb') as f:
                    exe = plistlib.load(f).get('CFBundleExecutable')
        except Exception:
            exe = None
    if not exe:
        # 退而求其次：找和 app 同名的可执行文件
        cand = os.path.join(app_dir, os.path.basename(app_dir)[:-4])
        exe = os.path.basename(cand) if os.path.exists(cand) else None
    if not exe:
        sys.exit('[!] 无法确定主程序名（Info.plist 里没有 CFBundleExecutable）')
    return os.path.join(app_dir, exe)


def snapshot(binary, path):
    """记录关键布局，用于注入前后比对。"""
    with open(path, 'rb') as f:
        raw = f.read()
    first_sec = min((s.offset for s in binary.sections if s.offset > 0), default=0)
    return {
        # 段：地址与文件偏移必须一致；file_size 单列（__LINKEDIT 会因丢弃旧签名而缩小）
        'seg_layout': [(s.name, s.virtual_address, s.file_offset) for s in binary.segments],
        'seg_sizes': {s.name: s.file_size for s in binary.segments},
        'sections': [(s.name, s.virtual_address, s.size, s.offset) for s in binary.sections],
        'sizeofcmds': binary.header.sizeof_cmds,
        'ncmds': binary.header.nb_cmds,
        'first_section_offset': first_sec,
        'raw': raw,
    }


def check_dylib(path: str) -> None:
    """确认待注入的是 arm64 iOS dylib（避免把坏文件/错架构注进 App）。"""
    with open(path, 'rb') as f:
        head = f.read(32)
    if len(head) < 32:
        sys.exit('[!] dylib 文件不完整')
    be = struct.unpack('>I', head[:4])[0]      # fat 头是大端
    le = struct.unpack('<I', head[:4])[0]      # 普通 Mach-O 小端存储
    if be in (0xCAFEBABE, 0xCAFEBABF):
        sys.exit('[!] dylib 是 fat 二进制，请先 thin 成 arm64 单架构再注入')
    if le not in (0xFEEDFACF, 0xFEEDFACE):
        sys.exit(f'[!] dylib 不是 Mach-O (magic={be:#x}/{le:#x})')
    cputype, _cpusub, filetype, _n, _s, _fl = struct.unpack('<IiiIII', head[4:28])
    if cputype != 0x0100000C:
        sys.exit(f'[!] dylib 不是 arm64 (cputype={cputype:#x})')
    if filetype != 6:   # MH_DYLIB
        sys.exit(f'[!] 不是动态库 (filetype={filetype}, 期望 6=MH_DYLIB)')
    print(f'     dylib 校验通过: arm64 MH_DYLIB, {os.path.getsize(path)} bytes')


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    app_dir, dylib = sys.argv[1], sys.argv[2]
    dylib = os.path.abspath(dylib)
    if not os.path.isdir(app_dir):
        sys.exit(f'[!] 不是目录: {app_dir}')
    if not os.path.isfile(dylib):
        sys.exit(f'[!] 找不到 dylib: {dylib}')
    check_dylib(dylib)

    exe_path = find_executable(app_dir)
    install_name = sys.argv[3] if len(sys.argv) > 3 else '@executable_path/' + os.path.basename(dylib)

    # 1) 拷贝 dylib 进 bundle
    dest = os.path.join(app_dir, os.path.basename(dylib))
    if os.path.abspath(dest) != dylib:
        shutil.copy2(dylib, dest)
    print(f'[1/4] dylib -> {os.path.basename(dest)}')

    # 2) 注入 load command
    bin_before = lief.parse(exe_path)
    if bin_before is None:
        sys.exit('[!] LIEF 无法解析主程序')
    before = snapshot(bin_before, exe_path)

    if any(l.name == install_name for l in bin_before.libraries):
        print(f'[2/4] 已经注入过 {install_name}，跳过')
        return

    added = bin_before.add_library(install_name)
    if added is None:
        sys.exit('[!] add_library 失败')
    print(f'[2/4] 插入 {added.name}  (cmd 大小 {added.size}, 偏移 {hex(added.command_offset)} )')

    # 3) 写回
    out = exe_path + '.patched'
    bin_before.write(out)
    os.replace(out, exe_path)
    print('[3/4] 已写回主程序')

    # 4) 校验
    bin_after = lief.parse(exe_path)
    after = snapshot(bin_after, exe_path)
    ok = True

    if not any(l.name == install_name for l in bin_after.libraries):
        print('[!] 校验失败：LC_LOAD_DYLIB 不存在'); ok = False
    if after['seg_layout'] != before['seg_layout']:
        print('[!] 校验失败：段的地址/文件偏移发生变化'); ok = False
    if after['sections'] != before['sections']:
        print('[!] 校验失败：section 表发生变化'); ok = False
    if after['sizeofcmds'] > after['first_section_offset']:
        print('[!] 校验失败：load commands 越界，覆盖了第一个 section'); ok = False
    size_changed = {k for k in before['seg_sizes']
                    if before['seg_sizes'][k] != after['seg_sizes'].get(k)}
    if size_changed - {'__LINKEDIT'}:
        print(f'[!] 校验失败：非 __LINKEDIT 段的大小变了: {size_changed}'); ok = False
    if '__LINKEDIT' in size_changed:
        print('      注: __LINKEDIT 缩小 (旧代码签名被移除)，稍后需用 ldid 重签')

    # 代码区（load commands 之后 ~ __TEXT 末尾）必须逐字节相同
    start = (after['sizeofcmds'] + 0xFFF) & ~0xFFF
    end = min(len(before['raw']), len(after['raw']), before['first_section_offset'] + 0x1000000)
    # __TEXT 结束位置 = 第二个段之前的最大范围；直接用段表算
    text = next((s for s in bin_after.segments if s.name == '__TEXT'), None)
    if text and text.file_size and text.file_offset == 0:
        end = min(end, text.file_size)
    same = before['raw'][start:end] == after['raw'][start:end]
    print(f"[4/4] 代码区 [{hex(start)}, {hex(end)}) 逐字节一致: {same}")
    if not same:
        print('[!] 校验失败：代码区被改动'); ok = False

    print(f"      段数 {len(before['seg_layout'])}->{len(after['seg_layout'])}  "
          f"section {len(before['sections'])}->{len(after['sections'])}  "
          f"ncmds {before['ncmds']}->{after['ncmds']}")
    if not ok:
        sys.exit('[!] 注入校验未通过')
    print(f'[+] 注入完成: {install_name}')


if __name__ == '__main__':
    main()
