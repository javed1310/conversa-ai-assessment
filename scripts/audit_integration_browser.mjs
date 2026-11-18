// Real browser/API/Next integration, isolated local services and database, no npm test dependency.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, openSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const logs = join(root, '.cache/integration-20261118-browser');
mkdirSync(logs, { recursive: true });
const python = join(root, 'backend/.venv-system/Scripts/python.exe');
const database = `ai_engagement_sd10_browser_${Date.now()}`;
const apiPort = 4018, webPort = 3018;
const owned = [];
let socket;
const pause = ms => new Promise(done => setTimeout(done, ms));
function stop(process) {
  if (process?.pid && process.exitCode === null) spawnSync('taskkill.exe', ['/PID', String(process.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
}
async function availablePort(port = 0) {
  const server = createServer();
  await new Promise((done, fail) => { server.once('error', fail); server.listen(port, '127.0.0.1', done); });
  const selected = server.address().port;
  await new Promise(done => server.close(done));
  return selected;
}
function service(binary, args, cwd, env, label) {
  const child = spawn(binary, args, { cwd, env: { ...process.env, ...env }, windowsHide: true,
    stdio: ['ignore', openSync(join(logs, `${label}.out.log`), 'w'), openSync(join(logs, `${label}.err.log`), 'w')] });
  owned.push(child);
  return child;
}
async function ready(url, process) {
  for (let attempt = 0; attempt < 60; attempt++) {
    if (process.exitCode !== null) throw new Error(`Owned process exited; inspect ${logs}`);
    try { if ((await fetch(url)).ok) return; } catch {}
    await pause(250);
  }
  throw new Error(`Readiness timeout: ${url}`);
}
const report = { module:'INTEGRATION-20261118', passed: false, provider: 'disabled', scope: 'Offline real-browser response race, errors and token isolation.', checks: [] };

try {
  await availablePort(apiPort); await availablePort(webPort);
  const api = service(python, ['-m', 'app'], join(root, 'backend'), {
    API_PORT: String(apiPort), MONGODB_URI: `mongodb://127.0.0.1:27017/${database}`,
    AI_CHAT_ENABLED: 'false', AUTOMATION_ENABLED: 'false', AUTOMATION_POLL_MS: '500',
  }, 'api');
  const web = service(process.execPath, [join(root, 'frontend/node_modules/next/dist/bin/next'),
    'start', '--hostname', '127.0.0.1', '--port', String(webPort)], join(root, 'frontend'), {
    ASSESSMENT_API_ORIGIN: `http://127.0.0.1:${apiPort}`,
  }, 'web');
  await ready(`http://127.0.0.1:${apiPort}/api/v1/health/ready`, api);
  await ready(`http://127.0.0.1:${webPort}`, web);
  const chrome = ['C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync);
  assert.ok(chrome, 'Installed Chrome/Edge required.');
  const debugPort = await availablePort();
  service(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
    `--remote-debugging-port=${debugPort}`, `--user-data-dir=${join(logs, database)}`, 'about:blank'], root, {}, 'browser');
  let target;
  for (let i = 0; i < 60; i++) {
    try { target = (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(t => t.type === 'page'); } catch {}
    if (target) break;
    await pause(150);
  }
  assert.ok(target);
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, fail) => { socket.onopen = done; socket.onerror = fail; });
  let counter = 0;
  let replyOverride = null;
  let rejectNextSession=false;
  const sessionPosts=[];
  const sentBodies = [];
  const pageRequests=[];
  let interruptNextReply = false;
  let holdNextList = false, heldList = null;
  let historyMode = null, heldHistory = null;
  let documentFailure=false;
  let documentMock=false, documentStatus='ingesting', documentReads=0;
  const sentMessages = [];
  const pending = new Map(), errors = [];
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Network.requestWillBeSent' && message.params.request.method==='POST' && message.params.request.url.endsWith('/messages')) sentBodies.push(message.params.request.postData);
    if (message.method === 'Network.requestWillBeSent' && message.params.request.url.includes('/conversations?cursor=')) pageRequests.push(message.params.request.url);
    if (message.method === 'Network.requestWillBeSent' && message.params.request.method==='POST' && message.params.request.url.endsWith('/sessions')) sessionPosts.push(message.params.request.postData);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
    if (message.method === 'Network.requestWillBeSent' && message.params.request.method === 'POST'
        && message.params.request.url.endsWith('/messages')) sentMessages.push(message.params.request.postData);
    if (message.method === 'Fetch.requestPaused') {
      if(rejectNextSession && message.params.request.method==='POST' && message.params.request.url.endsWith('/sessions')){rejectNextSession=false;void call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:503,responseHeaders:[{name:'Content-Type',value:'application/json'}],body:Buffer.from(JSON.stringify({error:{code:'BACKEND_UNAVAILABLE',message:'Controlled session creation failure',retryable:true,requestId:'00000000-0000-4000-8000-000000000001'}})).toString('base64')});return;}
      if (documentMock && message.params.request.method==='GET' && message.params.request.url.includes('/training-institute/documents')) {
        const detail=message.params.request.url.includes('/versions/1');
        const doc={businessId:'training-institute',documentId:'refresh-fixture',version:1,title:'Refresh fixture',format:'text',status:documentStatus,expectedChunks:1,createdAt:'2026-11-16T10:00:00Z'};
        if (detail) documentReads++;
        if(detail && documentFailure){void call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:503,responseHeaders:[{name:'Content-Type',value:'application/json'}],body:Buffer.from(JSON.stringify({error:{code:'BACKEND_UNAVAILABLE',message:'Controlled document failure',retryable:true,requestId:'00000000-0000-4000-8000-000000000001'}})).toString('base64')});return;}
        void call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:200,
          responseHeaders:[{name:'Content-Type',value:'application/json'}],
          body:Buffer.from(JSON.stringify(detail?{...doc,content:'Synthetic refresh fixture'}:{items:[doc],nextCursor:null})).toString('base64')});
        return;
      }

      if (historyMode && message.params.request.url.endsWith('/turns')) {
        if (historyMode === 'hold') { heldHistory = message.params.requestId; historyMode = null; return; }
        historyMode = null;
        void call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:200,
          responseHeaders:[{name:'Content-Type',value:'application/json'}],
          body:Buffer.from(JSON.stringify({items:[{turnId:'turn-1',messageId:'message-1',sequence:1,inputText:'FIRST_HISTORY',status:'failed',output:null}],nextCursor:null})).toString('base64')});
        return;
      }

      if (replyOverride && message.params.request.method==='POST' && message.params.request.url.endsWith('/messages')) {
        const {status,code,invalid}=replyOverride; replyOverride=null;
        void call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:status,
          responseHeaders:[{name:'Content-Type',value:'application/json'}],
          body:Buffer.from(JSON.stringify(invalid?{malformed:true}:{error:{code,message:'Controlled browser failure',retryable:status>=500,requestId:'00000000-0000-4000-8000-000000000001'}})).toString('base64')});
        return;
      }
      if (holdNextList && message.params.request.url.includes('/training-institute/conversations')) {
        holdNextList = false; heldList = message.params.requestId; return;
      }
      const drop = interruptNextReply && message.params.request.url.endsWith('/messages');
      if (drop) interruptNextReply = false;
      void call(drop ? 'Fetch.failRequest' : 'Fetch.continueRequest', {
        requestId: message.params.requestId, ...(drop ? { errorReason: 'Failed' } : {}),
      }).catch(error => errors.push(error.message));
    }
    if (message.id) {
      const callback = pending.get(message.id); pending.delete(message.id);
      if (message.error) callback.reject(new Error(message.error.message)); else callback.resolve(message.result);
    }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++counter;
    const timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 20000);
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const until = async expression => {
    for (let i = 0; i < 1200; i++) { if (await evaluate(expression)) return; await pause(100); }
    throw new Error(`Browser expectation failed: ${expression}`);
  };
  await call('Runtime.enable'); await call('Page.enable'); await call('Network.enable');
  await call('Page.navigate', { url: `http://127.0.0.1:${webPort}` });

  await call('Page.addScriptToEvaluateOnNewDocument',{source:`window.__auditSessions={};const originalFetch=window.fetch;window.fetch=async function(...args){const result=await originalFetch.apply(this,args);if(String(args[0]).endsWith('/sessions')&&args[1]?.method==='POST'&&result.ok){const data=await result.clone().json();window.__auditSessions[data.businessId]=data;}return result;};`});await call('Page.reload');
  const activeText = "document.querySelector('[data-active=true]').innerText";
  const button = text => evaluate(`Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.getClientRects().length&&(b.textContent.trim()===${JSON.stringify(text)}||b.textContent.trim()===${JSON.stringify(text)}+' \u2197'||b.querySelector('span:nth-child(2)')?.textContent.trim()===${JSON.stringify(text)}||b.getAttribute('aria-label')===${JSON.stringify(text)})).click()`);
  const fill = (label, value) => evaluate(`(()=>{const e=Array.from(document.querySelectorAll('[aria-label='+${JSON.stringify(JSON.stringify(label))}+']')).find(e=>e.getClientRects().length);const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const switchBusiness = id => evaluate(`(()=>{const e=document.getElementById('business');e.value=${JSON.stringify(id)};e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  const check = name => { report.checks.push({ name, passed: true }); console.log(`PASS: ${name}`); };

  await until("document.querySelectorAll('#business option').length===2");
  await switchBusiness('training-institute');
  for (const width of [1440, 768, 390, 320]) {
    await call('Emulation.setDeviceMetricsOverride', {width, height: 1000, deviceScaleFactor: 1, mobile: false});
    for (const tab of ['Chat', 'Conversations', 'Leads', 'Knowledge base']) {
      await button(tab); await pause(300);
      assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), `${tab} overflows at ${width}`);
      if (width === 1440 || width === 390) {
        const shot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
        writeFileSync(join(logs, `${tab.replaceAll(' ', '-')}-${width}.png`), Buffer.from(shot.data, 'base64'));
      }
    }
  }
  check('All four screens fit 320, 390, 768 and 1440 pixel viewports');
  await call('Emulation.setDeviceMetricsOverride', {width:1440, height:1000, deviceScaleFactor:1, mobile:false});
  await button('Chat');
  assert.equal(await evaluate("document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled"),false);
  await fill('Message','Direct typing before session');
  rejectNextSession=true;
  await call('Fetch.enable',{patterns:[{urlPattern:'*sessions',requestStage:'Request'}]});
  await button('Send message');await until(`${activeText}.includes('Controlled session creation failure')`);
  assert.equal(await evaluate("document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').value"),'Direct typing before session');
  assert.equal(sentBodies.length,0);
  const sessionsBefore=sessionPosts.length;
  await evaluate("(()=>{const b=document.querySelector('[data-active=true] .composer button');b.click();b.click();})()");
  await until(`${activeText}.includes('ENGINE_UNAVAILABLE')`);
  assert.equal(sentBodies.length,1);assert.equal(sessionPosts.length,sessionsBefore+1);
  await call('Fetch.disable');
  check('Direct typing auto-creates one session and sends once; failed session creation preserves draft');
  await button('New conversation');await until("document.querySelector('[data-active=true] .prompt-cards button')!==null");
  const beforeSuggested=sentBodies.length;
  await evaluate("document.querySelector('[data-active=true] .prompt-cards button').click()");
  await until("!!window.__auditSessions['training-institute'] && !document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled");
  assert.ok((await evaluate("document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').value")).length > 10);
  assert.equal(sentBodies.length, beforeSuggested);
  check('Suggested question starts a session and prefills without sending');
  await button('New conversation');
  await until("document.querySelector('[data-active=true] .composer button').textContent==='Send message'");
  await until(`${activeText}.includes('AI chat is currently unavailable.')`);
  const original = await evaluate("window.__auditSessions['training-institute'].sessionId");
  await fill('Message', 'Offline engine availability check');
  await button('Send message');
  await until(`${activeText}.includes('ENGINE_UNAVAILABLE')`);
  assert.equal(await evaluate("document.querySelectorAll('[data-active=true] .assistant').length"), 0);
  check('Disabled engine shows truthful 503 without an invented assistant answer');
  assert.equal(await evaluate("document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled"),false);
  assert.equal(await evaluate("Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='New conversation').disabled"),false);
  check('Known rejection enables composer and new conversation without an automatic resend');
  await fill('Message', 'Keyboard send check');
  const keyboardBefore = sentBodies.length;
  const shiftAllowed = await evaluate(`(()=>{const input=document.querySelector('[data-active=true] textarea[aria-label="Message"]');const event=new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true,cancelable:true});return input.dispatchEvent(event);})()`);
  assert.equal(shiftAllowed, true);
  assert.equal(sentBodies.length, keyboardBefore);
  await evaluate(`document.querySelector('[data-active=true] textarea[aria-label="Message"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}))`);
  await until(`${activeText}.includes('ENGINE_UNAVAILABLE') && document.querySelector('[data-active=true] .composer button').textContent==='Send message'`);
  assert.equal(sentBodies.length, keyboardBefore + 1);
  check('Enter sends once; Shift+Enter leaves the native newline behavior available');

  await call('Fetch.enable', { patterns: [{ urlPattern: '*conversations', requestStage: 'Response' }] });
  holdNextList = true;
  await button('Conversations');
  for (let i=0; i<100 && !heldList; i++) await pause(50);
  assert.ok(heldList, 'Institute response must be held until business switch');
  assert.ok(await evaluate(`${activeText}.includes('Loading conversations')`));
  await switchBusiness('fitness-studio');
  await button('Conversations');
  await until(`${activeText}.includes('No conversations yet.')`);
  try { await call('Fetch.continueRequest', { requestId: heldList }); } catch { /* Abort may already retire the paused request. */ }
  await pause(300);
  assert.ok(!(await evaluate(activeText)).includes(original.replace('Session ', '')));
  assert.ok(await evaluate(`${activeText}.includes('No conversations yet.')`));
  await call('Fetch.disable');
  check('Delayed institute response cannot overwrite fitness data after business switch');
  await switchBusiness('training-institute');
  await button('Chat');
  assert.equal(await evaluate("window.__auditSessions['training-institute'].sessionId"), original);
  assert.ok(await evaluate(`${activeText}.includes('Offline engine availability check')`));
  check('Rejected message and owning session remain associated with original business');
  const boundary = await evaluate(`(async()=>{
    const a=await(await fetch('/api/v1/businesses/training-institute/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    const b=await(await fetch('/api/v1/businesses/training-institute/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    const list=await(await fetch('/api/v1/businesses/training-institute/conversations?limit=1')).json();
    const next=await(await fetch('/api/v1/businesses/training-institute/conversations?limit=1&cursor='+encodeURIComponent(list.nextCursor))).json();
    const response=await fetch('/api/v1/businesses/training-institute/sessions/'+a.sessionId+'/messages',{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':b.sessionToken},body:JSON.stringify({messageId:'foreign-token',content:'Hello',timestamp:new Date().toISOString()})});
    return {first:list.items.length,next:next.items.length,different:list.items[0].sessionId!==next.items[0].sessionId,status:response.status};
  })()`);
  assert.deepEqual(boundary, {first:1,next:1,different:true,status:404});
  check('Next proxy preserves page cursor/query and rejects a foreign session token');
  await call('Fetch.enable',{patterns:[{urlPattern:'*messages',requestStage:'Request'}]});
  for (const [status,code] of [[400,'VALIDATION_ERROR'],[404,'RESOURCE_NOT_FOUND'],[503,'TURN_FAILED']]) {
    if (await evaluate("!!Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='Start conversation')")) {
      await button('Start conversation'); await until("!!window.__auditSessions['training-institute'] && !document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled");
    }
    replyOverride={status,code};
    await fill('Message','Controlled '+code); await button('Send message');
    await until(`${activeText}.includes('${code}')`);
    assert.equal(await evaluate("Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).some(b=>b.textContent==='Check message / retry same message')"),false);
    assert.ok(await evaluate("Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).some(b=>['Start conversation','New conversation'].includes(b.textContent)&&!b.disabled)"));
  }
  check('400, 404 and terminal failed-turn responses have an explicit next-session path');
  await button('Start conversation'); await until("!!window.__auditSessions['training-institute'] && !document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled");
  replyOverride={status:503,code:'BACKEND_UNAVAILABLE'};
  await fill('Message','Uncertain delivery keeps original identity'); await button('Send message');
  await until(`${activeText}.includes('BACKEND_UNAVAILABLE')`);
  replyOverride={status:503,code:'BACKEND_UNAVAILABLE'};
  await button('Check message / retry same message');
  await until("!!Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='Review unresolved message'&&!b.disabled)");
  assert.equal(sentBodies.at(-1),sentBodies.at(-2));
  const beforeLeave=sentBodies.length;
  await button('Review unresolved message');
  assert.ok(await evaluate(`${activeText}.includes('does not cancel or retry')`));
  await button('Leave this session without retrying');
  assert.equal(sentBodies.length,beforeLeave);
  await button('Start conversation'); await until("!!window.__auditSessions['training-institute'] && !document.querySelector('[data-active=true] textarea[aria-label=\"Message\"]').disabled");
  check('Uncertain delivery retries the exact body; leaving warns and does not resend');
  replyOverride={status:200,code:'MALFORMED_SUCCESS'};
  await fill('Message','Malformed success retains pending identity'); await button('Send message');
  await until(`${activeText}.includes('INVALID_RESPONSE')`);
  assert.ok(await evaluate("Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).some(b=>b.textContent==='Check message / retry same message')"));
  replyOverride={status:200,code:'MALFORMED_SUCCESS'};
  await button('Check message / retry same message');
  await until("!!Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='Review unresolved message'&&!b.disabled)");
  assert.equal(sentBodies.at(-1),sentBodies.at(-2));
  check('Malformed success fails closed and retains the original message for exact replay');
  replyOverride={status:404,code:'IGNORED_MALFORMED_ERROR',invalid:true};
  await button('Check message / retry same message');
  await until(`${activeText}.includes('REQUEST_FAILED')`);
  assert.ok(await evaluate("Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).some(b=>b.textContent==='Check message / retry same message')"));
  check('Malformed 404 error is treated as uncertain and keeps the original pending message');

  await call('Fetch.disable');
  await call('Fetch.enable',{patterns:[{urlPattern:'*conversations',requestStage:'Response'}]});
  holdNextList=true; heldList=null;
  await button('Conversations');
  for(let i=0;i<100&&!heldList;i++) await pause(50);
  assert.ok(heldList);
  await call('Fetch.fulfillRequest',{requestId:heldList,responseCode:200,
    responseHeaders:[{name:'Content-Type',value:'application/json'}],
    body:Buffer.from(JSON.stringify({items:[{businessId:'fitness-studio',sessionId:'foreign-session',channel:'web',createdAt:new Date().toISOString(),updatedAt:new Date().toISOString()}],nextCursor:null})).toString('base64')});
  await until(`${activeText}.includes('INVALID_RESPONSE')`);
  assert.ok(!(await evaluate(activeText)).includes('foreign-session'));
  check('Structurally valid foreign-business page is rejected before display');
  await call('Fetch.disable');
  await button('Refresh conversations');
  await until("document.querySelectorAll('[data-active=true] section[aria-label=\"Conversations\"] .record-list button.record').length>=2");
  await call('Fetch.enable',{patterns:[{urlPattern:'*turns',requestStage:'Response'}]});
  historyMode='first';
  await evaluate("document.querySelectorAll('[data-active=true] section[aria-label=\"Conversations\"] .record-list button.record')[0].click()");
  await until(`${activeText}.includes('FIRST_HISTORY')`);
  historyMode='hold';
  await evaluate("document.querySelectorAll('[data-active=true] section[aria-label=\"Conversations\"] .record-list button.record')[1].click()");
  for(let i=0;i<100&&!heldHistory;i++) await pause(50);
  assert.ok(heldHistory);
  assert.ok(!(await evaluate("document.querySelector('[data-active=true] .detail').innerText")).includes('FIRST_HISTORY'));
  await call('Fetch.fulfillRequest',{requestId:heldHistory,responseCode:200,
    responseHeaders:[{name:'Content-Type',value:'application/json'}],
    body:Buffer.from(JSON.stringify({items:[{turnId:'turn-2',messageId:'message-2',sequence:1,inputText:'SECOND_HISTORY',status:'failed',output:null}],nextCursor:null})).toString('base64')});
  await until(`${activeText}.includes('SECOND_HISTORY')`);
  check('Selecting another conversation hides old history while its new response is delayed');
  await call('Fetch.disable');
  await button('Knowledge base'); await button('Add document');
  await evaluate(`(()=>{
    const input=document.querySelector('.composer-overlay input[type=file]');
    const file=new File(['older content'],'old.md');
    file.arrayBuffer=()=>new Promise(resolve=>window.releaseOldFile=()=>resolve(new TextEncoder().encode('older content').buffer));
    const transfer=new DataTransfer();transfer.items.add(file);input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  await evaluate(`(()=>{
    const input=document.querySelector('.composer-overlay input[type=file]');
    const transfer=new DataTransfer();transfer.items.add(new File(['newer content'],'new.md'));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  await until("document.querySelector('.composer-overlay textarea[aria-label=\"Document content\"]').value==='newer content'");
  await evaluate('window.releaseOldFile()');await pause(200);
  assert.equal(await evaluate("document.querySelector('.composer-overlay textarea[aria-label=\"Document content\"]').value"),'newer content');
  check('A delayed earlier file cannot overwrite the newer selected document content');
  await button('Close Add document');
  documentMock=true;
  await call('Fetch.enable',{patterns:[{urlPattern:'*documents*',requestStage:'Response'}]});
  await button('Refresh documents');
  await until("Array.from(document.querySelectorAll('[data-active=true] .record-list button')).some(b=>b.textContent.includes('Refresh fixture'))");
  await evaluate("Array.from(document.querySelectorAll('[data-active=true] .record-list button')).find(b=>b.textContent.includes('Refresh fixture')).click()");
  await until("document.querySelector('.reader-overlay .detail').innerText.includes('ingesting')");
  documentStatus='ready';
  assert.ok(await evaluate("Array.from(document.querySelectorAll('.reader-overlay button')).some(b=>b.textContent.trim()==='Refresh document')"), 'Open reader needs its own accessible refresh/retry control');
  await button('Refresh document');
  await until("document.querySelector('.reader-overlay .detail').innerText.includes('ready')");
  assert.equal(documentReads,2);
  check('Refresh documents reloads selected detail and displays updated ingestion status');
  documentFailure=true; await button('Refresh document');await until("document.querySelector('.reader-overlay').innerText.includes('Controlled document failure')");documentFailure=false;await button('Refresh document');await until("document.querySelector('.reader-overlay .detail').innerText.includes('ready')");check('Open reader shows failed reads and retries successfully without closing');
  await button('Close Document reader');
  await call('Fetch.disable');documentMock=false;
  await evaluate(`(async()=>{for(let i=0;i<55;i++) await fetch('/api/v1/businesses/training-institute/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});})()`);
  await button('Conversations');await button('Refresh conversations');
  await until("!!Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='More conversations'&&!b.disabled)");
  const pageBefore=pageRequests.length;
  await evaluate("(()=>{const b=Array.from(document.querySelectorAll('.section-navigation button,[data-active=true] button,[role=dialog] button')).find(b=>b.textContent==='More conversations');b.click();b.click();})()");
  await until("document.querySelectorAll('[data-active=true] section[aria-label=\"Conversations\"] .record-list button.record').length>20");
  await pause(100);
  assert.equal(pageRequests.length,pageBefore+1);
  const ids=await evaluate("Array.from(document.querySelectorAll('[data-active=true] section[aria-label=\"Conversations\"] .record-list button.record')).map(b=>b.querySelector('small').textContent)");
  assert.equal(new Set(ids).size,ids.length);
  check('Rapid More clicks issue one cursor request and append no duplicate records');
  await call('Fetch.disable');

  const leadFixture = spawnSync(python, ['-'], {cwd:join(root,'backend'),windowsHide:true,encoding:'utf8',input:`import asyncio
from datetime import UTC,datetime
from pymongo import AsyncMongoClient
from app.contracts import LeadInput,AutomationEvent,Summary
from app.persistence_models import LeadRecord,MockNotification
from app.repositories import stored
async def main():
    name=${JSON.stringify(database)}
    assert name.startswith('ai_engagement_sd10_browser_')
    now=datetime.now(UTC)
    summary=Summary(lead_id='audit-lead',customer_name='Audit Customer',summary='Audit fixture summary: data science interest.',intent='Learning enquiry',qualification_status='qualified',recommended_action='Counselor follow-up')
    lead=LeadRecord(lead_id='audit-lead',business_id='training-institute',session_id='audit-lead-session',created_at=now,customer=LeadInput(name='Audit Customer',phone='9876543210',stated_interest='data science'),automation=AutomationEvent(event_id='audit-event',status='completed',attempts=1,summary=summary),automation_input=(),callback_request_id='audit-callback',callback_requested_at=now,notification=MockNotification(notification_id='audit-notification',event_id='audit-event',lead_id='audit-lead',created_at=now,payload=summary))
    async with AsyncMongoClient('mongodb://127.0.0.1:27017',serverSelectionTimeoutMS=2000) as client:
        await client[name].leads.insert_one(stored(lead))
asyncio.run(main())
`});
  assert.equal(leadFixture.status,0,leadFixture.stderr);
  await button('Leads');await until(`${activeText}.includes('Audit Customer')`);
  assert.ok(await evaluate(`${activeText}.includes('Schedule not supplied')`));
  assert.ok(!(await evaluate(activeText)).includes('Any schedule'));
  await evaluate("document.querySelector('[data-active=true] .lead-card summary').click()");
  await until(`${activeText}.includes('Audit fixture summary')`);
  assert.ok(await evaluate(`${activeText}.includes('Requested')&&${activeText}.includes('Stored locally')`));
  check('Real Mongo lead REST projection renders callback, summary and mock notification; absent schedule is not invented');
  await switchBusiness('fitness-studio');await button('Leads');await until(`${activeText}.includes('No leads yet.')`);
  assert.ok(!(await evaluate(activeText)).includes('Audit Customer'));
  check('Lead UI keeps institute fixture data out of the fitness business');
  stop(api); await call('Page.reload');
  await until("document.body.innerText.includes('BACKEND_UNAVAILABLE') && !!document.querySelector('[role=alert]')");
  assert.deepEqual(errors, []);
  report.passed = true; report.javascriptExceptions = errors;
  check('Initial backend outage renders a retryable connection error');
  console.log('PASS: current frontend/backend browser integration, records and failure boundaries.');

} finally {
  socket?.close();
  for (const process of owned.reverse()) stop(process);
  assert.match(database, /^ai_engagement_sd10_browser_\d+$/);
  const cleanup = spawnSync(python, ['-'], { windowsHide: true, encoding: 'utf8', input: `import asyncio
from pymongo import AsyncMongoClient
async def main():
    name=${JSON.stringify(database)}
    assert name.startswith('ai_engagement_sd10_browser_')
    async with AsyncMongoClient('mongodb://127.0.0.1:27017',serverSelectionTimeoutMS=2000) as client:
        await client.drop_database(name)
asyncio.run(main())
` });
  assert.equal(cleanup.status, 0, 'Owned browser test database cleanup failed.');
  report.ownedDatabaseRemoved = true;
  writeFileSync(join(root, 'docs/evidence/DIRECT_MESSAGE-browser.json'), JSON.stringify(report, null, 2)+'\n');
}
