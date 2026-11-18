import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, writeFileSync } from 'node:fs';
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const compiled = spawnSync(process.execPath, [join(root,'frontend/node_modules/typescript/bin/tsc'),
  'lib/api.ts','lib/response-validation.ts','lib/response-schemas.ts','lib/contracts.ts','app/api/v1/[...path]/route.ts',
  '--outDir',join(root,'.cache/sd10-node'),'--rootDir','.', '--module','commonjs','--target','ES2022',
  '--skipLibCheck','--esModuleInterop','--typeRoots','node_modules/@types'],
  {cwd:join(root,'frontend'),windowsHide:true,encoding:'utf8'});
assert.equal(compiled.status,0,compiled.stdout+compiled.stderr);
const require = createRequire(import.meta.url);
const { api, ApiError } = require('../.cache/sd10-node/lib/api.js');
const { matchesSchema } = require('../.cache/sd10-node/lib/response-validation.js');
const { GET, POST } = require('../.cache/sd10-node/app/api/v1/[...path]/route.js');
const savedFetch = globalThis.fetch;
const checks = [];
const signal = new AbortController().signal;
const fixture = {businessId:'training-institute',sessionId:'session-1',sessionToken:'synthetic-token'.repeat(4),engineReady:false,channel:'web'};
const message = {messageId:'message-1',content:'Hello',timestamp:'2026-11-16T10:00:00Z'};
const reply = {status:'completed',sessionId:'session-1',messageId:'message-1',reply:'Verified fixture response',sources:[],actions:[]};
const doc = {businessId:'training-institute',documentId:'policy',version:1,title:'Policy',format:'text',status:'ready',expectedChunks:1,createdAt:'2026-11-16T10:00:00Z',content:'Synthetic policy'};
async function check(name, run) { await run(); checks.push({name,passed:true}); }
function response(body,status=200) { globalThis.fetch = async () => Response.json(body,{status}); }
async function invalid(run) { await assert.rejects(run, e => e instanceof ApiError && e.code==='INVALID_RESPONSE'); }
try {
  await check('Valid session and completed/processing messages are accepted',async()=>{
    response(fixture); assert.equal((await api.start('training-institute')).sessionId,'session-1');
    response(reply); assert.equal((await api.send('training-institute',fixture,message)).reply,reply.reply);
    response({status:'processing',sessionId:'session-1',messageId:'message-1'},202);
    assert.equal((await api.send('training-institute',fixture,message)).status,'processing');
  });
  await check('Valid nested tool receipts and owned policy sources accepted',async()=>{
    response({...reply,actions:[{executionId:'execution-1',handlerInvoked:true,result:{status:'success',callId:'call-1',tool:'getCourseDetails',data:{courseID:'DS202'}}}],
      sources:[{businessId:'training-institute',knowledgeBaseId:'institute-kb',documentId:'policy',documentVersion:1,chunkId:'chunk-1',title:'Policy',excerpt:'Synthetic policy'}]});
    assert.equal((await api.send('training-institute',fixture,message)).actions.length,1);
  });
  await check('Valid lead summary accepted and foreign summary identity rejected',async()=>{
    const summary={leadId:'lead-1',customerName:'Synthetic Customer',summary:'Fixture summary',intent:'Learning enquiry',qualificationStatus:'qualified',recommendedAction:'Discuss options'};
    const lead={businessId:'training-institute',leadId:'lead-1',sessionId:'session-1',createdAt:'2026-11-16T10:00:00Z',customer:{name:'Synthetic Customer',phone:'9876543210',statedInterest:'data science'},automation:{eventId:'event-1',status:'completed',attempts:1,retryable:false,summary}};
    response({items:[lead]});assert.equal((await api.leads('training-institute',signal)).items.length,1);
    response({items:[{...lead,automation:{...lead.automation,summary:{...summary,leadId:'other-lead'}}}]});
    await invalid(()=>api.leads('training-institute',signal));
  });
  for (const [name,body] of [
    ['Missing reply',{...reply,reply:undefined}],['Wrong reply type',{...reply,reply:123}],
    ['Missing discriminator',{...reply,status:undefined}],['Unknown status',{...reply,status:'done'}],
    ['Wrong session',{...reply,sessionId:'other'}],['Wrong message',{...reply,messageId:'other'}],
    ['Malformed nested source',{...reply,sources:[{}]}],['Unexpected field',{...reply,secret:'not-rendered'}],
    ['Foreign source',{...reply,sources:[{businessId:'fitness-studio',knowledgeBaseId:'fitness-kb',documentId:'policy',documentVersion:1,chunkId:'chunk-1',title:'Foreign',excerpt:'Foreign'}]}],
    ['Missing action status',{...reply,actions:[{executionId:'exec-1',result:{callId:'call-1',tool:'createLead',data:{}}}]}],
  ]) await check(name+' rejected',async()=>{response(body);await invalid(()=>api.send('training-institute',fixture,message));});
  await check('Foreign session-start business rejected',async()=>{response({...fixture,businessId:'fitness-studio'});await invalid(()=>api.start('training-institute'));});
  await check('Malformed JSON is a safe typed response failure',async()=>{
    globalThis.fetch=async()=>new Response('<html>bad upstream</html>');await invalid(()=>api.start('training-institute'));
  });
  await check('Unvalidated error payload is not reflected to the user',async()=>{
    response({error:{message:'RAW PRIVATE STACK',code:'RAW'}},503);
    await assert.rejects(()=>api.start('training-institute'),e=>e.code==='REQUEST_FAILED'&&!e.message.includes('PRIVATE'));
  });
  await check('Valid backend error code and status are preserved',async()=>{
    response({error:{code:'ENGINE_UNAVAILABLE',message:'Unavailable',retryable:true,requestId:'00000000-0000-4000-8000-000000000001'}},503);
    await assert.rejects(()=>api.send('training-institute',fixture,message),e=>e.code==='ENGINE_UNAVAILABLE'&&e.status===503);
  });
  await check('Foreign document and mismatched requested version rejected',async()=>{
    for(const body of [{...doc,businessId:'fitness-studio'},{...doc,version:2}]) {
      response(body);await invalid(()=>api.document('training-institute','policy',1,signal));
    }
  });
  await check('Foreign list item rejected',async()=>{response({items:[{...doc,businessId:'fitness-studio',content:undefined}]});await invalid(()=>api.documents('training-institute',signal));});
  await check('Completed turn without output rejected',async()=>{
    response({items:[{turnId:'turn-1',messageId:'message-1',sequence:1,inputText:'Hello',status:'completed',output:null}]});
    await invalid(()=>api.turns('training-institute','session-1',signal));
  });
  await check('Invalid bounds, enums, timestamps and identifiers rejected',async()=>{
    for(const body of [{...doc,version:0},{...doc,format:'pdf'},{...doc,createdAt:'invalid'},{...doc,createdAt:'2026-02-30T10:00:00Z'},{...doc,createdAt:'2026-11-16T10:00:00'},{...doc,documentId:'bad space'}]) {
      response(body);await invalid(()=>api.document('training-institute','policy',1,signal));
    }
    assert.equal(matchesSchema({businesses:[{businessId:'ok',businessName:'name',agentRole:99}]},'BusinessList'),false);
  });
  await check('Caller cancellation and deadline signals are combined',async()=>{
    const owner=new AbortController(); let actual;
    globalThis.fetch=async(url,options)=>{actual=options.signal;return Response.json({businesses:[]});};
    await api.businesses(owner.signal);assert.notEqual(actual,owner.signal);owner.abort();assert.equal(actual.aborted,true);
  });
  await check('Read timeout remains active with a caller signal',async()=>{
    const timeout=AbortSignal.timeout;AbortSignal.timeout=()=>AbortSignal.abort(new DOMException('timeout','TimeoutError'));
    globalThis.fetch=async(url,options)=>{options.signal.throwIfAborted();return Response.json({});};
    try {await assert.rejects(()=>api.businesses(signal),e=>e.code==='REQUEST_TIMEOUT');} finally {AbortSignal.timeout=timeout;}
  });
  await check('Same-message retries retain exact body and token',async()=>{
    const calls=[];globalThis.fetch=async(url,options)=>{calls.push(options);return Response.json(reply);};
    await api.send('training-institute',fixture,message);await api.send('training-institute',fixture,message);
    assert.equal(calls[0].body,calls[1].body);assert.equal(calls[0].headers['X-Session-Token'],'synthetic-token'.repeat(4));
  });
  await check('Proxy propagates GET cancellation',async()=>{
    const controller=new AbortController();let actual;
    globalThis.fetch=async(url,options)=>{actual=options.signal;controller.abort();return Response.json({businesses:[]});};
    await GET(new Request('http://localhost/api/v1/businesses',{signal:controller.signal}),{params:Promise.resolve({path:['businesses']})});
    assert.equal(actual.aborted,true);
  });
  await check('Proxy does not interpret disconnected POST as server cancellation',async()=>{
    const controller=new AbortController();let actual;
    globalThis.fetch=async(url,options)=>{actual=options.signal;controller.abort();return Response.json(fixture);};
    await POST(new Request('http://localhost/api/v1/businesses/training-institute/sessions',{method:'POST',body:'{}',signal:controller.signal}),{params:Promise.resolve({path:['businesses','training-institute','sessions']})});
    assert.equal(actual.aborted,false);
  });
} finally {globalThis.fetch=savedFetch;mkdirSync(new URL('../docs/results/',import.meta.url),{recursive:true});writeFileSync(new URL('../docs/results/frontend-api.json',import.meta.url),JSON.stringify({passed:checks.length===26,checks,scope:'Compiled actual frontend client/validator/proxy with intercepted fetch; no live model calls.'},null,2)+'\n');}
console.log(`PASS: ${checks.length} frontend API/validator/proxy checks.`);
