"""Read-only schema dump for Hermes SQLite databases.

Usage: python dump_schema.py <db_path> [--rows] [--out out.md]
Never writes to the DB. Opens with mode=ro&immutable=1.
"""
import sqlite3, sys, json, pathlib, urllib.parse

def uri_for(path, immutable=True):
    p = pathlib.Path(path).resolve()
    s = p.as_posix()
    if not s.startswith('/'):
        s = '/' + s
    q = '?mode=ro&immutable=1' if immutable else '?mode=ro'
    return 'file://' + urllib.parse.quote(s) + q

def connect(path, immutable=True):
    con = sqlite3.connect(uri_for(path, immutable), uri=True)
    con.row_factory = sqlite3.Row
    return con

def dump(path, immutable=True, count_rows=True):
    con = connect(path, immutable)
    cur = con.cursor()
    info = {}
    info['pragma_journal'] = cur.execute('PRAGMA journal_mode').fetchone()[0]
    info['page_size'] = cur.execute('PRAGMA page_size').fetchone()[0]
    info['freelist_count'] = cur.execute('PRAGMA freelist_count').fetchone()[0]
    info['page_count'] = cur.execute('PRAGMA page_count').fetchone()[0]
    info['encoding'] = cur.execute('PRAGMA encoding').fetchone()[0]
    info['fk_enabled'] = cur.execute('PRAGMA foreign_keys').fetchone()[0]
    info['user_version'] = cur.execute('PRAGMA user_version').fetchone()[0]
    info['schema_version'] = cur.execute('PRAGMA schema_version').fetchone()[0]
    info['application_id'] = cur.execute('PRAGMA application_id').fetchone()[0]
    tables = []
    objs = cur.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type DESC, name").fetchall()
    names = [r['name'] for r in objs if r['type']=='table']
    info['views'] = [dict(r) for r in objs if r['type']=='view']
    info['triggers'] = [dict(r) for r in objs if r['type']=='trigger']
    for r in objs:
        if r['type'] != 'table':
            continue
        t = {'name': r['name'], 'sql': r['sql']}
        try:
            cols = cur.execute(f'PRAGMA table_info("{r["name"]}")').fetchall()
            t['columns'] = [{'cid':c['cid'],'name':c['name'],'type':c['type'],'notnull':c['notnull'],'dflt':c['dflt_value'],'pk':c['pk']} for c in cols]
        except Exception as e:
            t['columns'] = []
            t['colerr'] = str(e)
        try:
            t['indexes'] = [dict(x) for x in cur.execute(f'PRAGMA index_list("{r["name"]}")').fetchall()]
            idx_detail = {}
            for ix in t['indexes']:
                idx_detail[ix['name']] = [dict(y) for y in cur.execute(f'PRAGMA index_info("{ix["name"]}")').fetchall()]
            t['index_columns'] = idx_detail
        except Exception as e:
            t['indexes'] = []
            t['idxerr'] = str(e)
        try:
            t['fks'] = [dict(x) for x in cur.execute(f'PRAGMA foreign_key_list("{r["name"]}")').fetchall()]
        except Exception as e:
            t['fks'] = []
        if count_rows:
            try:
                t['rows'] = cur.execute(f'SELECT COUNT(*) FROM "{r["name"]}"').fetchone()[0]
            except Exception as e:
                t['rows'] = None
                t['rowerr'] = str(e)
        tables.append(t)
    info['tables'] = tables
    info['indexes_all'] = [dict(r) for r in cur.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name,name").fetchall()]
    con.close()
    return info

if __name__ == '__main__':
    path = sys.argv[1]
    immutable = '--rw-shm' not in sys.argv
    info = dump(path, immutable=immutable)
    out = sys.argv[sys.argv.index('--out')+1] if '--out' in sys.argv else None
    txt = json.dumps(info, ensure_ascii=False, indent=1, default=str)
    if out:
        pathlib.Path(out).write_text(txt, encoding='utf-8')
        print('wrote', out, len(txt))
    else:
        print(txt)
