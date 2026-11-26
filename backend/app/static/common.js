const 投递台 = {
  async getJSON(url, opt) {
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.错误 || data.error || r.statusText);
    return data;
  },
  async postJSON(url, body) {
    return this.getJSON(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  安全文本(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  },
  徽章(状态) {
    const span = document.createElement('span');
    span.className = '徽章 ' + (状态 || '');
    span.textContent = 状态 || '—';
    return span;
  },
  行(html) {
    const tr = document.createElement('tr');
    tr.innerHTML = html;
    return tr;
  },
  空态(msg) { return msg || '暂无数据'; },
  查询参数() {
    return Object.fromEntries(new URLSearchParams(location.search).entries());
  },
  票号(t) {
    return t ? '<span class="票号" title="流水与互证同票的票号/锁令牌">' + 投递台.安全文本(t) + '</span>' : '<span class="说明">—</span>';
  },
  超时标(r) {
    const 超 = r && (r.是否超时 === 1 || r.是否超时 === true || r.超时 === true ||
      r.状态 === '超时' || (r.说明 && String(r.说明).includes('超时')));
    return 超 ? '<span class="徽章 超时">超时</span>' : '<span class="说明">—</span>';
  },
  命运卡(d) {
    // d 为 /api/事件/<id> 详情；渲染“事件命运 + 中文说明 + 关键计数”
    if (!d) return '';
    const tone = d.命运色调 ? ('tone-' + d.命运色调) : 'tone-muted';
    const 灯色 = d.空成功 ? '红' : (d.状态 === '已投递' ? '绿' : (d.状态 === '死信' ? '红' : '黄'));
    return '<div class="命运卡 ' + tone + '">'
      + '<h3><span class="灯 ' + 灯色 + '"></span>事件命运：' + 投递台.安全文本(d.命运 || d.状态) + '</h3>'
      + '<p>' + 投递台.安全文本(d.命运说明 || '') + '</p>'
      + '<div class="命运格">'
      + '<div>事件号<b>' + 投递台.安全文本(d.事件号) + '</b></div>'
      + '<div>正式尝试行<b>' + 投递台.安全文本(d.尝试数 ?? (d.尝试 ? d.尝试.length : 0)) + '</b></div>'
      + '<div>成功尝试<b>' + 投递台.安全文本(d.成功尝试数 ?? 0) + '</b></div>'
      + '<div>超时次数<b>' + 投递台.安全文本(d.超时次数 ?? 0) + '</b></div>'
      + '<div>票号<b style="font-size:12px">' + 投递台.安全文本((d.尝试 && d.尝试[0] && (d.尝试[0].票号 || d.尝试[0].锁令牌)) || '—') + '</b></div>'
      + '</div></div>';
  },
};
