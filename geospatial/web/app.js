const $ = (id) => document.getElementById(id);
let locationState = {latitude: 40.8135, longitude: -74.0745};
let selectedName = 'MetLife Stadium';
let selectedAddress = '1 MetLife Stadium Drive, East Rutherford, NJ 07073';
let placement = null;
let map, marker, footprint, forward;
let previewVersion = 0;
let searchVersion = 0;
let timer;

function status(message) { $('status').textContent = message; }
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed. Please try again.');
  return data;
}
function coordinates() {
  $('latitude').value = locationState.latitude.toFixed(6);
  $('longitude').value = locationState.longitude.toFixed(6);
  $('position').textContent = `${locationState.latitude.toFixed(5)}, ${locationState.longitude.toFixed(5)}`;
}
function drawBuilding(corners, transform) {
  // Fixed camera: east goes right, north goes up-left; all coordinates are in meters.
  const factor = 0.65;
  const project = ([east, north, up]) => [200 + (east - north * 0.5) * factor, 163 - (north * 0.45 + east * 0.18 + up) * factor];
  const points = (list) => list.map(p => project(p).join(',')).join(' ');
  const height = 30 * transform.metersPerModelUnit;
  const roof = corners.map(([x,y,z]) => [x,y,z+height]);
  $('shadow').setAttribute('points', points(corners.map(([x,y]) => [x,y,0])));
  $('walls').replaceChildren();
  // Paint far faces first to keep the near faces visible as heading changes.
  const faces = corners.map((p,i) => {
    const j = (i+1)%4;
    return [p, corners[j], roof[j], roof[i]];
  }).sort((a,b) => project(a[0])[1]+project(a[1])[1]-project(b[0])[1]-project(b[1])[1]);
  for (const [index, face] of faces.entries()) {
    const polygon = document.createElementNS('http://www.w3.org/2000/svg','polygon');
    polygon.setAttribute('points', points(face));
    polygon.setAttribute('fill', ['#90ab70','#a9c286','#85a164','#bad695'][index]);
    polygon.setAttribute('stroke','#344d37');
    $('walls').append(polygon);
  }
  $('roof').setAttribute('points',points(roof));
  const center = project([0,0,transform.verticalOffsetMeters + height]);
  const front = project(roof[0].map((v,i) => (v+roof[1][i])/2));
  for (const [key,val] of Object.entries({x1:center[0],y1:center[1],x2:front[0],y2:front[1]})) $('front-line').setAttribute(key,val);
  $('dimensions').textContent = `${(60*transform.metersPerModelUnit).toFixed(0)} × ${(90*transform.metersPerModelUnit).toFixed(0)} m footprint · ${transform.verticalOffsetMeters} m ground offset`;
}
async function update() {
  const version = ++previewVersion;
  $('download').disabled = true;
  const transform = {...locationState, elevationMeters:0, headingDegrees:Number($('heading').value), metersPerModelUnit:Number($('scale').value), verticalOffsetMeters:Number($('offset').value),anchor:'ground-center',upAxis:'Y'};
  $('heading-value').textContent = `${transform.headingDegrees}°`;
  $('scale-value').textContent = `${transform.metersPerModelUnit.toFixed(1)}×`;
  $('offset-value').textContent = `${transform.verticalOffsetMeters} m`;
  try {
    const data = await api('/api/preview', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({assetId:'building-preview', name:selectedName,sourceAddress:selectedAddress,transform})});
    if (version !== previewVersion) return;
    placement = data.placement;
    $('json').textContent = JSON.stringify(placement,null,2);
    $('download').disabled = false;
    coordinates();
    $('place-name').textContent = selectedName;
    drawBuilding(data.localCorners,placement.transform);
    if (map) {
      const center = [locationState.latitude,locationState.longitude];
      marker.setLatLng(center);
      footprint.setLatLngs(data.footprint);
      forward.setLatLngs([center,data.front]);
    }
  } catch (error) { if (version === previewVersion) status(error.message); }
}
function setLocation(lat,lon) {
  locationState = {latitude:lat, longitude:((lon+180)%360+360)%360-180};
  coordinates();
  update();
}
if (window.L) {
  $('map').replaceChildren();
  map = L.map('map').setView([40.8135,-74.0745],16);
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
  tiles.on('tileerror', () => status('Some map tiles could not load. Placement controls and the building preview still work.'));
  marker = L.marker([40.8135,-74.0745],{draggable:true,icon:L.divIcon({className:'anchor',iconSize:[18,18],iconAnchor:[9,9]}),title:'Drag to position building'}).addTo(map);
  footprint = L.polygon([],{color:'#48633a',weight:2,fillColor:'#b5d180',fillOpacity:.55,interactive:false}).addTo(map);
  forward = L.polyline([],{color:'#2f472f',weight:4,interactive:false}).addTo(map);
  const move = ({lat,lng}) => { if(Math.abs(lat)>85) {status('Choose a location between 85° south and 85° north.'); return;} setLocation(lat,lng); status('Position adjusted manually. Review the anchor before exporting.'); };
  marker.on('dragend',()=>move(marker.getLatLng()));
  map.on('click',event=>move(event.latlng));
  L.control.scale({imperial:false}).addTo(map);
} else {
  $('map').textContent = 'Map library unavailable. Connect to the internet and reload for the street map. The building preview and manual coordinates still work.';
}
$('search-form').addEventListener('submit', async event => {
  event.preventDefault();
  const version = ++searchVersion;
  $('search-button').disabled = true;
  $('results').replaceChildren();
  status('Finding matching places…');
  try {
    const data = await api(`/api/search?q=${encodeURIComponent($('address').value)}`);
    if (version !== searchVersion) return;
    status(data.results.length ? 'Choose a match below to place the building.' : 'No matches. Try a more specific address or enter coordinates.');
    for (const result of data.results) {
      const button = document.createElement('button');
      button.textContent = result.label;
      button.addEventListener('click',()=>{
        if(Math.abs(result.latitude)>85) {status('This location is outside the map preview’s latitude range.');return;}
        selectedName = result.label.split(',')[0]; selectedAddress = result.label;
        setLocation(result.latitude,result.longitude);
        if(map) map.setView([result.latitude,result.longitude],17);
        status(result.source);
        $('results').replaceChildren();
      });
      $('results').append(button);
    }
  } catch(error) { if(version===searchVersion) status(error.message); }
  finally { if(version===searchVersion) $('search-button').disabled = false; }
});
$('coordinates').addEventListener('submit',event=>{
  event.preventDefault();
  setLocation(Number($('latitude').value),Number($('longitude').value));
  status('Coordinates adjusted manually; the place label is unchanged.');
  if(map) map.setView([locationState.latitude,locationState.longitude],17);
});
for (const id of ['heading','scale','offset']) $(id).addEventListener('input',()=>{
  ++previewVersion; $('download').disabled = true;
  clearTimeout(timer); timer = setTimeout(update,40);
});
$('recenter').addEventListener('click',()=>{if(map) map.setView([locationState.latitude,locationState.longitude],17);});
$('demo').addEventListener('click',()=>{
  ++searchVersion; $('search-button').disabled=false; $('results').replaceChildren();
  selectedName='MetLife Stadium'; selectedAddress='1 MetLife Stadium Drive, East Rutherford, NJ 07073';
  $('address').value='MetLife Stadium'; $('heading').value=0; $('scale').value=1; $('offset').value=0;
  setLocation(40.8135,-74.0745); if(map) map.setView([40.8135,-74.0745],16);
  status('Prepared venue: approximate coordinates. The block is a placeholder, not the stadium reconstruction.');
});
$('download').addEventListener('click',()=>{
  if(!placement || $('download').disabled) return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(placement,null,2)+'\n'],{type:'application/json'}));
  const link=document.createElement('a'); link.href=url; link.download='placement.json'; link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
status('Prepared venue: approximate coordinates. The block is a placeholder, not the stadium reconstruction.');
update();
