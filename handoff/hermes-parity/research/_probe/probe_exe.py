import re

exe = r'E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\hermes-agent-cn-runtime-win32-x64.exe'
targets = [b'pre_tool_call', b'shell-hooks-allowlist', b'hooks_auto_accept', b'CONTEXT COMPACTION',
           b'max_iterations_reached', b'DEFAULT_MAX_ITERATIONS', b'prompt_builder']
hits = {t: 0 for t in targets}
tail = b''
with open(exe, 'rb') as f:
    while True:
        chunk = f.read(1 << 22)
        if not chunk:
            break
        buf = tail + chunk
        for t in targets:
            hits[t] += buf.count(t)
        tail = buf[-64:]
print({k.decode(): v for k, v in hits.items()})
