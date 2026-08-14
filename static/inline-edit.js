const editableContentSelector='.content strong,.content .caption,.content .topics';

function editFieldFor(node){
  if(node.matches('strong'))return 'title';
  if(node.matches('.caption'))return 'body';
  return 'topics';
}

function valueForEdit(item,field){
  if(field==='title')return item.title||'';
  if(field==='body')return item.body||item.caption||'';
  return (item.topics||[]).map(topic=>`#${topic}`).join(' ');
}

function parseTopics(value){
  return value.split(/[\s,，]+/).map(topic=>topic.trim().replace(/^#+/,'')).filter(Boolean).slice(0,6);
}

async function saveInlineContent(id,field,value){
  const item=state.items.find(row=>String(row.id)===String(id));
  if(!item)return;
  const payload={title:item.title||'',body:item.body||item.caption||'',topics:item.topics||[]};
  if(field==='topics')payload.topics=parseTopics(value);else payload[field]=value.trim();
  if(!payload.title||!payload.body)throw Error('标题和正文不能为空');
  await api(`/api/materials/${id}/content`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  await load();
}

function startInlineEdit(node){
  if(node.closest('.inline-editor'))return;
  const itemNode=node.closest('.item');
  const id=itemNode?.querySelector('.account')?.dataset.id;
  const item=state.items.find(row=>String(row.id)===String(id));
  if(!item)return;
  const field=editFieldFor(node),original=valueForEdit(item,field);
  const editor=field==='body'?document.createElement('textarea'):document.createElement('input');
  editor.className=`inline-editor inline-${field}`;
  editor.value=original;
  editor.setAttribute('aria-label',field==='title'?'编辑标题':field==='body'?'编辑正文':'编辑话题');
  if(field==='title')editor.maxLength=80;
  if(field==='topics')editor.placeholder='例如：#日常 #城市随拍';
  node.replaceWith(editor);editor.focus();editor.select();
  let finished=false;
  const finish=async(save)=>{
    if(finished)return;finished=true;
    if(!save||editor.value.trim()===original.trim()){await load();return;}
    editor.disabled=true;
    try{await saveInlineContent(id,field,editor.value)}catch(error){alert(error.message);await load();}
  };
  editor.onblur=()=>finish(true);
  editor.onkeydown=event=>{
    if(event.key==='Escape'){event.preventDefault();finish(false);}
    if(field!=='body'&&event.key==='Enter'){event.preventDefault();finish(true);}
    if(field==='body'&&event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();finish(true);}
  };
}

document.addEventListener('click',event=>{const target=event.target.closest(editableContentSelector);if(target)startInlineEdit(target)});
