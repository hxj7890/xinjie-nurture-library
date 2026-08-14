function publishedAccountKeyFor(item){return item.assigned_account_id?`id:${item.assigned_account_id}`:`key:${item.assigned_account_key||'unknown'}`}

function publishedAccountLabel(item){
  const account=[...(state.accounts||[]),...(state.syncAccounts||[])].find(row=>String(row.id)===String(item.assigned_account_id));
  const platform=platformName(item.assigned_platform||account?.platform||'');
  return `${platform} · ${account?.nickname||item.assigned_account_key||'未记录账号'}`;
}

function applyPublishedAccountFilter(){
  const selected=state.publishedAccountKey||'';
  document.querySelectorAll('#list .item').forEach(card=>{
    const id=card.querySelector('.account')?.dataset.id;
    const item=state.items.find(row=>String(row.id)===String(id));
    card.hidden=Boolean(selected&&item&&publishedAccountKeyFor(item)!==selected);
  });
}

function renderPublishedAccountFilters(){
  let panel=document.querySelector('#publishedAccountFilters');
  if(state.filter!=='published'){
    state.publishedAccountKey='';
    panel?.remove();
    return;
  }
  if(!panel){
    panel=document.createElement('div');
    panel.id='publishedAccountFilters';
    panel.className='published-account-filters';
    document.querySelector('#filters')?.insertAdjacentElement('afterend',panel);
  }
  const groups=state.items.filter(item=>item.status==='published').reduce((all,item)=>{
    const key=publishedAccountKeyFor(item);
    (all[key]||(all[key]={label:publishedAccountLabel(item),items:[]})).items.push(item);
    return all;
  },{});
  const entries=Object.entries(groups);
  if(state.publishedAccountKey&&!groups[state.publishedAccountKey])state.publishedAccountKey='';
  panel.innerHTML=entries.length?`<div class="published-account-heading">已发布账号</div><div class="filters">${[['','全部',state.items.filter(item=>item.status==='published').length],...entries.map(([key,group])=>[key,group.label,group.items.length])].map(([key,label,count])=>`<button type="button" class="filter ${state.publishedAccountKey===key?'active':''}" data-published-account="${esc(key)}">${esc(label)} ${count}</button>`).join('')}</div>`:'<p class="meta published-empty">已发布素材会按账号归类在这里。</p>';
  panel.onclick=event=>{
    const button=event.target.closest('[data-published-account]');
    if(!button)return;
    state.publishedAccountKey=button.dataset.publishedAccount;
    renderPublishedAccountFilters();
    applyPublishedAccountFilter();
  };
  applyPublishedAccountFilter();
}

const publishedAccountObserver=new MutationObserver(()=>setTimeout(renderPublishedAccountFilters,0));
publishedAccountObserver.observe(document.querySelector('#filters'),{childList:true,subtree:true});
publishedAccountObserver.observe(document.querySelector('#list'),{childList:true});
renderPublishedAccountFilters();
