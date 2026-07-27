let sid=null, pages=[], cur=0, rects={};  // rects: {page: [[x0,y0,x1,y1] PDF]}
const $=i=>document.getElementById(i);
const layer=$('layer'), wrap=$('wrap'), img=$('pageimg');

$('file').onchange = async e => {
  const f = e.target.files[0]; if(!f) return;
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/api/open', {method:'POST', body:fd});
  if(!r.ok){ alert('Erreur: '+await r.text()); return; }
  const d = await r.json();
  sid=d.sid; pages=d.pages; cur=0; rects={};
  $('drop').hidden=true; wrap.hidden=false;
  show();
};

function scale(){ return pages[cur].w / img.clientWidth; }  // pts PDF par px ecran

function show(){
  img.src = `/api/page/${sid}/${cur}`;
  img.onload = paint;
  $('pnum').textContent = `${cur+1} / ${pages.length}`;
  $('prev').disabled = cur===0;
  $('next').disabled = cur>=pages.length-1;
  paint();
}

function paint(){
  layer.innerHTML='';
  const s=scale(), o=pages[cur];
  (rects[cur]||[]).forEach((r,i)=>{
    const d=document.createElement('div'); d.className='box';
    d.style.left=((r[0]-o.x0)/s)+'px'; d.style.top=((r[1]-o.y0)/s)+'px';
    d.style.width=((r[2]-r[0])/s)+'px'; d.style.height=((r[3]-r[1])/s)+'px';
    const x=document.createElement('div'); x.className='x'; x.textContent='x';
    x.onclick=ev=>{ev.stopPropagation(); rects[cur].splice(i,1); paint();};
    d.appendChild(x); layer.appendChild(d);
  });
  const n=(rects[cur]||[]).length, t=Object.values(rects).reduce((a,b)=>a+b.length,0);
  $('undo').disabled=!n; $('clear').disabled=!n; $('export').disabled=!t;
  $('status').textContent=`${n} zone(s) sur cette page, ${t} au total.`;
}

let start=null, ghost=null;
layer.onmousedown = e => {
  if(e.target.classList.contains('x')) return;
  const b=layer.getBoundingClientRect();
  start=[e.clientX-b.left, e.clientY-b.top];
  ghost=document.createElement('div'); ghost.className='box'; layer.appendChild(ghost);
};
window.onmousemove = e => {
  if(!ghost) return;
  const b=layer.getBoundingClientRect();
  const x=e.clientX-b.left, y=e.clientY-b.top;
  ghost.style.left=Math.min(x,start[0])+'px'; ghost.style.top=Math.min(y,start[1])+'px';
  ghost.style.width=Math.abs(x-start[0])+'px'; ghost.style.height=Math.abs(y-start[1])+'px';
};
window.onmouseup = e => {
  if(!ghost) return;
  const b=layer.getBoundingClientRect();
  const x=e.clientX-b.left, y=e.clientY-b.top;
  const x0=Math.min(x,start[0]), y0=Math.min(y,start[1]);
  const x1=Math.max(x,start[0]), y1=Math.max(y,start[1]);
  ghost.remove(); ghost=null; start=null;
  if(x1-x0<3 || y1-y0<3) return;
  const s=scale(), o=pages[cur];
  (rects[cur]=rects[cur]||[]).push([x0*s+o.x0, y0*s+o.y0, x1*s+o.x0, y1*s+o.y0]);
  paint();
};

$('prev').onclick=()=>{ if(cur>0){cur--; show();} };
$('next').onclick=()=>{ if(cur<pages.length-1){cur++; show();} };
$('undo').onclick=()=>{ (rects[cur]||[]).pop(); paint(); };
$('clear').onclick=()=>{ delete rects[cur]; paint(); };
window.addEventListener('keydown', e=>{
  if(!sid) return;
  if(e.key==='PageDown'||e.key==='ArrowRight') $('next').click();
  if(e.key==='PageUp'||e.key==='ArrowLeft') $('prev').click();
  if(e.ctrlKey && e.key==='z') $('undo').click();
});
window.addEventListener('resize', paint);

$('export').onclick = async () => {
  $('status').textContent='Traitement...';
  const r = await fetch('/api/export', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sid, rects, pixelate:$('pixelate').checked,
                          strip_meta:$('meta').checked})
  });
  if(!r.ok){ $('status').textContent='Erreur: '+await r.text(); return; }
  const d = await r.json();
  const a=document.createElement('a'); a.href=d.download; a.download=d.filename;
  document.body.appendChild(a); a.click(); a.remove();
  $('status').innerHTML = d.leak_count
    ? `<span class="warn">Attention: ${d.leak_count} fragment(s) de texte touchent encore les zones (`
      + d.leaks.map(l=>`p${l.page}: ${l.text}`).join(', ') + `). Verifiez le resultat.</span>`
    : `<span class="ok">Export OK, aucun texte residuel detecte dans les zones.</span>`;
};
