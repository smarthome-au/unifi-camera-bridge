// Exercise the shipped setup handlers without a browser or camera credentials.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
async function fixture(suggestion){
  const nodes=new Map(),requests=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,open:false,textContent:'',addEventListener(){},close(){this.open=false;}});return nodes.get(id);};
  const form=node('auth-form');form.elements={host:{value:''},password:{value:'test-admin-password'}};
  form.reset=()=>{form.elements.host.value='';form.elements.password.value='';};
  node('camera-form').elements={model:{}};
  let setup=true;
  const context=vm.createContext({document:{getElementById:node},setInterval(){},fetch:async(path,options)=>{
    if(options.body){requests.push({path,body:JSON.parse(options.body)});setup=false;}
    return {ok:true,json:async()=>path==='/api/session'?{setup_required:setup,authenticated:false,suggested_host:setup?suggestion:''}:{ok:true}};
  }});
  vm.runInContext(source,context);await vm.runInContext('session()',context);
  return {node,form,context,requests};
}
(async()=>{
  const lan=await fixture('192.168.50.20');
  assert.equal(lan.form.elements.host.value,'192.168.50.20');
  assert.equal(lan.form.elements.host.required,true);
  lan.form.elements.host.value='192.168.60.20';
  await vm.runInContext('session()',lan.context);
  assert.equal(lan.form.elements.host.value,'192.168.60.20','Re-render must retain manual overrides');
  await lan.form.onsubmit({preventDefault(){},currentTarget:lan.form});
  assert.deepEqual(lan.requests,[{path:'/api/setup',body:{password:'test-admin-password',host:'192.168.60.20'}}]);
  assert.equal(lan.node('setup-fields').hidden,true);
  assert.equal(lan.form.elements.host.required,false);
  const local=await fixture('');
  assert.equal(local.form.elements.host.value,'');
  assert.match(local.node('setup-host-hint').textContent,/enter the address/);
  const html=fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
  assert.doesNotMatch(html,/name="token"|setup-token|One-time setup code/);
  console.log('Setup UI: automatic fill, manual override, token-free submission and login fallback passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
