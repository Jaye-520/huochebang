// DOM-free state checks for the route-planning homepage; run with node --test.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: '', checked: false, hidden: false, innerHTML: '', textContent: '',
      dataset: {},
      classList: {toggle() {}, add() {}, remove() {}},
      querySelectorAll: () => [],
      setAttribute() {}, addEventListener() {}, close() {}, showModal() {},
      getBoundingClientRect: () => ({width:1000,height:640,top:0,left:0})
    });
    return elements.get(id);
  };
  const ctx = vm.createContext({
    document: {getElementById: element},
    fetch: () => new Promise(() => {}),
    URLSearchParams, console, Date, Number, Math, JSON, String, Array, Object, Promise, Error,
    window: {addEventListener() {}},
    setTimeout, clearTimeout
  });
  vm.runInContext(fs.readFileSync('src/x001/web_static/atlas.js','utf8'),ctx);
  ctx.testGeo = JSON.parse(fs.readFileSync('src/x001/web_static/atlas-data.json','utf8'));
  vm.runInContext(`
    geo=testGeo;boundaries={features:[]};
    origin=destination=originDistrict=destinationDistrict=corridor=mapRegion=focusedPlace=stopCity='';
    activeRouteKey=null;selected=null;view={x:0,y:0,w:1000,h:640};
    records=[
      {id:1,origin:'银川',destination:'唐山',loading_location:'银川市',unloading_location:'唐山市',cargo:'钢材',captured_at:'2026-11-13 10:00',price:'270元/吨',estimated_distance:'1186.9km',images:[]},
      {id:2,origin:'银川',destination:'唐山',loading_location:'银川市',unloading_location:'唐山市',cargo:'煤炭',captured_at:'2026-11-13 09:00',price:'请先与货主协商',estimated_distance:'1100km',images:[]},
      {id:3,origin:'石嘴山',destination:'邯郸',loading_location:'石嘴山市',unloading_location:'邯郸市',cargo:'焦炭',captured_at:'2026-11-12 08:00',price:'200元/吨',estimated_distance:'900km',images:[]}
    ];
  `,ctx);
  return {ctx,element,run:code => vm.runInContext(code,ctx)};
}

test('typed origin and destination form one route context',() => {
  const {element,run} = fixture();
  element('origin-input').value = '银川';
  element('destination-input').value = '唐山';
  run('planFromState()');
  assert.equal(run('origin'),'银川');
  assert.equal(run('destination'),'唐山');
  assert.equal(run('activeRouteKey'),JSON.stringify(['银川','唐山']));
  assert.equal(element('freight-count').textContent,'2 条');
  assert.match(element('freight-title').textContent,/银川 → 唐山/);
  assert.match(element('planner-status').textContent,/银川 → 唐山 · 匹配 2 条/);
  assert.ok(element('route-summary').innerHTML.includes('1/2'));
});

test('two map clicks create origin and destination instead of replacing the first',() => {
  const {element,run} = fixture();
  run(`setEndpoint({city:'银川',district:'',label:'银川'});setEndpoint({city:'唐山',district:'',label:'唐山'})`);
  assert.equal(run('origin'),'银川');
  assert.equal(run('destination'),'唐山');
  assert.equal(run('activeRouteKey'),JSON.stringify(['银川','唐山']));
  assert.equal(element('origin-input').value,'银川');
  assert.equal(element('destination-input').value,'唐山');
  assert.match(element('place-status').textContent,/唐山/);
});

test('numeric price coverage excludes negotiated prices',() => {
  const {element,run} = fixture();
  run(`records=[{id:1,origin:'银川',destination:'唐山',price:'270元/吨',captured_at:'2026-11-13',images:[]},{id:2,origin:'银川',destination:'唐山',price:'请先与货主协商',captured_at:'2026-11-13',images:[]}];renderSummary()`);
  assert.equal(element('total').textContent,2);
  assert.equal(element('numeric-price').textContent,'50%');
});

test('route freight list stays intact and uses a separate detail dialog',() => {
  const {element,run} = fixture();
  run(`origin='银川';destination='唐山';activeRouteKey=JSON.stringify(['银川','唐山']);renderAll()`);
  assert.equal(element('route-summary').hidden,false);
  assert.equal((element('route-orders').innerHTML.match(/class="order-card/g) || []).length,2);
  assert.ok(element('route-orders').innerHTML.includes('钢材'));
  assert.equal(element('freight-detail').innerHTML,'');
  run("records[0].route='宁夏河北'");
  run('openDetail(1)');
  assert.match(element('detail-title').textContent,/货源 #1/);
  assert.ok(element('detail-content').innerHTML.includes('270元/吨'));
  assert.ok(element('detail-content').innerHTML.includes('查看采集线路「宁夏河北」的明细'));
});

test('corridor node inspection keeps the active route',() => {
  const {element,run} = fixture();
  run(`origin='银川';destination='唐山';activeRouteKey=JSON.stringify(['银川','唐山']);corridor='g20';stopCity='石家庄';renderAll()`);
  assert.equal(run('activeRouteKey'),JSON.stringify(['银川','唐山']));
  assert.match(element('freight-title').textContent,/石家庄 · 沿途节点/);
  assert.equal(element('route-clear').hidden,false);
  assert.equal(element('route-clear').textContent,'返回线路');
});

test('empty search clears selected route and its map line',() => {
  const {element,run} = fixture();
  run(`origin='银川';destination='唐山';activeRouteKey=JSON.stringify(['银川','唐山']);renderAll()`);
  element('search').value = '不存在的货物';
  run('renderAll()');
  assert.equal(element('total').textContent,0);
  assert.equal(run('activeRouteKey'),null);
  assert.ok(element('route-groups').innerHTML.includes('没有匹配的线路'));
  assert.ok(!element('geography').innerHTML.includes('class="route-line"'));
});

test('selected freight can use district coordinates for its map endpoints',() => {
  const {run} = fixture();
  const point = run(`(()=>{const d=geo.districts[0];const r={id:9,origin:'银川',destination:'唐山',origin_district:d.id};return recordPoint(r,'origin')})()`);
  assert.ok(Array.isArray(point));
  assert.ok(point.every(Number.isFinite));
});

test('clicking a map city after locking a route inspects the node instead of rewriting endpoints',() => {
  const {element,run} = fixture();
  run(`origin='银川';destination='唐山';activeRouteKey=JSON.stringify(['银川','唐山']);renderAll();setEndpoint({city:'石家庄',district:'',label:'石家庄'})`);
  assert.equal(run('origin'),'银川');
  assert.equal(run('destination'),'唐山');
  assert.equal(run('activeRouteKey'),JSON.stringify(['银川','唐山']));
  assert.equal(run('stopCity'),'石家庄');
  assert.match(element('freight-title').textContent,/石家庄 · 沿途节点/);
});

test('node freight respects the active search without applying locked origin and destination',() => {
  const {element,run} = fixture();
  run(`origin='银川';destination='唐山';activeRouteKey=JSON.stringify(['银川','唐山']);stopCity='石嘴山'`);
  element('search').value = '焦炭';
  run('renderFreight()');
  assert.match(element('freight-title').textContent,/石嘴山 · 沿途节点/);
  assert.equal(element('freight-count').textContent,'1 条');
  assert.ok(element('route-orders').innerHTML.includes('焦炭'));
});

test('failed refresh clears previous results and allows retry',async () => {
  const {ctx,element,run} = fixture();
  ctx.fetch = async () => {throw Error('offline')};
  await run('refresh()');
  assert.equal(run('records.length'),0);
  assert.equal(element('total').textContent,'—');
  assert.ok(element('route-groups').innerHTML.includes('读取失败'));
  assert.equal(element('notice').hidden,false);
  ctx.fetch = async () => ({ok:true,json:async()=>({records:[]})});
  await run('refresh()');
  assert.equal(element('notice').hidden,true);
  assert.equal(element('total').textContent,0);
});
