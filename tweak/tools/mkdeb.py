#!/usr/bin/env python3
"""把 MobileSubstrate 目录树打成一个标准 .deb（纯 Python，不依赖 dpkg-deb / ar）。

debian-binary 结构：ar 容器内依次放
    debian-binary   ("2.0\n")
    control.tar.gz  含 ./control
    data.tar.gz     含 ./Library/... 等实际文件

用法:
    python3 mkdeb.py <stage 目录> <输出 .deb>
其中 <stage 目录> 下应有 DEBIAN/control 以及要安装的文件树。
"""
import io, os, sys, tarfile, time


def tar_gz_tree(base_dir: str, entries):
    """把 entries（相对 base_dir 的路径）打进 tar.gz，带 ./ 前缀、root:root、合理权限。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', format=tarfile.GNU_FORMAT) as tf:
        seen_dirs = set()
        for rel in entries:
            full = os.path.join(base_dir, rel)
            is_dir = os.path.isdir(full)
            parts = rel.split('/')
            # 补齐父目录：文件只补到 parts[:-1]，目录补到自身
            depth = len(parts) if is_dir else len(parts) - 1
            for i in range(1, depth + 1):
                d = '/'.join(parts[:i])
                if d and d not in seen_dirs:
                    seen_dirs.add(d)
                    ti = tarfile.TarInfo('./' + d)
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = 'root'
                    ti.mtime = int(time.time())
                    tf.addfile(ti)
            if not is_dir:
                ti = tarfile.TarInfo('./' + rel)
                ti.size = os.path.getsize(full)
                ti.mode = 0o644
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = 'root'
                ti.mtime = int(time.time())
                with open(full, 'rb') as f:
                    tf.addfile(ti, f)
    return buf.getvalue()


def ar_write(out_path: str, members):
    """members: [(name, bytes), ...] —— 写 ar(5) 归档"""
    with open(out_path, 'wb') as f:
        f.write(b'!<arch>\n')
        for name, data in members:
            assert len(name) <= 15, name
            hdr = (
                name.ljust(16)
                + str(int(time.time())).ljust(12)
                + '0'.ljust(6) + '0'.ljust(6)      # uid / gid
                + '100644'.ljust(8)                # mode
                + str(len(data)).ljust(10)
                + '`\n'
            ).encode('ascii')
            assert len(hdr) == 60, len(hdr)
            f.write(hdr)
            f.write(data)
            if len(data) % 2:
                f.write(b'\n')


def collect(root: str, sub: str, prefix: str):
    out = []
    base = os.path.join(root, sub)
    for dirpath, _dirs, files in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base).replace(os.sep, '/')
        for fn in files:
            rel = f'{prefix}/{fn}' if rel_dir == '.' else f'{prefix}/{rel_dir}/{fn}'
            out.append(rel)
    return out


def main():
    stage, out = sys.argv[1], sys.argv[2]
    control = os.path.join(stage, 'DEBIAN', 'control')
    if not os.path.isfile(control):
        sys.exit(f'[!] 缺少 {control}')

    control_tar = tar_gz_tree(os.path.join(stage, 'DEBIAN'), ['control'])
    data_entries = collect(stage, 'Library', 'Library')
    if not data_entries:
        sys.exit('[!] stage 里没有 Library/ 内容')
    data_tar = tar_gz_tree(stage, data_entries)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    ar_write(out, [('debian-binary', b'2.0\n'),
                   ('control.tar.gz', control_tar),
                   ('data.tar.gz', data_tar)])
    print(f'[+] wrote {out}  ({os.path.getsize(out)} bytes, {len(data_entries)} file(s))')


if __name__ == '__main__':
    main()
