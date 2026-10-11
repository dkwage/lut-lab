// Actual UI functions with a minimal DOM; no browser, native window or packages.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/../lutlab/static/index.html','utf8');
const section=(from,to)=>html.slice(html.indexOf(from),html.indexOf(to,html.indexOf(from)));
const nodes=new Map();
function element(tag,className='',textContent=''){
  const node={tag,className,textContent,children:[],dataset:{},style:{},
    append(...children){for(const child of children){this.children.push(child);child.parentElement=this;}},
    prepend(child){this.children.unshift(child);child.parentElement=this;},setAttribute(){},remove(){this.removed=true;},focus(){},select(){this.selected=true;}};
  return node;
}
const node=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id);};
const panels=[],controls=new Map(),uploads=[],requests=[];
const document={body:element('body'),activeElement:element('button'),createElement:element,
  querySelectorAll:()=>[],execCommand:()=>true};
const context=vm.createContext({console,document,element,$:node,controls,Map,Set,Promise,JSON,Object,Error,
  navigator:{},window:{addEventListener(){}},upload:files=>uploads.push(files),
  rawValues:{},rawCurves:{},rawSpecs:[],channels:[],channel:'luma',zones:[],metricLabels:[],
  curvePanel:{insertBefore(){}},curve:{},curveHint:{},syncControls(){},tabs(){},
  panel(id,title,reset){const box=element('details'),body=element('div');box.id=id;box.reset=reset;box.append(body);panels.push(box);return body;},
  control(body,key,label){const row=element('label','',label);body.append(row);controls.set(key,{row});},
});
const run=code=>vm.runInContext(code,context);
(async()=>{
  run(section('async function copyText(', "$('copy').onclick="));
  await run("copyText('fallback command')");
  let field=document.body.children.at(-1);
  assert.equal(field.value,'fallback command');assert.equal(field.selected,true);assert.equal(field.removed,true);
  context.navigator.clipboard={writeText:async text=>requests.push(text)};
  await run("copyText('native command')");assert.deepEqual(requests,['native command']);
  context.navigator.clipboard.writeText=async()=>{throw Error('denied');};
  await run("copyText('denied fallback')");assert.equal(document.body.children.at(-1).value,'denied fallback');
  document.execCommand=()=>false;
  await assert.rejects(()=>run("copyText('failure')"),/복사하지 못했습니다/);
  assert.equal(document.body.children.at(-1).removed,true);

  run(section("$('upload').onclick=",'function defaultName()'));
  let picker=0;node('file').click=()=>picker++;node('upload').onclick();assert.equal(picker,1);
  node('file').files=[{name:'picked.jpg'}];node('file').onchange();
  node('drop-zone').classList={add(){},remove(){}};
  let prevented=false;node('drop-zone').ondrop({preventDefault(){prevented=true;},dataTransfer:{files:[{name:'dropped.arw'}]}});
  assert.equal(prevented,true);assert.equal(uploads[0][0].name,'picked.jpg');assert.equal(uploads[1][0].name,'dropped.arw');

  run(section('const RAW_LABELS=', 'let heldMode='));
  context.rawSpecs=run("Object.keys(RAW_LABELS).filter(k=>/^(effects|vignette|grain|detail)\\./.test(k)).reverse().map(id=>({id,min:0,max:100,step:1,default:0}))");
  run('buildRawPanels()');
  const groups=id=>panels.find(p=>p.id===id).children[0].children.filter(n=>n.className==='raw-group').map(g=>[g.children[0].textContent,g.children.slice(1).map(n=>n.textContent)]);
  assert.deepEqual(groups('raw-effects'),[['효과',['텍스처','명료도','안개 제거']],['비네팅',['양','중간점','원형률','페더','밝은 영역']],['그레인',['양','크기','거칠기']]]);
  assert.deepEqual(groups('raw-detail'),[['선명하게',['양','반경','세부 묘사','마스킹']],['노이즈 감소',['휘도','세부','대비','색상','색상 세부','매끄러움']]]);
  panels.find(p=>p.id==='raw-effects').reset();assert.equal(context.rawValues['grain.amount'],0);

  if(process.argv[2]){
    const appHTML=fs.readFileSync(process.argv[2],'utf8');
    const bootstrap=appHTML.match(/<script>([\s\S]*?)<\/script>/)[1];
    class Storage{getItem(key){return this[key]??null;}setItem(key,value){this[key]=String(value);}removeItem(key){delete this[key];}}
    const localStorage=new Storage(),saved=[];
    const app=vm.createContext({console,Storage,localStorage,document,fetch:async(url,options)=>{saved.push(JSON.parse(options.body));return {ok:true};}});
    vm.runInContext(bootstrap,app);
    assert.equal(localStorage.getItem('lut-lab-drop-zone-collapsed'),'true');
    assert.equal(localStorage.getItem('lut-lab-favorites'),'["example.cube"]');
    vm.runInContext("localStorage.setItem('lut-lab-drop-zone-collapsed','false')",app);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(saved.at(-1)['lut-lab-drop-zone-collapsed'],'false');
  }
  console.log('PASS: file input/drop routing, clipboard available/absent/denied/failure, RAW subgroup labels/reset, native storage restore/write');
})().catch(error=>{console.error(error);process.exitCode=1;});
