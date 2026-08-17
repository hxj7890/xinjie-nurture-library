const galleryOriginalRender=render;
render=function(){galleryOriginalRender();requestAnimationFrame(()=>{renderPublishedTimes();renderImageGalleries()})};

function publishTime(value){
  if(!value)return '';
  const timestamp=typeof value==='number'||/^\d+$/.test(String(value))?Number(value)*1000:value;
  const date=new Date(timestamp);
  if(Number.isNaN(date.getTime()))return '';
  const part=n=>String(n).padStart(2,'0');
  return `${date.getFullYear()}/${part(date.getMonth()+1)}/${part(date.getDate())} ${part(date.getHours())}:${part(date.getMinutes())}`;
}

function renderPublishedTimes(){
  const cards=[...document.querySelectorAll('#list .item')];
  cards.forEach(card=>{
    const id=card.querySelector('.account')?.dataset.id;
    const item=state.items.find(row=>String(row.id)===String(id));
    const tag=card.querySelector('.tag.published');
    if(!item||!tag)return;
    const time=publishTime(item.updated_at);
    tag.textContent=time?`已发布 · ${time}`:'已发布';
  });
  const published=cards.filter(card=>{
    const id=card.querySelector('.account')?.dataset.id;
    return state.items.find(row=>String(row.id)===String(id))?.status==='published';
  }).sort((left,right)=>{
    const leftItem=state.items.find(row=>String(row.id)===String(left.querySelector('.account')?.dataset.id));
    const rightItem=state.items.find(row=>String(row.id)===String(right.querySelector('.account')?.dataset.id));
    return Number(rightItem?.updated_at||0)-Number(leftItem?.updated_at||0);
  });
  published.forEach(card=>card.parentElement?.append(card));
}

function renderImageGalleries(){
  document.querySelectorAll('#list .item').forEach(card=>{
    const id=card.querySelector('.account')?.dataset.id;
    const item=state.items.find(row=>String(row.id)===String(id));
    const photos=card.querySelector('.photos');
    if(!item||!photos)return;
    const images=item.images||[];
    if(!images.length){photos.replaceChildren();return}
    let index=0,startX=0;
    const image=document.createElement('img');
    image.className='gallery-image';
    image.alt=`素材图片 1/${images.length}`;
    const update=nextIndex=>{
      index=(nextIndex+images.length)%images.length;
      image.src=mediaUrl(images[index]);
      image.alt=`素材图片 ${index+1}/${images.length}`;
      if(counter)counter.textContent=`${index+1} / ${images.length}`;
    };
    photos.classList.add('photo-gallery');
    photos.replaceChildren(image);
    let counter;
    if(images.length>1){
      const previous=document.createElement('button');
      previous.type='button';previous.className='gallery-nav previous';previous.setAttribute('aria-label','查看上一张图片');previous.textContent='‹';
      const next=document.createElement('button');
      next.type='button';next.className='gallery-nav next';next.setAttribute('aria-label','查看下一张图片');next.textContent='›';
      counter=document.createElement('span');counter.className='gallery-counter';
      previous.onclick=()=>update(index-1);
      next.onclick=()=>update(index+1);
      photos.onpointerdown=event=>{startX=event.clientX};
      photos.onpointerup=event=>{if(Math.abs(event.clientX-startX)>35)update(index+(event.clientX<startX?1:-1))};
      photos.append(previous,next,counter);
    }
    update(0);
  });
}

renderPublishedTimes();
renderImageGalleries();
