import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result, ledger=None):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        if ledger is not None:
            # 设备身份台账快照：链路状态、连续视图与独立审计轨迹，随运行凭证一并封存。
            archive.writestr('identity_ledger.json',json.dumps(ledger,ensure_ascii=False,indent=2))
    return buffer.getvalue()
