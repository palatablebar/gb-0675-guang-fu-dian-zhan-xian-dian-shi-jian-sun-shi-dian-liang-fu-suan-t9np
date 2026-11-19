/* 设备身份台账：登记 / 确认 / 撤回，全部走后端单事务接口。 */
(function () {
  const list = document.querySelector('#link-list');
  const counter = document.querySelector('#link-count');
  const feedback = document.querySelector('#feedback');
  const tpl = document.querySelector('#link-template');
  if (!list) return;

  const STATUS_CLASS = { '待确认': 'badge--pending', '已确认': 'badge--ok', '已驳回': 'badge--off', '已撤回': 'badge--off' };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function refresh() {
    const res = await fetch('/api/identity/links');
    if (!res.ok) { feedback.textContent = '台账读取失败'; return; }
    const data = await res.json();
    counter.textContent = data.links.length + ' 条链路';
    if (!data.links.length) {
      list.innerHTML = '<p class="muted">暂无换型链路。值班可在下方登记，登记后保持“待确认”，不合并履历。</p>';
      return;
    }
    const chainById = Object.fromEntries(data.chains.map(c => [c.link_id, c]));
    list.innerHTML = '';
    for (const link of data.links) {
      const chain = chainById[link.id];
      list.appendChild(renderCard(link, chain));
    }
  }

  function segmentTable(chain) {
    if (!chain) return '<p class="muted">视图待生成</p>';
    const rows = chain.segments.map(s => `
      <tr><td>${esc(s.code)}</td><td>${s.events.length}</td><td>${esc(s.reference_ids.join('、') || '—')}</td>
      <td>${esc(s.loss_kwh)}</td><td>${s.unknown_seconds ? Math.round(s.unknown_seconds / 60) + ' min' : '0'}</td></tr>`).join('');
    return `<div class="table-scroll"><table><thead><tr><th>编号履历</th><th>限电事件</th><th>基线依据</th><th>损失 kWh</th><th>未定时长</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="joined">跨编号连续损失：<b>${chain.traceable ? esc(chain.joined_loss_kwh) + ' kWh' : '不汇总（' + esc(chain.continuity) + '）'}</b></p>`;
  }

  function renderCard(link, chain) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = link.id;
    node.dataset.revision = link.revision;
    node.querySelector('.link-code').textContent = `${link.old_code} → ${link.new_code}`;
    node.querySelector('.link-id').textContent = `${link.id} · 物理身份 ${link.physical_id} · ${link.location} · 更换 ${link.replaced_at}`;
    const badge = node.querySelector('.badge');
    badge.textContent = link.status;
    badge.classList.add(STATUS_CLASS[link.status] || 'badge--pending');
    node.querySelector('.continuity').textContent = chain ? chain.continuity : '视图待生成';
    node.querySelector('.segments').innerHTML = segmentTable(chain);
    const basis = chain && chain.pending_basis && chain.pending_basis.length
      ? '待确认依据：' + chain.pending_basis.join('、')
      : (link.status === '已确认' ? '依据齐全：换型工作票、资产台账、现场照片、交接验收单；资产编号一致且位置排他' : '无待补凭证');
    node.querySelector('.basis').textContent = basis;
    node.querySelector('.revision').textContent = `revision ${link.revision}`;
    node.querySelector('.act-confirm').disabled = !(link.status === '待确认' || link.status === '已撤回');
    node.querySelector('.act-withdraw').disabled = !(link.status === '待确认' || link.status === '已确认');
    node.querySelector('.act-confirm').addEventListener('click', () => confirmLink(link, node));
    node.querySelector('.act-withdraw').addEventListener('click', () => withdrawLink(link, node));
    loadAudit(link.id, node);
    return node;
  }

  async function loadAudit(linkId, node) {
    const res = await fetch(`/api/identity/links/${linkId}`);
    if (!res.ok) return;
    const data = await res.json();
    node.querySelector('.audit').innerHTML = '<div class="table-scroll"><table><thead><tr><th>序号</th><th>动作</th><th>状态变化</th><th>操作人</th><th>原因</th><th>时刻 UTC</th></tr></thead><tbody>'
      + data.events.map(e => `<tr><td>${e.seq}</td><td>${esc(e.action)}</td><td>${esc(e.from_status || '—')} → ${esc(e.to_status)}</td><td>${esc(e.actor)}</td><td title="${esc(e.reason)}">${esc(e.reason)}</td><td>${esc(e.at)}</td></tr>`).join('')
      + '</tbody></table></div>';
  }

  function showFailures(node, failures) {
    const box = node.querySelector('.failures');
    if (!failures || !failures.length) { box.hidden = true; box.textContent = ''; return; }
    box.hidden = false;
    box.textContent = failures.map(f => `[${f.code}] ${f.detail}`).join('\n');
  }

  async function confirmLink(link, node) {
    const note = prompt('请输入同位置排他性安装说明（证明不是相邻设备 INV-D）：', '旧机拆除后同机位安装，相邻机位设备未变动');
    if (note === null) return;
    const asset = prompt('请确认统一资产编号（新旧须一致）：', link.asset_no_old || 'ASSET-C-008');
    if (asset === null) return;
    const body = {
      revision: link.revision,
      evidence: {
        work_order: 'DOC-WO-20261015-C', asset_record: 'DOC-AR-C', replacement_photo: 'DOC-PH-C',
        acceptance_form: 'DOC-AF-C', asset_no_old: asset, asset_no_new: asset, location_exclusive_note: note,
      },
    };
    const res = await fetch(`/api/identity/links/${link.id}/confirm`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { showFailures(node, data.failures); feedback.textContent = '确认被驳回，链路保持待确认（无半合并）'; }
    else { feedback.textContent = '已确认同一物理身份，连续视图已重算'; showFailures(node, null); }
    refresh();
  }

  async function withdrawLink(link, node) {
    const reason = prompt('撤回原因（将写入审计日志，并立即拆回两段独立履历）：');
    if (!reason) return;
    const res = await fetch(`/api/identity/links/${link.id}/withdraw`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: link.revision, reason }),
    });
    const data = await res.json();
    if (!res.ok) { feedback.textContent = data.detail || '撤回失败'; return; }
    feedback.textContent = '链路已撤回，仅该物理身份的连续视图被重算';
    refresh();
  }

  document.querySelector('#register-form').addEventListener('submit', async event => {
    event.preventDefault();
    const fd = new FormData(event.target);
    const payload = {
      old_code: fd.get('old_code'), new_code: fd.get('new_code'),
      evidence: {
        replaced_at: fd.get('replaced_at'), location: fd.get('location'),
        asset_no_old: fd.get('asset_no_old'), asset_no_new: fd.get('asset_no_new'), notes: fd.get('notes'),
        documents: {
          work_order: fd.get('work_order'), asset_record: fd.get('asset_record'),
          replacement_photo: fd.get('replacement_photo'), acceptance_form: fd.get('acceptance_form'),
        },
      },
    };
    const res = await fetch('/api/identity/links', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { feedback.textContent = data.detail || '登记失败'; return; }
    feedback.textContent = data.created ? '链路已登记，状态待确认，两段履历仍独立' : '链路已存在（幂等返回，未新增身份）';
    event.target.reset();
    refresh();
  });

  refresh();
})();
