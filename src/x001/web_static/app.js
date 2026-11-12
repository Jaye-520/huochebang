const $ = id => document.getElementById(id);
const page = document.body.className.replace('page-', '');
let snapshot, route = '', search = '', vehicle = '', length = '', trucks = '', price = '', period = '', day = '', fetching = false, refreshQueued = false, toastTimer;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const statusNames = {done:'已识别',capturing:'采集中',queued:'待识别',processing:'识别中',capture_failed:'采集失败',ocr_failed:'识别失败'};
const modeNames = {collect:'手机采集',process:'处理截图',reprocess:'重新识别'};
function toast(message){const el=$('toast');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3400)}

function overview(data){
  const s=data.stats||{};
  for(const key of ['total','done','pending','failed']) $('stat-'+key).textContent=s[key]??'—';
  $('route-count').textContent=`${s.routes||0} 条线路`;
  const select=$('analysis-route');
  const routeKey=data.routes.map(x=>x.route).join('|');
  if(select.dataset.routes!==routeKey){select.innerHTML='<option value="">全部线路</option>'+data.routes.map(x=>`<option value="${esc(x.route)}">${esc(x.route)}</option>`).join('');select.dataset.routes=routeKey;select.value=route}
  $('hero-running').textContent=data.process.running?`${modeNames[data.process.mode]||'任务'}运行中`:'采集器待命';
  $('hero-sync').textContent=data.ready?`最近同步 ${new Date(data.updated_at).toLocaleTimeString('zh-CN',{hour12:false})}`:'等待数据库初始化';
  const max=Math.max(1,...data.activity.map(x=>x.total));
  $('chart').innerHTML=data.activity.length?data.activity.map(x=>`<div class="bar-col" title="${esc(x.hour)} · ${x.total} 条"><div class="bar" style="height:${Math.max(4,x.total/max*145)}px"></div><span>${esc(x.hour.slice(-5))}</span></div>`).join(''):'<div class="empty-chart">采集开始后，这里会显示趋势</div>';
  const routeMax=Math.max(1,...data.routes.map(x=>x.total));
  $('route-bars').innerHTML=data.routes.length?data.routes.slice(0,5).map(x=>`<a class="route-link" href="/records?route=${encodeURIComponent(x.route)}"><div class="route-info"><span>${esc(x.route)}</span><span>${x.total} 条 ↗</span></div><div class="route-track"><div class="route-fill" style="width:${x.total/routeMax*100}%"></div></div></a>`).join(''):'<div class="empty-chart">识别到线路后，这里会显示分布</div>';
  const quality=data.analysis?.quality||{},total=quality.total||0;
  $('quality-price').textContent=total?`${Math.round((quality.price||0)/total*100)}%`:'—';
  $('quality-weight').textContent=total?`${Math.round((quality.weight||0)/total*100)}%`:'—';
  $('quality-review').textContent=quality.review??'—';
  const cargo=data.analysis?.cargo_top||[],cargoMax=Math.max(1,...cargo.map(x=>x.total));
  $('cargo-bars').innerHTML=cargo.length?cargo.map(x=>`<div><div class="route-info"><span>${esc(x.name)}</span><span>${x.total} 条</span></div><div class="route-track"><div class="route-fill" style="width:${x.total/cargoMax*100}%"></div></div></div>`).join(''):'<div class="empty-chart">完成识别后，这里会显示货物结构</div>';
}

function records(data){
  const select=$('route-filter');
  const routeKey=data.routes.map(x=>x.route).join('|');
  if(select.dataset.routes!==routeKey){select.innerHTML='<option value="">全部线路</option>'+data.routes.map(x=>`<option value="${esc(x.route)}">${esc(x.route)}</option>`).join('');select.dataset.routes=routeKey;select.value=route}
  const matches=data.matching_total??data.records.length;
  $('record-count').textContent=data.records.length<matches?`显示最近 ${data.records.length} 条 · 共 ${matches} 条匹配记录`:`共 ${matches} 条匹配记录`;
  const body=$('rows');
  body.innerHTML=data.records.length?data.records.map(r=>{
    const status=r.status||'unknown',badge=status==='done'?'ok':status.includes('failed')?'bad':'warn';
    return `<tr data-id="${r.id}"><td class="id-cell">#${r.id}</td><td><strong>${esc(r.loading_location||'装货地未识别')} → ${esc(r.unloading_location||'卸货地未识别')}</strong><small>${esc(r.route||'线路未记录')}</small></td><td><strong>${esc(r.cargo||r.card_key||'识别中')}</strong></td><td><div class="spec-list"><span><i>重量</i>${esc(r.weight||'—')}</span><span><i>里程</i>${esc(r.estimated_distance||'—')}</span><span><i>车型</i>${esc(r.vehicle||'—')}</span><span><i>车数</i>${esc(r.truck_count||'未显示')}</span></div></td><td class="price">${esc(r.price||r.list_price||'—')}</td><td><span class="record-date">${esc(r.captured_at||'—')}</span><span class="badge ${badge}">${esc(statusNames[status]||status)}</span></td><td>↗</td></tr>`;
  }).join(''):'<tr><td colspan="7" class="empty-row">暂无匹配货源。采集开始后会自动显示。</td></tr>';
  body.querySelectorAll('tr[data-id]').forEach(row=>row.addEventListener('click',()=>openDetail(Number(row.dataset.id))));
  const mobile=$('mobile-records');
  mobile.innerHTML=data.records.length?data.records.map(r=>{
    const state=r.status||'unknown',badge=state==='done'?'ok':state.includes('failed')?'bad':'warn';
    return `<button type="button" class="freight-card" data-id="${r.id}"><span class="freight-card-top"><span class="id-cell">#${r.id} · ${esc(r.route||'线路未记录')}</span><span class="badge ${badge}">${esc(statusNames[state]||state)}</span></span><strong>${esc(r.loading_location||'装货地未识别')} <span>→</span> ${esc(r.unloading_location||'卸货地未识别')}</strong><span class="freight-card-cargo">${esc(r.cargo||'货物未识别')}</span><span class="freight-specs"><span>重量 <b>${esc(r.weight||'—')}</b></span><span>里程 <b>${esc(r.estimated_distance||'—')}</b></span><span>车型 <b>${esc(r.vehicle||'—')}</b></span><span>车数 <b>${esc(r.truck_count||'未显示')}</b></span></span><span class="freight-card-bottom"><span>${esc(r.captured_at||'—')}</span><b>${esc(r.price||r.list_price||'运价未显示')}</b></span></button>`;
  }).join(''):'<div class="empty-row">暂无匹配货源。采集开始后会自动显示。</div>';
  mobile.querySelectorAll('[data-id]').forEach(card=>card.addEventListener('click',()=>openDetail(Number(card.dataset.id))));
}

function openDetail(id){
  const r=snapshot?.records.find(x=>x.id===id);if(!r)return;
  $('detail-title').textContent=`货源 #${id}`;
  const fields=[['线路',r.route],['采集时间',r.captured_at],['装货地',r.loading_location],['卸货地',r.unloading_location],['货物',r.cargo],['重量',r.weight],['预估里程',r.estimated_distance],['车型要求',r.vehicle],['需要车数',r.truck_count],['运价',r.price||r.list_price],['货主',r.shipper],['其他信息',r.other_info],['处理状态',statusNames[r.status]||r.status]];
  const images=(r.images||[]).filter(image=>image.name.startsWith('detail-'));
  const gallery=images.length?`<div class="detail-images"><div class="detail-images-head"><strong>原始详情截图</strong><span>点击图片可查看原尺寸</span></div><a id="detail-image-link" href="${esc(images[0].url)}" target="_blank" rel="noopener"><img id="detail-image" src="${esc(images[0].url)}" alt="货源 #${id} 详情截图" loading="lazy"></a><div class="image-tabs">${images.map((image,index)=>`<button type="button" data-image="${esc(image.url)}" class="${index===0?'active':''}">详情 ${index+1}</button>`).join('')}</div></div>`:'<div class="detail-images empty-images">该货源暂无可用详情截图</div>';
  $('detail-content').innerHTML='<div class="detail-grid">'+fields.map(([label,value])=>`<div class="detail-item"><span>${label}</span><strong>${esc(value||'未显示')}</strong></div>`).join('')+'</div>'+(r.review_fields?.length?`<p class="review-note">待核对：${r.review_fields.map(x=>esc(snapshot.fields?.[x]||x)).join('、')}</p>`:'')+(r.error?`<p class="review-note">错误：${esc(r.error)}</p>`:'')+gallery;
  $('detail-content').querySelectorAll('[data-image]').forEach(button=>button.addEventListener('click',()=>{const url=button.dataset.image;$('detail-image').src=url;$('detail-image-link').href=url;$('detail-content').querySelectorAll('[data-image]').forEach(x=>x.classList.toggle('active',x===button))}));
  $('detail').showModal();
}

function consoleView(data){
  const p=data.process;
  $('start').disabled=p.running;$('stop').disabled=!p.running;$('process').disabled=p.running;$('reprocess').disabled=p.running;$('clear-images').disabled=p.running||(data.stats?.pending||0)>0;
  const pill=$('run-pill');pill.textContent=p.running?'运行中':p.exit_code===null?'待命':p.exit_code===0?'已完成':'已停止 / 出错';pill.className='badge '+(p.running?'ok':p.exit_code===null?'muted':p.exit_code===0?'ok':'bad');
  $('run-detail').textContent=p.started_at?`${modeNames[p.mode]||'任务'} · ${p.started_at.replace('T',' ')}`:'尚未启动任务';
  const logs=$('logs'),atBottom=logs.scrollHeight-logs.scrollTop-logs.clientHeight<35;
  logs.innerHTML=p.logs.length?p.logs.map(line=>`<div class="log">${esc(line)}</div>`).join(''):'<div class="log empty">等待任务启动…</div>';
  if(atBottom)logs.scrollTop=logs.scrollHeight;
}

async function refresh(){
  if(fetching){refreshQueued=true;return}fetching=true;
  try{
    const query=page==='records'?new URLSearchParams({route,search,vehicle,length,trucks,price,period,day:period==='day'?day:''}):page==='overview'?new URLSearchParams({route,search}):'';
    const response=await fetch('/api/snapshot?'+query,{cache:'no-store'}),data=await response.json();
    if(!response.ok)throw Error(data.error||'读取失败');
    snapshot=data;
    if(page==='overview')overview(data);else if(page==='records')records(data);else consoleView(data);
  }catch(error){toast(error.message)}finally{fetching=false;if(refreshQueued){refreshQueued=false;refresh()}}
}
async function checkDevice(){
  try{const response=await fetch('/api/device'),data=await response.json();$('side-device').textContent=data.connected?`已连接 ${data.devices[0]}`:'手机未连接';$('side-dot').classList.toggle('on',data.connected);if($('device-badge')){$('device-badge').textContent=data.connected?'已连接':'未连接';$('device-badge').className='badge '+(data.connected?'ok':'bad')}}catch{if($('device-badge'))$('device-badge').textContent='检测失败'}
}
async function action(path,payload={}){
  try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await response.json();if(!response.ok)throw Error(data.error||'操作失败');toast(path.endsWith('stop')?'停止指令已发送':'任务已启动');refresh()}catch(error){toast(error.message)}
}
if(page==='records'){
  route=new URLSearchParams(location.search).get('route')||'';
  $('search').addEventListener('input',()=>{clearTimeout(window.searchTimer);window.searchTimer=setTimeout(()=>{search=$('search').value.trim();refresh()},250)});
  $('route-filter').onchange=()=>{route=$('route-filter').value;history.replaceState(null,'',route?'/records?route='+encodeURIComponent(route):'/records');refresh()};
  for(const [id,key] of [['vehicle-filter','vehicle'],['length-filter','length'],['trucks-filter','trucks'],['price-filter','price']]){
    $(id).onchange=()=>{if(key==='vehicle')vehicle=$(id).value;else if(key==='length')length=$(id).value;else if(key==='trucks')trucks=$(id).value;else price=$(id).value;refresh()};
  }
  $('period-filter').onchange=()=>{period=$('period-filter').value;$('day-field').hidden=period!=='day';if(period==='day'&&!day){const now=new Date();day=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;$('day-filter').value=day}refresh()};
  $('day-filter').onchange=()=>{day=$('day-filter').value;refresh()};
  $('reset-filters').onclick=()=>{route=search=vehicle=length=trucks=price=period=day='';$('search').value='';for(const id of ['route-filter','vehicle-filter','length-filter','trucks-filter','price-filter','period-filter','day-filter'])$(id).value='';$('day-field').hidden=true;history.replaceState(null,'','/records');refresh()};
  $('export').onclick=async()=>{
    const button=$('export');button.disabled=true;
    try{
      const query=new URLSearchParams({route,search,vehicle,length,trucks,price,period,day:period==='day'?day:''});
      const response=await fetch('/api/export?'+query);
      if(!response.ok){const result=await response.json();throw Error(result.error||'导出失败')}
      const blob=await response.blob(),url=URL.createObjectURL(blob);
      const disposition=response.headers.get('Content-Disposition')||'';
      const match=/filename\*=UTF-8''([^;]+)/i.exec(disposition);
      const link=document.createElement('a');link.href=url;link.download=match?decodeURIComponent(match[1]):'X001-货源明细.xlsx';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      toast('已导出当前筛选结果');
    }catch(error){toast(error.message)}finally{button.disabled=false}
  };
  $('close-detail').onclick=()=>$('detail').close();$('detail').addEventListener('click',e=>{if(e.target===$('detail'))$('detail').close()});
}
if(page==='overview'){
  $('analysis-route').onchange=()=>{route=$('analysis-route').value;refresh()};
}
if(page==='console'){
  document.querySelectorAll('[data-step]').forEach(button=>button.addEventListener('click',()=>{const input=$(button.dataset.step),minimum=Number(input.min||1),maximum=Number(input.max||100000),current=input.value===''?(button.dataset.delta==='1'?minimum-1:minimum+1):Number(input.value);input.value=Math.max(minimum,Math.min(maximum,current+Number(button.dataset.delta)));input.dispatchEvent(new Event('input',{bubbles:true}))}));
  $('start').onclick=()=>action('/api/start',{mode:'collect',route:$('expected-route').value,maximum:$('maximum').value||null,capacity:$('capacity').value,ocr:$('ocr').value});
  $('stop').onclick=()=>action('/api/stop');$('process').onclick=()=>action('/api/start',{mode:'process',ocr:$('ocr').value});
  $('reprocess').onclick=()=>{if(confirm('将使用已有截图重新识别全部已完成记录。确定开始吗？'))action('/api/start',{mode:'reprocess',ocr:$('ocr').value})};
  $('clear-images').onclick=async()=>{
    if(!confirm('将永久清除全部采集截图。已识别的货源数据会保留，但旧截图无法恢复或重新识别。继续吗？'))return;
    try{const response=await fetch('/api/clear-images',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),result=await response.json();if(!response.ok)throw Error(result.error||'清除失败');toast(`已清除 ${result.removed} 张截图；新截图文件夹从 1 开始`);refresh()}catch(error){toast(error.message)}
  };
}
function tick(){$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false})}
tick();setInterval(tick,1000);refresh();checkDevice();setInterval(refresh,2000);setInterval(checkDevice,15000);
