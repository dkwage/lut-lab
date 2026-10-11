// No browser/GPU needed: exercise the actual UI's async state and render functions.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/../lutlab/static/index.html','utf8');
function section(from,to){return html.slice(html.indexOf(from),html.indexOf(to,html.indexOf(from)));}
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,disabled:false,style:{},dataset:{},value:'',classList:{toggle(){}},setAttribute(){}});return nodes.get(id);};
const calls=[],renders=[];
let resolveOld;
const context=vm.createContext({console,Map,Set,Promise,JSON,Math,Number,String,Error,AbortController,Response,
  setTimeout:()=>1,clearTimeout(){},devicePixelRatio:1,performance,
  URL:{createObjectURL:blob=>'blob:'+blob.name,revokeObjectURL(){}},
  Image:class{constructor(){this.naturalWidth=1600;this.naturalHeight=1000;}async decode(){}},
  document:{querySelectorAll:()=>[]},$:node,
  clone:value=>JSON.parse(JSON.stringify(value)),
  photo:null,rawMode:false,rawSession:null,rawValues:{},rawCurves:{},rawSpecs:[],photoGeneration:0,
  rawOpenQueue:Promise.resolve(),infoGeneration:0,generation:0,loadingRecipe:false,timer:null,controller:null,
  config:{available:false,error:'rr-lut unavailable',raw:{available:true}},selected:null,originalCache:null,
  controls:new Map(),channels:['luma','red','green','blue'],mode:'split',imageSize:[1,1],currentURLs:[],dragging:false,
  recipe:{version:1,adjustments:{},amount:1},baseLut:null,recipeMode:'none',
  setTat(){},drawLuts(){},showJpgPanels(){},buildRawPanels(){},syncControls(){},fitFrame(){},updateHistogram(){},
  message(){},disableEditor(){},schedule(){},cubeBody:()=>({recipe:{}}),
  api:async(path,body)=>{calls.push({path,body});if(path==='raw/open')return {session:'token-'+body.photo,settings:{curve:{}},controls:[{id:'light.exposure',value:0}]};return {session:'reset',settings:{curve:{}},controls:[],unmapped:[]};},
  fetch:async(path,options)=>{const body=JSON.parse(options.body);renders.push({path,body});if(body.values?.['light.exposure']===1)await new Promise(resolve=>resolveOld=resolve);return {ok:true,headers:{get:()=>12},blob:async()=>({name:body.values?.['light.exposure']??'original'})};}
});
const run=code=>vm.runInContext(code,context);
run(section('const renderAvailable=', 'function readRaw('));
run(section('function readRaw(', 'const RAW_LABELS='));
run(section('async function render(token)', 'function setMode('));
run("const engineAvailable=()=>rawMode?config.raw?.available:config.available;");
(async()=>{
  await run("selectPhoto({path:'one.arw',name:'one',kind:'raw'})");
  assert.equal(run('rawMode'),true);assert.equal(node('mode-badge').textContent,'RAW · XMP');assert.equal(node('editor').disabled,false);
  assert.equal(node('copy').hidden,true);assert.match(node('save').textContent,/\.xmp/);
  assert.equal(run('rawSession'),'token-one.arw');
  await Promise.all([run("selectPhoto({path:'old.arw',name:'old',kind:'raw'})"),run("selectPhoto({path:'new.arw',name:'new',kind:'raw'})")]);
  assert.equal(run('rawSession'),'token-new.arw');assert.equal(run('photo.path'),'new.arw');
  assert.deepEqual(calls.filter(c=>c.path==='raw/open').map(c=>c.body.photo),['one.arw','old.arw','new.arw']);
  run("rawValues={'light.exposure':1};generation=10");const old=run('render(10)');
  for(let i=0;i<10&&!resolveOld;i++)await Promise.resolve();
  run("rawValues={'light.exposure':2};generation=11;dragging=true");await run('render(11)');
  assert.equal(node('after').src,'blob:2');resolveOld();await old;assert.equal(node('after').src,'blob:2');
  assert.equal(renders.at(-1).path,'/api/raw/render');assert.equal(renders.at(-1).body.size,1024);
  run('dragging=false;generation=12');await run('render(12)');assert.equal(renders.at(-1).body.size,1600);
  assert.equal(renders.filter(r=>r.body.original).length,2); // two racing first renders; subsequent original is cached.
  await run("selectRawLut({path:'filter.xmp',name:'filter',kind:'xmp'})");assert.equal(calls.at(-1).path,'raw/preset');assert.equal(run('rawSession'),'reset');
  await run("selectPhoto({path:'test.jpg',name:'test',kind:'image'})");assert.equal(run('rawMode'),false);assert.equal(node('copy').hidden,false);assert.equal(node('editor').disabled,false);assert.equal(node('install').hidden,true);assert.equal(node('engine-error').textContent,'보정 엔진(rr-lut) 없음');
  const basicInput={}, amountInput={};
  const panel=(id,input)=>({id,querySelectorAll:()=>[input],setAttribute(key,value){this[key]=value;}});
  const basic=panel('basic',basicInput),amount=panel('amount-panel',amountInput);
  context.document.querySelectorAll=()=>[basic,amount];
  run('enableEditor()');assert.equal(basicInput.disabled,true);assert.equal(basic.inert,true);
  assert.equal(amountInput.disabled,false);assert.equal(amount.inert,false);assert.equal(node('save').disabled,false);
  run('config.available=true;config.install="exports";enableEditor()');assert.equal(node('editor').disabled,false);
  assert.equal(basicInput.disabled,false);assert.equal(node('install').hidden,false);
  console.log('PASS: actual UI functions: mode routing, separate engine availability, serialized opens, RAW preset/settings, original cache, drag/release resolution, stale preview rejection');
})().catch(error=>{console.error(error);process.exitCode=1;});
