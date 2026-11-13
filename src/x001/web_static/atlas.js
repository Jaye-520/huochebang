'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let geo, boundaries, records = [], version = 0;
let origin = '', destination = '', originDistrict = '', destinationDistrict = '';
let corridor = '', mapRegion = '', focusedPlace = '', stopCity = '';
let selected = null, activeRouteKey = null;
let view = {x:0,y:0,w:1000,h:640};
const project = (lon,lat) => [(lon-103.5)*56,(42.1-lat)*71];
const cities = () => Object.fromEntries(geo.cities.map(c => [c.name,c]));
const point = place => {
  if (place && typeof place === 'object' && Number.isFinite(place.lon)) return project(place.lon,place.lat);
  const c = cities()[place];
  return c ? project(c.lon,c.lat) : null;
};
const scope = name => geo.city_scopes?.[name] || name;
const district = id => geo.districts?.find(d => d.id === id) || null;
const priceLabel = r => {
  const value = String(r.price || r.list_price || '').trim();
  if (!value) return '运价未填写';
  return /请先|协商|电议/.test(value) ? '协商价' : value;
};
const numericPrice = r => {
  const value = String(r.price || r.list_price || '').trim();
  return /[0-9]/.test(value) && !/请先|协商|电议/.test(value);
};
const distanceOf = r => {
  const value = parseFloat(String(r.estimated_distance || '').replace(/[^\d.]/g,''));
  return Number.isFinite(value) ? value : null;
};
const routeKeyOf = r => JSON.stringify([r.origin || '', r.destination || '']);
const routeLabel = ([o,d]) => `${o || '待定位'} → ${d || '待定位'}`;
const activeCorridor = () => geo.corridors.find(c => c.id === corridor);
const shortTime = value => String(value || '').slice(5,16) || '时间未知';
const latestTime = list => list.map(r => r.captured_at || '').sort().at(-1) || '';

function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = false;
}
async function read(url) {
  const response = await fetch(url,{cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '读取失败');
  return data;
}
function resolveCity(value) {
  const query = String(value || '').trim();
  if (!query) return '';
  const normalized = query.replace(/市$/,'');
  const matches = geo.cities.filter(c => [c.name,c.full_name,...(c.aliases || [])].some(name => String(name || '').replace(/市$/,'') === normalized));
  return matches.length ? scope(matches[0].name) : null;
}
function resolvePlace(value) {
  const query = String(value || '').trim();
  if (!query) return null;
  const cityMatches = geo.cities.filter(c => [c.name,c.full_name,...(c.aliases || [])].includes(query));
  if (cityMatches.length === 1) return {city:scope(cityMatches[0].name),district:'',label:scope(cityMatches[0].name)};
  const districtMatches = geo.districts.filter(d => [d.name,d.full_name,`${d.parent} / ${d.full_name}`].includes(query));
  if (districtMatches.length === 1) {
    const d = districtMatches[0];
    return {city:d.parent,district:d.id,label:`${d.parent} / ${d.full_name}`};
  }
  return null;
}
function searchedRecords() {
  const q = $('search').value.trim().toLowerCase();
  return records.filter(r => !q || [r.loading_location,r.unloading_location,r.cargo,r.route,r.origin,r.destination]
    .some(v => String(v || '').toLowerCase().includes(q)));
}
function filteredRecords() {
  return searchedRecords().filter(r => {
    if (origin && r.origin !== origin) return false;
    if (destination && r.destination !== destination) return false;
    if (originDistrict && r.origin_district !== originDistrict) return false;
    if (destinationDistrict && r.destination_district !== destinationDistrict) return false;
    return true;
  });
}
function routeGroups() {
  const groups = new Map();
  for (const record of filteredRecords()) {
    const key = routeKeyOf(record);
    const group = groups.get(key) || {key,records:[]};
    group.records.push(record);
    groups.set(key,group);
  }
  return groups;
}
function sortedGroups(groups) {
  return [...groups.values()].sort((a,b) => b.records.length - a.records.length ||
    routeLabel(JSON.parse(a.key)).localeCompare(routeLabel(JSON.parse(b.key)),'zh-CN'));
}
function distanceLabel(list) {
  const values = list.map(distanceOf).filter(v => v !== null).sort((a,b) => a-b);
  if (!values.length) return '里程未识别';
  return values[0] === values.at(-1) ? `${values[0]} km` : `${values[0]}–${values.at(-1)} km`;
}
function plannerStatus(count = filteredRecords().length) {
  const a = originDistrict ? `${origin} · ${district(originDistrict)?.full_name || '区县'}` : origin;
  const b = destinationDistrict ? `${destination} · ${district(destinationDistrict)?.full_name || '区县'}` : destination;
  if (a && b) $('planner-status').textContent = `当前规划：${a} → ${b} · 匹配 ${count} 条货源。点击地图节点不会清除线路。`;
  else if (a) $('planner-status').textContent = `已设起点 ${a}，继续选择终点；也可以先查看从该城市出发的线路。`;
  else if (b) $('planner-status').textContent = `已设终点 ${b}，可继续选择起点；当前展示到达该城市的线路。`;
  else $('planner-status').textContent = '可只填起点或终点，也可以直接在地图上依次选择两个城市。';
}
function syncInputs() {
  $('origin-input').value = origin;
  $('destination-input').value = destination;
}
function planFromState() {
  const typedOrigin = resolveCity($('origin-input').value);
  const typedDestination = resolveCity($('destination-input').value);
  if (typedOrigin === null || typedDestination === null) {
    notice('请输入列表中的城市名称，例如「银川」「唐山」。');
    return;
  }
  if (typedOrigin && typedDestination && typedOrigin === typedDestination) {
    notice('起点和终点不能是同一个城市。');
    return;
  }
  origin = typedOrigin;
  destination = typedDestination;
  originDistrict = destinationDistrict = '';
  activeRouteKey = null;
  stopCity = '';
  selected = null;
  syncInputs();
  $('notice').hidden = true;
  if (origin && destination) {
    const key = JSON.stringify([origin,destination]);
    if (routeGroups().has(key)) activeRouteKey = key;
  }
  renderAll();
  if (activeRouteKey) focusRoute(JSON.parse(activeRouteKey));
}
function setEndpoint(place) {
  if (!place) return;
  focusedPlace = place.district || place.city;
  if (origin && destination && activeRouteKey !== null) {
    stopCity = place.city;
    selected = null;
    renderAll();
    $('place-status').textContent = `正在查看 ${place.label} 节点货源；当前线路保留`;
    return;
  }
  if (origin && destination) {
    origin = place.city; destination = '';
    originDistrict = place.district; destinationDistrict = '';
  } else if (!origin) {
    origin = place.city; originDistrict = place.district;
  } else {
    destination = place.city; destinationDistrict = place.district;
  }
  activeRouteKey = null;
  stopCity = '';
  selected = null;
  syncInputs();
  if (origin && destination) {
    const key = JSON.stringify([origin,destination]);
    if (routeGroups().has(key)) activeRouteKey = key;
  }
  renderAll();
  if (activeRouteKey) focusRoute(JSON.parse(activeRouteKey));
  $('place-status').textContent = `已选择 ${place.label}`;
}
function resetAll() {
  origin = destination = originDistrict = destinationDistrict = '';
  corridor = mapRegion = focusedPlace = stopCity = '';
  activeRouteKey = null; selected = null;
  view = {x:0,y:0,w:1000,h:640};
  syncInputs();
  $('search').value = '';
  $('period').value = '';
  $('place-search').value = '';
  $('map-region').value = '';
  $('notice').hidden = true;
  refresh();
}

function renderSummary() {
  const list = filteredRecords();
  const groups = routeGroups();
  const numeric = list.filter(numericPrice).length;
  $('total').textContent = list.length;
  $('route-total').textContent = groups.size;
  $('numeric-price').textContent = list.length ? `${Math.round(numeric / list.length * 100)}%` : '—';
  plannerStatus(list.length);
}
function renderRoutes() {
  const groups = routeGroups();
  if (activeRouteKey !== null && !groups.has(activeRouteKey)) {
    activeRouteKey = null;
    selected = null;
  }
  const list = sortedGroups(groups);
  $('route-count').textContent = `${list.length} 条线路`;
  $('route-groups').innerHTML = list.length ? list.map(group => {
    const [o,d] = JSON.parse(group.key);
    const items = group.records;
    const cargo = [...new Set(items.map(r => r.cargo).filter(Boolean))];
    const cargoText = cargo.length > 2 ? `${cargo.slice(0,2).join('、')} 等 ${cargo.length} 类` : cargo.join('、') || '货物待识别';
    const numeric = items.filter(numericPrice).length;
    return `<button class="route-card ${activeRouteKey === group.key ? 'selected' : ''}" data-route-key="${esc(group.key)}" aria-pressed="${activeRouteKey === group.key}">
      <span class="route-card-title"><b>${esc(o || '待定位')}</b><i>→</i><b>${esc(d || '待定位')}</b><em>${items.length} 条</em></span>
      <span class="route-card-cargo">${esc(cargoText)}</span>
      <span class="route-card-metrics"><span>数字运价 <b>${numeric}/${items.length}</b></span><span>${esc(distanceLabel(items))}</span><span>最近 ${esc(shortTime(latestTime(items)))}</span></span>
    </button>`;
  }).join('') : '<div class="empty">没有匹配的线路<br>调整起点、终点、时间或搜索词。</div>';
  $('route-groups').querySelectorAll('[data-route-key]').forEach(button => button.onclick = () => {
    activeRouteKey = button.dataset.routeKey;
    stopCity = ''; selected = null;
    renderAll();
    focusRoute(JSON.parse(activeRouteKey));
  });
}
function contextList() {
  if (stopCity) {
    return {title:`${stopCity} · 沿途节点`,intro:'按节点城市匹配的货源，仅用于查看沿途机会，不代表无需绕行。',records:searchedRecords().filter(r => r.origin === stopCity || r.destination === stopCity),stop:true};
  }
  if (activeRouteKey !== null) {
    const group = routeGroups().get(activeRouteKey);
    if (group) return {title:routeLabel(JSON.parse(activeRouteKey)),intro:'同一装卸城市下的全部货源。点击任一条查看完整字段与原始截图。',records:group.records,stop:false};
  }
  return null;
}
function renderFreight() {
  const context = contextList();
  $('route-clear').hidden = !context;
  $('route-clear').textContent = context?.stop ? '返回线路' : '取消线路';
  $('route-summary').hidden = !context;
  if (!context) {
    $('freight-title').textContent = '线路货源';
    $('freight-count').textContent = '';
    $('route-intro').textContent = '从左侧选择一条候选线路，在这里比较该线路的全部货源。';
    $('route-orders').innerHTML = '<div class="empty freight-empty"><span>↗</span><b>尚未选择线路</b><p>设置起终点后，可从左侧候选线路进入规划上下文。</p></div>';
    return;
  }
  const list = context.records;
  $('freight-title').textContent = context.title;
  $('freight-count').textContent = `${list.length} 条`;
  $('route-intro').textContent = context.intro;
  const numeric = list.filter(numericPrice).length;
  $('route-summary').innerHTML = `<div><strong>${list.length}</strong><span>匹配货源</span></div><div><strong>${numeric}/${list.length}</strong><span>数字运价</span></div><div><strong>${esc(distanceLabel(list).replace(' km',''))}</strong><span>里程 km</span></div>`;
  $('route-orders').innerHTML = list.length ? list.map(r => `<button class="order-card ${selected === r.id ? 'selected' : ''}" data-id="${r.id}" aria-pressed="${selected === r.id}">
    <span class="order-head"><b>#${r.id}</b><time>${esc(shortTime(r.captured_at))}</time></span>
    <strong>${esc(r.cargo || '货物待识别')}</strong>
    <span class="order-location">${esc(r.origin || '装货地待定位')} → ${esc(r.destination || '卸货地待定位')}</span>
    <span class="order-meta">${esc(r.weight || '重量未填写')} · ${esc(r.vehicle || '车型未填写')}</span>
    <span class="order-foot"><b>${esc(priceLabel(r))}</b><span>${esc(r.truck_count || '车数未显示')}</span></span>
  </button>`).join('') : '<div class="empty">当前线路暂无可显示的货源。</div>';
  $('route-orders').querySelectorAll('[data-id]').forEach(button => button.onclick = () => {
    selected = Number(button.dataset.id);
    $('route-orders').querySelectorAll('.order-card').forEach(card => card.classList.toggle('selected',card === button));
    renderMap();
    openDetail(selected);
  });
}
function openDetail(id) {
  const r = records.find(item => item.id === id);
  if (!r) return;
  const fields = [
    ['线路',r.route],['采集时间',r.captured_at],['装货地点',r.loading_location],['卸货地点',r.unloading_location],
    ['货物',r.cargo],['重量',r.weight],['预估里程',r.estimated_distance],['车型 / 车长',r.vehicle],
    ['需要车数',r.truck_count],['运价',r.price || r.list_price],['货主',r.shipper],['其他信息',r.other_info]
  ];
  const images = (r.images || []).filter(image => String(image.name || '').startsWith('detail-'));
  const gallery = images.length ? `<div class="detail-images"><div class="detail-images-head"><strong>原始详情截图</strong><span>点击图片查看原尺寸</span></div><a id="detail-image-link" href="${esc(images[0].url)}" target="_blank" rel="noopener"><img id="detail-image" src="${esc(images[0].url)}" alt="货源 #${id} 详情截图" loading="lazy"></a><div class="image-tabs">${images.map((image,index) => `<button type="button" data-image="${esc(image.url)}" class="${index === 0 ? 'active' : ''}">详情 ${index+1}</button>`).join('')}</div></div>` : '<div class="detail-images empty-images">该货源暂无可用详情截图</div>';
  $('detail-title').textContent = `货源 #${id} · ${r.origin || '待定位'} → ${r.destination || '待定位'}`;
  $('detail-content').innerHTML = '<div class="detail-grid">' + fields.map(([label,value]) => `<div class="detail-item"><span>${label}</span><strong>${esc(value || '未显示')}</strong></div>`).join('') + '</div>' +
    (r.review ? '<p class="review-note">包含待核对字段，请结合原始截图确认。</p>' : '') + gallery +
    `<a class="detail-link" href="/records?route=${encodeURIComponent(r.route || '')}" target="_blank" rel="noopener">${r.route ? '查看采集线路「' + esc(r.route) + '」的明细' : '查看全部货源明细'} ↗</a>`;
  $('detail-content').querySelectorAll('[data-image]').forEach(button => button.onclick = () => {
    $('detail-image').src = button.dataset.image;
    $('detail-image-link').href = button.dataset.image;
    $('detail-content').querySelectorAll('[data-image]').forEach(item => item.classList.toggle('active',item === button));
  });
  const dialog = $('freight-detail');
  if (typeof dialog.showModal === 'function') dialog.showModal();
}
function renderCorridors() {
  const choices = [{id:'',road:'',name:'不显示'},...geo.corridors];
  $('corridors').innerHTML = choices.map(c => `<button class="${corridor === c.id ? 'active' : ''}" data-corridor="${esc(c.id)}" aria-pressed="${corridor === c.id}">${c.road ? esc(c.road) + ' · ' : ''}${esc(c.name)}</button>`).join('');
  $('corridors').querySelectorAll('[data-corridor]').forEach(button => button.onclick = () => {
    corridor = button.dataset.corridor;
    stopCity = '';
    renderAll();
  });
}
function renderStops() {
  const c = activeCorridor();
  if (!c) {
    $('stops').innerHTML = '<p class="stop-empty">选择一条参考走廊，查看沿途节点及节点货源数量。</p>';
    return;
  }
  const totals = {};
  for (const r of records) for (const name of new Set([r.origin,r.destination].filter(Boolean))) totals[name] = (totals[name] || 0) + 1;
  $('stops').innerHTML = c.cities.map((name,i) => {
    const city = scope(name);
    const count = totals[city] || 0;
    return `<button class="stop ${stopCity === city ? 'selected' : ''}" data-city="${esc(city)}"><span>${String(i+1).padStart(2,'0')} · ${esc(name)}</span><b>${count} 条</b></button>`;
  }).join('');
  $('stops').querySelectorAll('[data-city]').forEach(button => button.onclick = () => {
    stopCity = stopCity === button.dataset.city ? '' : button.dataset.city;
    selected = null;
    renderAll();
  });
}
function boundaryPath(feature) {
  const polys = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
  return polys.map(poly => poly.map(ring => ring.map((coord,i) => (i ? 'L' : 'M') + project(...coord).join(',')).join('') + 'Z').join('')).join('');
}
function recordPoint(r,side) {
  const id = side === 'origin' ? r.origin_district : r.destination_district;
  return point(district(id)) || point(r[side]);
}
function renderMap() {
  const list = filteredRecords(), c = activeCorridor();
  const routePair = activeRouteKey ? JSON.parse(activeRouteKey) : null;
  const r = records.find(item => item.id === selected);
  const totals = {};
  for (const record of list) for (const name of new Set([record.origin,record.destination].filter(Boolean))) totals[name] = (totals[name] || 0) + 1;
  const districtCounts = {};
  for (const record of list) for (const id of new Set([record.origin_district,record.destination_district].filter(Boolean))) districtCounts[id] = (districtCounts[id] || 0) + 1;
  let html = boundaries.features.map(f => `<path class="province ${['河北省','天津市','宁夏回族自治区'].includes(f.properties.name) ? 'focus' : ''}" d="${boundaryPath(f)}"/>`).join('');
  const labels = [['宁夏',105.6,38.8],['内蒙古',110,41],['陕西',108.5,36.4],['山西',111.8,36.7],['河北',116.2,40.7],['天津',118,38.7],['甘肃',103.7,36.3],['山东',117.2,36.4],['河南',113.5,35.4],['北京',116.4,40.2]];
  if (!mapRegion) html += labels.map(([n,x,y]) => {const p = project(x,y);return `<text class="province-label" x="${p[0]}" y="${p[1]}">${n}</text>`}).join('');
  if (c) html += `<polyline class="corridor" points="${c.cities.map(n => point(n) || point(scope(n))).filter(Boolean).map(p => p.join(',')).join(' ')}"/>`;
  if (routePair?.[0] && routePair?.[1]) {
    const start = point(routePair[0]), end = point(routePair[1]);
    if (start && end) html += `<path class="route-line" d="M${start.join(',')}L${end.join(',')}"/>`;
  }
  if (r) {
    const start = recordPoint(r,'origin'), end = recordPoint(r,'destination');
    if (start && end) html += `<path class="od" d="M${start.join(',')}L${end.join(',')}"/>`;
  }
  const corridorNames = new Set(c?.cities.map(scope) || []);
  const selectedDistricts = new Set([r?.origin_district,r?.destination_district,originDistrict,destinationDistrict].filter(Boolean));
  const nodes = [
    ...geo.cities.map(n => ({...n,isDistrict:false})),
    ...(geo.districts || []).filter(n => n.province === mapRegion || selectedDistricts.has(n.id)).map(n => ({...n,isDistrict:true}))
  ];
  const rect = $('map').getBoundingClientRect?.() || {width:1000,height:640};
  const scale = view.w / Math.max(200,Math.min(1000,rect.width,rect.height / .64));
  const occupied = [];
  function priority(n) {
    return (focusedPlace === (n.id || n.name) ? 1000 : 0) +
      (origin === n.name || destination === n.name || stopCity === n.name ? 500 : 0) +
      (routePair?.includes(scope(n.name)) ? 300 : 0) +
      (selectedDistricts.has(n.id) ? 250 : 0) +
      (n.isDistrict ? 100 : 0) +
      (corridorNames.has(scope(n.name)) ? 40 : 0) +
      (totals[scope(n.name)] ? 20 : 0);
  }
  nodes.sort((a,b) => priority(b) - priority(a));
  for (const node of nodes) {
    const p = project(node.lon,node.lat), isDistrict = node.isDistrict, nodeScope = scope(node.name);
    const nodeCount = isDistrict ? (districtCounts[node.id] || 0) : (totals[nodeScope] || 0);
    const scopeLabel = isDistrict ? `${node.parent} · 区县` : nodeScope === node.name ? '城市级' : `${nodeScope}市域`;
    const chosen = isDistrict ? selectedDistricts.has(node.id) || originDistrict === node.id || destinationDistrict === node.id :
      origin === nodeScope || destination === nodeScope || stopCity === nodeScope || routePair?.includes(nodeScope);
    const relevant = focusedPlace === (node.id || node.name) || chosen || corridorNames.has(nodeScope);
    if (!nodeCount && !relevant && !(isDistrict && mapRegion)) continue;
    const radius = nodeCount ? Math.min(12,5 + Math.sqrt(nodeCount) * 1.25) : isDistrict ? 2.5 : 3.5;
    const markerRadius = chosen ? Math.max(8,radius) : radius;
    const labelText = isDistrict ? node.full_name : node.name;
    const inside = p[0] >= view.x && p[0] <= view.x + view.w && p[1] >= view.y && p[1] <= view.y + view.h;
    let label = '';
    if (inside && (!mapRegion || node.province === mapRegion) && (nodeCount || relevant)) {
      const width = (labelText.length * 13 + 8) * scale, height = 18 * scale;
      for (const [dx,dy] of [[9,-8],[9,19],[-(labelText.length * 13 + 16),-8],[-(labelText.length * 13 + 16),19]]) {
        const box = {x:p[0] + dx * scale,y:p[1] + (dy - 14) * scale,w:width,h:height};
        if (!occupied.some(b => box.x < b.x + b.w && box.x + box.w > b.x && box.y < b.y + b.h && box.y + box.h > b.y)) {
          occupied.push(box);
          label = `<text x="${dx * scale}" y="${dy * scale}" style="font-size:${13 * scale}px;stroke-width:${3 * scale}px">${esc(labelText)}</text>`;
          break;
        }
      }
    }
    html += `<g class="city ${isDistrict ? 'district' : ''} ${nodeCount ? 'has-freight' : ''} ${chosen ? 'chosen' : ''}" ${isDistrict ? `data-place='${esc(JSON.stringify({city:node.parent,district:node.id,label:`${node.parent} / ${node.full_name}`}))}'` : `data-place='${esc(JSON.stringify({city:nodeScope,district:'',label:nodeScope}))}'`} role="button" tabindex="0" aria-label="${esc(labelText)}，${esc(scopeLabel)}，${nodeCount} 条货源" transform="translate(${p})"><title>${esc(labelText)} · ${nodeCount} 条货源（${esc(scopeLabel)}）</title><circle r="${markerRadius * scale}" style="stroke-width:${1.5 * scale}px"/>${label}</g>`;
  }
  $('geography').innerHTML = html;
  $('geography').querySelectorAll('.city').forEach(el => {
    const choose = () => setEndpoint(JSON.parse(el.dataset.place));
    el.onclick = () => {if (!dragMoved) choose()};
    el.onkeydown = e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault();choose()}};
  });
  applyView();
}
function renderContext() {
  if (activeRouteKey) {
    $('map-title').textContent = routeLabel(JSON.parse(activeRouteKey));
    $('map-context').textContent = stopCity ? `正在查看 ${stopCity} 节点货源；当前线路 ${routeLabel(JSON.parse(activeRouteKey))} 仍保留。` : '当前线路已锁定。点击地图城市可查看节点货源；更换起终点请修改上方 A / B。';
  } else if (origin || destination) {
    $('map-title').textContent = '候选线路分布';
    $('map-context').textContent = `${origin || '任意起点'} → ${destination || '任意终点'} · 选择左侧线路后进入规划上下文。`;
  } else {
    $('map-title').textContent = '货源流向总览';
    $('map-context').textContent = '在地图上依次点击两个城市，即可形成起点和终点。';
  }
}
function renderAll() {
  if (!geo || !boundaries) return;
  renderSummary();
  renderRoutes();
  renderFreight();
  renderCorridors();
  renderStops();
  renderMap();
  renderContext();
}
function focusNodes(nodes) {
  const points = nodes.map(n => project(n.lon,n.lat)).filter(Boolean);
  if (!points.length) return;
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const w = Math.max(150,(Math.max(...xs) - Math.min(...xs)) * 1.3,(Math.max(...ys) - Math.min(...ys)) * 1.3 / .64);
  view = {x:cx - w / 2,y:cy - w * .32,w,h:w * .64};
  renderMap();
}
function focusRoute([o,d]) {
  const nodes = [cities()[o],cities()[d]].filter(Boolean);
  if (!nodes.length) return;
  const points = nodes.map(n => project(n.lon,n.lat));
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const w = Math.max(320,(Math.max(...xs) - Math.min(...xs)) * 1.35,(Math.max(...ys) - Math.min(...ys)) * 1.35 / .64);
  view = {x:cx - w / 2,y:cy - w * .32,w,h:w * .64};
  renderMap();
}
function setRegion(region) {
  mapRegion = region;
  focusedPlace = '';
  if (region) focusNodes(geo.districts.filter(d => d.province === region));
  else {view = {x:0,y:0,w:1000,h:640};renderMap()}
  $('place-status').textContent = region ? `${region} · ${geo.districts.filter(d => d.province === region).length} 个区县，点击区县可作为规划端点` : '全局城市视图 · 可切换到区县';
}
function setupPlaces() {
  const cityNames = [...new Set(geo.cities.map(c => scope(c.name)))].sort((a,b) => a.localeCompare(b,'zh-CN'));
  $('city-options').innerHTML = cityNames.map(name => `<option value="${esc(name)}"></option>`).join('');
  $('place-options').innerHTML = [
    ...geo.cities.filter(c => !c.scope).map(c => `<option value="${esc(c.name)}">${esc(c.province)}</option>`),
    ...geo.districts.map(d => `<option value="${esc(`${d.parent} / ${d.full_name}`)}"></option>`)
  ].join('');
  $('map-region').onchange = () => setRegion($('map-region').value);
  const find = () => {
    const place = resolvePlace($('place-search').value);
    if (!place) {$('place-status').textContent = '没有精确匹配地点，请从下拉列表选择城市或「城市 / 区县」。';return}
    mapRegion = place.district ? district(place.district).province : '';
    $('map-region').value = mapRegion;
    if (place.district) focusNodes([district(place.district)]);
    setEndpoint(place);
  };
  $('find-place').onclick = find;
  $('place-search').onkeydown = e => {if (e.key === 'Enter') {e.preventDefault();find()}};
  setRegion('');
}
async function refresh() {
  if (!geo || !boundaries) return;
  const current = ++version;
  $('sync').textContent = '正在读取…';
  try {
    const data = await read('/api/atlas?' + new URLSearchParams({period:$('period').value}));
    if (current !== version) return;
    records = data.records;
    if (!records.some(r => r.id === selected)) selected = null;
    if (stopCity && !records.some(r => r.origin === stopCity || r.destination === stopCity)) stopCity = '';
    const groups = routeGroups();
    if (activeRouteKey !== null && !groups.has(activeRouteKey)) {
      activeRouteKey = null;
      selected = null;
    }
    $('sync').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN',{hour12:false});
    $('notice').hidden = true;
    renderAll();
  } catch (e) {
    if (current !== version) return;
    records = [];
    selected = null; activeRouteKey = null; stopCity = '';
    for (const id of ['total','route-total','numeric-price']) $(id).textContent = '—';
    $('route-groups').innerHTML = '<div class="empty">读取失败，请点击「刷新数据」重试。</div>';
    $('route-orders').innerHTML = '<div class="empty">货源读取失败。</div>';
    $('sync').textContent = '读取失败';
    notice('货源读取失败：' + e.message);
  }
}
function applyView() {$('map').setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`)}
function zoom(factor) {
  const w = Math.max(60,Math.min(1600,view.w * factor)), h = w * .64;
  view = {x:view.x + (view.w - w) / 2,y:view.y + (view.h - h) / 2,w,h};
  renderMap();
}
let drag = null, dragMoved = false;
$('map').onpointerdown = e => {if (e.button !== 0) return;drag = {x:e.clientX,y:e.clientY,v:{...view}};dragMoved = false};
$('map').onpointermove = e => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 5) {dragMoved = true;$('map').setPointerCapture?.(e.pointerId)}
  const rect = $('map').getBoundingClientRect(), scale = Math.max(view.w / rect.width,view.h / rect.height);
  view.x = drag.v.x - dx * scale; view.y = drag.v.y - dy * scale;
  applyView();
};
$('map').onpointerup = () => {drag = null;if (dragMoved) renderMap()};
$('map').onpointercancel = () => {drag = null};
$('zoom-in').onclick = () => zoom(.8);
$('zoom-out').onclick = () => zoom(1.25);
$('reset-map').onclick = () => setRegion(mapRegion);
$('plan').onclick = planFromState;
$('origin-input').onkeydown = e => {if (e.key === 'Enter') {e.preventDefault();planFromState()}};
$('destination-input').onkeydown = e => {if (e.key === 'Enter') {e.preventDefault();planFromState()}};
$('swap').onclick = () => {
  const a = $('origin-input').value, b = $('destination-input').value;
  $('origin-input').value = b; $('destination-input').value = a;
  const od = originDistrict; originDistrict = destinationDistrict; destinationDistrict = od;
  planFromState();
};
$('reset').onclick = resetAll;
$('refresh').onclick = refresh;
$('period').onchange = refresh;
$('search').oninput = () => {
  if (selected !== null && !filteredRecords().some(r => r.id === selected)) selected = null;
  renderAll();
};
$('route-clear').onclick = () => {
  if (stopCity) stopCity = '';
  else {activeRouteKey = null;view = {x:0,y:0,w:1000,h:640}}
  selected = null;
  renderAll();
};
$('close-detail').onclick = () => $('freight-detail').close?.();
$('freight-detail').addEventListener?.('click',e => {if (e.target === $('freight-detail')) $('freight-detail').close?.()});
(async () => {
  try {
    [geo,boundaries] = await Promise.all([read('/atlas-data.json'),read('/china-map.json')]);
    setupPlaces();
    await refresh();
  } catch (e) {
    notice('地图资源加载失败，请刷新页面：' + e.message);
  }
})();
let resizeTimer;
window.addEventListener('resize',() => {clearTimeout(resizeTimer);resizeTimer = setTimeout(() => {if (geo && boundaries) renderMap()},100)});
