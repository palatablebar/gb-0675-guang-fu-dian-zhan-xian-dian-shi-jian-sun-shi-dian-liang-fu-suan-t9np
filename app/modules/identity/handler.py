from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from app import identity_db, store
from app.engine import evaluate
from app.errors import BusinessError
from app.web import render

router = APIRouter()


def _db():
    # 延迟读取 main.DB_PATH，使测试可通过 monkeypatch 切换隔离库。
    from app.main import DB_PATH
    return DB_PATH


def _devices(run):
    return run['context']['devices']


@router.get('/identity')
def page(request: Request):
    return render(request, 'identity')


@router.post('/api/identity')
def calculate(payload: dict = Body(default={})):
    # 与其他模块一致的独立演示入口：跑到身份台账模块（第 8 步）。
    return evaluate(payload, until=8)


@router.get('/api/identity/links')
def links():
    path = _db()
    return {'links': identity_db.list_links(path), 'chains': identity_db.all_chains(path)}


@router.post('/api/identity/links')
def register(request: Request, payload: dict = Body(...)):
    path = _db()
    run_id = request.query_params.get('run_id')
    run = store.get(path, run_id) if run_id else _latest_run()
    link, created = identity_db.register(path, payload, _devices(run))
    chain = identity_db.chain(path, link['id'])
    return JSONResponse({'link': link, 'chain': chain, 'created': created},
                        status_code=201 if created else 200)


@router.post('/api/identity/links/{link_id}/confirm')
def confirm(link_id: str, payload: dict = Body(...)):
    path = _db()
    run = _latest_run()
    link, failures, chain = identity_db.confirm(path, link_id, payload, _devices(run))
    if failures:
        return JSONResponse({'link': link, 'failures': failures, 'merged': False}, status_code=422)
    return {'link': link, 'chain': chain, 'failures': [], 'merged': True}


@router.post('/api/identity/links/{link_id}/withdraw')
def withdraw(link_id: str, payload: dict = Body(...)):
    path = _db()
    link, chain = identity_db.withdraw(path, link_id, payload)
    return {'link': link, 'chain': chain}


@router.get('/api/identity/links/{link_id}')
def detail(link_id: str):
    path = _db()
    return {'link': identity_db.get_link(path, link_id),
            'chain': identity_db.chain(path, link_id),
            'events': identity_db.events(path, link_id)}


def _latest_run():
    path = _db()
    history = store.history(path)
    if not history:
        raise BusinessError('NO_RUN', '尚无运行批次，无法核对设备清册', 422)
    return store.get(path, history[0]['id'])
