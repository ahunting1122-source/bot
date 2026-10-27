import sqlite3, json, sys

p = r'E:\Hermes Agent CN Desktop\data\hermes-home\state.db'
c = sqlite3.connect('file:' + p + '?mode=ro&immutable=1', uri=True)
rows = c.execute("select type,name from sqlite_master where type in ('table','view') order by type,name").fetchall()
print('OBJECT COUNT', len(rows))
print('TABLES:', [n for t, n in rows if t == 'table'])
print('VIEWS:', [n for t, n in rows if t == 'view'])
for name in ('system_prompts', 'compression_locks', 'async_delegations', 'sessions'):
    try:
        sql = c.execute("select sql from sqlite_master where name=?", (name,)).fetchone()
        print('\n=== DDL', name, '===')
        print(sql[0] if sql else 'MISSING')
    except Exception as e:
        print(name, 'ERR', e)
