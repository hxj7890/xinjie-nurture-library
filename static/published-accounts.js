// 进入素材库优先处理尚未配置的内容；用户仍可随时切换到全部或其他状态。
if(state.filter==='all')state.filter='queued';

const originalRender=render;
render=function(){state.items.forEach(item=>{if(item.status==='submitted')item.status='published'});originalRender()}
function isPublishedMaterial(item){return item.status==='published'}
function publishedAccountKeyFor(item){return item.assigned_account_id?`id:${item.assigned_account_id}`:`key:${item.assigned_account_key||'unknown'}`}

function publishedAccountLabel(item){
  if(!item.assigned_account_id&&!item.assigned_account_key)return '未选择账号';
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
  const groupedStatus=state.filter==='all'?'':state.filter;
  if(!groupedStatus){
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
  const groupedItems=state.items.filter(item=>groupedStatus==='published'?isPublishedMaterial(item):item.status===groupedStatus);
  const groups=groupedItems.reduce((all,item)=>{
    const key=publishedAccountKeyFor(item);
    (all[key]||(all[key]={label:publishedAccountLabel(item),items:[]})).items.push(item);
    return all;
  },{});
  const entries=Object.entries(groups);
  if(state.publishedAccountKey&&!groups[state.publishedAccountKey])state.publishedAccountKey='';
  const statusCopy={
    queued:['待配置账号','待配置素材会按账号归类在这里。'],
    scheduled:['定时发布账号','定时发布素材会按账号归类在这里。'],
    published:['已发布账号','已发布素材会按账号归类在这里。'],
    failed:['发布失败账号','发布失败素材会按账号归类在这里。']
  };
  const [heading,emptyText]=statusCopy[groupedStatus]||['素材账号','该分类素材会按账号归类在这里。'];
  panel.innerHTML=entries.length?`<div class="published-account-heading">${heading}</div><div class="filters">${[['','全部',groupedItems.length],...entries.map(([key,group])=>[key,group.label,group.items.length])].map(([key,label,count])=>`<button type="button" class="filter ${state.publishedAccountKey===key?'active':''}" data-published-account="${esc(key)}">${esc(label)} ${count}</button>`).join('')}</div>`:`<p class="meta published-empty">${emptyText}</p>`;
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
const imageGalleryScript=document.createElement('script');
imageGalleryScript.src='/image-gallery.js?v=20260817-publish-time';
document.head.append(imageGalleryScript);
