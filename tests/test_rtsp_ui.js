const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const nodes=new Map();
function element(){return {value:'',checked:false,disabled:false,hidden:false,open:false,children:[],append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}};}
function node(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);}
const fields=['id','name','model','address','username','password','onvif_port','rtsp_port','enabled','events','ptz','hq_path','lq_path','rtsp_anonymous'];
const form=node('camera-form');form.elements={};for(const name of fields)form.elements[name]=element();
form.elements[Symbol.iterator]=function*(){for(const name of fields)yield this[name];};
form.reset=()=>{for(const e of form.elements){e.value='';e.checked=false;}form.elements.model.value='auto';form.elements.onvif_port.value='80';form.elements.rtsp_port.value='554';};
node('auth-form').elements={password:element(),host:element()};
const ctx=vm.createContext({document:{getElementById:node,createElement:element},setInterval(){},fetch:async()=>({ok:true,json:async()=>({setup_required:false,authenticated:false})})});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),ctx);
(async()=>{
 await vm.runInContext('session()',ctx);
 vm.runInContext('state={revision:5};openCamera()',ctx);
 form.elements.model.value='rtsp';vm.runInContext('modelChanged()',ctx);
 assert.equal(node('rtsp-fields').hidden,false);assert.equal(form.elements.hq_path.required,true);
 assert.equal(node('ptz-option').hidden,true);assert.equal(node('events-option').hidden,true);assert.equal(node('onvif-port-field').hidden,true);
 assert.equal(node('camera-user-label').textContent,'RTSP / Protect username');
 for(const [k,v] of Object.entries({name:'RTSP test',address:'192.168.1.20',username:'test',password:'test-only',hq_path:'/main',lq_path:'/sub'}))form.elements[k].value=v;
 form.elements.rtsp_anonymous.checked=true;
 const requests=[];ctx.requestCapture=requests;
 vm.runInContext(`api=async(path,data)=>{requestCapture.push({path,data});return path==='/api/inspect'?{id:'test-job'}:path.startsWith('/api/inspection/')?{status:'ready',message:'Ready'}:{};};refresh=async()=>{};`,ctx);
 await form.onsubmit({preventDefault(){},currentTarget:form});
 assert.equal(requests[0].path,'/api/inspect');assert.equal(requests[0].data.hq_path,'/main');assert.equal(requests[0].data.rtsp_anonymous,true);
 assert.equal(form.elements.password.value,'');assert.equal(form.elements.hq_path.value,'');assert.equal(form.elements.hq_path.disabled,true);
 assert.equal(node('camera-save').textContent,'Add detected camera');
 await form.onsubmit({preventDefault(){},currentTarget:form});
 assert.equal(requests.at(-1).path,'/api/add-inspected');assert.equal(requests.at(-1).data.inspection,'test-job');
 ctx.saved={id:'test',name:'Saved',model:'rtsp',address:'192.168.1.20',username:'test',onvif_port:80,rtsp_port:554,rtsp_anonymous:true,enabled:true,events:false,ptz:false};
 vm.runInContext('openCamera(saved)',ctx);
 assert.equal(form.elements.rtsp_anonymous.checked,true);assert.equal(form.elements.rtsp_anonymous.disabled,true);assert.equal(form.elements.hq_path.required,false);
 vm.runInContext('openCamera()',ctx);form.elements.model.value='auto';vm.runInContext('modelChanged()',ctx);
 assert.equal(node('rtsp-fields').hidden,true);assert.equal(node('onvif-port-field').hidden,false);assert.equal(form.elements.hq_path.required,false);
 console.log('RTSP UI: mode selection, inspection/add flow, secret clearing, saved settings and ONVIF mode passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
