(() => {
  const root = document.querySelector('#strategyList');
  if (!root) return;
  const api = async (url, options = {}) => {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.detail || '请求失败');
    return data;
  };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const platform = value => ({douyin:'抖音', xiaohongshu:'小红书'}[value] || value || '其他平台');
  const weekdays = ['一','二','三','四','五','六','日'];
  let accounts = [], strategies = [], expanded = '';
  const defaults = account => ({
    account_id: String(account.id), platform: account.platform, account_key: account.account_key,
    nickname: account.nickname || account.account_key, enabled: Number(account.enabled) === 1,
    position: account.account_note || '', persona:'', audience:'', content_topics:[], tone:'', blocked_topics:[],
    weekly_quota:3, publish_days:[1,3,6], publish_times:['20:00'], min_interval_days:1, auto_publish:true
  });
  const strategyFor = account => ({...defaults(account), ...(strategies.find(item => String(item.publish_account_id) === String(account.id) || item.account_key === account.account_key) || {})});
  const comma = values => (values || []).join('、') || '未设置';
  const card = account => {
    const strategy = strategyFor(account), ready = Boolean(strategy.position && strategy.persona && strategy.content_topics?.length);
    const open = expanded === String(account.id);
    return `<article class="strategy-card ${open ? 'open' : ''}" data-id="${escape(account.id)}">
      <button type="button" class="strategy-summary" data-toggle="${escape(account.id)}" aria-expanded="${open}">
        <span class="strategy-avatar">${escape((account.nickname || account.account_key || '?').slice(0, 1))}</span>
        <span class="strategy-name"><strong>${escape(account.nickname || account.account_key)}</strong><small>${escape(platform(account.platform))} · ${ready ? '策略已完善' : '待完善策略'}</small></span>
        <span class="strategy-position">${escape(strategy.position || '点击设置账号定位')}</span><span class="strategy-chevron">⌄</span>
      </button>
      ${open ? form(strategy) : ''}
    </article>`;
  };
  const tags = (name, label, values, placeholder) => `<label>${label}<input name="${name}" value="${escape((values || []).join('，'))}" placeholder="${placeholder}"></label>`;
  const form = strategy => `<form class="strategy-form" data-id="${escape(strategy.account_id)}">
    <div class="strategy-grid"><label>账号定位<input name="position" maxlength="80" value="${escape(strategy.position)}" placeholder="例如：职场成长 / 轻创业女性"></label><label>目标受众<input name="audience" maxlength="200" value="${escape(strategy.audience)}" placeholder="例如：23–35 岁职场女性"></label></div>
    <label>账号人设<textarea name="persona" maxlength="600" placeholder="这个账号是谁、有什么经历、如何说话？">${escape(strategy.persona)}</textarea></label>
    <div class="strategy-grid">${tags('content_topics', '内容主题', strategy.content_topics, '内容主题，用逗号分隔')}${tags('blocked_topics', '禁发主题', strategy.blocked_topics, '禁发主题，用逗号分隔')}</div>
    <div class="strategy-grid"><label>文案语气<input name="tone" maxlength="120" value="${escape(strategy.tone)}" placeholder="例如：真诚、清醒、口语化"></label><label>每周发布篇数<input name="weekly_quota" type="number" min="1" max="14" value="${escape(strategy.weekly_quota)}"></label></div>
    <fieldset><legend>发布日</legend><div class="weekday-row">${weekdays.map((name, index) => `<label><input type="checkbox" name="publish_days" value="${index}" ${strategy.publish_days?.includes(index) ? 'checked' : ''}>周${name}</label>`).join('')}</div></fieldset>
    <div class="strategy-grid"><label>发布时间<input name="publish_times" value="${escape((strategy.publish_times || []).join('，'))}" placeholder="例如：20:00"></label><label>最小间隔（天）<input name="min_interval_days" type="number" min="0" max="30" value="${escape(strategy.min_interval_days)}"></label></div>
    <label class="strategy-switch"><input name="auto_publish" type="checkbox" ${strategy.auto_publish ? 'checked' : ''}> 自动排期与发布：超时未审核的合规任务，保留坑位并按计划发布</label>
    <div class="strategy-actions"><span class="strategy-hint">系统会优先使用最早可用坑位；本周满额后自动顺延至下周。</span><button type="submit">保存账号策略</button></div>
  </form>`;
  const render = () => {
    const enabled = accounts.filter(account => Number(account.enabled) === 1);
    root.innerHTML = enabled.length ? enabled.map(card).join('') : '<p class="meta">请先同步已授权账号，再设置账号策略。</p>';
  };
  const split = value => value.split(/[，,\n]+/).map(item => item.trim()).filter(Boolean).slice(0, 8);
  root.addEventListener('click', event => {
    const toggle = event.target.closest('[data-toggle]');
    if (!toggle) return;
    expanded = expanded === toggle.dataset.toggle ? '' : toggle.dataset.toggle;
    render();
  });
  root.addEventListener('submit', async event => {
    const formNode = event.target.closest('.strategy-form');
    if (!formNode) return;
    event.preventDefault();
    const account = accounts.find(item => String(item.id) === String(formNode.dataset.id));
    if (!account) return;
    const formData = new FormData(formNode);
    const payload = {...defaults(account),
      position: formData.get('position').trim(), persona: formData.get('persona').trim(), audience: formData.get('audience').trim(),
      content_topics: split(formData.get('content_topics')), blocked_topics: split(formData.get('blocked_topics')), tone: formData.get('tone').trim(),
      weekly_quota: Number(formData.get('weekly_quota')), publish_days: formData.getAll('publish_days').map(Number),
      publish_times: split(formData.get('publish_times')), min_interval_days: Number(formData.get('min_interval_days')), auto_publish: formData.get('auto_publish') === 'on'
    };
    const submit = formNode.querySelector('button[type="submit"]'); submit.disabled = true;
    try {
      const saved = await api('/api/nurture/accounts/strategy', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      strategies = [...strategies.filter(item => item.account_key !== saved.account_key), saved];
      render();
    } catch (error) { alert(error.message); submit.disabled = false; }
  });
  const load = async () => {
    try {
      const [remote, local] = await Promise.all([api('/api/account-sync/accounts'), api('/api/nurture/accounts')]);
      accounts = remote.items || []; strategies = local.items || []; render();
    } catch (error) { root.innerHTML = `<p class="meta">账号策略读取失败：${escape(error.message)}</p>`; }
  };
  load();
})();
