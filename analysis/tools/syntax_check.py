import re, sys

src = open(sys.argv[1], encoding='utf-8').read()
s = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
s = re.sub(r'//[^\n]*', '', s)
s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)

stack, ok = [], True
pairs = {')': '(', ']': '[', '}': '{'}
for i, ch in enumerate(s):
    if ch in '([{':
        stack.append((ch, i))
    elif ch in ')]}':
        if not stack or stack[-1][0] != pairs[ch]:
            print(f'MISMATCH: unexpected {ch!r} at line {s[:i].count(chr(10))+1}')
            ok = False
            break
        stack.pop()
if ok:
    print('balanced OK' if not stack else f'UNCLOSED: {[c for c,_ in stack]}')

print('hook call sites:', len(re.findall(r'AKSwizzle(?:Class|Instance)Method\(', src)))
print('IMP functions :', len(re.findall(r'^static\s+[\w\s\*]+AK\w+\(', src, flags=re.M)))
print('lines:', src.count('\n') + 1)
