#!/usr/bin/env node
/**
 * Manual, synthetic-only DOM rehearsal of the installed local operator app.
 * Requires Node >=22 and an existing Chrome executable; installs nothing.
 * Launch the app with a NEW private workspace, then run once per sample profile:
 * node scripts/customer360_browser_check.mjs --origin http://127.0.0.1:PORT \
 *   --profile billing-support --output NEW_DIRECTORY --synthetic-fixtures
 * The output parent must exist. Add --scripted-model only with the explicitly
 * injected tests.customer360.scripted_browser_server; never a provider server.
 *
 * This does not establish vendor authentication, model reasoning, hosted access
 * control, clean-install success, or benchmark results. The default path never
 * clicks model controls. The optional scripted path runs the real agent loop
 * with a declared test transport. Both perform actual local sample extraction.
 */
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, readFile, writeFile, chmod, rm, stat, lstat, link, unlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve, join, isAbsolute} from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const bound = (value, n=2000) => String(value ?? '').slice(0, n);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

// Frozen operator questions only: no expected values or implementation filters.
export const CONFIGURED_QUESTIONS = Object.freeze({
  'billing-support':'For this customer, show the remaining amount on open invoices by source currency and quote the latest available support update with its source attribution. Use the reviewed metric and preserve its unit; do not convert currencies.',
  'salesforce-gong':'For this customer, show the open opportunity value by source currency and quote the latest available call evidence with its source attribution. Use the reviewed metric and preserve its unit; do not convert currencies or describe pipeline as recurring revenue.'
});
export const SCRIPTED_BOOTSTRAP_INTENT = 'Prepare a customer review with the approved account metrics and attributed source evidence.';

export function configuredRequestMatches(profile, operation, args, index) {
  const expected=index===0?profile.bootstrap:profile.asks?.[index===1?'initial':'refreshed'];
  return Number.isInteger(index)&&index>=0&&index<3&&expected
    &&operation===(index===0?'context.bootstrap':'ask')&&args.allow_model_data===true
    &&args.max_cost_usd===expected.max_cost_usd&&args.max_turns===expected.max_turns
    &&(index===0?args.intent===expected.intent:args.question===expected.question);
}

export function parseArguments(args) {
  const options = {};
  for (let i=0; i<args.length; i++) {
    const key = args[i];
    if (key === '--synthetic-fixtures') { options.synthetic = true; continue; }
    if (key === '--scripted-model') { options.scriptedModel = true; continue; }
    if (key === '--external-refresh-check') { options.externalRefresh = true; continue; }
    if (key === '--allow-model-data') { options.allowModelData = true; continue; }
    if (!['--origin','--profile','--output','--chrome','--timeout-seconds','--configured-model-plan'].includes(key)
        || options[key.slice(2)] !== undefined || !args[i+1] || args[i+1].startsWith('--')) {
      throw new Error('Use --origin, --profile, --output, --synthetic-fixtures and optional --scripted-model/--external-refresh-check/--chrome/--timeout-seconds.');
    }
    options[key.slice(2)] = args[++i];
  }
  if (!options.synthetic) throw new Error('This browser check requires --synthetic-fixtures and a new synthetic-only app workspace.');
  if (!/^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/.test(options.origin || '')
      || Number(new URL(options.origin).port) > 65535) throw new Error('Use an exact numeric loopback app origin with a valid explicit port.');
  if (!['billing-support','salesforce-gong'].includes(options.profile)) throw new Error('Choose billing-support or salesforce-gong.');
  if (!options.output) throw new Error('Use a new --output directory for retained evidence.');
  if(options['configured-model-plan'] && (options.scriptedModel || options.externalRefresh || !options.allowModelData
     || !isAbsolute(options['configured-model-plan'])))throw new Error('Configured mode requires an absolute plan and --allow-model-data; it cannot use scripted or external-refresh modes.');
  if(options.allowModelData&&!options['configured-model-plan'])throw new Error('--allow-model-data requires --configured-model-plan.');
  const timeout = options['timeout-seconds'] === undefined ? 900 : Number(options['timeout-seconds']);
  if (!Number.isInteger(timeout) || timeout < 30 || timeout > 1800) throw new Error('Timeout must be an integer from 30 through 1800 seconds.');
  return {...options, output:resolve(options.output), timeout,
    chrome:options.chrome || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'};
}

export async function loadConfiguredPlan(options) {
  const path=options['configured-model-plan'];if(!path)return null;
  const info=await lstat(path);
  if(!info.isFile()||info.isSymbolicLink()||info.size>64000||info.mode&0o077
    ||(process.getuid&&info.uid!==process.getuid()))throw new Error('Configured smoke plan must be a bounded owner-only regular file.');
  const bytes=await readFile(path),plan=JSON.parse(bytes.toString('utf8'));
  if(plan.spec!=='oriel.customer360.configured-model-plan/v1'||plan.synthetic!==true||plan.allow_model_data!==true
     ||plan.total_max_cost_usd!==3||typeof plan.run_id!=='string'||!/^[A-Za-z0-9_-]{1,80}$/.test(plan.run_id)
     ||!Array.isArray(plan.profiles)||plan.profiles.length!==2
     ||plan.profiles.map(p=>p.name).join(',')!=='billing-support,salesforce-gong')throw new Error('Require the exact predeclared two-profile, USD 3 configured smoke plan.');
  for(const profile of plan.profiles){
    if(typeof profile.origin!=='string'||!/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(profile.origin)
      ||new URL(profile.origin).origin!==profile.origin||typeof profile.browser_output!=='string'||!isAbsolute(profile.browser_output))throw new Error('Each profile must bind its numeric loopback origin and absolute browser output.');
    for(const job of [profile.bootstrap,profile.asks?.initial,profile.asks?.refreshed])
      if(!job||job.max_cost_usd!==0.5||job.max_turns!==4)throw new Error('Each configured job must retain the frozen USD 0.50 and four-turn limits.');
    if(profile.bootstrap.intent!==CONFIGURED_QUESTIONS[profile.name])throw new Error('Bootstrap intent must equal the frozen operator question.');
    for(const phase of ['initial','refreshed'])
      if(profile.asks[phase].question!==CONFIGURED_QUESTIONS[profile.name])throw new Error('Use the exact frozen operator questions.');
  }
  if(new Set(plan.profiles.map(p=>p.origin)).size!==2||new Set(plan.profiles.map(p=>resolve(p.browser_output))).size!==2)throw new Error('Profiles require separate app origins and proof directories.');
  const profile=plan.profiles.find(p=>p.name===options.profile);
  if(profile.origin!==options.origin||resolve(profile.browser_output)!==options.output)throw new Error('Driver origin/output do not match this profile in the frozen plan.');
  return {plan,profile,sha256:hash(bytes)};
}

class CDP {
  constructor(socket) {
    this.socket=socket; this.sequence=0; this.pending=new Map(); this.listeners=new Map();
    socket.addEventListener('message', event => {
      const message=JSON.parse(String(event.data));
      if (message.id) {
        const item=this.pending.get(message.id); if (!item) return;
        this.pending.delete(message.id);clearTimeout(item.timer);
        if(message.error)item.reject(new Error(bound(message.error.message)));
        else item.resolve(message.result || {});
      } else for(const listener of this.listeners.get(message.method) || []) {
        Promise.resolve().then(()=>listener(message.params || {},message.sessionId)).catch(error=>{this.listenerError=error;});
      }
    });
    socket.addEventListener('close',()=>{for(const item of this.pending.values()){clearTimeout(item.timer);item.reject(new Error('Browser debugging connection closed.'));}this.pending.clear();});
  }
  static async connect(url) {
    const parsed=new URL(url);
    if(parsed.protocol !== 'ws:' || parsed.hostname !== '127.0.0.1')throw new Error('Chrome debugging endpoint must be numeric loopback.');
    const socket=new WebSocket(url);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    return new CDP(socket);
  }
  on(method, listener) {if(!this.listeners.has(method))this.listeners.set(method,[]);this.listeners.get(method).push(listener);}
  send(method, params={}, sessionId) {
    if(this.listenerError)return Promise.reject(this.listenerError);
    const id=++this.sequence;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('Chrome command timed out: '+method));},15000);
      this.pending.set(id,{resolve,reject,timer});
      this.socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
    });
  }
  close() {this.socket.close();}
}

async function privateWrite(path, value) {
  await writeFile(path, value, {mode:0o600, flag:'wx'});
}

async function publishJSON(path,value){
  const temporary=path+'.'+randomUUID()+'.tmp';
  await privateWrite(temporary,JSON.stringify(value,null,2)+'\n');
  try{await link(temporary,path);}finally{await unlink(temporary);}
}

export async function runBrowserCheck(options) {
  if(Number(process.versions.node.split('.')[0])<22 || typeof WebSocket !== 'function')throw new Error('Use Node 22 or newer with its native WebSocket.');
  if(!(await stat(options.chrome)).isFile())throw new Error('An existing Chrome executable is required.');
  const configured=await loadConfiguredPlan(options),useAgent=Boolean(configured||options.scriptedModel);
  const bootstrapIntent=configured?configured.profile.bootstrap.intent:SCRIPTED_BOOTSTRAP_INTENT;
  await mkdir(options.output,{mode:0o700}); // Exclusive; never overwrite prior evidence.
  const started=new Date().toISOString(), deadline=Date.now()+options.timeout*1000;
  const receipt={spec:configured?'oriel.customer360.configured-browser-smoke/v1':'oriel.customer360.browser-rehearsal/v1',status:'running',started_at:started,
    profile:options.profile,origin:options.origin,synthetic:true,interaction:'chrome_cdp_actual_dom_forms_and_clicks',
    actual_model_calls:0,reported_model_cost_usd:0,configured_model_enabled:Boolean(configured),
    configured_plan_sha256:configured?.sha256||null,scripted_model_enabled:Boolean(options.scriptedModel),external_refresh_check:Boolean(options.externalRefresh),model_reasoning_verified:false,vendor_adapter_verified:false,clean_install_verified:false,
    hosted_authorization_verified:false,claim_eligible:false,checkpoints:[],operations:[],network_errors:[],
    console_errors:[],blocked_requests:[],screenshots:[],job_receipts:[],implementation:{node:process.version,assets:{},
      driver_sha256:hash(await readFile(fileURLToPath(import.meta.url)))}};
  const profile=await mkdtemp(join(tmpdir(),'oriel-browser-check-'));await chmod(profile,0o700);
  let chrome,cdp,session,stderr='',exited=false;
  const responses=new Map(),completedJobs=new Set(),inflight=new Set(),scopeTexts=new Map();
  let holdMessagePlans=false;
  const heldMessagePlans=[];
  let metricName=options.profile==='billing-support'?'outstanding_invoices':'open_pipeline';
  const push=(key,value)=>{if(receipt[key].length<100)receipt[key].push(value);else receipt[key+'_truncated']=true;};
  const safeURL=url=>{try{const u=new URL(url);return u.origin===options.origin?u.pathname:bound(u.protocol+'//'+u.hostname,200);}catch{return 'invalid_url';}};
  const evaluate=async expression=>{
    if(Date.now()>deadline)throw new Error('Overall browser rehearsal deadline exceeded.');
    const value=await cdp.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);
    if(value.exceptionDetails)throw new Error('DOM evaluation failed: '+bound(value.exceptionDetails.exception?.description||value.exceptionDetails.text));
    return value.result?.value;
  };
  const visible=selector=>`(()=>{const e=document.querySelector(${JSON.stringify(selector)});return !!e&&!e.closest('[hidden]')&&e.getClientRects().length>0;})()`;
  const wait=async(expression,label,ms=120000)=>{
    const until=Math.min(deadline,Date.now()+ms);let last;
    while(Date.now()<until){last=await evaluate(expression);if(last)return last;await sleep(100);}
    throw new Error('Timed out waiting for '+label+'.');
  };
  const click=async selector=>{
    await wait(visible(selector),'visible '+selector,20000);
    const point=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(e.disabled)throw new Error('Control is disabled');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1},session);
    await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1},session);
  };
  const fill=async(selector,value)=>{
    await wait(visible(selector),'visible '+selector,20000);
    await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  };
  const textOf=selector=>evaluate(`document.querySelector(${JSON.stringify(selector)})?.innerText||''`);
  const enterReviewedMapping=async()=>{
    // Copy only the authored mapping visibly offered by the product. The
    // driver has no expected answers and never sends an API mutation itself.
    const mapping=await evaluate(`JSON.parse(document.querySelector('#sample-mapping pre').textContent)`);
    await click('#mapping-builder > summary');
    await fill('#mapping-name',mapping.name);await fill('#mapping-description',mapping.description);
    for(let index=0;index<mapping.bindings.length;index++){
      if(index)await click('#add-binding');
      const binding=mapping.bindings[index],base='#binding-list > .binding:nth-child('+(index+1)+')';
      for(const [attribute,value]of Object.entries({table:binding.table,entity:binding.entity,kind:binding.kind,
        'record-id':binding.record_id,'ref-connection':binding.customer_ref.connection,
        'ref-entity':binding.customer_ref.entity,'ref-column':binding.customer_ref.column})){
        await fill(base+' [data-binding-'+attribute+']',value);
      }
      let roleIndex=0;
      for(const [role,column]of Object.entries(binding.fields)){
        if(roleIndex)await click(base+' [data-action="add-role"]');
        await fill(base+' .role-row:nth-child('+(roleIndex+1)+') [data-role]',role);
        await fill(base+' .role-row:nth-child('+(roleIndex+1)+') [data-column]',column);roleIndex++;
      }
      for(const [role,policy]of Object.entries(binding.measures||{})){
        await click(base+' [data-action="add-measure"]');
        const measure=base+' [data-measure-row]:last-child';
        const defaults=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(measure)});return ['role','unit','currency','aggregation','meaning'].map(k=>e.querySelector('[data-measure-'+k+']').value);})()`);
        if(JSON.stringify(defaults)!=='["","unknown","","unknown",""]')throw new Error('Measure controls must not infer policy from the selected numeric column.');
        await fill(measure+' [data-measure-role]',role);
        await fill(measure+' [data-measure-unit]',policy.unit);
        if(policy.currency_field)await fill(measure+' [data-measure-currency]',policy.currency_field);
        await fill(measure+' [data-measure-aggregation]',policy.aggregation);
        await fill(measure+' [data-measure-meaning]',policy.meaning);
      }
      const temporal=Object.fromEntries(['attachments','passages'].filter(key=>binding[key]).map(key=>[key,binding[key]]));
      if(Object.keys(temporal).length){
        await click(base+' details > summary');
        await fill(base+' [data-binding-temporal]',JSON.stringify(temporal));
      }
    }
    await runJob('#mapping-form button[type="submit"]',`document.querySelectorAll('[data-action="approve-profile"]').length>0`);
    const expected=options.profile==='billing-support'?'Currency — minor units':'Currency — major units';
    const proposed=await textOf('#mapping-proposals [data-measure-policy-role="amount"]');
    if(!proposed.includes(expected)||!proposed.includes('Sum permitted')||!proposed.includes('Currency role: currency'))throw new Error('Mapping proposal must expose the actual submitted measure policy before approval.');
    await checkpoint('measure-policy-entered-and-proposed',['#mapping-proposals']);
    await screenshot('measure-policy-proposal','#mapping-proposals');
    await click('#mapping-builder > summary');
  };
  const checkpoint=async(name,selectors=[])=>{
    const observed={};for(const selector of selectors)observed[selector]=bound(await textOf(selector),16000);
    const item={name,at:new Date().toISOString(),observed};push('checkpoints',item);
    await privateWrite(join(options.output,String(receipt.checkpoints.length).padStart(2,'0')+'-'+name+'.json'),JSON.stringify(item,null,2)+'\n');
  };
  const screenshot=async(name,selector)=>{
    if(selector)await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start'})`);
    const capture=await cdp.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);
    const bytes=Buffer.from(capture.data,'base64'),file=name+'.png';await privateWrite(join(options.output,file),bytes);
    push('screenshots',{file,sha256:hash(bytes)});
  };
  const runJob=async(selector,condition='true',{failure=false,ms=180000}={})=>{
    const before=receipt.operations.length;await click(selector);
    const until=Math.min(deadline,Date.now()+20000);
    while(receipt.operations.length===before&&Date.now()<until)await sleep(50);
    if(receipt.operations.length===before)throw new Error('DOM action did not submit a local job: '+selector+'; '+bound(await textOf('#global-error')));
    // A failed/unknown terminal job cannot satisfy its success-only DOM
    // predicate. Observe failure independently so even a paid-job refusal
    // stops promptly; never wait out that operation's model timeout first.
    await wait(`document.querySelector('#job-status')?.classList.contains('terminal') &&
      (document.querySelector('#job-status').classList.contains('failed') ||
       !document.querySelector('#global-error').hidden || (${condition}))`,'terminal job for '+selector,ms);
    const settled=Math.min(deadline,Date.now()+15000);
    while(inflight.size && Date.now()<settled)await sleep(50);
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const failed=await evaluate(`document.querySelector('#job-status').classList.contains('failed') || !document.querySelector('#global-error').hidden`);
    if(failed!==failure)throw new Error((failure?'Expected a visible refused job':'Unexpected job refusal')+': '+bound((await textOf('#global-error'))||(await textOf('#job-detail'))));
  };
  const approve=async kind=>{
    for(let n=0;n<10;n++) {
      const proposal=await evaluate(`document.querySelector('[data-action="approve-${kind}"]')?.closest('[data-proposal-id]')?.dataset.proposalId||null`);
      if(!proposal)return;
      const base='[data-proposal-id='+JSON.stringify(proposal)+']';
      await click(base+' input[data-confirm-proposal]');
      await runJob(base+' [data-action="approve-'+kind+'"]',`!document.querySelector(${JSON.stringify(base+' [data-action="approve-'+kind+'"]')})`);
    }
    throw new Error('Too many pending approvals for the bounded synthetic rehearsal.');
  };
  const semanticReview=async(kind,phase)=>{
    const operation=kind==='context_review'?'context.bootstrap':'ask';
    const job=receipt.job_receipts.findLast(item=>item.operation===operation),agent=job?.agent;
    if(!job||job.status!=='succeeded'||!agent||agent.model_origin!=='configured_claude_cli'
      ||agent.evidence_origin!=='model_agent'||agent.receipt_persisted!==true||agent.cost_complete!==true||agent.spend_unknown!==false
      ||!Number.isInteger(agent.model_calls)||agent.model_calls<1||agent.model_calls>4
      ||typeof agent.reported_cost_usd!=='number'||!Number.isFinite(agent.reported_cost_usd)||agent.reported_cost_usd<0||agent.reported_cost_usd>0.5
      ||!Array.isArray(agent.model_ids)||!agent.model_ids.length)throw new Error('Configured model job lacks complete real-adapter usage or exceeds its frozen job limit.');
    const request={kind,request_id:randomUUID(),run_id:configured.plan.run_id,profile:options.profile,phase,job_id:job.job_id};
    if(kind==='context_review'){
      if(agent.status!=='candidate'||!agent.candidate?.proposal_id||!agent.candidate?.approval_sha256)throw new Error('Configured bootstrap must return an exact unapproved candidate.');
      Object.assign(request,agent.candidate);
    }else{
      if(!['answered','abstained'].includes(agent.status)||!agent.answer_id)throw new Error('Configured ask must retain a scoped answer receipt.');
      request.answer_id=agent.answer_id;
    }
    await publishJSON(join(options.output,'model-review-request-'+request.request_id+'.json'),request);
    const responsePath=join(options.output,'model-review-response-'+request.request_id+'.json');let response;
    const until=Math.min(deadline,Date.now()+600000);
    while(Date.now()<until){
      try{
        const info=await lstat(responsePath);
        if(!info.isFile()||info.isSymbolicLink()||info.size>16000||info.mode&0o077
          ||(process.getuid&&info.uid!==process.getuid()))throw new Error('Independent model review must be a bounded private file.');
        response=JSON.parse(await readFile(responsePath,'utf8'));break;
      }catch(error){if(error.code!=='ENOENT')throw error;}
      await sleep(100);
    }
    if(!response||response.approved!==true||Object.entries(request).some(([key,value])=>response[key]!==value))throw new Error('Independent semantic review did not approve the exact configured-model artifact; no later paid action was started.');
    if(kind==='context_review'){
      if(typeof response.metric_name!=='string'||!/^[a-z][a-z0-9_]{0,63}$/.test(response.metric_name))throw new Error('Semantic review must bind the approved primary metric name.');
      metricName=response.metric_name;
    }else if(response.metric_name!==undefined&&response.metric_name!==metricName)throw new Error('Answer review changed the approved metric identity.');
    receipt.semantic_reviews??=[];
    receipt.semantic_reviews.push({...request,approved:true,metric_name:metricName,response_sha256:hash(await readFile(responsePath))});
    return response;
  };
  const approveReviewedContext=async review=>{
    const base='[data-proposal-id='+JSON.stringify(review.proposal_id)+']';
    const displayed=await textOf(base+' .proposal-hash');
    if(displayed!=='Approval SHA-256: '+review.approval_sha256)throw new Error('Displayed proposal does not match the independently reviewed candidate hash.');
    await click(base+' input[data-confirm-proposal]');
    await runJob(base+' [data-action="approve-context"]',`!document.querySelector(${JSON.stringify(base+' [data-action="approve-context"]')})`);
  };
  const setPaidOptions=async phase=>{
    const settings=phase==='bootstrap'?configured.profile.bootstrap:configured.profile.asks[phase];
    await click('[data-view="review"]');await click('#tab-context');
    if(!await evaluate(`document.querySelector('#model-consent').checked`))await click('#model-consent');
    await fill('#model-budget',String(settings.max_cost_usd));await fill('#model-turns',String(settings.max_turns));
  };
  const verifyReviewIntent=async selector=>{
    const values=await evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector+' [data-bootstrap-intent-review]')})).map(e=>e.textContent)`);
    if(!values.length||values.some(value=>value!==bootstrapIntent)
       ||!(await textOf(selector+' [data-bootstrap-intent]')).includes('Retained operator task request — not reviewed policy'))
      throw new Error('The reviewed proposal must retain the exact entered operator intent with its limited authority.');
    receipt.bootstrap_intent=bootstrapIntent;
  };
  const verifyScopePresentation=async selector=>{
    const rows=await evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector+' [data-definition-name]')})).filter(e=>e.classList.contains('metric')||e.classList.contains('definition-row')).map(e=>{
      const scope=e.querySelector('[data-calculation-scope]'),note=e.querySelector('[data-definition-note]');
      return {name:e.dataset.definitionName,scope:scope?.textContent,note:note?.textContent,
        scopeBeforeNote:!!(scope&&note&&(scope.compareDocumentPosition(note)&Node.DOCUMENT_POSITION_FOLLOWING))};})`);
    if(!rows.length||rows.some(row=>row.scope!==scopeTexts.get(row.name)||!row.scopeBeforeNote
       ||!row.note?.startsWith('Provisional description / reviewer note — not calculation scope')))
      throw new Error('Every reviewed definition and metric must lead with exact server-derived calculation scope, separately from provisional prose.');
  };
  const verifyMetric=async phase=>{
    await verifyScopePresentation('#answer-panel');
    // Independently authored fixture expectations, not copied from a result.
    const billing=options.profile==='billing-support',name=metricName;
    const expected=billing?(phase==='initial'?{EUR:'30000',USD:'125000'}:{EUR:'30000',USD:'10000'}):
      (phase==='initial'?{EUR:'3000',USD:'12500'}:{EUR:'3000',USD:'9000'});
    const values=await evaluate(`Array.from(document.querySelectorAll('#answer-panel [data-definition-name="${name}"] .metric-value')).map(e=>({currency:e.dataset.currency,value:e.dataset.exactValue}))`);
    if(values.length!==Object.keys(expected).length||values.some(v=>expected[v.currency]!==v.value)||new Set(values.map(v=>v.currency)).size!==values.length)
      throw new Error('Rendered '+phase+' currency-separated metric does not match the independent synthetic expectation.');
    return values;
  };
  try {
    chrome=spawn(options.chrome,['--headless=new','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
      '--user-data-dir='+profile,'--no-first-run','--no-default-browser-check','--disable-background-networking',
      '--disable-component-update','--disable-sync','--disable-default-apps','--disable-domain-reliability',
      '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1','--password-store=basic','--window-size=1440,1100','about:blank'],
      {stdio:['ignore','ignore','pipe'],detached:process.platform!=='win32'});
    chrome.on('error',error=>{stderr+=bound(error.message);});chrome.on('exit',()=>{exited=true;});
    chrome.stderr.on('data',chunk=>{if(stderr.length<10000)stderr+=String(chunk).slice(0,10000-stderr.length);});
    let port;
    for(let n=0;n<150;n++) {
      try {const lines=(await readFile(join(profile,'DevToolsActivePort'),'utf8')).trim().split('\n');if(/^\d+$/.test(lines[0])){port=Number(lines[0]);break;}}catch{}
      if(exited)throw new Error('Isolated Chrome exited before its debugging endpoint was ready.');await sleep(100);
    }
    if(!port)throw new Error('Isolated Chrome did not expose a local debugging port.');
    const version=await (await fetch('http://127.0.0.1:'+port+'/json/version')).json();
    receipt.implementation.browser=version.Browser;cdp=await CDP.connect(version.webSocketDebuggerUrl);
    const target=await cdp.send('Target.createTarget',{url:'about:blank'});
    session=(await cdp.send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
    cdp.on('Runtime.exceptionThrown',(params,id)=>{if(id===session)push('console_errors',{kind:'exception',message:bound(params.exceptionDetails?.exception?.description||params.exceptionDetails?.text)});});
    cdp.on('Runtime.consoleAPICalled',(params,id)=>{if(id===session&&params.type==='error')push('console_errors',{kind:'console',message:bound((params.args||[]).map(x=>x.value??x.description??'').join(' '))});});
    cdp.on('Network.requestWillBeSent',(params,id)=>{if(id===session)inflight.add(params.requestId);});
    cdp.on('Network.responseReceived',(params,id)=>{
      if(id!==session)return;
      const path=safeURL(params.response.url);
      if(params.response.status>=400)push('network_errors',{path,status:params.response.status});
      if(params.response.status===200&&(path==='/'||path==='/app.js'||path==='/style.css'||/^\/api\/jobs\/[0-9a-f-]{36}$/.test(path)))responses.set(params.requestId,path);
    });
    cdp.on('Network.loadingFailed',(params,id)=>{if(id===session)inflight.delete(params.requestId);});
    cdp.on('Network.loadingFinished',async(params,id)=>{
      if(id!==session)return;
      try {
        const path=responses.get(params.requestId);responses.delete(params.requestId);if(!path)return;
        const response=await cdp.send('Network.getResponseBody',{requestId:params.requestId},session);
        const bytes=Buffer.from(response.body,response.base64Encoded?'base64':'utf8');
        if(bytes.length>8_000_000)throw new Error('Local response exceeds the browser evidence bound.');
        if(!path.startsWith('/api/')){receipt.implementation.assets[path]=hash(bytes);return;}
        const job=JSON.parse(bytes.toString('utf8'));
        if(!['succeeded','failed'].includes(job.status)||completedJobs.has(job.job_id))return;
        completedJobs.add(job.job_id);
        // Passive observations of responses caused by DOM actions. Never inject
        // answers or issue API mutations outside the application's own forms.
        const result=job.result||{},answer=result.answer||{};
        for(const entry of job.presentation?.calculation_scopes||[]){
          let definition=job;
          for(const key of entry.path||[])definition=definition?.[key];
          if(typeof definition?.name==='string'&&entry.scope?.spec_version==='oriel.customer360.calculation-scope/v1'
             &&typeof entry.scope.text==='string')scopeTexts.set(definition.name,entry.scope.text);
        }
        push('job_receipts',{job_id:job.job_id,operation:job.operation,status:job.status,response_sha256:hash(bytes),
          domain_ids:Object.fromEntries(['snapshot_id','proposal_id','approval_sha256','context_id','answer_id','review_id','comparison_id']
            .filter(key=>typeof result[key]==='string').map(key=>[key,result[key]])),
          agent:typeof result.model_origin==='string'?{status:result.status,model_origin:result.model_origin,evidence_origin:result.evidence_origin,
            receipt_persisted:result.receipt_persisted,model_calls:result.usage?.model_calls,
            reported_cost_usd:result.usage?.reported_cost_usd,cost_complete:result.usage?.cost_complete,
            spend_unknown:result.usage?.spend_unknown,model_ids:result.usage?.model_ids,
            candidate:result.candidate?{proposal_id:result.candidate.proposal_id,approval_sha256:result.candidate.approval_sha256}:null,
            answer_id:answer.answer_id||null}:null});
        if(configured){
          const agents=receipt.job_receipts.filter(item=>item.agent).map(item=>item.agent);
          receipt.actual_model_calls=agents.every(item=>Number.isInteger(item.model_calls))?agents.reduce((total,item)=>total+item.model_calls,0):null;
          receipt.reported_model_cost_usd=agents.every(item=>item.cost_complete===true&&typeof item.reported_cost_usd==='number'&&Number.isFinite(item.reported_cost_usd))?
            Number(agents.reduce((total,item)=>total+item.reported_cost_usd,0).toFixed(9)):null;
        }
      } finally {inflight.delete(params.requestId);}
    });
    cdp.on('Fetch.requestPaused',async(params,id)=>{
      if(id!==session)return;
      let permitted=false,operation;
      try {
        const url=new URL(params.request.url);permitted=url.origin===options.origin;
        if(permitted&&params.request.method==='POST') {
          const body=JSON.parse(params.request.postData||'{}');operation=body.operation;
          permitted=url.pathname==='/api/jobs'&&['source.add','sample.connect','sample.prepare','links.approve','source.inspect',
            'profile.propose','profile.approve','materialize','context.propose','context.approve','context.show','brief',
            'evidence','review.save','review.list','review.load','review.compare',
            ...(useAgent?['context.bootstrap','ask']:[])].includes(operation);
          if(permitted&&configured&&['context.bootstrap','ask'].includes(operation)){
            const paid=receipt.operations.filter(item=>['context.bootstrap','ask'].includes(item.operation)).length;
            permitted=configuredRequestMatches(configured.profile,operation,body.arguments||{},paid);
          }
          if(permitted&&operation==='context.bootstrap')permitted=body.arguments?.intent===bootstrapIntent;
          if(permitted)push('operations',{operation,at:new Date().toISOString(),
            ...(operation==='context.bootstrap'?{intent:body.arguments.intent}:{})});
        }
      }catch{}
      if(permitted&&holdMessagePlans&&params.request.method==='GET'){
        const url=new URL(params.request.url);
        if(url.pathname==='/api/source-plan'&&url.searchParams.get('type')==='discord'
           &&JSON.stringify(url.searchParams.getAll('resource'))==='["messages"]'){
          heldMessagePlans.push(params.requestId);return;
        }
      }
      if(!permitted){push('blocked_requests',{path:safeURL(params.request.url),method:params.request.method,operation:operation||null});await cdp.send('Fetch.failRequest',{requestId:params.requestId,errorReason:'BlockedByClient'},session);}
      else await cdp.send('Fetch.continueRequest',{requestId:params.requestId},session);
    });
    await cdp.send('Runtime.enable',{},session);await cdp.send('Page.enable',{},session);await cdp.send('Network.enable',{},session);
    await cdp.send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]},session);
    await cdp.send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false},session);
    await cdp.send('Page.navigate',{url:options.origin+'/'},session);
    await wait(`document.querySelector('#session-status')?.textContent==='Ready for your first source'`,'fresh application session',30000);
    await checkpoint('fresh-workspace',['#session-status','#sources-list']);
    if(await evaluate(`document.querySelector('#model-consent').checked`))throw new Error('Model consent must begin disabled.');
    if(options.scriptedModel && (await textOf('#model-origin')) !== 'Scripted test transport · no actual model')throw new Error('--scripted-model requires the visibly declared scripted test transport; refusing configured provider mode.');
    if(configured&&(await textOf('#model-origin'))!=='Configured Claude CLI · selected data leaves this workspace')throw new Error('Configured smoke requires the real default adapter, not an injected client.');

    // A refused ordinary source form demonstrates visible recovery before any
    // sample preparation. No credentials, vendor endpoints or files are read.
    await click('#toggle-connect');await fill('#source-type','discord');
    await wait(`document.querySelector('#resource-plan pre') && document.querySelector('#resource-options [data-resource="messages"]')`,'declared connector selection plan',15000);
    await evaluate(`(()=>{const choices=[...document.querySelectorAll('#resource-options [data-resource]')];for(const item of choices)item.checked=item.value==='messages';choices[0].dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
    await wait(`(()=>{const e=document.querySelector('#resource-plan pre');if(!e)return false;const p=JSON.parse(e.textContent);return JSON.stringify(p.selected_outputs)==='["messages"]';})()`,'messages-only selection plan',15000);
    const fetchPlan=await evaluate(`JSON.parse(document.querySelector('#resource-plan pre').textContent)`);
    if(fetchPlan.status!=='declaration_derived'||JSON.stringify(fetchPlan.required_fetch_resources)!=='["channels","guilds","messages"]'
       ||!(await textOf('#resource-plan')).includes('Also fetched to locate selected records: channels, guilds.'))throw new Error('Resource preview must expose unselected parent reads.');
    await checkpoint('declared-parent-fetch-preview',['#resource-plan']);
    await fill('#source-name','browser_empty_selection');
    await evaluate(`(()=>{for(const e of document.querySelectorAll('#credential-fields input'))e.value='ORIEL_SYNTHETIC_UNUSED_REFERENCE';const choices=[...document.querySelectorAll('#resource-options [data-resource]')];for(const item of choices)item.checked=item.value==='roles';choices[0].dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
    const rolesPlanReady=`(()=>{const e=document.querySelector('#resource-plan pre');return !!e&&JSON.stringify(JSON.parse(e.textContent).selected_outputs)==='["roles"]';})()`;
    await wait(rolesPlanReady,'roles-only plan before delayed selection',15000);
    const beforeChangedSelection=receipt.operations.length;
    holdMessagePlans=true;
    try{
      await evaluate(`(()=>{const choices=[...document.querySelectorAll('#resource-options [data-resource]')];for(const item of choices)item.checked=item.value==='messages';choices[0].dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
      const heldUntil=Math.min(deadline,Date.now()+15000);
      while(!heldMessagePlans.length&&Date.now()<heldUntil)await sleep(50);
      if(!heldMessagePlans.length)throw new Error('Expected a real source-plan request to be paused.');
      await click('#add-source');
      while(heldMessagePlans.length<2&&Date.now()<heldUntil)await sleep(50);
      if(heldMessagePlans.length!==2)throw new Error('Save must await its pending dependency preview.');
      await evaluate(`(()=>{const choices=[...document.querySelectorAll('#resource-options [data-resource]')];for(const item of choices)item.checked=item.value==='roles';choices[0].dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
      await wait(rolesPlanReady,'newer selection while original submit awaits',15000);
    }finally{
      holdMessagePlans=false;
      for(const requestId of heldMessagePlans.splice(0))await cdp.send('Fetch.continueRequest',{requestId},session);
    }
    await wait(`!document.querySelector('#global-error').hidden&&document.querySelector('#global-error').textContent.includes('selection changed')`,'changed selection refusal',10000);
    if(receipt.operations.length!==beforeChangedSelection||!await evaluate(rolesPlanReady))throw new Error('A stale dependency check must not submit or overwrite the new selection.');
    await checkpoint('changed-selection-refused',['#global-error','#resource-plan']);
    await evaluate(`(()=>{for(const e of document.querySelectorAll('#credential-fields input'))e.value='ORIEL_SYNTHETIC_UNUSED_REFERENCE';const choices=[...document.querySelectorAll('#resource-options [data-resource]')];for(const item of choices)item.checked=false;choices[0].dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
    const beforeEmptySelection=receipt.operations.length;
    await click('#add-source');
    await wait(`!document.querySelector('#global-error').hidden && document.querySelector('#global-error').textContent.includes('Select at least one resource')`,'empty selection refusal',10000);
    if(receipt.operations.length!==beforeEmptySelection)throw new Error('Empty resource selection must not submit a default-resource connection.');
    await checkpoint('empty-resource-selection-refused',['#global-error','#resource-plan']);
    await fill('#source-type','csv');
    await wait(visible('#source-path'),'CSV file configuration',15000);
    await fill('#source-name','browser_missing');await fill('#source-path','/oriel-synthetic-no-such-directory/missing.csv');
    // Actual local failure with an impossible success predicate exercises the
    // same unexpected-failure path as a refused model job, without inference.
    const failureStarted=Date.now();let terminalFailure;
    try{await runJob('#add-source','false',{ms:10000});}
    catch(error){terminalFailure=error;}
    if(!terminalFailure?.message.startsWith('Unexpected job refusal:'))
      throw new Error('A terminal refused job must fail before waiting for its impossible success DOM predicate.');
    const refusedJob=receipt.job_receipts.findLast(item=>item.operation==='source.add');
    if(refusedJob?.status!=='failed')throw new Error('Terminal-failure regression requires a retained actual failed source job.');
    receipt.terminal_failure_check={operation:'source.add',job_id:refusedJob.job_id,
      impossible_success_predicate:true,observed_refusal_before_success_wait:true,
      elapsed_seconds:(Date.now()-failureStarted)/1000,model_calls:0};
    await checkpoint('visible-connection-refusal',['#global-error','#job-title','#sources-list']);
    await click('#close-connect');

    await click('input[name="sample-profile"][value="'+options.profile+'"]');
    await runJob('#connect-sample',`document.querySelector('#sample-status').textContent.includes('Initial')`);
    await click('[data-view="review"]');
    await runJob('#prepare-sample',`document.querySelectorAll('[data-action="approve-links"]').length>0`);
    await fill('#reviewer',configured?'Automated independent synthetic semantic review':'Synthetic Browser Reviewer');
    if(await evaluate(`document.querySelectorAll('#identity-proposals .conflict').length`)===0)throw new Error('Expected ambiguous synthetic identities to remain visibly unresolved.');
    await approve('links');await checkpoint('identity-reviewed-conflicts-retained',['#identity-proposals']);
    await click('#tab-mapping');await runJob('#inspect-sources',`document.querySelector('#inspection-summary').textContent.length>0`);
    await enterReviewedMapping();
    await approve('profile');await runJob('#materialize',`!document.querySelector('#materialization-status').hidden`);
    await checkpoint('mapping-inspected-approved-materialized',['#inspection-summary','#materialization-status']);
    await click('#tab-context');
    if(useAgent)await fill('#bootstrap-intent',bootstrapIntent);
    if(configured){
      await setPaidOptions('bootstrap');
      await runJob('#bootstrap-context',`document.querySelectorAll('[data-action="approve-context"]').length>0`,{ms:600000});
      await checkpoint('configured-context-candidate-unapproved',['#context-proposals','#job-detail']);
      await verifyReviewIntent('#context-proposals');await verifyScopePresentation('#context-proposals');
      const reviewed=await semanticReview('context_review','initial');
      await approveReviewedContext(reviewed);
    }else if(options.scriptedModel){
      await click('#model-consent');await fill('#model-budget','0.25');await fill('#model-turns','6');
      await runJob('#bootstrap-context',`document.querySelectorAll('[data-action="approve-context"]').length>0`);
      await checkpoint('scripted-context-candidate-unapproved',['#context-proposals','#job-detail']);
    }else await runJob('#sample-context',`document.querySelectorAll('[data-action="approve-context"]').length>0`);
    if(!configured){if(useAgent)await verifyReviewIntent('#context-proposals');await verifyScopePresentation('#context-proposals');await approve('context');}
    if(useAgent)await verifyReviewIntent('#approved-context');
    await verifyScopePresentation('#approved-context');
    await checkpoint(configured?'configured-context-reviewed':options.scriptedModel?'scripted-context-reviewed':'authored-context-reviewed',['#approved-context','#model-origin']);
    await click('[data-view="customers"]');
    const customerSelector=await evaluate(`(()=>{const e=[...document.querySelectorAll('[data-customer-id]')].find(x=>x.textContent.includes('customer_a'));return e?'[data-customer-id="'+e.dataset.customerId+'"]':null;})()`);
    if(!customerSelector)throw new Error('Synthetic customer_a must be selectable in the actual directory.');
    await click(customerSelector);await runJob('#load-brief',`document.querySelectorAll('#answer-panel [data-evidence-id]').length>0`);
    await verifyMetric('initial');
    const initialSnapshot=await textOf('#snapshot-label');
    await checkpoint('initial-brief-with-gaps',['#answer-panel']);await screenshot('initial-brief','#answer-panel');
    if(useAgent){
      if(configured){await setPaidOptions('initial');await click('[data-view="customers"]');await click(customerSelector);}
      await fill('#question',configured?configured.profile.asks.initial.question:'Summarize the reviewed account metric and show attributed source evidence.');
      await runJob('#ask-form button[type="submit"]',`document.querySelector('#answer-panel').textContent.includes('Model origin:')`,{ms:configured?600000:180000});
      if(configured)await semanticReview('answer_review','initial');
      await verifyMetric('initial');
      await checkpoint(configured?'initial-configured-agent-answer':'initial-scripted-agent-answer',['#answer-panel']);await screenshot(configured?'initial-configured-answer':'initial-scripted-answer','#answer-panel');
    }
    await runJob('#answer-panel [data-evidence-id]',`document.querySelector('#evidence-dialog').open`);
    if(!(await textOf('#evidence-content')).trim())throw new Error('Evidence dialog must contain attributed source evidence.');
    await checkpoint('source-quote-opened',['#evidence-content']);await screenshot('source-quote');await click('#close-evidence');
    await runJob('#save-review',`document.querySelectorAll('[data-action="compare-review"]').length>0`);
    const reviewSelector=await evaluate(`(()=>{const e=document.querySelector('[data-action="compare-review"]');return '[data-action="compare-review"][data-review-id="'+e.dataset.reviewId+'"]';})()`);
    await checkpoint('brief-saved',['#saved-reviews']);

    await runJob('#refresh-sample',`document.querySelector('#sample-status').textContent.includes('Refreshed')`);
    await click('[data-view="review"]');
    await runJob('#prepare-sample',`document.querySelector('#snapshot-label').textContent.length>20`);
    await approve('links');await click('#tab-mapping');
    await runJob('#materialize',`!document.querySelector('#materialization-status').hidden`);
    await click('#tab-context');await runJob('#show-context',`document.querySelector('#approved-context').textContent.includes('Approved business context')`);
    await click('[data-view="customers"]');await click(customerSelector);
    await runJob('#load-brief',`document.querySelectorAll('#answer-panel [data-evidence-id]').length>0`);
    await verifyMetric('refreshed');
    const retainedNotes=await textOf('#answer-panel [data-gap-origin="approved_context"]');
    if(!retainedNotes.includes(initialSnapshot)||!retainedNotes.includes('not rechecked after refresh')
       ||initialSnapshot===await textOf('#snapshot-label'))throw new Error('Retained review notes must identify their original approval snapshot after refresh.');
    await checkpoint('refreshed-brief',['#answer-panel']);await screenshot('refreshed-brief','#answer-panel');
    if(useAgent){
      if(configured){await setPaidOptions('refreshed');await click('[data-view="customers"]');await click(customerSelector);}
      await fill('#question',configured?configured.profile.asks.refreshed.question:'Summarize the refreshed reviewed account metric and show current source evidence.');
      await runJob('#ask-form button[type="submit"]',`document.querySelector('#answer-panel').textContent.includes('Model origin:')`,{ms:configured?600000:180000});
      if(configured)await semanticReview('answer_review','refreshed');
      await verifyMetric('refreshed');
      await checkpoint(configured?'refreshed-configured-agent-answer':'refreshed-scripted-agent-answer',['#answer-panel']);await screenshot(configured?'refreshed-configured-answer':'refreshed-scripted-answer','#answer-panel');
    }
    await runJob('#list-reviews',`document.querySelectorAll('[data-action="compare-review"]').length>0`);
    await runJob(reviewSelector,`document.querySelector('#comparison-panel').textContent.length>20`);
    const definition=metricName;
    const comparison=await evaluate(`Array.from(document.querySelectorAll('#comparison-panel tr[data-definition-name="${definition}"][data-currency="USD"] td')).map(x=>x.textContent)`);
    const expectedBefore=options.profile==='billing-support'?'125000':'12500',expectedAfter=options.profile==='billing-support'?'10000':'9000';
    if(comparison.length!==4||comparison[1]!==expectedBefore||comparison[2]!==expectedAfter||comparison[3]!=='Not comparable'
       ||!(await textOf('#comparison-panel')).includes('identity_sha256'))throw new Error('Comparison must show exact before/current values and withhold a delta after reviewed identity changes.');
    await checkpoint('saved-current-comparison',['#comparison-panel']);await screenshot('comparison','#comparison-panel');
    const gaps=await textOf('#answer-panel [data-gap-origin="current_projection"]');
    if(!gaps.includes('Current snapshot coverage')||!gaps.includes('No reviewed source capability'))throw new Error('The rendered brief must expose current capability and evidence gaps separately from retained notes.');
    if(!useAgent&&await evaluate(`document.querySelector('#model-consent').checked`))throw new Error('This non-model rehearsal must leave model consent disabled.');
    if(receipt.blocked_requests.length||receipt.console_errors.length||receipt.network_errors.some(x=>x.path!=='/favicon.ico'))throw new Error('Unexpected browser exception, network failure or blocked request; inspect the retained bounded diagnostics.');
    if(receipt.job_receipts.length!==receipt.operations.length)throw new Error('Every observed form submission must have a retained terminal job response hash.');
    if(options.scriptedModel){
      const agents=receipt.job_receipts.filter(x=>x.agent);
      if(agents.length!==3||agents.some(x=>x.status!=='succeeded'||x.agent.receipt_persisted!==true||x.agent.model_origin!=='injected_client_unverified'
         ||x.agent.model_calls!==2||x.agent.reported_cost_usd!==0))throw new Error('Expected three retained two-turn zero-cost scripted agent runs, without real inference.');
    }
    if(configured){
      const agents=receipt.job_receipts.filter(item=>item.agent);
      if(agents.length!==3||receipt.semantic_reviews?.length!==3||receipt.reported_model_cost_usd===null
         ||receipt.reported_model_cost_usd>1.5||!Number.isInteger(receipt.actual_model_calls)||receipt.actual_model_calls<3)throw new Error('Configured profile requires all three independently reviewed, fully costed real-model jobs.');
      receipt.configured_semantic_checks_passed=true;
      receipt.budget_scope='this_profile_only; controller_must_reconcile_shared_total';
      receipt.primary_metric_name=metricName;
    }
    if(options.externalRefresh){
      // A trusted test controller publishes a REAL changed source snapshot
      // outside the browser. No special product route or fake response exists.
      const request={spec:'oriel.customer360.browser-refresh-request/v1',request_id:randomUUID(),
        profile:options.profile,snapshot_id:await textOf('#snapshot-label'),phase:'initial',operation:'samples.connect'};
      await privateWrite(join(options.output,'external-refresh-request.json'),JSON.stringify(request,null,2)+'\n');
      const ackPath=join(options.output,'external-refresh-complete.json');let ack;
      const until=Math.min(deadline,Date.now()+60000);
      while(Date.now()<until){
        try{
          const info=await lstat(ackPath);
          if(!info.isFile()||info.isSymbolicLink()||info.size>16000||info.mode&0o077)throw new Error('Refresh acknowledgment must be a bounded private regular file.');
          ack=JSON.parse(await readFile(ackPath,'utf8'));break;
        }catch(error){if(error.code!=='ENOENT')throw error;}
        await sleep(100);
      }
      if(!ack||ack.request_id!==request.request_id||ack.profile!==request.profile||ack.phase!=='initial'
        ||ack.status!=='passed'||ack.actual_dlt_executed!==true||ack.model_calls!==0||ack.vendor_calls!==0
        ||!(/^[a-f0-9]{64}$/).test(ack.extraction_receipt_sha256||''))throw new Error('The real synthetic external refresh was not confirmed by the trusted test controller.');
      receipt.external_refresh=ack;
      const previousSnapshot=await textOf('#snapshot-label');
      for(const [label,selector] of [
        ['evidence','#answer-panel [data-evidence-id]'],['save','#save-review'],
        ['brief','#load-brief'],['compare',reviewSelector]]){
        await runJob(selector,`!document.querySelector('#global-error').hidden`,{failure:true});
        const stale=await evaluate(`({metrics:document.querySelectorAll('#answer-panel .metric').length,
          receipts:document.querySelectorAll('#answer-panel .answer-receipts').length,
          validated:document.querySelector('#answer-panel').textContent.includes('Evidence checked for this snapshot'),
          comparison:document.querySelector('#comparison-panel').textContent.trim(),dialog:document.querySelector('#evidence-dialog').open})`);
        if(stale.metrics||stale.receipts||stale.validated||stale.comparison||stale.dialog)throw new Error('Failed current '+label+' must remove stale validated output, calculation receipts, comparison and evidence dialog.');
        if((await textOf('#snapshot-label'))!==previousSnapshot)throw new Error('The stale-read check must not silently adopt an unreviewed external publication.');
        await checkpoint('stale-'+label+'-refused',['#global-error','#answer-panel','#comparison-panel']);
      }
      await screenshot('stale-current-refused','#answer-panel');
      const historicalSelector=reviewSelector.replace('compare-review','load-review');
      await runJob(historicalSelector,`document.querySelector('#answer-panel').textContent.includes('Historical saved brief')`);
      await verifyMetric('initial');
      await checkpoint('historical-review-after-source-change',['#answer-panel']);
      await screenshot('historical-review-after-source-change','#answer-panel');
      if(receipt.job_receipts.length!==receipt.operations.length)throw new Error('External-refresh checks must retain every terminal job response.');
      if(receipt.console_errors.length||receipt.blocked_requests.length)throw new Error('Unexpected browser error during stale-result checks.');
    }
    await cdp.send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
    await screenshot('mobile-brief','#answer-panel');
    if(!await evaluate('document.documentElement.scrollWidth <= window.innerWidth+2'))throw new Error('The mobile page has horizontal overflow.');
    await checkpoint('mobile-layout',['#customer-header']);
    receipt.status='passed';
    receipt.verified=['actual_browser_dom_onboarding','visible_source_configuration_refusal','terminal_job_failure_preempts_success_dom_wait','explicit_identity_mapping_context_reviews',
      'static_resource_dependency_preview','changed_selection_during_save_refused','empty_resource_selection_refused','retained_context_notes_have_approval_provenance',
      'measure_policy_form_defaults_unknown','explicit_measure_policy_form_submission_and_review',
      'deterministic_calculation_scope_separate_from_provisional_notes',...(useAgent?['operator_bootstrap_intent_entered_and_retained']:[]),
      'unresolved_identity_visibility','real_local_sample_extraction','customer_brief','attributed_quote_dialog',
      'saved_brief','source_refresh','saved_current_comparison','visible_gaps',configured?'explicit_configured_model_consent':options.scriptedModel?'explicit_scripted_transport_consent':'model_consent_off',
      'mobile_no_horizontal_overflow',...(options.scriptedModel?['scripted_bootstrap_stages','scripted_agent_ask_before_and_after_refresh']:[]),
      ...(configured?['real_configured_bootstrap_and_asks','independent_semantic_review_before_approval','independent_answers_before_and_after_refresh']:[]),
      ...(options.externalRefresh?['out_of_band_actual_source_refresh','failed_current_reads_remove_stale_results','historical_review_remains_labeled']:[])];
  } catch(error) {
    receipt.status='failed';receipt.error=bound(error.message);
    if(cdp&&session) {
      try{await checkpoint('failure',['#global-error','#job-title','#job-detail','#snapshot-label','#answer-panel']);}catch{}
      try{await screenshot('failure');}catch{}
    }
  } finally {
    if(cdp){try{await cdp.send('Browser.close');}catch{}cdp.close();}
    const signalOwnedChrome=signal=>{
      if(!chrome?.pid)return;
      try{if(process.platform!=='win32')process.kill(-chrome.pid,signal);else if(!exited)chrome.kill(signal);}
      catch(error){if(error.code!=='ESRCH')throw error;}
    };
    try{
      // Chrome descendants can inherit stderr and keep Node alive after the
      // browser parent exits. This group belongs only to our fresh profile.
      signalOwnedChrome('SIGTERM');for(let i=0;i<30&&chrome&&!exited;i++)await sleep(100);
      signalOwnedChrome('SIGKILL');for(let i=0;i<30&&chrome&&!exited;i++)await sleep(100);
      chrome?.stderr?.destroy();chrome?.unref();
      if(chrome&&!exited)throw new Error('Owned Chrome process did not exit within the cleanup bound.');
    }catch(error){receipt.status='failed';receipt.error=bound(error.message);}
    // Only the newly allocated owned profile is removed; never a user profile.
    try{await rm(profile,{recursive:true,force:true});receipt.browser_cleanup={main_process_exited:!chrome||exited,isolated_profile_removed:true};}
    catch{receipt.status='failed';receipt.error='Owned isolated Chrome profile cleanup failed.';receipt.browser_cleanup={main_process_exited:!chrome||exited,isolated_profile_removed:false};}
    receipt.finished_at=new Date().toISOString();receipt.elapsed_seconds=(Date.parse(receipt.finished_at)-Date.parse(started))/1000;
    await privateWrite(join(options.output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n');
  }
  return receipt;
}

if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  try {
    const options=parseArguments(process.argv.slice(2)),receipt=await runBrowserCheck(options);
    // The DOM flow, durable receipt and owned browser cleanup have completed.
    // Do not let a native WebSocket close handle strand a finished CLI process.
    process.stdout.write(JSON.stringify({status:receipt.status,profile:receipt.profile,receipt:join(options.output,'receipt.json'),actual_model_calls:receipt.actual_model_calls,reported_model_cost_usd:receipt.reported_model_cost_usd})+'\n',
      ()=>process.exit(receipt.status==='passed'?0:1));
  } catch(error) {process.stderr.write(bound(error.message)+'\n',()=>process.exit(1));}
}
