// Node 22+, Chrome, and a running LUT Lab; no npm dependencies.
// node tests/test_lut_lab_ui.cjs http://127.0.0.1:8767
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),{setTimeout:delay}=require('node:timers/promises');
const url=process.argv[2]||'http://127.0.0.1:8765';
(async()=>{
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'lut-lr-chrome-'));
 const chrome=spawn(process.env.CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--remote-debugging-port=0','--no-first-run','--no-default-browser-check','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 let ws;
 try{
  const portFile=path.join(profile,'DevToolsActivePort');for(let i=0;!fs.existsSync(portFile)&&i<100;i++)await delay(100);
  const port=fs.readFileSync(portFile,'utf8').split('\n')[0],pages=await(await fetch(`http://127.0.0.1:${port}/json`)).json();
  ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0;const pending=new Map(),errors=[],requests=[];
  ws.onmessage=event=>{const msg=JSON.parse(event.data);if(msg.id){const item=pending.get(msg.id);pending.delete(msg.id);msg.error?item.reject(Error(JSON.stringify(msg.error))):item.resolve(msg.result);}else if(msg.method==='Runtime.exceptionThrown')errors.push(msg.params.exceptionDetails);else if(msg.method==='Network.requestWillBeSent')requests.push(msg.params.request.url);};
  const send=(method,params={})=>new Promise((resolve,reject)=>{pending.set(++id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
  const wait=expression=>evaluate(`(async()=>{for(let i=0;i<300;i++){if(${expression})return;await new Promise(r=>setTimeout(r,100));}throw Error('Timeout: '+${JSON.stringify(expression)});})()`);
  const ready=()=>wait(`!loadingRecipe && !$('frame').hidden && $('render-time').textContent.includes('ms')`);
  const mouse=async(type,x,y,extra={})=>send('Input.dispatchMouseEvent',{type,x,y,...extra});
  const drag=async(x,y,dx,dy,modifiers=0)=>{await mouse('mousePressed',x,y,{button:'left',clickCount:1,modifiers});await mouse('mouseMoved',x+dx,y+dy,{button:'left',buttons:1,modifiers});await mouse('mouseReleased',x+dx,y+dy,{button:'left',clickCount:1,modifiers});};
  const bounds=selector=>evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);
  const shot=async()=>{await evaluate('document.fonts.ready');await delay(250);const result=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.mkdirSync(path.resolve(__dirname,'../docs'),{recursive:true});fs.writeFileSync(path.resolve(__dirname,'../docs/screenshot.png'),Buffer.from(result.data,'base64'));};
  await send('Runtime.enable');await send('Network.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1600,height:1000,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url});
  await wait(`typeof config!=='undefined'&&Boolean(config.raw)&&photos.length>0`);
  if(process.argv.includes('--no-engine')){
    await evaluate(`selectPhoto(photos.find(p=>p.kind!=='raw'))`);await ready();
    assert.equal(await evaluate(`!config.available&&$('install').hidden&&$('adjustments.exposure').disabled&&!$('amount').disabled`),true);
    assert.equal(await evaluate(`$('engine-error').textContent`),'보정 엔진(rr-lut) 없음');
    await evaluate(`selectLut(luts.find(p=>p.kind==='cube'))`);await ready();
    assert.equal(await evaluate('recipeMode'),'compose');
    await evaluate(`controls.get('amount').row.querySelector('.num').value='150';controls.get('amount').row.querySelector('.num').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));`);await ready();
    assert.equal(await evaluate('recipe.amount'),1.5);
    assert.equal(await evaluate(`$('save').disabled`),false);
    await evaluate(`$('save').click();$('save-name').value='strength-test';$('confirm').click()`);
    await wait(`!$('save-dialog').open&&luts.some(p=>p.name==='strength-test')`);
    assert.equal(await evaluate(`luts.some(p=>p.name==='strength-test')`),true);
    assert.equal(errors.length,0,JSON.stringify(errors));
    console.log('PASS: no-engine JPG preview, cube selection, strength, save, disabled adjustment panels and hidden install');
    return;
  }
  if(!process.argv.includes('--raw-only')){
  await evaluate(`selectPhoto(photos.find(p=>p.kind!=='raw'))`);await ready();
  assert.equal(await evaluate(`document.querySelectorAll('#panels>details').length`),8);
  assert.equal(await evaluate(`['basic','tone-curve','hsl','grading','effects','detail','calibration','amount-panel'].every((id,i)=>$('panels').children[i].id===id)`),true);
  await evaluate(`selectLut(luts.find(x=>x.name==='Colour Study'))`);await ready();
  assert.equal(await evaluate('recipeMode'),'preset');
  assert.equal(await evaluate(`getValue('adjustments.colorGrading.shadows.saturation')>0`),true);
  await evaluate(`const field=controls.get('adjustments.exposure').row.querySelector('.num');field.value='0.37';field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));`);await ready();
  assert.equal(await evaluate(`recipe.adjustments.exposure`),.37);
  await evaluate(`$('adjustments.exposure').dispatchEvent(new MouseEvent('dblclick'))`);assert.equal(await evaluate('recipe.adjustments.exposure'),0);
  await evaluate(`controls.get('amount').row.querySelector('.num').value='150';controls.get('amount').row.querySelector('.num').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));`);assert.equal(await evaluate('recipe.amount'),1.5);
  await evaluate(`$('amount-panel').querySelector('.text-button').click()`);assert.equal(await evaluate('recipe.amount'),1);
  await evaluate('document.activeElement.blur()');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'\\',code:'Backslash'});assert.equal(await evaluate('mode'),'original');await send('Input.dispatchKeyEvent',{type:'keyUp',key:'\\',code:'Backslash'});assert.equal(await evaluate('mode'),'split');
  await evaluate(`setMode('split');setSplit(50)`);let b=await bounds('#divider');await drag(b.x+b.w/2,b.y+b.h/2,80,0);assert.equal(await evaluate('split>50'),true);
  console.log('PASS: preset sliders, typed values, double-click/panel reset, amount percent, backslash and draggable compare');

  await evaluate(`$('basic').open=false;$('tone-curve').open=true;$('grading').open=true;$('hsl').open=false;document.querySelector('.editor-scroll').scrollTop=160`);
  // Add a point through the SVG, then move it and delete it.
  await evaluate(`recipe.adjustments.curves={luma:[{x:0,y:0},{x:255,y:255}]};channel='luma';drawCurve()`);
  b=await bounds('#curve');const center={x:b.x+b.w/2,y:b.y+b.h/2};await drag(center.x,center.y,12,-12);
  assert.equal(await evaluate('curvePoints().length'),3);
  assert.equal(await evaluate('curvePoints()[1].y>128'),true);
  await evaluate(`curve.querySelector('[data-point="1"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);assert.equal(await evaluate('curvePoints().length'),2);
  // Monotone interpolation and constant tails, including a reversal and a flat segment.
  assert.equal(await evaluate(`curveValues([{x:0,y:0},{x:255,y:255}]).every((y,x)=>Math.abs(y-x)<1e-10)`),true);
  assert.equal(await evaluate(`curveValues([{x:20,y:40},{x:80,y:180},{x:160,y:180},{x:230,y:60}]).every((y,x)=>y>=40-1e-8&&y<=180+1e-8&&(x>=20||y===40)&&(x<=230||y===60))`),true);
  await evaluate(`const points=Array.from({length:16},(_,i)=>({x:i*17,y:i*17}));savePoints(points);drawCurve()`);await drag(center.x,center.y,0,0);assert.equal(await evaluate('curvePoints().length'),16);
  await evaluate(`$('tone-curve').querySelector('.text-button').click();savePoints([{x:0,y:0},{x:128,y:148},{x:255,y:255}]);drawCurve()`);
  console.log('PASS: point curve add/drag/remove/max16, keyboard-ready nodes, monotone Hermite interpolation and constant tails');

  b=await bounds('.wheel[data-zone="shadows"]');await drag(b.x+b.w/2,b.y+b.h/2,20,-10);
  const hue=await evaluate(`getValue('adjustments.colorGrading.shadows.hue')`);assert.equal(await evaluate(`getValue('adjustments.colorGrading.shadows.saturation')>0`),true);
  await drag(b.x+b.w/2,b.y+b.h/2,0,25,8);assert.equal(await evaluate(`getValue('adjustments.colorGrading.shadows.hue')`),hue);
  assert.equal(await evaluate(`Number(controls.get('adjustments.colorGrading.shadows.saturation').row.querySelector('.num').value)===getValue('adjustments.colorGrading.shadows.saturation')`),true);
  await evaluate(`document.querySelector('.wheel[data-zone="shadows"]').dispatchEvent(new MouseEvent('dblclick'));`);assert.equal(await evaluate(`getValue('adjustments.colorGrading.shadows.saturation')`),0);
  console.log('PASS: four grading wheels, wheel/slider synchronization, Shift saturation-only drag and zone reset');

  // The chart has saturated pixels; select its displayed preview, not its source file.
  await evaluate(`selectPhoto(photos.find(x=>x.name==='chart.jpg'));setMode('filtered');setTat('hsl');hslTab='saturation';delete recipe.adjustments.hsl;syncControls()`);await ready();
  assert.equal(await evaluate(`bandWeights(0).reduce((n,p)=>n+p[1],0)`),1);
  b=await bounds('#frame');const x=b.x+b.w*.75,y=b.y+b.h*.2;
  const sampled=await evaluate(`displayedPixel({clientX:${x},clientY:${y}})`);assert.equal(sampled.length,3);
  await drag(x,y,0,-36);assert.equal(await evaluate(`Object.keys(recipe.adjustments.hsl).length`),2);
  assert.equal(await evaluate(`Object.values(recipe.adjustments.hsl).every(v=>v.saturation>0)`),true);
  await evaluate(`setTat('curve');channel='luma'`);await drag(x,y,0,-20);assert.equal(await evaluate('curvePoints().length>=3'),true);
  await evaluate(`setTat(null)`);await ready();
  assert.equal(await evaluate(`!$('histogram-box').hidden && $('shadow-clip').title.includes('픽셀') && $('histogram').getContext('2d').getImageData(0,0,512,100).data.some(v=>v>0)`),true);
  console.log('PASS: displayed-pixel HSL targeted drag across two hue bands, curve targeted drag, rendered RGB histogram/clipping');

  // Stale renders must not replace later slider state, even if the transport ignores abort.
  await evaluate(`window.realFetch=window.fetch;window.fetch=async(...args)=>{if(args[0]==='/api/render'){const result=await realFetch(args[0],{...args[1],signal:undefined});if(JSON.parse(args[1].body).recipe?.adjustments?.contrast===11)await new Promise(r=>setTimeout(r,700));return result;}return realFetch(...args);};putValue('adjustments.contrast',11);schedule(0);`);
  await delay(180);await evaluate(`putValue('adjustments.contrast',8);schedule(0)`);await ready();await delay(800);
  assert.equal(await evaluate(`(async()=>{const shown=new Uint8Array(await(await realFetch($('after').src)).arrayBuffer());const size=Math.min(4096,Math.ceil(Math.max($('stage').clientWidth,$('stage').clientHeight)*devicePixelRatio));const expected=new Uint8Array(await(await realFetch('/api/render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({photo:photo.path,...cubeBody(),size})})).arrayBuffer());return shown.length===expected.length&&shown.every((v,i)=>v===expected[i]);})()`),true);
  await evaluate(`window.fetch=realFetch;selectLut(luts.find(x=>(x.badges||[]).some(b=>b.startsWith('B&W'))))`);await ready();assert.equal(await evaluate('recipeMode'),'compose');assert.match(await evaluate(`$('recipe-note').textContent`),/흑백/);
  await evaluate(`$('install').click()`);assert.equal(await evaluate(`$('save-dialog').open&&$('target').textContent.startsWith(config.install)`),true);await evaluate(`$('cancel').click()`);
  console.log('PASS: stale render rejection, B&W compose notice, install confirmation target (no files installed)');

  assert.equal(await evaluate(`document.querySelector('.editor-heading h2').textContent`),'조정');
  await evaluate(`$('search').value='';$('source').value='';$('category').value='';onlyFavorites=false;drawLuts()`);
  assert.equal(await evaluate(`(()=>{const list=$('luts'),r=list.getBoundingClientRect();return [...list.querySelectorAll('.lut-row')].filter(row=>{const b=row.getBoundingClientRect();return b.top>=r.top&&b.bottom<=r.bottom;}).length>=10;})()`),true);
  await evaluate(`favorites.clear();drawLuts();document.querySelector('.star').click()`);
  for(const selector of ['.star.saved','#favorites']){
    if(selector==='#favorites')await evaluate(`$('favorites').click()`);
    assert.deepEqual(await evaluate(`(()=>{const b=document.querySelector(${JSON.stringify(selector)}),s=getComputedStyle(b);return [b.getAttribute('aria-pressed'),b.textContent,s.backgroundColor,s.color];})()`),['true','★','rgba(0, 0, 0, 0)','rgb(242, 242, 239)']);
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab'});
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    assert.equal(await evaluate(`getComputedStyle(document.querySelector(${JSON.stringify(selector)})).outlineStyle`),'solid');
  }
  await evaluate(`$('favorites').click();document.querySelector('.star.saved').click()`);
  assert.equal(await evaluate(`document.querySelector('.star').textContent`),'☆');
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.star')).backgroundColor`),'rgba(0, 0, 0, 0)');
  assert.equal(await evaluate(`getComputedStyle($('favorites')).backgroundColor`),'rgba(0, 0, 0, 0)');
  await evaluate(`selectZone('shadows');$('grading').open=true`);
  assert.equal(await evaluate(`(()=>{const zone=getComputedStyle(document.querySelector('.zone h3 button[aria-pressed=true]')),tab=getComputedStyle(document.querySelector('.tabs button[aria-pressed=true]'));return zone.backgroundColor===tab.backgroundColor&&zone.color===tab.color&&parseFloat(zone.paddingTop)>=5;})()`),true);
  console.log('PASS: at least 10 fully visible LUT rows, transparent checked/unchecked stars, visible star focus rings, grading tab styling and adjustment heading');

  for(const [width,height]of [[1440,900],[1600,1000]]){
    await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
    for(const [photosCollapsed,lutsCollapsed]of [[false,false],[true,false],[false,true],[true,true]]){
      await evaluate(`for(const [id,collapsed]of [['drop-zone',${photosCollapsed}],['lut-browser',${lutsCollapsed}]]){const button=document.querySelector('[data-column="'+id+'"]');if(button.getAttribute('aria-expanded')===String(collapsed))button.click();}`);
      await ready();
      const check=()=>evaluate(`(()=>{
        const rect=id=>$(id).getBoundingClientRect(),photo=rect('drop-zone'),library=rect('lut-browser'),preview=document.querySelector('.preview').getBoundingClientRect(),side=document.querySelector('.sidebar').getBoundingClientRect();
        return document.documentElement.scrollWidth<=${width}&&document.documentElement.scrollHeight<=${height}&&photo.right<=library.left&&library.right<=preview.left&&preview.right<=side.left&&preview.width>=500&&$('save').getBoundingClientRect().bottom<=${height}&&photo.width===${photosCollapsed?44:164}&&library.width===${lutsCollapsed?44:286}&&document.querySelector('[data-column="drop-zone"]').getAttribute('aria-expanded')==='${!photosCollapsed}'&&document.querySelector('[data-column="lut-browser"]').getAttribute('aria-expanded')==='${!lutsCollapsed}'&&[...document.querySelectorAll('.column-toggle')].every(b=>b.getAttribute('aria-label')&&b.getBoundingClientRect().width>0);
      })()`);
      assert.equal(await check(),true,`${width}x${height}, collapsed photos=${photosCollapsed} LUTs=${lutsCollapsed}`);
      const origin=await evaluate('performance.timeOrigin');await send('Page.reload');await wait(`performance.timeOrigin!==${origin}&&typeof config!=='undefined'&&config.available&&luts.length>0`);await ready();
      assert.equal(await check(),true,'collapse state persists across reload');
      if(lutsCollapsed){
        await evaluate('document.activeElement.blur()');
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowDown',code:'ArrowDown'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowDown',code:'ArrowDown'});await ready();
        assert.equal(await evaluate("selected.path===visible.filter(p=>p.kind!=='xmp')[0].path"),true);
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowUp',code:'ArrowUp'});await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowUp',code:'ArrowUp'});await ready();
        assert.equal(await evaluate("selected.path===visible.filter(p=>p.kind!=='xmp').at(-1).path"),true);
        assert.equal(await check(),true,'collapsed keyboard navigation keeps layout');
      }
    }
  }
  // Storage may be unavailable: toggles still work for the current page.
  await evaluate(`window.storageSet=Storage.prototype.setItem;Storage.prototype.setItem=()=>{throw Error('storage blocked')};document.querySelectorAll('.column-toggle').forEach(b=>b.click());Storage.prototype.setItem=storageSet;document.querySelectorAll('.column-toggle').forEach(b=>b.click());`);
  console.log('PASS: library left of preview, both columns collapse/expand, reload persistence, collapsed arrow shortcuts, storage-write failure, all four states at both viewports without overflow');
  await evaluate(`document.querySelectorAll('.column-toggle').forEach(b=>{if(b.getAttribute('aria-expanded')==='false')b.click()})`);await ready();
  await evaluate(`selectPhoto(photos.find(x=>x.name==='chart.jpg'));selectLut(luts.find(x=>x.name==='Colour Study'));setMode('split');setSplit(50);$('search').value='Portra';drawLuts();`);await ready();
  await evaluate(`document.querySelectorAll('#panels>details').forEach(p=>p.open=true);document.querySelector('.editor-scroll').scrollTop=0`);
  await evaluate(`for(const id of ['basic','hsl','calibration','amount-panel'])$(id).open=false;$('tone-curve').open=true;$('grading').open=true;savePoints([{x:0,y:6},{x:64,y:57},{x:192,y:204},{x:255,y:252}]);change();document.querySelector('.editor-scroll').scrollTop=$('tone-curve').offsetTop-document.querySelector('.editor-scroll').offsetTop;`);await ready();
  await evaluate(`$('search').value='';favorites.add(selected.path);drawLuts();document.querySelector('.lut-row.selected').scrollIntoView({block:'center'});document.querySelectorAll('#panels>details').forEach(p=>p.open=['basic','grading','amount-panel'].includes(p.id));document.querySelector('.editor-scroll').scrollTop=0`);await ready();
  await evaluate(`document.querySelectorAll('.column-toggle').forEach(b=>b.click())`);await ready();
  await evaluate(`document.querySelectorAll('.column-toggle').forEach(b=>{if(b.getAttribute('aria-expanded')==='false')b.click()})`);await ready();await shot();
  assert.equal(errors.length,0,JSON.stringify(errors));assert.equal(requests.filter(u=>!u.startsWith(url)&&!u.startsWith('blob:')&&!u.startsWith('data:')).length,0);
  await evaluate(`window.realFetch=window.fetch;window.fetch=async(...args)=>args[0]==='/api/render'?new Response(JSON.stringify({detail:'rr-lut 실행 실패: 테스트'}),{status:503,headers:{'Content-Type':'application/json'}}):realFetch(...args);schedule(0);`);await wait(`$('editor').disabled && !$('engine-error').hidden`);assert.equal(await evaluate(`$('save').disabled&&$('install').disabled`),true);
  console.log('PASS: 1440x900 and 1600x1000 viewport fit, offline requests, zero JS errors, 503 disables panels/actions');
  console.log('Screenshot: docs/screenshot.png (synthetic colour chart)');
  await evaluate('window.fetch=realFetch');
  }
  // RAW checks use a real sample when present, otherwise a deterministic MCP fixture.
  await wait(`typeof photoGeneration!=='undefined'&&Boolean(config.raw)`);
  let fakeRaw=false;
  if(!await evaluate(`photos.some(p=>p.kind==='raw')&&config.raw.available`)){
    fakeRaw=true;
    const fixture=JSON.parse(require('node:child_process').execFileSync(process.env.PYTHON||'python3',[path.join(__dirname,'fixtures/fake_lightcraft.py'),'--fixture'],{encoding:'utf8',env:{...process.env,LIGHTCRAFT_CONFIG_DIR:profile}}));
    await evaluate(`window.rawFixture=${JSON.stringify(fixture)};window.rawRealFetch=fetch;window.rawRequests=[];
      window.fetch=async(input,options={})=>{
        if(!String(input).startsWith('/api/raw/'))return rawRealFetch(input,options);
        const body=JSON.parse(options.body||'{}');rawRequests.push({input,body});
        if(input==='/api/raw/render')return new Response(Uint8Array.from(atob(rawFixture.jpeg),c=>c.charCodeAt(0)),{headers:{'Content-Type':'image/jpeg','X-Render-Ms':'1.0'}});
        if(input==='/api/raw/reset')rawFixture.controls.forEach(s=>s.value=s.default);
        return Response.json({...rawFixture,unmapped:[]});
      };
      config.raw={available:true};photos.push({path:'/fixture/sample.dng',name:'sample.dng',kind:'raw',thumbnail:'data:image/jpeg;base64,'+rawFixture.jpeg});drawPhotos();`);
  }
  await evaluate(`$('search').value='';$('source').value='';$('category').value='';onlyFavorites=false;selectPhoto(photos.find(p=>p.kind==='raw'))`);await ready();
  assert.equal(await evaluate(`$('mode-badge').textContent`),'RAW · XMP');
  assert.equal(await evaluate(`['raw-effects','raw-detail'].every(id=>$(id)&&!$(id).hidden&&!$('editor').disabled)&&$('copy').hidden&&$('save').textContent.includes('.xmp')`),true);
  assert.deepEqual(await evaluate(`[...$('raw-effects').querySelectorAll('.raw-group')].map(g=>[g.querySelector('h3').textContent,[...g.querySelectorAll('label')].map(l=>l.textContent)])`),
    [['효과',['텍스처','명료도','안개 제거']],['비네팅',['양','중간점','원형률','페더','밝은 영역']],['그레인',['양','크기','거칠기']]]);
  assert.deepEqual(await evaluate(`[...$('raw-detail').querySelectorAll('.raw-group')].map(g=>[g.querySelector('h3').textContent,[...g.querySelectorAll('label')].map(l=>l.textContent)])`),
    [['선명하게',['양','반경','세부 묘사','마스킹']],['노이즈 감소',['휘도','세부','대비','색상','색상 세부','매끄러움']]]);
  assert.equal(await evaluate(`['hdr','optics','geometry','crop','bw'].every(prefix=>![...controls.keys()].some(k=>k.startsWith('raw.'+prefix+'.')))`),true);
  await evaluate(`$('raw.light.exposure').value='0.5';$('raw.light.exposure').dispatchEvent(new Event('input'));`);await ready();
  assert.equal(await evaluate(`rawValues['light.exposure']`),.5);
  await evaluate(`$('raw.light.exposure').dispatchEvent(new MouseEvent('dblclick'));`);await ready();
  assert.equal(await evaluate(`rawValues['light.exposure']`),0);
  await evaluate(`savePoints([{x:0,y:5},{x:255,y:250}]);change()`);await ready();
  assert.equal(await evaluate(`curvePoints()[0].y`),5);
  await evaluate(`openSave('save')`);assert.match(await evaluate(`$('target').textContent`),/\.xmp$/);await evaluate(`$('cancel').click()`);
  await evaluate(`setMode('filtered');document.activeElement.blur()`);
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'\\',code:'Backslash'});assert.equal(await evaluate('mode'),'original');
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'\\',code:'Backslash'});assert.equal(await evaluate('mode'),'filtered');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'y',code:'KeyY'});assert.equal(await evaluate('mode'),'side');
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'y',code:'KeyY'});
  if(fakeRaw){
    await evaluate(`rawRequests.length=0;dragging=true;putValue('raw.effects.clarity',12);schedule(0)`);await ready();
    assert.equal(await evaluate(`rawRequests.filter(r=>r.input==='/api/raw/render').at(-1).body.size`),1024);
    await evaluate(`dragging=false;schedule(0)`);await ready();
    assert.equal(await evaluate(`rawRequests.filter(r=>r.input==='/api/raw/render').at(-1).body.size`),1600);
  }
  for(const [width,height]of [[1440,900],[1600,1000]]){
    await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});await ready();
    assert.equal(await evaluate(`document.documentElement.scrollWidth<=${width}&&document.documentElement.scrollHeight<=${height}&&$('save').getBoundingClientRect().bottom<=${height}`),true);
  }
  await evaluate(`setMode('split');$('raw-effects').open=true;$('raw-detail').open=true;$('raw-basic').open=false;document.querySelector('.editor-scroll').scrollTop=$('raw-effects').offsetTop-document.querySelector('.editor-scroll').offsetTop`);
  
  await evaluate(`selectPhoto(photos.find(p=>p.kind!=='raw'))`);
  await wait(`!rawMode&&!loadingRecipe`);
  assert.equal(await evaluate(`$('mode-badge').textContent==='JPG · CUBE'&&['effects','detail'].every(id=>$(id).getAttribute('aria-disabled')==='true'&&!$(id).open)&&!$('copy').hidden`),true);
  assert.equal(errors.length,0,JSON.stringify(errors));
  console.log('PASS: RAW badge/panels/curves/controls, XMP dialog, held original and side-by-side, drag/release sizes, both viewports, return to JPG, zero JS errors'+(fakeRaw?' (fake engine fixture)':''));
 }finally{ws?.close();chrome.kill();if(chrome.exitCode===null&&chrome.signalCode===null)await new Promise(resolve=>chrome.once('exit',resolve));fs.rmSync(profile,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
