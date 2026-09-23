/* Extra network UI. Precise user coordinates stay in memory only. */
let activeCoords=null, map=null, mapLayer=null, previousOptions=[];
const km=m=>m>=1000?(m/1000).toFixed(1)+' km':m+' m';
const fareText=o=>o.fare_min==null?'Fare estimate unavailable':o.fare_min===o.fare_max?formatFare(o.fare_min):`${formatFare(o.fare_min)} – ${formatFare(o.fare_max)}`;
const durationText=o=>o.duration_min==null?'Duration estimate unavailable':`About ${o.duration_min} min (estimate)`;
function journeyCard(o,i){
 const steps=[];
 if(o.walk_to_board_m!=null)steps.push(`Walk approximately ${km(o.walk_to_board_m)} to ${o.segments[0].board_stop.name}. Straight-line estimate; actual walking route may differ.`);
 else steps.push(`Go to ${o.segments[0].board_stop.name}.`);
 o.segments.forEach((s,n)=>{
   if(n)steps.push(`Change transport at ${s.board_stop.name}.`);
   steps.push(`Take ${s.mode} toward ${s.towards} from ${s.board_stop.name}. Get down at ${s.alight_stop.name}.`);
 });
 steps.push('Walk from the final stop to your destination. Check the local walking route before travelling.');
 return `<article class="route-card network-card"><div class="route-main"><div><span class="transport-badge">OPTION ${i+1}</span><h3>${escapeHtml(o.origin)} → ${escapeHtml(o.destination)}</h3><p>${o.segments.map(s=>escapeHtml(s.mode)).join(' + ')} · ${o.transfers} ${o.transfers===1?'transfer':'transfers'}</p></div><span class="source-badge ${o.all_verified?'verified':''}">${o.all_verified?'Source checked':'Partly unverified'}</span></div><div class="route-facts"><span><strong>${escapeHtml(fareText(o))}</strong><br>estimated fare</span><span><strong>${escapeHtml(durationText(o))}</strong><br>estimated journey time</span>${o.walk_to_board_m!=null?`<span><strong>${km(o.walk_to_board_m)}</strong><br>straight-line to boarding</span>`:''}</div><ol class="journey-steps">${steps.map(step=>`<li>${escapeHtml(step)}</li>`).join('')}</ol><div class="source-details">${o.segments.map(s=>`<div><b>${escapeHtml(s.mode)} · Route #${s.route_id}</b> · ${escapeHtml(s.via.join(' → '))}. ${s.verified?'Published route':'Information not verified'}${s.source_url?` · <a href="${escapeHtml(s.source_url)}" target="_blank" rel="noopener noreferrer">Source ↗</a>`:''}</div>`).join('')}</div><p class="caution">Fares may change depending on time, operator and transport conditions. Walking distance is straight-line only. Confirm service locally.</p><div class="route-actions"><button class="outline-button" type="button" data-map-journey="${i}">Show on map</button><button class="outline-button" type="button" data-fare-route="${o.segments[0].route_id}" data-fare-mode="${escapeHtml(o.segments[0].mode)}">Report current fare</button><button class="plain-button" type="button" data-route-report="${o.segments[0].route_id}" data-route-name="${escapeHtml(o.origin+' to '+o.destination)}">Report route update</button></div></article>`;
}
function renderJourneys(){let options=[...previousOptions],sort=$('#journey-sort').value;
 const menu=$('#journey-sort');
 for(const option of menu.options){
   option.disabled=option.value==='fare'?options.some(x=>x.fare_min==null):option.value==='duration'?options.some(x=>x.duration_min==null):option.value==='walking'?options.some(x=>x.walk_to_board_m==null):false;
 }
 if(menu.selectedOptions[0]?.disabled){sort='transfers';menu.value=sort}
 if(sort==='fare' && options.every(x=>x.fare_min!=null))options.sort((a,b)=>a.fare_min-b.fare_min);
 else if(sort==='duration' && options.every(x=>x.duration_min!=null))options.sort((a,b)=>a.duration_min-b.duration_min);
 else if(sort==='walking' && options.every(x=>x.walk_to_board_m!=null))options.sort((a,b)=>a.walk_to_board_m-b.walk_to_board_m);
 else options.sort((a,b)=>a.transfers-b.transfers);
 $('#journey-sort').value=sort;
 $('#journey-results').innerHTML=options.length?options.map(journeyCard).join(''):empty('No reviewed connection for this journey','Try a different stop or check back as transport data is verified.');
 window.displayedJourneys=options;
}
async function loadJourneys(origin,destination){
 const searchCoords=origin==='Current location' && activeCoords;
 const params=new URLSearchParams({destination, ...(searchCoords?{lat:activeCoords.latitude,lon:activeCoords.longitude}:{origin})});
 $('#journey-results').innerHTML=empty('Finding journey options','Checking known transport connections…');
 try{const result=searchCoords?await api('/journeys',{method:'POST',body:JSON.stringify({lat:activeCoords.latitude,lon:activeCoords.longitude,destination})}):await api('/journeys?'+params);previousOptions=result.options;renderJourneys();if(result.reason)$('#journey-results').innerHTML=empty('Route information is incomplete',result.reason);renderMap(result.options[0]);}
 catch(e){previousOptions=[];$('#journey-results').innerHTML=empty('Could not plan this journey',e.message)}
}
$('#journey-sort').addEventListener('change',renderJourneys);
function mapMarkerText(type){return {bus:'🚌',brt:'🚌',keke:'🛺',rail:'🚆',ferry:'⛴️',other:'📍',user:'📍',destination:'🏁'}[type]||'📍'}
function marker(lat,lon,type,label){return L.marker([lat,lon],{icon:L.divIcon({className:'stop-marker',html:`<span>${mapMarkerText(type)}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}).bindPopup(escapeHtml(label))}
function ensureMap(){if(map)return true;if(typeof L==='undefined'){$('#map-message').textContent='Map library could not load. Journey steps remain available above.';return false}map=L.map('lagos-map',{scrollWheelZoom:false}).setView([6.52,3.38],11);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors',maxZoom:19}).addTo(map);mapLayer=L.layerGroup().addTo(map);return true}
function renderMap(option){if(!ensureMap())return;mapLayer.clearLayers();const points=[];
 if(activeCoords){marker(activeCoords.latitude,activeCoords.longitude,'user','Your approximate current location').addTo(mapLayer);points.push([activeCoords.latitude,activeCoords.longitude])}
 const segments=option?.segments||[];
 if(segments.length){segments.forEach((s,index)=>{const pts=[];s.path_stops.forEach((stop,j)=>{const name=stop.name;if(stop.latitude!=null){const p=[stop.latitude,stop.longitude];pts.push(p);points.push(p);marker(...p,j===s.path_stops.length-1&&index===segments.length-1?'destination':stop.stop_type||'bus',name).addTo(mapLayer)}});if(pts.length>1)L.polyline(pts,{color:index%2?'#df9b38':'#13886a',weight:5,dashArray:'7,7'}).addTo(mapLayer)});
 $('#map-message').textContent='Lines connect known station coordinates schematically. Missing coordinates are not placed on the map.';
 }else{$('#map-message').textContent='Known stops appear on this map when coordinates are available. No route geometry is claimed.'}
 if(points.length)map.fitBounds(L.latLngBounds(points).pad(.25),{maxZoom:14});setTimeout(()=>map.invalidateSize(),100);
}
async function showNearby(coords){try{const stops=await api('/stops/nearby',{method:'POST',body:JSON.stringify({lat:coords.latitude,lon:coords.longitude})});$('#nearby-home').innerHTML=stops.length?stops.map(s=>`<div class="stop-card"><strong>${mapMarkerText(s.stop_type)} ${escapeHtml(s.name)}</strong><small>${km(s.distance_m)} straight-line · ${escapeHtml(s.transport_types)} · ${s.verified?'Source checked':'Unverified stop'}</small><span>${s.routes_served.length?s.routes_served.map(r=>escapeHtml(r.destination)).join(', '):'No reviewed connections linked yet'}</span></div>`).join(''):empty('No mapped stops nearby yet','Nearby search needs transport stops with verified coordinates. Enter an origin manually.');if(stops.length&&ensureMap()){mapLayer.clearLayers();const pts=[];stops.forEach(s=>{marker(s.latitude,s.longitude,s.stop_type,s.name).addTo(mapLayer);pts.push([s.latitude,s.longitude])});marker(coords.latitude,coords.longitude,'user','Your approximate location').addTo(mapLayer);pts.push([coords.latitude,coords.longitude]);map.fitBounds(L.latLngBounds(pts).pad(.2),{maxZoom:14})}}
 catch(e){$('#nearby-home').innerHTML=empty('Could not find nearby stops',e.message)}}
function requestLocation(purpose){if(!navigator.geolocation){toast('Location is unavailable. Enter your starting point manually.');return}navigator.geolocation.getCurrentPosition(position=>{activeCoords={latitude:position.coords.latitude,longitude:position.coords.longitude};if(purpose==='nearby'){showNearby(activeCoords);location.hash='home'}else{const input=purpose==='home'?$('#origin'):$('#route-origin');input.value='Current location';const destination=purpose==='home'?$('#destination').value:$('#route-destination').value;if(destination.trim().length>=2)search('Current location',destination);else toast('Location ready. Enter your destination to find a route.')}},()=>{activeCoords=null;toast('We couldn’t access your location. Enter your starting point manually.')},{enableHighAccuracy:false,timeout:12000,maximumAge:60000})}
document.querySelectorAll('[data-locate]').forEach(b=>b.addEventListener('click',()=>requestLocation(b.dataset.locate)));
for(const el of ['#origin','#route-origin'])$(el).addEventListener('input',e=>{if(e.target.value!=='Current location')activeCoords=null});
let suggestionTimer;
for(const el of ['#origin','#destination','#route-origin','#route-destination'])$(el).addEventListener('input',e=>{clearTimeout(suggestionTimer);const q=e.target.value.trim();if(q.length<2)return;suggestionTimer=setTimeout(async()=>{try{const options=await api('/locations/suggest?'+new URLSearchParams({q}));$('#known-locations').innerHTML=options.map(x=>`<option value="${escapeHtml(x.name)}">${escapeHtml(x.area||x.location)}</option>`).join('')}catch{}},250)});
document.addEventListener('click',e=>{const mapButton=e.target.closest('[data-map-journey]');if(mapButton){renderMap(window.displayedJourneys[Number(mapButton.dataset.mapJourney)]);$('#lagos-map').scrollIntoView({behavior:'smooth'})}
 const fare=e.target.closest('[data-fare-route]');if(fare){location.hash='reports';showPage('reports');$('#fare-report-form').elements.route_id.value=fare.dataset.fareRoute;$('#fare-report-form').elements.transport_type.value=fare.dataset.fareMode;$('#fare-report-form').scrollIntoView({behavior:'smooth'})}
 const update=e.target.closest('[data-route-report]');if(update){location.hash='reports';showPage('reports');$('#report-location').value=update.dataset.routeName;$('#report-form').dataset.routeId=update.dataset.routeReport}
 const review=e.target.closest('[data-action="review-fare"]');if(review)setTimeout(loadFareReports,400)
});
$('#fare-report-form').elements.paid_on.value=new Date().toISOString().slice(0,10);
$('#fare-report-form').addEventListener('submit',async e=>{e.preventDefault();if(!user)return openAuth();const data=Object.fromEntries(new FormData(e.target));data.route_id=Number(data.route_id);data.amount=Number(data.amount);try{await api('/fare-reports',{method:'POST',body:JSON.stringify(data)});e.target.reset();toast('Fare report sent for review. Published fares have not changed.');loadFareReports()}catch(err){toast(err.message)}});
async function loadFareReports(){if(!user){$('#my-fare-reports').innerHTML='';return}try{const rows=await api('/me/fare-reports');$('#my-fare-reports').innerHTML=rows.map(r=>`<div class="stack-item"><strong>Route #${r.route_id}: ${formatFare(r.amount)} · ${escapeHtml(r.status)}</strong><small>Paid ${escapeHtml(r.paid_on)}</small></div>`).join('')}catch{}}
window.addEventListener('hashchange',()=>{if(location.hash==='#reports')loadFareReports();if(location.hash==='#routes'&&map)setTimeout(()=>map.invalidateSize(),100)});
