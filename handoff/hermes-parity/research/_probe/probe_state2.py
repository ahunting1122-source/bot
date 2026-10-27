import sqlite3

p = r'E:\Hermes Agent CN Desktop\data\hermes-home\state.db'
c = sqlite3.connect('file:' + p + '?mode=ro&immutable=1', uri=True)

print('=== messages DDL ===')
print(c.execute("select sql from sqlite_master where name='messages'").fetchone()[0])

print('\n=== row counts ===')
for t in ('messages', 'sessions', 'system_prompts', 'compression_locks', 'session_model_usage', 'session_turn_leases'):
    try:
        print(t, c.execute('select count(*) from ' + t).fetchone()[0])
    except Exception as e:
        print(t, 'ERR', e)

print('\n=== system_prompts sample ===')
for r in c.execute("select hash, length(prompt), substr(prompt,1,80) from system_prompts limit 3"):
    print(r)

print('\n=== compaction messages ===')
rows = c.execute(
    "select session_id, role, substr(content,1,160), length(content) from messages "
    "where content like '[CONTEXT COMPACTION%' or content like '[CONTEXT SUMMARY]:%' limit 3"
).fetchall()
for r in rows:
    print(r)

print('\n=== lineage: sessions with parent ===')
rows = c.execute(
    "select id, parent_session_id, end_reason, message_count, api_call_count, source "
    "from sessions where parent_session_id is not null limit 8"
).fetchall()
for r in rows:
    print(r)

print('\n=== end_reason distribution ===')
for r in c.execute("select end_reason, count(*) from sessions group by end_reason order by 2 desc limit 15"):
    print(r)
