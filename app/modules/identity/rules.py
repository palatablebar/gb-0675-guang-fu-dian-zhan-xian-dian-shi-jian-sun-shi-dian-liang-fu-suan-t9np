"""设备身份台账领域规则。

裁定：待证后并。值班可先登记“待确认”换型链路，但两个编号在凭证齐全并通过
审计校验前保持独立物理身份，履历不打通、损失不汇总；校验通过后在单个数据库
事务内原子切换为同一物理身份，任何失败都回到切换前状态，不产生半合并。
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from app.errors import BusinessError

# 链路状态机：登记 -> 确认 / 驳回；确认 -> 撤回。不存在中间态。
PENDING = '待确认'
CONFIRMED = '已确认'
REJECTED = '已驳回'
WITHDRAWN = '已撤回'

# 审计要求的最小凭证集。
REQUIRED_DOCS = ('work_order', 'asset_record', 'replacement_photo', 'acceptance_form')
DOC_LABELS = {
    'work_order': '换型工作票',
    'asset_record': '资产台账记录',
    'replacement_photo': '更换现场照片',
    'acceptance_form': '交接验收单',
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value, field):
    if not isinstance(value, str) or not value.strip():
        raise BusinessError('BAD_EVIDENCE', f'{field}时间缺失或格式无效')
    text = value.strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise BusinessError('BAD_EVIDENCE', f'{field}时间格式无效')
    if dt.tzinfo is None:
        raise BusinessError('BAD_EVIDENCE', f'{field}时间必须带时区偏移')
    return dt


def link_id(old_code, new_code):
    # 确定性编号：同一对编号重复登记/重试不会产生第二条链路。
    return 'LNK-' + sha256(f'{old_code}>{new_code}'.encode()).hexdigest()[:12]


def physical_id(codes):
    return 'PHY-' + sha256('|'.join(sorted(codes)).encode()).hexdigest()[:12]


def _doc_status(evidence):
    docs = evidence.get('documents') or {}
    if not isinstance(docs, dict):
        raise BusinessError('BAD_EVIDENCE', '凭证须为对象')
    missing = [DOC_LABELS[k] for k in REQUIRED_DOCS if not str(docs.get(k, '')).strip()]
    return docs, missing


def register_payload(payload, known_devices):
    """校验登记请求，返回规范化字段。不做任何写入。"""
    old_code = str(payload.get('old_code', '')).strip()
    new_code = str(payload.get('new_code', '')).strip()
    if not old_code or not new_code:
        raise BusinessError('BAD_EVIDENCE', '必须提供新旧两个设备编号')
    if old_code == new_code:
        raise BusinessError('BAD_EVIDENCE', '新旧编号不能相同')
    for code in (old_code, new_code):
        if code not in known_devices:
            raise BusinessError('UNKNOWN_DEVICE', f'设备编号 {code} 不在设备清册')
    evidence = payload.get('evidence') or {}
    if not isinstance(evidence, dict):
        raise BusinessError('BAD_EVIDENCE', '凭证须为对象')
    docs, missing = _doc_status(evidence)
    replaced_at = parse_dt(evidence.get('replaced_at'), '更换')
    location = str(evidence.get('location', '')).strip()
    if not location:
        raise BusinessError('BAD_EVIDENCE', '安装位置缺失，无法核验是否相邻设备')
    asset_old = str(evidence.get('asset_no_old', '')).strip()
    asset_new = str(evidence.get('asset_no_new', '')).strip()
    notes = str(evidence.get('notes', '')).strip()
    normalized = {
        'old_code': old_code,
        'new_code': new_code,
        'replaced_at': replaced_at.isoformat(),
        'location': location,
        'asset_no_old': asset_old,
        'asset_no_new': asset_new,
        'documents': docs,
        'notes': notes,
    }
    return normalized, missing


def confirm_checks(link, evidence, members_of_code, same_location_devices, history):
    """审计校验。全部通过才允许确认；任一不过返回失败原因，链路保持待确认。"""
    failures = []
    _, missing = _doc_status(evidence)
    if missing:
        failures.append({'code': 'DOC_INCOMPLETE', 'detail': '凭证不齐全：' + '、'.join(missing)})

    asset_old = str(link.get('asset_no_old', '')).strip()
    asset_new = str(link.get('asset_no_new', '')).strip()
    if not asset_old or not asset_new:
        failures.append({'code': 'ASSET_GAP', 'detail': '缺少新旧资产编号，无法证明同一物理身份'})
    elif asset_old != asset_new:
        failures.append({'code': 'ASSET_MISMATCH', 'detail': '资产编号不一致，可能是两台相邻设备'})

    # 更换时刻在登记时已固定，凭证阶段只做合法性复核。
    parse_dt(link['replaced_at'], '更换')
    # 同位置其他设备是“相邻误并”高风险对象：要求凭证显式给出排他说明。
    neighbors = sorted(d for d in same_location_devices if d not in (link['old_code'], link['new_code']))
    if neighbors and not str(link.get('location_exclusive_note', '')).strip():
        failures.append({'code': 'NEIGHBOR_RISK',
                         'detail': f'同位置存在相邻设备 {("、".join(neighbors))}，缺少排他性安装位置说明'})

    # 两段运行记录的时间窗不得重叠：同一物理设备同一时刻只能有一个编号在运行。
    old_span = history.span(link['old_code'])
    new_span = history.span(link['new_code'])
    if old_span and new_span and old_span[1] > new_span[0]:
        failures.append({'code': 'OVERLAP_RUNTIME',
                         'detail': '新旧编号运行时段重叠，不可能是同一台设备的连续履历'})

    # 一个编号同一时刻只能归属一条已确认链路，防止共用履历。
    for code in (link['old_code'], link['new_code']):
        holder = members_of_code(code, exclude_link=link['id'])
        if holder:
            failures.append({'code': 'CODE_BOUND', 'detail': f'编号 {code} 已归属物理身份 {holder}'})
    return failures


# ---- 链路重算（只重算受影响物理身份，其他身份不动） ----

def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def code_summary(history, code):
    """汇总单个编号在所有批次中的限电事件、基线依据与损失。"""
    events, references, loss_parts = [], set(), []
    loss_total = Decimal('0')
    unknown_seconds = 0
    for run_id, state in history.runs():
        for union in state.get('event_unions', []):
            if union.get('device') == code:
                events.append({'run_id': run_id, **union})
        for row in state.get('energy_rows', []):
            if row.get('device') != code:
                continue
            for ref in row.get('reference_ids', []) or []:
                references.add(ref)
            if row.get('loss_kwh') is None:
                unknown_seconds += int(row.get('seconds', 0))
            else:
                loss_total += _decimal(row['loss_kwh']) or Decimal('0')
                loss_parts.append({'run_id': run_id, 'segment': row['start'],
                                   'loss_kwh': row['loss_kwh'], 'sample_id': row.get('sample_id', '')})
    return {
        'code': code,
        'events': sorted(events, key=lambda r: (r['start'], r['run_id'])),
        'reference_ids': sorted(references),
        'loss_kwh': str(loss_total.quantize(Decimal('0.0001'))),
        'loss_parts': loss_parts,
        'unknown_seconds': unknown_seconds,
    }


def build_chain(link, history, confirmed):
    """构造一条链路对应物理身份的连续视图。

    confirmed=False 时只返回两段独立履历并显式标注不可连续追溯；
    confirmed=True 时按更换时刻拼接，跨编号事件/基线/损失可连续查看。
    """
    segments = [code_summary(history, link['old_code']), code_summary(history, link['new_code'])]
    traceable = bool(confirmed)
    joined_loss = None
    if traceable:
        joined_loss = str(
            sum((_decimal(s['loss_kwh']) for s in segments), Decimal('0')).quantize(Decimal('0.0001'))
        )
    chain = {
        'link_id': link['id'],
        'physical_id': link['physical_id'],
        'status': link['status'],
        'traceable': traceable,
        'continuity': '可连续追溯' if traceable else '不可连续追溯：凭证待确认，两段履历保持独立',
        'replaced_at': link['replaced_at'],
        'location': link['location'],
        'segments': segments,
        'joined_loss_kwh': joined_loss,
        'event_count': sum(len(s['events']) for s in segments),
        'unknown_seconds': sum(s['unknown_seconds'] for s in segments),
        'pending_basis': [] if traceable else [DOC_LABELS[k] for k in REQUIRED_DOCS
                                               if not str((link['documents'] or {}).get(k, '')).strip()],
    }
    chain['digest'] = sha256(json.dumps(chain, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return chain


def apply(state):
    """引擎内占位：身份台账是独立持久化域，不改变批次计算结果。

    批次视图仅汇总本批次中各编号的限电片段，真正的跨编号连续视图由
    app.identity_db 在独立表中维护，确保批次不可变、编号履历不被覆盖。
    """
    per_code = {}
    for row in state.get('energy_rows', []):
        bucket = per_code.setdefault(row['device'], {'device': row['device'], 'segments': 0,
                                                      'unknown': 0, 'loss_kwh': Decimal('0')})
        bucket['segments'] += 1
        if row.get('loss_kwh') is None:
            bucket['unknown'] += 1
        else:
            bucket['loss_kwh'] += _decimal(row['loss_kwh']) or Decimal('0')
    rows = []
    for code in sorted(per_code):
        bucket = per_code[code]
        rows.append({'device': code, 'segments': bucket['segments'], 'unknown': bucket['unknown'],
                     'loss_kwh': str(bucket['loss_kwh'].quantize(Decimal('0.0001')))})
    state['views']['identity'] = {
        'title': '身份台账（批次视角）', 'rows': rows,
        'metrics': {'涉及编号': len(rows), '说明': '跨编号连续追溯见身份台账页'},
    }
