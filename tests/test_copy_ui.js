const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
async function fixture(clipboard,legacy){
  const nodes=new Map(),copied=[],commands=[],intervals=[];let selected=null;
  function element(){return {hidden:false,value:'',textContent:'',children:[],append(...c){this.children.push(...c);},replaceChildren(...c){this.children=c;},setAttribute(){},addEventListener(){},focus(){selected=this;doc.activeElement=this;},select(){this.selection=[0,this.value.length];},setSelectionRange(a,b){this.selection=[a,b];}};}
  const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  node('camera-form').elements={model:{}};node('auth-form').elements={host:{},password:{}};
  const doc={activeElement:element(),getElementById:node,createElement:element,execCommand(action){commands.push(action);if(legacy==='throw')throw Error('blocked');if(legacy)copied.push(selected.value);return !!legacy;}};
  const ctx=vm.createContext({document:doc,navigator:clipboard?{clipboard:{writeText:async value=>{if(clipboard==='reject')throw Error('denied');copied.push(value);}}}:{},setTimeout(){},setInterval(fn,ms){intervals.push({fn,ms});},fetch:async()=>({ok:true,json:async()=>({authenticated:false,setup_required:false})})});
  vm.runInContext(source,ctx);await vm.runInContext('session()',ctx);
  // Click the actual rendered button, not a standalone copy helper.
  vm.runInContext(`state={host:'10.11.11.144',capacity:16,cameras:[{id:'camera',name:'Lounge',model:'cam-s3w',address:'10.11.11.140',enabled:true,endpoints:[{onvif_port:8101}]}],runtime:{camera:'Running'}};render();`,ctx);
  const all=[];function walk(e){all.push(e);for(const c of e.children||[])walk(c);}walk(node('cameras'));
  const button=all.find(e=>e.textContent==='Copy IP:port');const manual=all.find(e=>e.readOnly);
  await button.onclick();
  return {ctx,doc,intervals,copied,commands,manual,button,notice:node('notice')};
}
(async()=>{
  const secure=await fixture('ok',false);assert.deepEqual(secure.copied,['10.11.11.144:8101']);assert.deepEqual(secure.commands,[]);assert.equal(secure.button.textContent,'Copied!');assert.equal(secure.manual.hidden,true);
  for(const modern of [null,'reject']){
    const f=await fixture(modern,true);assert.deepEqual(f.copied,['10.11.11.144:8101']);assert.deepEqual(f.commands,['copy']);assert.equal(f.manual.hidden,true);assert.equal(f.button.disabled,false);assert.equal(f.button.textContent,'Copied!');
  }
  for(const legacy of [false,'throw']){
    const f=await fixture(null,legacy);assert.deepEqual(f.copied,[]);assert.equal(f.manual.hidden,false);assert.equal(f.manual.value,'10.11.11.144:8101');assert.deepEqual(f.manual.selection,[0,17]);assert.match(f.notice.textContent,/blocked automatic copying/);assert.doesNotMatch(f.notice.textContent,/copied\./);assert.equal(f.button.disabled,false);
    f.doc.activeElement=f.manual;vm.runInContext('refresh=()=>{throw Error("Must not refresh while selecting an address");}',f.ctx);f.intervals.find(x=>x.ms===10000).fn();
  }
  console.log('Copy UI: secure clipboard, HTTP fallback, denied clipboard and manual selection passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
