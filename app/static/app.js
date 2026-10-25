/* QPWS Permits Explorer — frontend. Vanilla JS + ECharts + MapLibre + Tabulator. */
(() => {
'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const fmt = n => n == null ? '–' : (Math.abs(n) >= 1e6 ? (n/1e6).toFixed(2)+'M' : Math.abs(n) >= 1e4 ? (n/1e3).toFixed(1)+'k' : Number.isInteger(n) ? n.toLocaleString() : (+n).toLocaleString(undefined,{maximumFractionDigits:2}));
const fmtFull = n => n == null ? '–' : (+n).toLocaleString();
const PAL = ['#1f6f4a','#c8742b','#2d5fa8','#8a5fb3','#b8a12b','#c23c5c','#2aa198','#7f8c8d','#9b6f3b','#4f7f3a'];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const LABEL = {bookings:'Bookings', nights:'Nights', person_nights:'Person-nights', people:'People', avg_nights:'Avg nights', avg_people:'Avg party size', avg_lead_days:'Avg lead days',
  permits:'Permits', permit_days:'Permit-days', park:'Park', area:'Camping area', state:'Visitor state', sa4:'Visitor region (SA4)', postcode:'Postcode', locality:'Locality',
  year:'Year', quarter:'Quarter', month:'Month', dow:'Day of week', nights_bucket:'Length of stay', party_size:'Party size', lead_bucket:'Lead time', era:'Data era',
  duration:'Permit duration', permit_class:'Permit class', product:'Product', day_type:'Day type', school_holiday:'School-holiday period', school_term:'School term'};
const L = k => (k === 'area' && S.dataset === 'vehicle') ? 'Recreation area' : (LABEL[k] || k);
// vehicle recreation areas have no per-area coordinates in the exports; approximate centroids for the map
const VEH_AREAS = {'Cooloola':[153.15,-26.00],"K'gari (Fraser Island)":[153.10,-25.35],'Bribie Island':[153.17,-27.00],'Moreton Island':[153.40,-27.17],"Combined Cooloola & K'gari":[153.12,-25.70],'Inskip Peninsula':[153.06,-25.82]};

// ------------------------------------------------------------------ state
const S = {dataset:'camping', view:'overview', from:'', to:'', state:[], park:[], area:[], postcode:[], duration:[], daytype:[], holiday:[], q:'', mapMode:'areas'};
let META = null;
const dirty = {overview:true, map:true, charts:true, table:true};

function readHash(){
  const h = new URLSearchParams(location.hash.slice(1));
  const dflt = {dataset:'camping', view:'overview', from:'', to:'', q:'', mapMode:'areas'};
  for (const k of Object.keys(dflt)) S[k] = h.has(k) ? h.get(k) : dflt[k];
  for (const k of ['state','park','area','postcode','duration','daytype','holiday']) S[k] = h.getAll(k).filter(Boolean);
}
function writeHash(){
  const h = new URLSearchParams();
  for (const k of ['dataset','view','from','to','q','mapMode']) if (S[k]) h.set(k, S[k]);
  for (const k of ['state','park','area','postcode','duration','daytype','holiday']) S[k].forEach(v => h.append(k, v));
  history.replaceState(null, '', '#' + h.toString());
}
function filterParams(extra = {}){
  const p = new URLSearchParams();
  p.set('dataset', S.dataset);
  if (S.from) p.set('from', S.from); if (S.to) p.set('to', S.to); if (S.q) p.set('q', S.q);
  S.state.forEach(v => p.append('state', v)); S.postcode.forEach(v => p.append('postcode', v));
  S.daytype.forEach(v => p.append('day_type', v)); S.holiday.forEach(v => p.append('holiday', v));
  if (S.dataset === 'camping') { S.park.forEach(v => p.append('park', v)); S.area.forEach(v => p.append('area', v)); }
  else { S.area.forEach(v => p.append('area', v)); S.duration.forEach(v => p.append('duration', v)); }
  Object.entries(extra).forEach(([k,v]) => v !== undefined && v !== '' && v !== null && p.set(k, v));
  return p;
}
async function api(path, extra){ const r = await fetch(`/api/${path}?${filterParams(extra)}`); if (!r.ok) throw new Error(await r.text()); return r.json(); }
const measures = () => META[S.dataset].measures;
const dims = () => META[S.dataset].dimensions;
const defaultMetric = () => S.dataset === 'camping' ? 'bookings' : 'permits';

// ------------------------------------------------------------------ UI wiring
function fillSelect(sel, items, selected, placeholder){
  sel.innerHTML = `<option value="">${placeholder}</option>` + items.filter(v => !selected.includes(v)).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  sel.value = '';
}
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function multiVals(sel){ return [...sel.selectedOptions].map(o => o.value); }

function syncControls(){
  document.body.dataset.ds = S.dataset;
  $$('#dataset button').forEach(b => b.classList.toggle('on', b.dataset.v === S.dataset));
  $$('#views button').forEach(b => b.classList.toggle('on', b.dataset.v === S.view));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + S.view));
  $('#from').value = S.from; $('#to').value = S.to; $('#q').value = S.q;
  fillSelect($('#state'), META.states, S.state, '+ Visitor state');
  fillSelect($('#daytype'), META.day_types, S.daytype, '+ Day type');
  fillSelect($('#holiday'), META.holidays, S.holiday, '+ School-holiday period');
  const camp = S.dataset === 'camping';
  $('#park').hidden = !camp; $('#varea').hidden = camp; $('#duration').hidden = camp;
  if (camp) fillSelect($('#park'), META.camping.park_list, S.park, '+ Park');
  else { fillSelect($('#varea'), META.vehicle.area_list, S.area, '+ Recreation area'); fillSelect($('#duration'), META.vehicle.duration_list, S.duration, '+ Permit duration'); }
  // metric selects
  for (const id of ['#ov-metric','#ts-metric','#map-metric']) {
    const el = $(id), cur = el.value;
    let opts = measures().filter(m => id !== '#map-metric' || !m.startsWith('avg')).map(m => `<option value="${m}">${L(m)}</option>`);
    if (id === '#map-metric' && camp) opts.push('<option value="occ_avg">Occupancy — average %</option>', '<option value="occ_full">Occupancy — nights ≥90% full</option>');
    el.innerHTML = opts.join('');
    el.value = [...el.options].some(o => o.value === cur) ? cur : defaultMetric();
  }
  const bd = $('#bd-by'), curBd = bd.value;
  bd.innerHTML = dims().filter(d => !['year','quarter','month'].includes(d)).map(d => `<option value="${d}">${L(d)}</option>`).join('');
  bd.value = dims().includes(curBd) ? curBd : (camp ? 'park' : 'area');
  const tb = $('#ts-by'), curTb = tb.value;
  tb.innerHTML = '<option value="">— none —</option>' + dims().filter(d => !['year','quarter','month','postcode','locality','dow'].includes(d)).map(d => `<option value="${d}">${L(d)}</option>`).join('');
  tb.value = dims().includes(curTb) ? curTb : '';
  // map controls
  $$('#map-mode button').forEach(b => b.classList.toggle('on', b.dataset.v === S.mapMode));
  $('#map-mode button[data-v=flows]').hidden = !camp;
  $('#map-mode button[data-v=areas]').textContent = camp ? 'Camping areas' : 'Recreation areas';
  $('#ov-top-title').textContent = camp ? 'Top parks' : 'Permits by recreation area';
  $('#ch-d1-t').textContent = camp ? 'Length of stay' : 'Permit duration';
  $('#ch-d2-t').textContent = camp ? 'Party size' : 'Permit class';
  // chips
  const chips = [];
  if (S.q) chips.push(['q', S.q, `“${S.q}”`]);
  S.state.forEach(v => chips.push(['state', v, 'State: ' + v]));
  S.postcode.forEach(v => chips.push(['postcode', v, 'Postcode ' + v]));
  S.park.forEach(v => chips.push(['park', v, v]));
  S.area.forEach(v => chips.push(['area', v, v]));
  S.duration.forEach(v => chips.push(['duration', v, v]));
  S.daytype.forEach(v => chips.push(['daytype', v, v]));
  S.holiday.forEach(v => chips.push(['holiday', v, v + ' holidays']));
  $('#chips').innerHTML = chips.map(([k,v,t]) => `<span class="chip">${esc(t)}<b data-k="${k}" data-v="${esc(v)}">✕</b></span>`).join('');
  $('#export').href = `/api/export.csv?${filterParams()}`;
  const m = META[S.dataset];
  $('#meta').textContent = `${fmtFull(m.rows)} ${camp?'bookings':'permits'} · ${m.date_min} → ${m.date_max}`;
}

function changed(opts = {}){
  Object.keys(dirty).forEach(k => dirty[k] = true);
  writeHash(); syncControls(); render();
}

function render(){
  if (!dirty[S.view]) return;
  dirty[S.view] = false;
  ({overview: renderOverview, map: renderMap, charts: renderCharts, table: renderTable})[S.view]();
}

// ------------------------------------------------------------------ charts (ECharts)
const charts = {};
function chart(id){
  if (!charts[id]) charts[id] = echarts.init($(id), null, {renderer:'canvas'});
  return charts[id];
}
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));
const FONT = '"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const baseOpt = () => ({color: PAL, textStyle:{fontFamily: FONT}, grid:{left:48,right:16,top:28,bottom:36,containLabel:true},
  tooltip:{trigger:'axis', valueFormatter: v => fmtFull(v)}, animationDuration:300});
function busy(id, on){ $(id).classList.toggle('loading', on); }

async function renderOverview(){
  const metric = $('#ov-metric').value;
  busy('#v-overview', true);
  const [sum, ts, top, heat, st, occ] = await Promise.all([
    api('summary'), api('timeseries', {grain:'month', metric}),
    api('breakdown', {by: S.dataset === 'camping' ? 'park' : 'area', metric, top: 12}),
    api('seasonality', {metric}), api('breakdown', {by:'state', metric, top: 10}),
    S.dataset === 'camping' ? api('occupancy/summary') : Promise.resolve({})]);
  busy('#v-overview', false);
  Object.assign(sum, {occ_avg: occ.avg_occ, occ_full: occ.pct_nights_full});
  const camp = S.dataset === 'camping';
  const k = camp ? [['bookings','Bookings'],['nights','Nights'],['person_nights','Person-nights'],['avg_nights','Avg nights'],['avg_people','Avg party'],['median_lead_days','Median lead (days)'],['pct_non_qld','% interstate/overseas'],['pct_nights_school_holiday','% nights in school hols'],['occ_avg','Avg occupancy %'],['occ_full','% area-nights ≥90% full'],['areas','Camping areas']]
                 : [['permits','Permits'],['permit_days','Permit-days'],['pct_annual','% annual'],['median_lead_days','Median lead (days)'],['pct_non_qld','% interstate/overseas'],['pct_school_holiday','% starting in school hols'],['areas','Recreation areas']];
  $('#kpis').innerHTML = k.map(([f,l]) => `<div class="kpi"><div class="v">${fmt(sum[f])}${(f.startsWith('pct')||f.startsWith('occ'))&&sum[f]!=null?'%':''}</div><div class="l">${l}</div></div>`).join('');
  chart('#ov-ts').setOption({...baseOpt(), xAxis:{type:'time'}, yAxis:{type:'value', axisLabel:{formatter: fmt}},
    series:[{type:'line', name:L(metric), showSymbol:false, areaStyle:{opacity:.12}, lineStyle:{width:1.6}, data: ts.map(r => [r.period, r.value])}],
    dataZoom:[{type:'inside'},{type:'slider', height:18, bottom:4}]}, true);
  chart('#ov-top').setOption({...baseOpt(), grid:{left:8,right:40,top:8,bottom:8,containLabel:true}, tooltip:{trigger:'item', valueFormatter:fmtFull},
    xAxis:{type:'value', axisLabel:{formatter:fmt}}, yAxis:{type:'category', inverse:true, data: top.map(r => r.key), axisLabel:{width:180, overflow:'truncate'}},
    series:[{type:'bar', name:L(metric), data: top.map(r => r.value), label:{show:true, position:'right', formatter: p => fmt(p.value)}}]}, true);
  chart('#ov-top').off('click').on('click', p => { if (camp) S.park = [p.name]; else S.area = [p.name]; changed(); });
  heatOpt('#ov-heat', heat, metric);
  const stTot = st.reduce((a,r) => a + (r.value||0), 0) || 1;
  chart('#ov-state').setOption({...baseOpt(), grid:{left:8,right:60,top:8,bottom:8,containLabel:true}, tooltip:{trigger:'item', valueFormatter:fmtFull},
    xAxis:{type:'value', axisLabel:{formatter:fmt}}, yAxis:{type:'category', inverse:true, data: st.map(r => r.key || 'Unknown')},
    series:[{type:'bar', name:L(metric), data: st.map(r => r.value), itemStyle:{color: p => p.name === 'QLD' ? PAL[0] : PAL[1]}, label:{show:true, position:'right', formatter: p => `${fmt(p.value)} · ${(100*p.value/stTot).toFixed(1)}%`}}]}, true);
  chart('#ov-state').off('click').on('click', p => { if (p.name !== 'Unknown') { S.state = [p.name]; changed(); } });
}
function heatOpt(id, rows, metric){
  const years = [...new Set(rows.map(r => r.year))].sort();
  const max = Math.max(...rows.map(r => r.value||0), 1);
  chart(id).setOption({textStyle:{fontFamily: FONT}, grid:{left:40,right:16,top:10,bottom:60,containLabel:true},
    tooltip:{formatter: p => `${MONTHS[p.value[0]]} ${p.value[1]}<br>${L(metric)}: <b>${fmtFull(p.value[2])}</b>`},
    xAxis:{type:'category', data: MONTHS}, yAxis:{type:'category', data: years},
    visualMap:{min:0, max, calculable:true, orient:'horizontal', left:'center', bottom:0, inRange:{color:['#eef3ee','#9fc9ae','#1f6f4a','#0e3a25']}, itemHeight:220, textStyle:{fontSize:11}},
    series:[{type:'heatmap', data: rows.map(r => [r.month-1, String(r.year), r.value]), emphasis:{itemStyle:{borderColor:'#000', borderWidth:1}}}]}, true);
}

async function renderCharts(){
  const metric = $('#ts-metric').value, grain = $('#ts-grain').value, by = $('#ts-by').value;
  const bdBy = $('#bd-by').value, bdTop = +$('#bd-top').value || 20;
  const camp = S.dataset === 'camping';
  busy('#v-charts', true);
  const shade = $('#ts-shade').checked, markPh = $('#ts-ph').checked;
  const [ts, yoy, bd, d1, d2, lead, dow, dt, sh, cal] = await Promise.all([
    api('timeseries', {grain, metric, by: by || undefined, top: 8}), api('yoy', {metric}),
    api('breakdown', {by: bdBy, metric, top: bdTop}),
    api('breakdown', {by: camp ? 'nights_bucket' : 'duration', metric, top: 20}),
    api('breakdown', {by: camp ? 'party_size' : 'permit_class', metric, top: 20}),
    api('breakdown', {by:'lead_bucket', metric, top: 10}), api('breakdown', {by:'dow', metric, top: 7}),
    api('daytype', {metric}), api('daytype', {metric, by:'school_holiday'}),
    (shade || markPh) ? fetch(`/api/calendar?${new URLSearchParams({from: S.from || '2010-01-01', to: S.to || META[S.dataset].date_max})}`).then(r => r.json()) : {school_holidays:[], public_holidays:[]}]);
  busy('#v-charts', false);
  // overlays: school holiday bands + public holiday lines (public holidays only at day/week grain, else unreadable)
  const bandColor = {Easter:'rgba(200,116,43,.14)', Winter:'rgba(45,95,168,.12)', Spring:'rgba(138,95,179,.13)', Summer:'rgba(184,161,43,.15)'};
  const markArea = shade ? {silent:true, label:{show: cal.school_holidays.length <= 8, fontSize:10, color:'#5f6b63'}, data: cal.school_holidays.map(h => [{xAxis: h.start, name: h.name, itemStyle:{color: bandColor[h.name]}}, {xAxis: h.end}])} : undefined;
  const markLine = (markPh && (grain === 'day' || grain === 'week')) ? {silent:true, symbol:'none', lineStyle:{color:'#c23c5c', type:'dashed', width:1}, label:{show:false},
    data: cal.public_holidays.map(h => ({xAxis: h.date, name: h.name}))} : undefined;
  // time series (optionally split)
  let series;
  if (by) {
    const groups = [...new Set(ts.map(r => r.grp))];
    series = groups.map(g => ({type:'line', name:g, showSymbol:false, stack: metric.startsWith('avg') ? undefined : 'a', areaStyle: metric.startsWith('avg') ? undefined : {opacity:.5}, lineStyle:{width:1}, data: ts.filter(r => r.grp === g).map(r => [r.period, r.value])}));
  } else series = [{type: grain === 'year' || grain === 'quarter' ? 'bar' : 'line', name:L(metric), showSymbol:false, areaStyle:{opacity:.12}, data: ts.map(r => [r.period, r.value])}];
  series[0].markArea = markArea; series[0].markLine = markLine;
  chart('#ch-ts').setOption({...baseOpt(), legend:{type:'scroll', top:0}, grid:{left:48,right:16,top:40,bottom:48,containLabel:true}, xAxis:{type:'time'}, yAxis:{type:'value', axisLabel:{formatter:fmt}}, series,
    dataZoom:[{type:'inside'},{type:'slider', height:18, bottom:4}]}, true);
  // YoY
  const years = [...new Set(yoy.map(r => r.year))].sort();
  chart('#ch-yoy').setOption({...baseOpt(), legend:{type:'scroll', top:0}, grid:{left:48,right:16,top:40,bottom:24,containLabel:true}, xAxis:{type:'category', data: MONTHS}, yAxis:{type:'value', axisLabel:{formatter:fmt}},
    color: years.map((y,i) => i === years.length-1 ? '#c8742b' : `rgba(31,111,74,${0.15 + 0.85*i/Math.max(1,years.length-1)})`),
    series: years.map(y => ({type:'line', name:String(y), showSymbol:false, lineStyle:{width: y === years[years.length-1] ? 2.5 : 1.2}, data: MONTHS.map((_,m) => (yoy.find(r => r.year === y && r.month === m+1)||{}).value ?? null)}))}, true);
  // breakdown
  const order = {nights_bucket:['1','2','3-4','5-7','8+'], party_size:['1','2','3-4','5-8','9+'], lead_bucket:['same day','1-7d','8-30d','1-3m','3-6m','6m+'], duration:['Daily','Weekly','Monthly','Annual','Other']};
  const sortBy = (rows, key) => order[key] ? order[key].map(k => rows.find(r => String(r.key) === k)).filter(Boolean) : rows;
  const hbar = (id, rows, key, click) => {
    rows = sortBy(rows, key);
    chart(id).setOption({...baseOpt(), grid:{left:8,right:50,top:8,bottom:8,containLabel:true}, tooltip:{trigger:'item', valueFormatter:fmtFull},
      xAxis:{type:'value', axisLabel:{formatter:fmt}}, yAxis:{type:'category', inverse:true, data: rows.map(r => key === 'dow' ? DOW[r.key] : String(r.key ?? 'Unknown')), axisLabel:{width:200, overflow:'truncate'}},
      series:[{type:'bar', name:L(metric), data: rows.map(r => r.value), label:{show:true, position:'right', formatter: p => fmt(p.value)}}]}, true);
    chart(id).off('click'); if (click) chart(id).on('click', click);
  };
  hbar('#ch-bd', bd, bdBy, p => { if (['park','area','state','postcode','duration'].includes(bdBy)) { S[bdBy === 'area' && !camp ? 'area' : bdBy] = [String(p.name)]; changed(); } });
  hbar('#ch-d1', d1, camp ? 'nights_bucket' : 'duration');
  hbar('#ch-d2', d2, camp ? 'party_size' : 'permit_class');
  hbar('#ch-lead', lead, 'lead_bucket');
  const dowRows = [1,2,3,4,5,6,0].map(d => dow.find(r => r.key === d) || {key:d, value:0});
  chart('#ch-dow').setOption({...baseOpt(), grid:{left:48,right:16,top:16,bottom:24,containLabel:true}, tooltip:{trigger:'item', valueFormatter:fmtFull},
    xAxis:{type:'category', data: dowRows.map(r => DOW[r.key])}, yAxis:{type:'value', axisLabel:{formatter:fmt}}, series:[{type:'bar', name:L(metric), data: dowRows.map(r => r.value)}]}, true);
  // per-calendar-day rates by day type / school-holiday period, indexed to the baseline (Weekday / Term time)
  const rateChart = (id, rows, baselineKey, colors) => {
    const base = (rows.find(r => r.key === baselineKey) || {}).per_day || null;
    chart(id).setOption({...baseOpt(), grid:{left:8,right:70,top:8,bottom:8,containLabel:true},
      tooltip:{trigger:'item', formatter: p => { const r = rows[p.dataIndex]; return `<b>${r.key}</b><br>${fmtFull(r.value)} ${L(metric)} over ${fmtFull(r.days)} days<br>= <b>${fmtFull(r.per_day)}</b> per day${base ? ` (${(r.per_day/base).toFixed(2)}× ${baselineKey})` : ''}`; }},
      xAxis:{type:'value', axisLabel:{formatter:fmt}, name: L(metric) + ' / day', nameLocation:'middle', nameGap:22},
      yAxis:{type:'category', inverse:true, data: rows.map(r => r.key)},
      series:[{type:'bar', data: rows.map((r,i) => ({value: r.per_day, itemStyle:{color: colors[i % colors.length]}})),
        label:{show:true, position:'right', formatter: p => base ? `${fmt(p.value)}  (${(p.value/base).toFixed(2)}×)` : fmt(p.value)}}]}, true);
    chart(id).off('click').on('click', p => { const k = rows[p.dataIndex].key; if (baselineKey === 'Weekday') S.daytype = [k]; else if (k !== 'Term time') S.holiday = [k]; changed(); });
  };
  rateChart('#ch-dt', dt, 'Weekday', ['#1f6f4a','#c23c5c','#c8742b','#2d5fa8','#7f8c8d']);
  rateChart('#ch-sh', sh, 'Term time', ['#7f8c8d','#c8742b','#2d5fa8','#8a5fb3','#b8a12b']);
  if (camp) await renderOccupancy(grain, markArea);
}

async function renderOccupancy(grain, markArea){
  const basis = $('#occ-basis').value, stat = $('#occ-stat').value;
  const g = grain === 'day' ? 'day' : grain === 'week' ? 'week' : 'month';
  const [sum, ts, dt, areas] = await Promise.all([
    api('occupancy/summary', {basis}), api('occupancy/timeseries', {basis, stat, grain: g}),
    api('occupancy/daytype', {basis}), api('occupancy/areas', {basis, top: 25, min_nights: 30})]);
  const k = [['avg_occ','Avg occupancy %'],['pct_nights_full','% of area-nights ≥90% full'],['pct_nights_half','% of area-nights ≥50%'],['areas','Areas with capacity'],['area_nights','Area-nights with guests']];
  $('#occ-kpis').innerHTML = k.map(([f,l]) => `<div class="kpi"><div class="v">${fmt(sum[f])}${f.includes('occ')||f.startsWith('pct')?'%':''}</div><div class="l">${l}</div></div>`).join('');
  chart('#ch-occ-ts').setOption({...baseOpt(), legend:{top:0}, grid:{left:48,right:48,top:30,bottom:40,containLabel:true}, xAxis:{type:'time'},
    yAxis:[{type:'value', name:'occupancy %', max: v => Math.max(100, Math.ceil(v.max/10)*10), axisLabel:{formatter: v => v+'%'}}, {type:'value', name:'% nights full', max:100, axisLabel:{formatter: v => v+'%'}, splitLine:{show:false}}],
    series:[{type:'line', name:`${stat} occupancy`, showSymbol:false, areaStyle:{opacity:.12}, data: ts.map(r => [r.period, r.value]), markArea, markLine:{silent:true, symbol:'none', lineStyle:{color:'#c23c5c', type:'dotted'}, label:{formatter:'100%'}, data:[{yAxis:100}]}},
            {type:'line', name:'% of area-nights ≥90% full', yAxisIndex:1, showSymbol:false, lineStyle:{width:1, type:'dashed'}, color:'#c23c5c', data: ts.map(r => [r.period, r.pct_full])}],
    dataZoom:[{type:'inside'},{type:'slider', height:16, bottom:4}]}, true);
  const order = ['School holiday','Public holiday','Long weekend','Weekend','Weekday'];
  const rows = order.map(k => dt.find(r => r.key === k)).filter(Boolean);
  chart('#ch-occ-dt').setOption({...baseOpt(), grid:{left:8,right:60,top:28,bottom:8,containLabel:true}, legend:{top:0}, tooltip:{trigger:'axis', valueFormatter: v => v+'%'},
    xAxis:{type:'value', max:100, axisLabel:{formatter: v => v+'%'}}, yAxis:{type:'category', inverse:true, data: rows.map(r => r.key)},
    series:[{type:'bar', name:'avg occupancy', data: rows.map(r => r.avg_occ), label:{show:true, position:'right', formatter: p => p.value+'%'}},
            {type:'bar', name:'% nights ≥90% full', data: rows.map(r => r.pct_full), color:'#c23c5c', label:{show:true, position:'right', formatter: p => p.value+'%'}}]}, true);
  chart('#ch-occ-areas').setOption({...baseOpt(), grid:{left:8,right:90,top:28,bottom:8,containLabel:true}, legend:{top:0},
    tooltip:{trigger:'axis', formatter: ps => { const r = areas[ps[0].dataIndex]; return `<b>${esc(r.area)}</b><br>${esc(r.park)}<br>Avg ${r.avg_occ}% · P90 ${r.p90_occ}% · ${fmtFull(r.nights_full)}/${fmtFull(r.nights_with_guests)} nights ≥90%<br>Capacity: ${r.capacity_type === 'people' ? r.people_cap + ' people' : r.sites_cap + ' sites / ' + r.people_cap + ' people'}`; }},
    xAxis:{type:'value', axisLabel:{formatter: v => v+'%'}}, yAxis:{type:'category', inverse:true, data: areas.map(r => r.area), axisLabel:{width:230, overflow:'truncate', interval:0}},
    series:[{type:'bar', name:'avg occupancy %', data: areas.map(r => Math.min(r.avg_occ, 300)), label:{show:true, position:'right', formatter: p => `${areas[p.dataIndex].avg_occ}% · ${fmtFull(areas[p.dataIndex].nights_full)} full nights`}}]}, true);
  chart('#ch-occ-areas').off('click').on('click', p => { const r = areas[p.dataIndex]; S.park = [r.park]; S.area = [r.area]; changed(); });
}

// ------------------------------------------------------------------ map
let map, mapReady = false, popup;
const BASEMAP = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';
const BLANK = {version:8, sources:{}, layers:[{id:'bg', type:'background', paint:{'background-color':'#e9ede8'}}]};
async function initMap(){
  let style = BASEMAP;
  try { const r = await fetch(BASEMAP, {method:'HEAD'}); if (!r.ok) style = BLANK; } catch { style = BLANK; }
  map = new maplibregl.Map({container:'map', style, center:[147.5,-22.5], zoom:4.6, attributionControl:true});
  map.addControl(new maplibregl.NavigationControl({showCompass:false}), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({unit:'metric'}), 'bottom-left');
  popup = new maplibregl.Popup({closeButton:false, closeOnClick:false, maxWidth:'280px'});
  map.on('load', () => {
    const empty = {type:'FeatureCollection', features:[]};
    map.addSource('flows', {type:'geojson', data: empty});
    map.addLayer({id:'flows', type:'line', source:'flows', paint:{'line-color':'#c8742b', 'line-opacity':0.45, 'line-width':['interpolate',['linear'],['get','w'],0,0.4,1,6]}});
    map.addSource('pts', {type:'geojson', data: empty});
    map.addLayer({id:'pts', type:'circle', source:'pts', paint:{
      'circle-radius':['interpolate',['linear'],['sqrt',['get','v']],0,2,1,30], 'circle-color':['get','c'], 'circle-opacity':0.6, 'circle-stroke-color':'#fff', 'circle-stroke-width':0.8}});
    map.on('mousemove', 'pts', e => { map.getCanvas().style.cursor = 'pointer'; const p = e.features[0].properties; popup.setLngLat(e.lngLat).setHTML(p.html).addTo(map); });
    map.on('mouseleave', 'pts', () => { map.getCanvas().style.cursor = ''; popup.remove(); });
    map.on('click', 'pts', e => {
      const p = e.features[0].properties;
      if (p.kind === 'area' && S.dataset === 'camping') { S.park = [p.park]; S.area = [p.area]; changed(); }
      else if (p.kind === 'varea') { S.area = [p.area]; changed(); }
      else if (p.kind === 'origin') { S.postcode = [String(p.postcode)]; changed(); }
    });
    mapReady = true; renderMap();
  });
}
async function renderMap(){
  if (!map) return initMap();
  if (!mapReady) return;
  const metric = $('#map-metric').value, minN = +$('#map-minn').value || 1, camp = S.dataset === 'camping';
  $('#map-minn-l').style.display = S.mapMode === 'origins' ? '' : 'none';
  busy('.map-ctl', true);
  let feats = [], hint = '', side = '';
  const sc = v => v; // scale handled in paint via sqrt interpolate on normalised v
  const capLine = r => r.capacity_type ? `<br>Capacity: ${r.capacity_type === 'people' ? `${fmtFull(r.people_cap)} people (open area)` : `${fmtFull(r.sites_cap)} sites · ${fmtFull(r.people_cap)} people`}` : '<br><i>no capacity record</i>';
  if (S.mapMode === 'areas') {
    if (camp && metric.startsWith('occ_')) {
      const rows = await api('occupancy/areas', {top: 2000, min_nights: 30});
      const val = r => metric === 'occ_avg' ? r.avg_occ : r.nights_full;
      const max = Math.max(...rows.map(val), 1);
      const col = r => r.avg_occ >= 70 ? '#c23c5c' : r.avg_occ >= 40 ? '#c8742b' : '#1f6f4a';
      feats = rows.map(r => ({type:'Feature', geometry:{type:'Point', coordinates:[r.lon, r.lat]}, properties:{kind:'area', park:r.park, area:r.area, v: Math.min(1, val(r)/max), c: col(r),
        html:`<b>${esc(r.area)}</b>${esc(r.park)}<br>Avg occupancy <b>${r.avg_occ}%</b> · P90 ${r.p90_occ}%<br>${fmtFull(r.nights_full)} of ${fmtFull(r.nights_with_guests)} nights ≥90% full${capLine(r)}`}}));
      hint = `${rows.length} areas with capacity data. Colour: green <40%, orange 40–70%, red ≥70% average occupancy. Capacity is today's site configuration applied to all years.`;
      side = sideTable(metric === 'occ_avg' ? 'Avg occ %' : 'Nights ≥90%', rows.slice(0,25).map(r => [r.area, val(r)]));
    } else if (camp) {
      const rows = await api('map/areas', {metric});
      const max = Math.max(...rows.map(r => r.value), 1);
      feats = rows.map(r => ({type:'Feature', geometry:{type:'Point', coordinates:[r.lon, r.lat]}, properties:{kind:'area', park:r.park, area:r.area, v:r.value/max, c:'#1f6f4a',
        html:`<b>${esc(r.area)}</b>${esc(r.park)}<br>${L(metric)}: <b>${fmtFull(r.value)}</b><br>Bookings ${fmtFull(r.bookings)} · nights ${fmtFull(r.nights)} · avg ${r.avg_nights}${capLine(r)}`}}));
      hint = `${rows.length} camping areas. Click an area to filter everything to it.`;
      side = sideTable(L(metric), rows.slice(0,25).map(r => [r.area, r.value]));
    } else {
      const rows = await api('breakdown', {by:'area', metric, top:20});
      const max = Math.max(...rows.map(r => r.value), 1);
      feats = rows.filter(r => VEH_AREAS[r.key]).map(r => ({type:'Feature', geometry:{type:'Point', coordinates:VEH_AREAS[r.key]}, properties:{kind:'varea', area:r.key, v:r.value/max, c:'#2d5fa8',
        html:`<b>${esc(r.key)}</b>${L(metric)}: <b>${fmtFull(r.value)}</b>`}}));
      hint = 'Vehicle permits carry no coordinates; recreation areas are shown at approximate centroids. Click to filter.';
      side = sideTable(L(metric), rows.map(r => [r.key, r.value]));
    }
  } else if (S.mapMode === 'origins') {
    const rows = await api('map/origins', {metric, min_n: minN});
    const max = Math.max(...rows.map(r => r.value), 1);
    feats = rows.map(r => ({type:'Feature', geometry:{type:'Point', coordinates:[r.lon, r.lat]}, properties:{kind:'origin', postcode:r.postcode, v:r.value/max, c: r.state === 'QLD' ? '#1f6f4a' : '#c8742b',
      html:`<b>${r.postcode} ${esc(r.locality||'')}</b>${r.state||''}<br>${L(metric)}: <b>${fmtFull(r.value)}</b>`}}));
    hint = `${rows.length} postcodes (≥ ${minN}). Green = QLD, orange = interstate. Click a postcode to filter to it. Tip: filter to a park first.`;
    side = sideTable(L(metric), rows.slice(0,25).map(r => [`${r.postcode} ${r.locality||''}`, r.value]));
  } else { // flows
    const rows = await api('map/flows', {top: 300});
    const max = Math.max(...rows.map(r => r.n), 1);
    map.getSource('flows').setData({type:'FeatureCollection', features: rows.map(r => ({type:'Feature', geometry:{type:'LineString', coordinates:[[r.o_lon,r.o_lat],[r.lon,r.lat]]}, properties:{w:r.n/max}}))});
    const byArea = {}; rows.forEach(r => { const k = r.park+'|'+r.area; byArea[k] = byArea[k] || {lon:r.lon, lat:r.lat, area:r.area, park:r.park, n:0}; byArea[k].n += r.n; });
    const amax = Math.max(...Object.values(byArea).map(a => a.n), 1);
    feats = Object.values(byArea).map(a => ({type:'Feature', geometry:{type:'Point', coordinates:[a.lon,a.lat]}, properties:{kind:'area', park:a.park, area:a.area, v:a.n/amax, c:'#1f6f4a', html:`<b>${esc(a.area)}</b>${esc(a.park)}<br>Bookings in top flows: ${fmtFull(a.n)}`}}));
    hint = (S.park.length || S.area.length || S.postcode.length) ? `Top ${rows.length} postcode → area flows for the current filter.` : 'Showing the 300 strongest postcode → area flows statewide. Filter to a park or postcode for a readable picture.';
    side = sideTable('Bookings', rows.slice(0,25).map(r => [`${r.postcode} ${r.locality||''} → ${r.area}`, r.n]));
  }
  if (S.mapMode !== 'flows') map.getSource('flows').setData({type:'FeatureCollection', features:[]});
  map.getSource('pts').setData({type:'FeatureCollection', features: feats});
  $('#map-hint').textContent = hint;
  $('#map-legend').innerHTML = `<span><i style="width:8px;height:8px"></i>low</span><span><i style="width:18px;height:18px"></i>high ${L(metric)}</span>`;
  $('#map-side').hidden = !side; $('#map-side').innerHTML = side;
  busy('.map-ctl', false);
  if (feats.length && (S.park.length || S.area.length || S.postcode.length)) {
    const b = new maplibregl.LngLatBounds(); feats.forEach(f => b.extend(f.geometry.coordinates)); map.fitBounds(b, {padding:80, maxZoom:11, duration:600});
  }
}
const sideTable = (lab, rows) => `<h4>Top ${rows.length} · ${lab}</h4><table>${rows.map(([k,v]) => `<tr><td>${esc(k)}</td><td>${fmt(v)}</td></tr>`).join('')}</table>`;

// ------------------------------------------------------------------ table
let tbl, tblDs = null;
function renderTable(){
  if (tbl && tblDs === S.dataset) { tbl.setData(); return; }
  tblDs = S.dataset;
  const camp = S.dataset === 'camping';
  const cols = camp ? [
      {title:'Booked', field:'booking_date', width:100}, {title:'Arrive', field:'arrival_date', width:100}, {title:'Depart', field:'departure_date', width:100},
      {title:'Nights', field:'nights', hozAlign:'right', width:70}, {title:'Adults', field:'adults', hozAlign:'right', width:70}, {title:'Child', field:'children', hozAlign:'right', width:65}, {title:'Inf', field:'infants', hozAlign:'right', width:55},
      {title:'Park', field:'park', minWidth:200}, {title:'Camping area', field:'area', minWidth:220},
      {title:'Postcode', field:'postcode', width:85}, {title:'Locality', field:'o_locality', width:140}, {title:'State', field:'state', width:65}, {title:'Lead d', field:'lead_days', hozAlign:'right', width:70},
      {title:'Day type', field:'day_type', width:110}, {title:'Sch. hols', field:'school_holiday', width:85}, {title:'Public hol', field:'public_holiday', width:130}, {title:'SH nights', field:'sh_nights', hozAlign:'right', width:80},
      {title:'Era', field:'era', width:90}, {title:'Source', field:'source_file', width:220}]
    : [{title:'Booked', field:'booking_date', width:100}, {title:'Start', field:'start_date', width:100},
      {title:'Recreation area', field:'area', minWidth:180}, {title:'Duration', field:'duration', width:90}, {title:'Class', field:'permit_class', width:100}, {title:'Days', field:'days_valid', hozAlign:'right', width:65},
      {title:'Product (raw)', field:'product_raw', minWidth:260},
      {title:'Postcode', field:'postcode', width:85}, {title:'Locality', field:'o_locality', width:140}, {title:'State', field:'state', width:65}, {title:'Lead d', field:'lead_days', hozAlign:'right', width:70},
      {title:'Day type', field:'day_type', width:110}, {title:'Sch. hols', field:'school_holiday', width:85}, {title:'Public hol', field:'public_holiday', width:130},
      {title:'Era', field:'era', width:110}, {title:'Source', field:'source_file', width:220}];
  if (tbl) tbl.destroy();
  tbl = new Tabulator('#table', {
    height: 'calc(100vh - 250px)', layout:'fitDataFill', columns: cols, placeholder:'No records match',
    pagination:true, paginationMode:'remote', paginationSize:100, paginationSizeSelector:[50,100,250,500], sortMode:'remote',
    ajaxURL:'/api/table',
    ajaxRequestFunc: async (url, config, params) => {
      const sort = params.sort && params.sort[0];
      const d = await api('table', {limit: params.size, offset: (params.page-1)*params.size, sort: sort ? sort.field : undefined, desc: sort ? sort.dir === 'desc' : true});
      $('#tbl-count').textContent = `${fmtFull(d.total)} records`;
      return {last_page: Math.max(1, Math.ceil(d.total / params.size)), data: d.rows};
    },
  });
}

// ------------------------------------------------------------------ search / typeahead
let sugTimer;
$('#q').addEventListener('input', e => {
  clearTimeout(sugTimer);
  const term = e.target.value.trim();
  if (term.length < 2) { $('#suggest').hidden = true; return; }
  sugTimer = setTimeout(async () => {
    const rows = await (await fetch(`/api/search?term=${encodeURIComponent(term)}`)).json();
    $('#suggest').innerHTML = rows.slice(0,20).map(r => `<div data-kind="${r.kind}" data-label="${esc(r.label)}" data-sub="${esc(r.sub||'')}"><span class="k ${r.kind}">${r.kind}</span><span>${esc(r.label)}</span><span class="s">${esc(r.sub||'')}${r.n?` · ${fmt(r.n)}`:''}</span></div>`).join('')
      + `<div data-kind="text" data-label="${esc(term)}"><span class="k">text</span><span>Search “${esc(term)}” in all fields</span></div>`;
    $('#suggest').hidden = false;
  }, 180);
});
$('#suggest').addEventListener('click', e => {
  const d = e.target.closest('div[data-kind]'); if (!d) return;
  const {kind, label, sub} = d.dataset;
  if (kind === 'park') { S.dataset = 'camping'; S.park = [label]; S.area = []; S.q = ''; }
  else if (kind === 'area') { S.dataset = 'camping'; S.park = sub ? [sub] : []; S.area = [label]; S.q = ''; }
  else if (kind === 'postcode') { S.postcode = [label]; S.q = ''; }
  else S.q = label;
  $('#suggest').hidden = true; changed();
});
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { S.q = e.target.value.trim(); $('#suggest').hidden = true; changed(); } });
document.addEventListener('click', e => { if (!e.target.closest('.search')) $('#suggest').hidden = true; });

// ------------------------------------------------------------------ events
$('#dataset').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; S.dataset = b.dataset.v; S.park = []; S.area = []; S.duration = []; changed(); });
$('#views').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; S.view = b.dataset.v; writeHash(); syncControls(); render(); setTimeout(() => { Object.values(charts).forEach(c => c.resize()); if (map) map.resize(); }, 30); });
$('#from').addEventListener('change', e => { S.from = e.target.value; changed(); });
$('#to').addEventListener('change', e => { S.to = e.target.value; changed(); });
const addTo = (k, v, also) => { if (!v) return; if (!S[k].includes(v)) S[k].push(v); if (also) also(); changed(); };
$('#state').addEventListener('change', e => addTo('state', e.target.value));
$('#park').addEventListener('change', e => addTo('park', e.target.value, () => S.area = []));
$('#varea').addEventListener('change', e => addTo('area', e.target.value));
$('#duration').addEventListener('change', e => addTo('duration', e.target.value));
$('#daytype').addEventListener('change', e => addTo('daytype', e.target.value));
$('#holiday').addEventListener('change', e => addTo('holiday', e.target.value));
$('#clear').addEventListener('click', () => { Object.assign(S, {from:'', to:'', state:[], park:[], area:[], postcode:[], duration:[], daytype:[], holiday:[], q:''}); changed(); });
$('#chips').addEventListener('click', e => { const b = e.target.closest('b'); if (!b) return; const {k, v} = b.dataset; if (k === 'q') S.q = ''; else S[k] = S[k].filter(x => String(x) !== v); changed(); });
$('.presets').addEventListener('click', e => {
  const p = e.target.dataset.p; if (!p) return;
  const max = new Date(META[S.dataset].date_max), iso = d => d.toISOString().slice(0,10);
  const y = max.getFullYear();
  if (p === 'all') { S.from = ''; S.to = ''; }
  else if (p === 'ytd') { S.from = `${y}-01-01`; S.to = iso(max); }
  else if (p === 'ly') { S.from = `${y-1}-01-01`; S.to = `${y-1}-12-31`; }
  else if (p === '12m') { const d = new Date(max); d.setFullYear(d.getFullYear()-1); d.setDate(d.getDate()+1); S.from = iso(d); S.to = iso(max); }
  else if (p === '5y') { S.from = `${y-4}-01-01`; S.to = iso(max); }
  changed();
});
['#ov-metric'].forEach(id => $(id).addEventListener('change', () => { dirty.overview = true; render(); }));
['#ts-metric','#ts-grain','#ts-by','#bd-by','#bd-top','#ts-shade','#ts-ph','#occ-basis','#occ-stat'].forEach(id => $(id).addEventListener('change', () => { dirty.charts = true; render(); }));
['#map-metric','#map-minn'].forEach(id => $(id).addEventListener('change', () => { dirty.map = true; render(); }));
$('#map-mode').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; S.mapMode = b.dataset.v; writeHash(); syncControls(); dirty.map = true; render(); });
window.addEventListener('hashchange', () => { readHash(); Object.keys(dirty).forEach(k => dirty[k] = true); syncControls(); render(); });

// ------------------------------------------------------------------ boot
(async () => {
  META = await (await fetch('/api/meta')).json();
  readHash(); syncControls(); render();
})();
})();
