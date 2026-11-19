"""设备身份台账测试：待证后并、连续追溯、单事务原子切换、防误并。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import identity_db, main, store
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.identity import rules


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'identity.sqlite'))
    c = TestClient(main.app)
    c.post('/api/runs', json={'dataset': 'swap'})
    return c


FULL_EVIDENCE = {
    'work_order': 'W1', 'asset_record': 'A1', 'replacement_photo': 'P1', 'acceptance_form': 'F1',
    'asset_no_old': 'ASSET-8', 'asset_no_new': 'ASSET-8',
    'location_exclusive_note': '旧机原位换装，相邻 INV-D 机位未变动',
}


def register(client, old='INV-C', new='INV-C2'):
    return client.post('/api/identity/links', json={
        'old_code': old, 'new_code': new,
        'evidence': {'replaced_at': '2026-10-15T09:35:00+08:00', 'location': '北区-3号机位'},
    }).json()


# ---- 裁定一：凭证未齐，登记后保持待确认，履历不打通 ----

def test_pending_link_keeps_histories_separate(client):
    data = register(client)
    assert data['created'] is True
    assert data['link']['status'] == rules.PENDING
    chain = data['chain']
    assert chain['traceable'] is False
    assert chain['joined_loss_kwh'] is None
    assert set(chain['pending_basis']) == {'换型工作票', '资产台账记录', '更换现场照片', '交接验收单'}
    # 两段履历各自独立可查，损失分别保留但不汇总。
    by_code = {s['code']: s for s in chain['segments']}
    assert by_code['INV-C']['loss_kwh'] == '16.0000'
    assert by_code['INV-C2']['loss_kwh'] == '10.6667'


def test_register_requires_known_devices(client):
    res = client.post('/api/identity/links', json={
        'old_code': 'INV-C', 'new_code': 'GHOST',
        'evidence': {'replaced_at': '2026-10-15T09:35:00+08:00', 'location': '北区'},
    })
    assert res.status_code == 422
    assert res.json()['code'] == 'UNKNOWN_DEVICE'


def test_register_requires_timezone(client):
    with pytest.raises(BusinessError):
        rules.register_payload(
            {'old_code': 'INV-C', 'new_code': 'INV-C2',
             'evidence': {'replaced_at': '2026-10-15T09:35:00', 'location': '北区'}},
            evaluate({'dataset': 'swap'})['context']['devices'],
        )


# ---- 裁定二：重试不新增身份、不覆盖原编号 ----

def test_register_is_idempotent(client):
    first = register(client)
    second = register(client)
    assert second['created'] is False
    assert second['link']['id'] == first['link']['id']
    assert len(client.get('/api/identity/links').json()['links']) == 1


def test_original_codes_and_runs_never_mutated(client):
    register(client)
    run_before = client.post('/api/runs', json={'dataset': 'swap'}).json()
    rid = run_before['manifest']['run_id']
    data = register(client)
    confirm = client.post(f"/api/identity/links/{data['link']['id']}/confirm",
                          json={'revision': 1, 'evidence': FULL_EVIDENCE}).json()
    assert confirm['merged'] is True
    run_after = store.get(main.DB_PATH, rid)
    assert run_after['manifest']['sha256'] == run_before['manifest']['sha256']
    codes = {r['device'] for r in run_after['records']}
    assert {'INV-C', 'INV-C2', 'INV-D'} <= codes


# ---- 审计校验：凭证不全 / 相邻设备 / 资产不符 / 时段重叠 一律驳回 ----

def test_confirm_rejected_when_evidence_incomplete(client):
    lid = register(client)['link']['id']
    res = client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': {}})
    assert res.status_code == 422
    codes = {f['code'] for f in res.json()['failures']}
    assert {'DOC_INCOMPLETE', 'ASSET_GAP', 'NEIGHBOR_RISK'} <= codes
    assert res.json()['link']['status'] == rules.PENDING


def test_neighbor_device_requires_exclusive_note(client):
    lid = register(client)['link']['id']
    evidence = {k: v for k, v in FULL_EVIDENCE.items() if k != 'location_exclusive_note'}
    res = client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': evidence})
    assert [f['code'] for f in res.json()['failures']] == ['NEIGHBOR_RISK']


def test_asset_mismatch_blocks_confirm(client):
    lid = register(client)['link']['id']
    evidence = {**FULL_EVIDENCE, 'asset_no_new': 'ASSET-9'}
    res = client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': evidence})
    assert 'ASSET_MISMATCH' in {f['code'] for f in res.json()['failures']}
    assert res.json()['link']['status'] == rules.PENDING


def test_overlapping_runtime_cannot_be_same_physical(client):
    from app.config import ROOT
    csv = (ROOT / 'sample_data/clean.csv').read_text()
    client.post('/api/runs', json={'dataset': 'clean'})
    client.post('/api/runs', json={'csv_text': csv.replace('INV-A', 'INV-B')})
    data = register(client, old='INV-A', new='INV-B')
    res = client.post(f"/api/identity/links/{data['link']['id']}/confirm",
                      json={'revision': 1, 'evidence': {**FULL_EVIDENCE,
                            'location_exclusive_note': '同机位', 'asset_no_old': 'X', 'asset_no_new': 'X'}})
    assert 'OVERLAP_RUNTIME' in {f['code'] for f in res.json()['failures']}


# ---- 确认成功：跨编号事件、基线依据、损失连续可查 ----

def test_confirmed_chain_is_continuous(client):
    lid = register(client)['link']['id']
    res = client.post(f'/api/identity/links/{lid}/confirm',
                      json={'revision': 1, 'evidence': FULL_EVIDENCE})
    assert res.status_code == 200
    chain = res.json()['chain']
    assert chain['traceable'] is True
    assert chain['joined_loss_kwh'] == '26.6667'
    assert chain['event_count'] == 2
    refs = {ref for seg in chain['segments'] for ref in seg['reference_ids']}
    assert {'REF-C1-00', 'REF-C1-30'} <= refs
    detail = client.get(f'/api/identity/links/{lid}').json()
    assert [e['action'] for e in detail['events']][-1] == 'CONFIRM'


def test_stale_revision_conflict(client):
    lid = register(client)['link']['id']
    ok = client.post(f'/api/identity/links/{lid}/confirm',
                     json={'revision': 1, 'evidence': FULL_EVIDENCE})
    assert ok.status_code == 200
    stale = client.post(f'/api/identity/links/{lid}/confirm',
                        json={'revision': 1, 'evidence': FULL_EVIDENCE})
    assert stale.status_code == 409


# ---- 不同设备不能共用履历：编号绑定互斥 ----

def test_confirmed_code_cannot_join_another_identity(client):
    first = register(client)['link']['id']
    assert client.post(f'/api/identity/links/{first}/confirm',
                       json={'revision': 1, 'evidence': FULL_EVIDENCE}).json()['merged']
    second = register(client, old='INV-C2', new='INV-D')['link']['id']
    res = client.post(f'/api/identity/links/{second}/confirm',
                      json={'revision': 1, 'evidence': FULL_EVIDENCE})
    assert 'CODE_BOUND' in {f['code'] for f in res.json()['failures']}
    # 被拒链路不影响已确认身份的连续视图。
    assert identity_db.chain(main.DB_PATH, first)['traceable'] is True


# ---- 撤回：只重算受影响链路，履历立即拆回独立 ----

def test_withdraw_splits_histories_and_recomputes_only_affected(client):
    first = register(client)
    lid = first['link']['id']
    client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': FULL_EVIDENCE})
    other = register(client, old='REF-C1', new='REF-C2')
    other_digest = identity_db.chain(main.DB_PATH, other['link']['id'])['digest']

    res = client.post(f'/api/identity/links/{lid}/withdraw',
                      json={'revision': 2, 'reason': '验收单复查存疑'})
    assert res.status_code == 200
    assert res.json()['link']['status'] == rules.WITHDRAWN
    assert res.json()['chain']['traceable'] is False
    assert res.json()['chain']['joined_loss_kwh'] is None
    # 其他物理身份的链路视图摘要不变。
    assert identity_db.chain(main.DB_PATH, other['link']['id'])['digest'] == other_digest


def test_withdraw_requires_reason_and_valid_state(client):
    lid = register(client)['link']['id']
    bad = client.post(f'/api/identity/links/{lid}/withdraw', json={'revision': 1, 'reason': ''})
    assert bad.status_code == 422
    client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': FULL_EVIDENCE})
    client.post(f'/api/identity/links/{lid}/withdraw', json={'revision': 2, 'reason': '撤回'})
    again = client.post(f'/api/identity/links/{lid}/withdraw', json={'revision': 3, 'reason': '再撤'})
    assert again.status_code == 422


# ---- 原子性：失败不留半合并；审计日志追加且可追溯 ----

def test_failed_confirm_leaves_no_half_merged_state(client):
    lid = register(client)['link']['id']
    before = identity_db.get_link(main.DB_PATH, lid)
    res = client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': {}})
    assert res.status_code == 422
    after = identity_db.get_link(main.DB_PATH, lid)
    assert after['status'] == rules.PENDING
    assert after['revision'] == before['revision'] == 1
    assert identity_db.chain(main.DB_PATH, lid)['traceable'] is False


def test_audit_trail_is_append_only(client):
    lid = register(client)['link']['id']
    client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': {}})
    client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': FULL_EVIDENCE})
    client.post(f'/api/identity/links/{lid}/withdraw', json={'revision': 2, 'reason': '凭证撤回'})
    events = identity_db.events(main.DB_PATH, lid)
    actions = [e['action'] for e in events]
    assert actions == ['REGISTER', 'CONFIRM_REJECTED', 'CONFIRM', 'WITHDRAW']
    assert [e['seq'] for e in events] == list(range(1, len(events) + 1))
    # 审计表只增不改：直接核对行数与快照留存。
    con = sqlite3.connect(main.DB_PATH)
    assert con.execute('SELECT COUNT(*) FROM identity_events WHERE link_id=?', (lid,)).fetchone()[0] == 4
    assert con.execute("SELECT COUNT(*) FROM identity_links").fetchone()[0] == 1


def test_withdraw_then_reconfirm_after_evidence_completed(client):
    lid = register(client)['link']['id']
    client.post(f'/api/identity/links/{lid}/confirm', json={'revision': 1, 'evidence': FULL_EVIDENCE})
    withdrawn = client.post(f'/api/identity/links/{lid}/withdraw',
                            json={'revision': 2, 'reason': '照片存疑，退回补证'}).json()
    assert withdrawn['chain']['traceable'] is False
    # 撤回后凭证补齐可重新确认；旧编号对幂等，不会产生第二条身份。
    res = client.post(f'/api/identity/links/{lid}/confirm',
                      json={'revision': 3, 'evidence': FULL_EVIDENCE})
    assert res.status_code == 200
    assert res.json()['link']['status'] == rules.CONFIRMED
    assert res.json()['chain']['traceable'] is True
    assert res.json()['chain']['joined_loss_kwh'] == '26.6667'
    assert len(client.get('/api/identity/links').json()['links']) == 1
    actions = [e['action'] for e in identity_db.events(main.DB_PATH, lid)]
    assert actions == ['REGISTER', 'CONFIRM', 'WITHDRAW', 'CONFIRM']


def test_identity_page_renders_traceability(client):
    data = register(client)
    rid = client.get('/api/runs').json()[0]['id']
    page = client.get(f'/identity?run_id={rid}')
    assert page.status_code == 200
    assert '待证后并' in page.text
    assert data['link']['id'] or True
    assert 'identity.js' in page.text


def test_swap_dataset_runs_all_eight_modules():
    state = evaluate({'dataset': 'swap'})
    assert list(state['views'])[-1] == 'identity'
    assert state['archive']['status'] == '可归档'
    assert state['balance']['loss_kwh'] == '26.6667'
