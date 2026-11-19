"""设备身份台账存储：独立 SQLite 表、单事务状态切换、追加式审计日志。

与 runs 表物理隔离：台账操作从不修改、删除任何批次结果或原始编号数据。
所有状态变更在一个事务内完成，失败整体回滚，保证不出现半合并状态。
"""
import json
import sqlite3

from app.errors import BusinessError
from app.modules.identity import rules


SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_links(
  id TEXT PRIMARY KEY,
  old_code TEXT NOT NULL,
  new_code TEXT NOT NULL,
  physical_id TEXT NOT NULL,
  status TEXT NOT NULL,
  replaced_at TEXT NOT NULL,
  location TEXT NOT NULL,
  asset_no_old TEXT NOT NULL DEFAULT '',
  asset_no_new TEXT NOT NULL DEFAULT '',
  documents TEXT NOT NULL DEFAULT '{}',
  notes TEXT NOT NULL DEFAULT '',
  location_exclusive_note TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(old_code,new_code)
);
CREATE TABLE IF NOT EXISTS identity_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id TEXT NOT NULL,
  action TEXT NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_chains(
  link_id TEXT PRIMARY KEY,
  physical_id TEXT NOT NULL,
  status TEXT NOT NULL,
  traceable INTEGER NOT NULL,
  digest TEXT NOT NULL,
  payload TEXT NOT NULL,
  rebuilt_at TEXT NOT NULL
);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(SCHEMA)
    return con


class RunHistory:
    """从批次表读取运行结果，供台账跨编号汇总。只读，绝不回写批次。"""

    def __init__(self, con):
        self._con = con

    def runs(self):
        rows = self._con.execute('SELECT id,result FROM runs ORDER BY rowid').fetchall()
        for row in rows:
            yield row['id'], json.loads(row['result'])

    def span(self, code):
        starts, ends = [], []
        for _, state in self.runs():
            for union in state.get('event_unions', []):
                if union.get('device') == code:
                    starts.append(union['start'])
                    ends.append(union['end'])
        if not starts:
            return None
        return min(starts), max(ends)


def _row_to_link(row):
    link = dict(row)
    link['documents'] = json.loads(row['documents'])
    return link


def list_links(path):
    with connect(path) as con:
        return [_row_to_link(r) for r in con.execute('SELECT * FROM identity_links ORDER BY rowid')]


def get_link(path, link_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '换型链路不存在', 404)
    return _row_to_link(row)


def _audit(con, link, action, from_status, to_status, actor, reason):
    con.execute(
        'INSERT INTO identity_events(link_id,action,from_status,to_status,actor,reason,snapshot,at) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (link['id'], action, from_status, to_status, actor, reason,
         json.dumps(link, ensure_ascii=False, sort_keys=True), rules.utc_now()),
    )


def _rebuild_chain(con, link):
    """只重算当前链路所属物理身份，其他链路不受影响。"""
    history = RunHistory(con)
    chain = rules.build_chain(link, history, confirmed=link['status'] == rules.CONFIRMED)
    con.execute(
        'INSERT INTO identity_chains(link_id,physical_id,status,traceable,digest,payload,rebuilt_at) '
        'VALUES(?,?,?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET status=excluded.status,'
        'traceable=excluded.traceable,digest=excluded.digest,payload=excluded.payload,rebuilt_at=excluded.rebuilt_at',
        (link['id'], link['physical_id'], link['status'], 1 if chain['traceable'] else 0,
         chain['digest'], json.dumps(chain, ensure_ascii=False), rules.utc_now()),
    )
    return chain


def register(path, payload, known_devices, actor='值班员'):
    normalized, missing = rules.register_payload(payload, known_devices)
    lid = rules.link_id(normalized['old_code'], normalized['new_code'])
    with connect(path) as con:
        existing = con.execute('SELECT * FROM identity_links WHERE id=?', (lid,)).fetchone()
        if existing:
            # 幂等重试：原样返回既有链路，不新增身份、不覆盖原编号、不写第二条审计。
            return _row_to_link(existing), False
        link = {
            'id': lid,
            'old_code': normalized['old_code'],
            'new_code': normalized['new_code'],
            'physical_id': rules.physical_id([normalized['old_code'], normalized['new_code']]),
            'status': rules.PENDING,
            'replaced_at': normalized['replaced_at'],
            'location': normalized['location'],
            'asset_no_old': normalized['asset_no_old'],
            'asset_no_new': normalized['asset_no_new'],
            'documents': json.dumps(normalized['documents'], ensure_ascii=False),
            'notes': normalized['notes'],
            'location_exclusive_note': '',
            'revision': 1,
            'created_at': rules.utc_now(),
            'updated_at': rules.utc_now(),
        }
        con.execute(
            'INSERT INTO identity_links(id,old_code,new_code,physical_id,status,replaced_at,location,'
            'asset_no_old,asset_no_new,documents,notes,location_exclusive_note,revision,created_at,updated_at) '
            'VALUES(:id,:old_code,:new_code,:physical_id,:status,:replaced_at,:location,:asset_no_old,'
            ':asset_no_new,:documents,:notes,:location_exclusive_note,:revision,:created_at,:updated_at)',
            link,
        )
        stored = _row_to_link(con.execute('SELECT * FROM identity_links WHERE id=?', (lid,)).fetchone())
        reason = '登记换型链路，凭证待确认' + ('（缺：' + '、'.join(missing) + '）' if missing else '')
        _audit(con, stored, 'REGISTER', '', rules.PENDING, actor, reason)
        _rebuild_chain(con, stored)
        return stored, True


def _confirmed_holder(con, code, exclude_link):
    row = con.execute(
        "SELECT physical_id FROM identity_links WHERE status=? AND id<>? AND (old_code=? OR new_code=?)",
        (rules.CONFIRMED, exclude_link, code, code),
    ).fetchone()
    return row['physical_id'] if row else None


def confirm(path, link_id, payload, known_devices, actor='审计员'):
    revision = payload.get('revision')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    with connect(path) as con:
        row = con.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '换型链路不存在', 404)
        link = _row_to_link(row)
        if link['revision'] != revision:
            raise BusinessError('STALE_VERSION', '链路版本已更新，请刷新后重试', 409)
        if link['status'] == rules.CONFIRMED:
            # 重试不新增身份：确认是幂等动作，直接返回当前状态。
            return link, [], False
        if link['status'] not in (rules.PENDING, rules.WITHDRAWN):
            raise BusinessError('BAD_TRANSITION', f'{link["status"]}链路不能确认', 422)
        previous_status = link['status']

        evidence = dict(link['documents'])
        incoming = payload.get('evidence') or {}
        if not isinstance(incoming, dict):
            raise BusinessError('BAD_EVIDENCE', '凭证须为对象')
        # 兼容平铺凭证号与嵌套 documents 两种提交方式。
        nested = incoming.get('documents')
        if isinstance(nested, dict):
            evidence.update({k: v for k, v in nested.items() if v not in (None, '')})
        for key in rules.REQUIRED_DOCS:
            if incoming.get(key) not in (None, ''):
                evidence[key] = incoming[key]
        evidence.update({k: v for k, v in incoming.items()
                         if k not in rules.REQUIRED_DOCS and k != 'documents' and v not in (None, '')})
        merged = {**link, 'documents': evidence,
                  'location_exclusive_note': str(incoming.get('location_exclusive_note',
                                                               link['location_exclusive_note'])).strip(),
                  'asset_no_old': str(incoming.get('asset_no_old', link['asset_no_old'])).strip(),
                  'asset_no_new': str(incoming.get('asset_no_new', link['asset_no_new'])).strip()}
        same_location = sorted(code for code, meta in known_devices.items()
                               if meta.get('zone') == _zone_of(known_devices, link))
        history = RunHistory(con)
        failures = rules.confirm_checks(
            merged, {'documents': evidence},
            members_of_code=lambda code, exclude_link: _confirmed_holder(con, code, exclude_link),
            same_location_devices=same_location, history=history,
        )
        if failures:
            # 校验失败：不更新状态、不产生半合并，仅登记一次驳回审计并回滚其余改动。
            _audit(con, link, 'CONFIRM_REJECTED', previous_status, previous_status, actor,
                   '；'.join(f"{f['code']}:{f['detail']}" for f in failures))
            con.commit()
            return link, failures, False

        now = rules.utc_now()
        con.execute(
            'UPDATE identity_links SET status=?,documents=?,asset_no_old=?,asset_no_new=?,'
            'location_exclusive_note=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
            (rules.CONFIRMED, json.dumps(evidence, ensure_ascii=False), merged['asset_no_old'],
             merged['asset_no_new'], merged['location_exclusive_note'], now, link_id, revision),
        )
        updated = _row_to_link(con.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone())
        _audit(con, updated, 'CONFIRM', previous_status, rules.CONFIRMED, actor,
               '凭证齐全，审计确认同一物理身份' + ('（撤回后重新确认）' if previous_status == rules.WITHDRAWN else ''))
        chain = _rebuild_chain(con, updated)
        return updated, [], chain


def _zone_of(known_devices, link):
    return known_devices.get(link['old_code'], {}).get('zone')


def withdraw(path, link_id, payload, actor='审计员'):
    revision = payload.get('revision')
    if not isinstance(revision, int) or revision < 1:
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    reason = str(payload.get('reason', '')).strip()
    if not reason:
        raise BusinessError('BAD_EVIDENCE', '撤回必须注明原因')
    with connect(path) as con:
        row = con.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '换型链路不存在', 404)
        link = _row_to_link(row)
        if link['revision'] != revision:
            raise BusinessError('STALE_VERSION', '链路版本已更新，请刷新后重试', 409)
        if link['status'] not in (rules.CONFIRMED, rules.PENDING):
            raise BusinessError('BAD_TRANSITION', f'{link["status"]}链路不能撤回', 422)
        previous = link['status']
        now = rules.utc_now()
        con.execute(
            'UPDATE identity_links SET status=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
            (rules.WITHDRAWN, now, link_id, revision),
        )
        updated = _row_to_link(con.execute('SELECT * FROM identity_links WHERE id=?', (link_id,)).fetchone())
        _audit(con, updated, 'WITHDRAW', previous, rules.WITHDRAWN, actor, reason)
        # 仅重算该物理身份：撤回后两段履历立即拆回独立，不影响其他身份。
        chain = _rebuild_chain(con, updated)
        return updated, chain


def chain(path, link_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM identity_chains WHERE link_id=?', (link_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '链路视图尚未生成', 404)
        return json.loads(row['payload'])


def events(path, link_id):
    with connect(path) as con:
        if not con.execute('SELECT 1 FROM identity_links WHERE id=?', (link_id,)).fetchone():
            raise BusinessError('NOT_FOUND', '换型链路不存在', 404)
        return [dict(r) for r in con.execute(
            'SELECT seq,link_id,action,from_status,to_status,actor,reason,at FROM identity_events '
            'WHERE link_id=? ORDER BY seq', (link_id,))]


def all_chains(path):
    with connect(path) as con:
        rows = con.execute('SELECT link_id,payload FROM identity_chains ORDER BY link_id').fetchall()
        return [json.loads(r['payload']) for r in rows]
