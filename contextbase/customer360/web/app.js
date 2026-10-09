"use strict";

// Dynamic source/model values only enter textContent, never executable markup.
(() => {
  const $ = id => document.getElementById(id);
  const all = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {csrf:null, workspace:{sources:[],profiles:[],customers:[],pending:{}}, catalog:[], connector:null,
    view:"sources", review:"identity", plan:null, inspection:null, snapshot:null, profile:"customer_review",
    customer:null, busy:false, job:null, timer:null, answer:null, answerMode:null, definitions:[], selectedSources:new Set(), started:0,
    connectorPlan:null,connectorPlanKey:null};
  const freshnessReads=new Set(["brief","evidence","evidence.latest","review.save","review.compare","ask","context.show","context.bootstrap","source.inspect","profile.propose","context.propose"]);
  const calculationScopes=new WeakMap();
  let sequence = 0, connectorSequence = 0, resourcePlanSequence = 0;
  const list = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === "string" ? value : JSON.stringify(value);
  const pretty = value => JSON.stringify(value, null, 2);
  const short = (value, size=16) => value ? String(value).slice(0,size)+(String(value).length>size?"…":"") : "Not available";
  function el(tag, className, value) {
    const node=document.createElement(tag);
    if(className) node.className=className;
    if(value!==undefined && value!==null) node.textContent=String(value);
    return node;
  }
  function replace(node,...children) {node.replaceChildren(...children.filter(Boolean));}
  function showError(error) {
    $("global-error").textContent=error?.message||String(error);
    $("global-error").hidden=false;
    $("global-error").scrollIntoView({block:"nearest",behavior:"auto"});
  }
  function clearError() {$("global-error").hidden=true;$("global-error").textContent="";}
  async function guarded(action) {try {await action();} catch(error) {showError(error);}}
  function button(label, action, className="button secondary small") {
    const node=el("button",className,label);node.type="button";
    node.addEventListener("click",()=>guarded(action));return node;
  }
  function empty(title,description) {
    const node=el("div","empty-state compact");node.append(el("h3","",title),el("p","",description));return node;
  }
  function artifact(value,label="Inspect artifact and receipts") {
    const node=el("details","advanced");node.append(el("summary","",label),el("p","caption","JSON preview: integers outside JavaScript’s safe range appear as decimal strings. Stored artifacts and approval hashes are unchanged."),el("pre","",pretty(value)));return node;
  }
  const badge=(value,variant="neutral")=>el("span","badge "+variant,value);
  function field(label,input,hint) {
    const node=el("label","",label);input.id||="oriel-field-"+(++sequence);node.htmlFor=input.id;node.append(input);
    if(hint) node.append(el("small","",hint));return node;
  }
  function input(value="",placeholder="") {const node=el("input");node.value=value;node.placeholder=placeholder;return node;}
  function select(options,value,placeholder) {
    const node=el("select");
    if(placeholder) {const item=el("option","",placeholder);item.value="";node.append(item);}
    for(const option of options) {const item=el("option","",typeof option==="string"?option:option.label);item.value=typeof option==="string"?option:option.value;node.append(item);}
    if(value!==undefined && all("option",node).some(item=>item.value===value)) node.value=value;
    return node;
  }
  function setSelect(node,options,preferred,placeholder) {
    const replacement=select(options,preferred,placeholder);replace(node,...replacement.childNodes);
    if(preferred && all("option",node).some(item=>item.value===preferred)) node.value=preferred;
  }
  async function api(path,options={}) {
    const response=await fetch(path,{credentials:"same-origin",cache:"no-store",...options});
    if(!(response.headers.get("content-type")||"").includes("application/json")) throw new Error("Unexpected local server response. No action was retried.");
    const value=await response.json();
    if(!response.ok) throw new Error(value.error?.message||text(value.error)||"Local request failed ("+response.status+").");
    for(const entry of list(value.presentation?.calculation_scopes)){
      let definition=value;
      for(const key of list(entry.path)){if(!definition||typeof definition!=="object"||!Object.hasOwn(definition,key)){definition=null;break;}definition=definition[key];}
      if(definition&&typeof definition==="object"&&typeof entry.scope?.text==="string")calculationScopes.set(definition,entry.scope);
    }
    return value;
  }
  function setBusy(value) {
    state.busy=value;
    for(const node of all("button[type=submit],button[data-mutation],#connect-sample,#capture-snapshot,#prepare-sample,#materialize,#bootstrap-context,#sample-context,#propose-definitions,#refresh-sample,#load-brief,#save-review,#list-reviews,#inspect-sources,#show-context")) node.disabled=value||node.dataset.unavailable==="true";
    $("profile-select").disabled=value;
    all("[data-customer-id]").forEach(node=>{node.disabled=value;});
  }
  function jobLabel(operation) {
    return ({"sample.connect":"Extracting sample sources","sample.prepare":"Preparing review proposals","source.add":"Saving source configuration","source.sync":"Extracting source data",snapshot:"Capturing committed sources","links.approve":"Recording identity approval","profile.approve":"Recording mapping approval",materialize:"Building the customer view","context.bootstrap":"Context agent is proposing definitions","context.approve":"Recording context approval",brief:"Checking freshness and preparing the brief",ask:"Agent is working with reviewed evidence","review.save":"Saving a validated brief","review.compare":"Comparing saved and current evidence"})[operation]||"Working on your request";
  }
  async function run(operation,args,complete) {
    if(state.busy) throw new Error("Another operation is running. Wait for its result before starting a new one.");
    if(!state.csrf) throw new Error("The local session is unavailable. Reload the page to reconnect.");
    clearError();setBusy(true);state.started=Date.now();
    if (["source.sync","sample.connect","snapshot","links.approve","profile.approve","context.approve","materialize","profile.refresh","ask"].includes(operation)) invalidateAnswer();
    if(operation==="brief")invalidateAnswer({preserveHistorical:true,reason:"Checking current evidence. Previous current results are hidden until this read succeeds."});
    if(operation==="review.compare")invalidateComparison();
    $("job-status").hidden=false;$("job-status").className="job-bar";
    $("job-title").textContent=jobLabel(operation);$("job-detail").textContent="Submitting once. The workspace will retain the result.";$("check-job").hidden=true;
    try {
      const job=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Oriel-CSRF":state.csrf},body:JSON.stringify({operation,arguments:args,request_id:crypto.randomUUID()})});
      if(!job.job_id) throw new Error("No job identifier was returned. Check workspace state before repeating this operation.");
      state.job={id:job.job_id,operation,complete};await pollJob();
    } catch(error) {
      invalidateFailedRead(operation);
      setBusy(false);$("job-status").className="job-bar terminal failed";$("job-title").textContent="Request not confirmed";
      $("job-detail").textContent="No automatic retry. Check workspace state before submitting again.";throw error;
    }
  }
  async function pollJob() {
    if(!state.job)return;
    clearTimeout(state.timer);const current=state.job;
    try {
      const job=await api("/api/jobs/"+encodeURIComponent(current.id));
      if(state.job!==current)return;
      if(job.status==="queued"||job.status==="running") {
        $("job-detail").textContent=(job.status==="queued"?"Queued":"Running")+" · "+Math.floor((Date.now()-state.started)/1000)+"s · You may browse while this finishes.";
        $("check-job").hidden=true;state.timer=setTimeout(()=>guarded(pollJob),1000);return;
      }
      if(!["succeeded","failed"].includes(job.status)) throw new Error("Unknown job state; the operation has not been retried.");
      state.job=null;setBusy(false);$("check-job").hidden=true;$("job-status").className="job-bar terminal"+(job.status==="failed"?" failed":"");
      if(job.status==="failed") {
        invalidateFailedRead(current.operation);
        $("job-title").textContent="Operation stopped";$("job-detail").textContent=job.error?.message||text(job.error)||"See workspace state before trying again.";
        await refreshState().catch(()=>{});showError(job.error?.message||text(job.error)||"The operation failed and was not retried.");
        if(job.result?.usage){const usage=job.result.usage;$("global-error").append(el("span","error-detail","Known model cost: $"+(usage.known_cost_usd??"unknown")+" · Cost complete: "+(usage.cost_complete===true?"yes":"no")+" · Receipt retained: "+(job.result.receipt_persisted===true?"yes":"unconfirmed")+". No model call was retried."));}
        return;
      }
      $("job-title").textContent="Operation complete";$("job-detail").textContent="Result retained. Any required approval is a separate step.";
      await refreshState();if(current.complete)await current.complete(job.result);
    } catch(error) {
      invalidateFailedRead(current.operation);
      if(!state.job)throw error;
      $("job-status").className="job-bar terminal failed";$("job-title").textContent="Connection interrupted; job outcome unknown";
      $("job-detail").textContent="This does not cancel the job. Check its status before another action.";$("check-job").hidden=false;showError(error);
      // Remain locked until the existing job's terminal state is known.
    }
  }
  function navigate(view,focus=false) {
    if(!["sources","review","customers"].includes(view))return;
    state.view=view;
    for(const name of ["sources","review","customers"])$("view-"+name).hidden=name!==view;
    all("[data-view]").forEach(node=>{node.classList.toggle("active",node.dataset.view===view);if(node.dataset.view===view)node.setAttribute("aria-current","page");else node.removeAttribute("aria-current");});
    $("breadcrumb-current").textContent=view[0].toUpperCase()+view.slice(1);history.replaceState(null,"","#"+view);if(focus)$("main").focus();
  }
  function reviewTab(name,focus=false) {
    if(!["identity","mapping","context"].includes(name))return;
    state.review=name;all("[data-review]").forEach(node=>{const active=node.dataset.review===name;node.setAttribute("aria-selected",String(active));node.tabIndex=active?0:-1;});
    for(const tab of ["identity","mapping","context"])$("review-"+tab).hidden=tab!==name;if(focus)$("tab-"+name).focus();
  }
  function scope(customer=false) {
    if(!state.snapshot)throw new Error("Capture committed sources or prepare the sample review first.");
    if(!state.profile)throw new Error("Choose a reviewed profile first.");
    if(customer&&!state.customer)throw new Error("Choose a customer first.");
    return {profile_name:state.profile,snapshot_id:state.snapshot,...(customer?{customer_id:state.customer}:{})};
  }
  function modelOptions() {
    if(!$("model-consent").checked) {navigate("review");reviewTab("context");$("model-settings").scrollIntoView({block:"center"});throw new Error("Model permission is off. Review the data-sharing notice and opt in before starting this run.");}
    const cost=Number($("model-budget").value),turns=Number($("model-turns").value);
    if(!Number.isFinite(cost)||cost<=0||cost>1||!Number.isInteger(turns)||turns<1||turns>6)throw new Error("Set a budget greater than zero and at most USD 1, with a maximum of 1–6 model turns.");
    return {allow_model_data:true,max_cost_usd:cost,max_turns:turns};
  }
  function numericFilter(raw) {
    const literal=raw.trim();
    const grammar=/^(-?)(0|[1-9]\d*)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/;
    if(literal.length>128||!grammar.test(literal))throw new Error("Enter a finite JSON number for this filter.");
    const value=Number(literal);
    if(!Number.isFinite(value)||(Number.isInteger(value)&&!Number.isSafeInteger(value)))throw new Error("This numeric filter cannot be represented exactly enough in the browser. Use a safe integer or a shorter exact decimal value.");
    const normalized=spelling=>{
      const match=spelling.match(grammar);let digits=(match[2]+(match[3]||"")).replace(/^0+/,"");
      if(!digits)return "0";
      let exponent=Number(match[4]||0)-(match[3]||"").length;
      while(digits.endsWith("0")){digits=digits.slice(0,-1);exponent++;}
      return match[1]+digits+"e"+exponent;
    };
    if(normalized(literal)!==normalized(String(value)))throw new Error("This filter would be rounded by the browser. Enter a value that survives an exact decimal round trip.");
    return value;
  }
  function adoptSnapshot(snapshot) {
    if(!snapshot)return;if(state.snapshot!==snapshot){state.answer=null;state.inspection=null;}
    state.snapshot=snapshot;renderScope();
  }
  function renderScope() {
    $("snapshot-label").textContent=state.snapshot||"Not captured yet";$("snapshot-label").title=state.snapshot||"";
    $("review-empty").hidden=Boolean(state.snapshot);$("review-workspace").hidden=!state.snapshot;
  }
  async function refreshState() {
    const latest=await api("/api/state");
    if(latest.busy){$("session-status").textContent="Operation running · local session";return;}
    state.workspace=latest;const workspace=state.workspace;
    $("session-status").textContent=workspace.exists?"Connected · local session":"Ready for your first source";
    if(!state.snapshot) {const profile=list(workspace.profiles).find(item=>item.name===state.profile)||list(workspace.profiles)[0];if(profile){state.profile=profile.name;adoptSnapshot(profile.materialization?.snapshot_id);}}
    const profiles=[...new Set([state.profile,...list(workspace.profiles).map(item=>item.name)].filter(Boolean))];
    setSelect($("profile-select"),profiles,state.profile);
    setSelect($("link-connection"),list(workspace.sources).map(item=>item.name),$("link-connection").value,"Choose connection");
    setSelect($("link-customer"),list(workspace.customers).map(item=>({value:item.id,label:item.label})),state.customer,"Choose customer");
    $("source-count").textContent=list(workspace.sources).length;$("customer-count").textContent=list(workspace.customers).length;
    renderSources();renderPending();renderCustomers();renderScope();
    $("refresh-sample").hidden=!workspace.sample&&!state.plan;$("prepare-sample").hidden=!workspace.sample&&!state.plan;
    if(workspace.sample) {const choice=all("input[name=sample-profile]").find(node=>node.value===workspace.sample.profile);if(choice)choice.checked=true;$("sample-status").textContent=(workspace.sample.phase==="refreshed"?"Refreshed":"Initial")+" sample connected";}
  }
  function renderSources() {
    const sources=list(state.workspace.sources);
    if(!sources.length){replace($("sources-list"),empty("No sources connected yet.","Start with the sample above, or connect a tool using environment variable references."));return;}
    replace($("sources-list"),...sources.map(source=>{
      const row=el("div","source-row"),choice=el("input");choice.type="checkbox";choice.setAttribute("aria-label","Include "+source.name+" in snapshot");choice.checked=state.selectedSources.has(source.name);
      choice.addEventListener("change",()=>choice.checked?state.selectedSources.add(source.name):state.selectedSources.delete(source.name));
      const name=el("div","source-name");name.append(el("strong","",source.name));
      const sync=source.last_sync||source.last_attempt||{},publication=source.last_snapshot||source.publication||source.last_publication||{};
      const status=source.status||sync.status||(publication.snapshot_id?"committed":"not synced");
      name.append(el("small","",(source.source_type||"source")+" · "+(source.reconfigured||source.configuration_changed_since_sync?"Configuration changed; sync required":String(status).replaceAll("_"," "))));
      row.append(choice,el("span","source-icon",(source.source_type||"db").slice(0,2)),name,badge(status,/failed|error/.test(status)?"error":"neutral"));
      const syncButton=button("Sync",()=>{
        const args={name:source.name};
        if($("sync-budget-enabled").checked){
          const requests=$("sync-max-requests"),seconds=$("sync-request-seconds");
          if(!requests.reportValidity()||!seconds.reportValidity())return;
          const max_requests=Number(requests.value),max_seconds=Number(seconds.value);
          if(!Number.isInteger(max_requests)||!Number.isInteger(max_seconds)||max_requests<1||max_seconds<1)return;
          args.request_budget={max_requests,max_seconds};
        }
        return run("source.sync",args,()=>{invalidateAnswer();$("snapshot-summary").textContent="Source sync finished. Capture the selected sources to create a new review snapshot.";$("snapshot-summary").hidden=false;});
      });
      syncButton.dataset.mutation="true";syncButton.disabled=state.busy;row.append(syncButton);return row;
    }));
  }
  const pending=kind=>list(state.workspace.pending?.[kind]?.proposals||state.workspace.pending?.[kind]);
  const customerLabel=id=>list(state.workspace.customers).find(item=>item.id===id)?.label||id;
  function bindingSummary(binding) {
    const row=el("div","line-item");row.append(el("strong","",binding.table+" → "+binding.kind),el("p","","Record ID: "+binding.record_id+" · Customer reference: "+binding.customer_ref?.connection+"."+binding.customer_ref?.entity+" / "+binding.customer_ref?.column));
    const roles=el("div","column-chips");for(const [role,column]of Object.entries(binding.fields||{}))roles.append(el("span","",column+" → "+role));row.append(roles);
    const measures=el("section","line-item");measures.dataset.measureSummary="true";measures.append(el("strong","","Measure policies"));
    const policies=Object.entries(binding.measures||{});
    if(!policies.length)measures.append(el("p","hint","No measure policy recorded. Units and permitted aggregation remain unknown; a numeric column alone does not establish them."));
    for(const [role,policy]of policies){
      const detail=el("div","line-item");detail.dataset.measurePolicyRole=role;
      detail.append(el("strong","",role+" · "+measureUnitLabel(policy.unit)),el("p","",measureAggregationLabel(policy.aggregation)+" · Currency role: "+(policy.currency_field||"Not specified")),el("p","",policy.meaning));
      if(policy.unit==="unknown"||policy.aggregation==="unknown")detail.append(badge("Unconfirmed policy — model sums unavailable","warning"));
      measures.append(detail);
    }
    measures.append(el("p","caption","Review the unit, currency dimension, permission to sum and business meaning with this mapping. Metric definitions still require their own review."));row.append(measures);
    for(const attachment of list(binding.attachments))row.append(el("p","caption","Related metadata: "+attachment.name+" · "+attachment.table+" · "+attachment.local_key+" → "+attachment.remote_key+" · At most one row for the same customer."));
    for(const passage of list(binding.passages))row.append(el("p","caption","Passage: "+passage.name+" · "+passage.field+passage.text_path+" · Time: "+passage.time?.meaning+" from "+(passage.time?.attachment||passage.time?.source)+" / "+(passage.time?.field||"")+passage.time?.path+" ("+passage.time?.encoding+")"));
    return row;
  }
  function calculationHeading(definition) {
    return definition?.operation==="count"?"Count · "+definition.kind+" records":definition?.operation==="sum"?"Sum · "+definition.kind+"."+definition.field:"Calculation scope unavailable";
  }
  function calculationView(definition,supplied=null,{draft=false}={}) {
    const node=el("div","caption");node.dataset.calculationScope="true";
    const scope=calculationScopes.get(definition)||supplied;
    if(scope?.spec_version==="oriel.customer360.calculation-scope/v1"&&typeof scope.text==="string")node.append(el("p","",scope.text));
    else if(draft){
      const predicates=list(definition.filters).map(predicate=>predicate.field+" "+predicate.operator+" "+JSON.stringify(predicate.value));
      node.append(el("p","",calculationHeading(definition)+". Filters (all must match): "+(predicates.join(" AND ")||"none")+". Unit: "+definition.unit+". Currency role: "+(definition.currency_field||"none")+". Only the selected customer's resolved records in its pinned snapshot. Draft preview; inspect the server's typed calculation scope before approval."));
    }else node.append(el("p","hint","Typed calculation scope is unavailable for this artifact. Do not infer the calculation from its description; inspect its recorded definition before use."));
    return node;
  }
  function definitionNote(definition) {
    const note=el("div","caption");note.dataset.definitionNote="true";
    note.append(el("strong","","Provisional description / reviewer note — not calculation scope"),el("p","",definition?.description||"No description recorded."));return note;
  }
  function operatorIntent(context) {
    const intents=[...new Set(list(context.stages).map(stage=>stage.intent).filter(value=>typeof value==="string"))];
    if(!intents.length)return null;
    const node=el("section","notice");node.dataset.bootstrapIntent="true";node.append(el("strong","","Retained operator task request — not reviewed policy"));
    for(const intent of intents){const value=el("p","",intent);value.dataset.bootstrapIntentReview="true";node.append(value);}
    node.append(el("p","caption","This request guided the proposal. It does not establish units, filters, business meaning or approval."));return node;
  }
  function definitionSummary(definition,{draft=false}={}) {
    const row=el("div","definition-row"),body=el("div");row.dataset.definitionName=definition.name;
    body.append(el("strong","",calculationHeading(definition)),calculationView(definition,null,{draft}),el("code","","Definition: "+definition.name),definitionNote(definition));row.append(body);return row;
  }
  function proposalCard(kind,proposal) {
    const conflict=proposal.status==="conflict"||list(proposal.conflicts).length>0;
    const card=el("article","proposal"+(conflict?" conflict":""));card.dataset.proposalId=proposal.proposal_id;
    const heading=el("div","proposal-header");heading.append(el("strong","",kind==="links"?list(proposal.links).length+" identity links":kind==="profile"?(proposal.profile?.name||"Field mapping"):"Business definitions"),badge(conflict?"Unresolved conflict":"Needs your review",conflict?"warning":"neutral"));card.append(heading);
    if(kind==="links")for(const link of list(proposal.links)){const row=el("div","line-item");row.append(el("strong","",link.connection+"."+link.entity+" / "+link.source_id+" → "+customerLabel(link.customer_id)),el("p","",link.basis));card.append(row);}
    if(kind==="profile")for(const binding of list(proposal.profile?.bindings))card.append(bindingSummary(binding));
    if(kind==="context"){const intent=operatorIntent(proposal);if(intent)card.append(intent);for(const definition of list(proposal.definitions))card.append(definitionSummary(definition));if(proposal.gaps?.length)card.append(gaps(proposal.gaps,{heading:"Notes in this context proposal",description:"Review these notes with the definitions. Proposal snapshot: "+proposal.snapshot_id}));}
    if(conflict)card.append(el("p","hint","This proposal cannot be approved. Keep these records unresolved until an explicit, unambiguous source reference is available."));
    card.append(artifact(proposal,"Review exact proposal and scope"));
    if(!conflict) {
      const check=el("input");check.type="checkbox";check.dataset.confirmProposal="true";
      const label=el("label","check-label");label.append(check,el("span","","I reviewed this exact proposal and its source scope."));card.append(label);
      const approve=button("Approve "+({links:"identity links",profile:"field mapping",context:"definitions"})[kind],async()=>{
        if(!check.checked)throw new Error("Review the proposal and select its confirmation checkbox first.");
        const reviewer=$("reviewer").value.trim();if(!reviewer){$("reviewer").focus();throw new Error("Enter the reviewer name before approving.");}
        await run(kind+".approve",{proposal_id:proposal.proposal_id,approval_sha256:proposal.approval_sha256,reviewer},result=>{
          if(kind==="profile"){state.profile=result.profile?.name||proposal.profile?.name||state.profile;$("profile-select").value=state.profile;}
          if(kind==="context")renderContext(result);
        });
      },"button");approve.dataset.action="approve-"+kind;approve.dataset.mutation="true";approve.disabled=state.busy;card.append(approve);
    }
    card.append(el("code","proposal-hash","Approval SHA-256: "+proposal.approval_sha256));return card;
  }
  function renderPending() {
    let count=0;for(const [kind,id]of [["links","identity-proposals"],["profile","mapping-proposals"],["context","context-proposals"]]){
      const proposals=pending(kind);count+=proposals.filter(item=>item.status!=="conflict").length;
      replace($(id),...(proposals.length?proposals.map(proposal=>proposalCard(kind,proposal)):[el("p","small-empty","No pending "+(kind==="links"?"identity links":kind==="profile"?"field mappings":"definitions")+".")]));
    }$("review-count").textContent=count;
  }
  function showPlan(plan) {
    state.plan=plan;state.profile=plan.mapping?.name||plan.profile||"customer_review";adoptSnapshot(plan.snapshot_id);
    setSelect($("profile-select"),[...new Set([state.profile,...list(state.workspace.profiles).map(item=>item.name)])],state.profile);
    const panel=$("sample-mapping");panel.hidden=false;replace(panel,el("h3","","Authored sample mapping"),el("p","quiet","Review these source fields before proposing. Approval is a separate step."));
    for(const binding of list(plan.mapping?.bindings))panel.append(bindingSummary(binding));
    const propose=button("Propose sample field mapping",()=>run("profile.propose",{profile:plan.mapping,snapshot_id:state.snapshot}),"button");propose.dataset.action="propose-sample-mapping";propose.dataset.mutation="true";
    panel.append(artifact(plan.mapping),propose);navigate("review");reviewTab("identity");
  }
  function renderContext(context) {
    const body=$("approved-context");replace(body,el("h3","","Approved business context"));
    const intent=operatorIntent(context);if(intent)body.append(intent);
    for(const definition of list(context.definitions))body.append(definitionSummary(definition));
    const approval=context.approved_snapshot_id||context.snapshot_id;
    body.append(gaps(context.gaps,{heading:"Notes retained from context approval",description:"Approval snapshot: "+approval+(approval!==context.snapshot_id?". These notes were not rechecked after refresh.":". These are review notes, not newly computed coverage checks.")}),artifact(context,"Inspect approved context and lineage"));
  }
  function gaps(items,options={}) {
    const node=el("div","gap-list");
    function section(values,heading,description,origin) {
      const part=el("section");if(origin)part.dataset.gapOrigin=origin;
      part.append(el("strong","",heading));if(description)part.append(el("p","caption",description));
      const ul=el("ul");for(const item of values)ul.append(el("li","",(item.kind?item.kind+": ":"")+(item.description||item.code||text(item))));part.append(ul);node.append(part);
    }
    if(options.heading){section(list(items),options.heading,options.description);return node;}
    if(!list(items).length){node.append(el("p","","No additional gaps recorded. Source completeness is not guaranteed."));return node;}
    const retained=items.filter(item=>item.origin==="approved_context"),current=items.filter(item=>item.origin==="current_projection"),unknown=items.filter(item=>!["approved_context","current_projection"].includes(item.origin));
    if(retained.length){
      const snapshots=[...new Set(retained.map(item=>item.source_snapshot_id).filter(Boolean))];
      section(retained,"Notes retained from context approval","Approval snapshot: "+(snapshots.join(", ")||"not recorded")+(retained.some(item=>item.not_rechecked_after_refresh)?". These notes were not rechecked after refresh; they may describe an earlier observation.":". These are review notes, not newly computed coverage checks."),"approved_context");
    }
    if(current.length)section(current,"Current snapshot coverage","Computed for this receipt's snapshot. A historical receipt describes its own recorded snapshot.","current_projection");
    if(unknown.length)section(unknown,"Notes without recorded provenance","This receipt does not distinguish retained review notes from snapshot coverage. Do not assume these describe the current source state.","unknown");
    return node;
  }

  function invalidateAnswer({preserveHistorical=false,reason="The source or review scope may have changed. Reopen the brief to check current evidence."}={}) {
    if(!(preserveHistorical&&state.answerMode==="historical")){
      state.answer=null;state.answerMode=null;
      replace($("answer-panel"),empty("Open a freshly validated brief.",reason));
      $("answer-panel").className="";$("answer-panel").dataset.answerMode="none";
    }
    invalidateComparison();
  }
  function invalidateComparison() {
    replace($("comparison-panel"));delete $("comparison-panel").dataset.comparisonId;
  }
  function invalidateFailedRead(operation) {
    if(freshnessReads.has(operation))invalidateAnswer({preserveHistorical:true,reason:"Current evidence could not be revalidated. Previous current results are hidden. Review source status and refresh the customer view before trying again."});
  }
  function renderCustomers() {
    const query=$("customer-search").value.toLowerCase(),customers=list(state.workspace.customers).filter(item=>item.label.toLowerCase().includes(query));
    if(!customers.length){replace($("customer-list"),el("p","small-empty",query?"No matching customer.":"Reviewed customer identities will appear here."));return;}
    replace($("customer-list"),...customers.map(customer=>{
      const node=button("",()=>chooseCustomer(customer.id),"customer-item"+(state.customer===customer.id?" selected":""));
      node.dataset.customerId=customer.id;
      node.disabled=state.busy;
      const initials=customer.label.split(/\s+/).slice(0,2).map(word=>word[0]||"").join("").toUpperCase();
      const body=el("span");body.append(el("span","",customer.label),el("small","",short(customer.id,18)));
      node.append(el("span","initials",initials),body);return node;
    }));
  }
  function chooseCustomer(id) {
    if(state.busy)throw new Error("Wait for the current operation before changing customer scope.");
    if(state.customer!==id){state.customer=id;invalidateAnswer();replace($("saved-reviews"));}
    renderCustomers();
    const heading=el("div");heading.append(el("h2","",customerLabel(id)),el("p","quiet",state.profile+" · "+short(state.snapshot,24)));
    replace($("customer-header"),heading,badge("Customer review"));navigate("customers");
  }
  function receiptGrid(value) {
    const grid=el("dl","receipt-grid");
    for(const [key,label]of [["profile","Profile"],["customer_id","Customer"],["snapshot_id","Snapshot"],["context_id","Context"],["definition_sha256","Definitions SHA"],["profile_approval","Mapping approval"],["materialization_id","Materialization"],["evidence_id","Source evidence"],["source_id","Source record"],["answer_id","Answer receipt"],["result_id","Metric result"]]){
      if(value[key])grid.append(el("dt","",label),el("dd","",value[key]));
    }return grid;
  }
  function showEvidence(value,title="Source evidence") {
    $("evidence-title").textContent=title;const body=$("evidence-content");
    replace(body,...(typeof value.text==="string"?[el("blockquote","",value.text)]:[]),receiptGrid(value),artifact(value,"Exact evidence and receipt"));
    if(!$("evidence-dialog").open)$("evidence-dialog").showModal();
  }
  function quoteCard(quote,historical=false) {
    const node=el("article","quote");node.append(el("blockquote","",quote.text));
    const attribution=el("div","quote-attribution");
    attribution.append(el("span","",quote.connection+" · "+quote.entity+" / "+quote.source_id+" · "+quote.field+(quote.is_partial_quote?" · excerpt":"")));
    const locator=quote.passage_name?{passage_name:quote.passage_name,json_pointer:quote.json_pointer}:{};
    const inspect=button(historical?"Inspect saved passage":"Open evidence",()=>historical?showEvidence(quote,"Historical source evidence"):run("evidence",{...scope(true),evidence_id:quote.evidence_id,field:quote.field,start:quote.start,end:quote.end,...locator},result=>showEvidence(result)),"button text-button small");
    inspect.dataset.evidenceId=quote.evidence_id;inspect.dataset.mutation="true";attribution.append(inspect);node.append(attribution);return node;
  }
  function renderAnswer(answer,{historical=false,runResult=null}={}) {
    if(!answer||!answer.answer_id)throw new Error("No validated answer receipt was returned.");
    if(answer.customer_id!==state.customer||answer.profile!==state.profile||(!historical&&answer.snapshot_id!==state.snapshot))throw new Error("The returned answer does not match the selected customer, profile, and snapshot. It was not displayed.");
    const heading=el("div");heading.append(el("h2","",customerLabel(answer.customer_id)),el("p","quiet",answer.profile+" · "+short(answer.snapshot_id,24)+(historical?" · historical":"")));
    replace($("customer-header"),heading,badge(historical?"Saved review":"Customer review",historical?"warning":""));
    state.answer=historical?null:answer;state.answerMode=historical?"historical":"current";const body=$("answer-panel");body.dataset.answerMode=state.answerMode;body.className="";replace(body);
    const status=el("div","brief-status");status.append(badge(historical?"Historical saved brief":"Evidence checked for this snapshot",historical?"warning":""),el("span","caption",short(answer.snapshot_id,22)));body.append(status);
    if(!answer.abstention)body.append(el("p","caption","Calculations and quotations match the selected evidence. Check that the selected passages address your question."));
    if(runResult) {
      const usage=runResult.usage||{},origin=runResult.model_origin==="injected_client_unverified"?"Scripted test (no real inference)":runResult.model_origin==="configured_claude_cli"?"Configured Claude CLI":(runResult.model_origin||"unknown");body.append(el("p","hint","Model origin: "+origin+" · Calls: "+(usage.model_calls??"unknown")+" · Reported cost: "+(usage.cost_complete===false||usage.reported_cost_usd===null?"unknown":"$"+String(usage.reported_cost_usd??usage.known_cost_usd??"unknown"))));
    }
    if(answer.abstention)body.append(empty("A grounded answer is unavailable.",answer.abstention.meaning||answer.abstention.code));
    const metrics=el("div","metrics");
    for(const metric of list(answer.metrics)) {
      const result=metric.result||metric,card=el("section","metric");card.dataset.definitionName=result.definition_name;
      card.append(el("h3","metric-name",calculationHeading(metric.definition)),calculationView(metric.definition,metric.calculation_scope),el("code","","Definition: "+result.definition_name));
      if(!list(result.values).length)card.append(el("div","metric-value","Unavailable"));
      for(const value of list(result.values)) {const amount=el("div","metric-value",String(value.value));amount.dataset.currency=value.currency||"";amount.dataset.exactValue=String(value.value);card.append(amount);if(value.currency)card.append(el("div","metric-unit",value.currency+(result.unit==="minor_currency"?" · minor units":"")));}
      card.append(el("div","metric-unit",(result.unit||"").replaceAll("_"," ")+" · "+(result.matched_records??"")+" matched records"));
      card.append(definitionNote(metric.definition));
      const inspect=button("Inspect calculation",()=>showEvidence(metric,"Reviewed metric calculation"),"button text-button small");inspect.dataset.metricId=metric.result_id;card.append(inspect);metrics.append(card);
    }
    if(metrics.childNodes.length)body.append(metrics);
    if(list(answer.quotes).length) {body.append(el("h3","","From the source"));for(const quote of answer.quotes)body.append(quoteCard(quote,historical));}
    else body.append(el("p","small-empty","No text passages were selected for this brief."));
    for(const selection of list(answer.temporal_selections)) {
      const label=selection.status==="latest_tied"?"Several passages share the latest source timestamp.":"Latest passage by reviewed source timestamp.";
      const detail=el("div","temporal-selection");detail.append(el("p","caption",label),artifact(selection,"Inspect passage times and selection coverage"));body.append(detail);
    }
    body.append(gaps(answer.gaps));
    const selection=answer.evidence_selection;
    if(selection)body.append(el("p","caption","Passages are a bounded selection"+(selection.truncated?" (more are available)":"")+", not an exhaustive account narrative."));
    const receipts=artifact(answer,"Snapshot, context, SQL and answer receipts");receipts.classList.add("answer-receipts");body.append(receipts);
  }
  function renderReviews(result) {
    const records=Array.isArray(result)?result:list(result.reviews);
    replace($("saved-reviews"),el("div","section-heading","Saved reviews"));
    if(!records.length){$("saved-reviews").append(el("p","small-empty","No saved briefs for this customer."));return;}
    for(const record of records) {
      const row=el("div","review-record"),body=el("div");body.append(el("strong","",record.created_at?new Date(record.created_at).toLocaleString():"Saved review"),el("small","","Historical · "+short(record.review_id,18)));row.append(body);
      const load=button("Open",()=>run("review.load",{review_id:record.review_id},value=>renderAnswer(value.brief,{historical:true})));
      const compare=button("Compare with current",()=>run("review.compare",{review_id:record.review_id,current_snapshot_id:scope().snapshot_id},renderComparison));
      load.dataset.action="load-review";compare.dataset.action="compare-review";load.dataset.reviewId=record.review_id;compare.dataset.reviewId=record.review_id;load.dataset.mutation=compare.dataset.mutation="true";row.append(load,compare);$("saved-reviews").append(row);
    }
  }
  function renderComparison(result) {
    const body=$("comparison-panel");replace(body,el("div","section-heading","What changed"),el("p","quiet","Current evidence compared with the retained historical review. Missing groups are not treated as zero."));
    body.dataset.comparisonId=result.comparison_id||"";
    if(list(result.changed_basis).length)body.append(el("div","notice","Review basis changed: "+result.changed_basis.join(", ")+". Numeric deltas are withheld rather than attributed only to source changes."));
    const metrics=result.metric_changes;
    if(Array.isArray(metrics)&&metrics.length){
      const scroll=el("div","table-scroll"),table=el("table"),head=el("thead"),heading=el("tr"),rows=el("tbody");
      for(const label of ["Metric / unit","Saved","Current","Change"])heading.append(el("th","",label));head.append(heading);
      for(const metric of metrics){
        const row=el("tr");row.dataset.definitionName=metric.definition_name;row.dataset.currency=metric.currency||"";
        row.append(el("td","",metric.definition_name+" · "+(metric.currency||metric.unit)),el("td","",metric.before===null?"Unavailable":metric.before),el("td","",metric.after===null?"Unavailable":metric.after),el("td","",metric.delta===null?"Not comparable":metric.delta));rows.append(row);
      }
      table.append(head,rows);scroll.append(table);body.append(scroll);
    }
    else if(Array.isArray(metrics))body.append(el("p","hint","No metric value changes in this comparison."));
    else if(metrics)body.append(artifact(metrics,"Metric changes"));
    const changes=result.record_changes||{};
    body.append(el("p","hint",list(changes.added).length+" added · "+list(changes.removed).length+" removed · "+list(changes.changed).length+" changed source records."));
    body.append(artifact(result.record_changes||{},"Changed source records"),artifact(result,"Full comparison and scope"));
  }
  async function loadConnector() {
    const type=$("source-type").value,request=++connectorSequence;resourcePlanSequence++;state.connector=null;state.connectorPlan=null;state.connectorPlanKey=null;
    replace($("resource-plan"));
    replace($("credential-fields"));replace($("resource-options"));replace($("parameter-fields"));$("path-field").hidden=true;$("resource-fieldset").hidden=true;
    if(!type){$("connector-detail").textContent="Choose a tool to see its requirements.";return;}
    $("connector-detail").textContent="Loading connector requirements…";
    const detail=await api("/api/connector?type="+encodeURIComponent(type));
    if($("source-type").value!==type||request!==connectorSequence)return;
    const connector=detail.connector||detail;state.connector=connector;
    const readiness=detail.readiness?.connectors?.[0]||detail.readiness||{};
    const row=el("div");row.append(el("p","hint",connector.description||type),badge(readiness.status||"Readiness unknown","neutral"),el("p","caption","Catalog availability does not verify your vendor account, permissions, or resource completeness."));
    row.append(artifact({readiness,limitations:connector.limitations},"Requirements and current limitations"));replace($("connector-detail"),row);
    for(const spec of list(connector.credential_specs)) {
      if(["csv","parquet"].includes(type)&&spec.name==="path")continue;
      const control=input("",spec.env_var||"ORIEL_"+type.toUpperCase()+"_"+spec.name.toUpperCase());control.required=spec.required!==false;control.autocomplete="off";control.pattern="[A-Za-z_][A-Za-z0-9_]*";control.dataset.credential=spec.name;
      $("credential-fields").append(field(spec.name+(spec.required===false?" (optional)":""),control,"Environment variable name only. Never paste a credential value."));
    }
    const isFile=["csv","parquet"].includes(type);$("path-field").hidden=!isFile;$("source-path").required=isFile;
    const resources=list(connector.resources);$("resource-fieldset").hidden=!resources.length;
    for(const resource of resources) {
      const choice=el("input");choice.type="checkbox";choice.value=resource.name;choice.checked=resource.selected!==false;choice.dataset.resource=resource.name;
      const label=el("label","check-label");label.append(choice,el("span","",resource.name));$("resource-options").append(label);
      choice.addEventListener("change",()=>guarded(loadSelectionPlan));
    }
    if(!$("source-name").value)$("source-name").value=type.replaceAll("-","_");
    await loadSelectionPlan();
  }
  function selectedResources() {return all("[data-resource]",$("resource-options")).filter(node=>node.checked).map(node=>node.value);}
  function selectionKey() {return JSON.stringify([$("source-type").value,selectedResources()]);}
  async function loadSelectionPlan() {
    const request=++resourcePlanSequence,key=selectionKey(),connector=state.connector;
    state.connectorPlan=null;state.connectorPlanKey=null;
    if(!connector)return;
    const resources=selectedResources(),body=$("resource-plan");
    if(list(connector.resources).length&&!resources.length){replace(body,el("p","hint","Select at least one resource. An empty selection will not restore connector defaults."));renderParameters();return;}
    replace(body,el("p","caption","Inspecting declared resource dependencies…"));
    const params=new URLSearchParams({type:connector.name});
    const defaults=list(connector.resources).filter(item=>item.selected!==false).map(item=>item.name);
    if(JSON.stringify([...resources].sort())!==JSON.stringify(defaults.sort()))for(const name of resources)params.append("resource",name);
    const plan=await api("/api/source-plan?"+params.toString());
    if(request!==resourcePlanSequence||key!==selectionKey()||connector!==state.connector)return;
    state.connectorPlan=plan;state.connectorPlanKey=key;
    const rows=el("div");
    if(plan.status==="declaration_derived"){
      const parents=list(plan.required_fetch_resources).filter(name=>!list(plan.selected_outputs).includes(name));
      rows.append(el("p","hint",parents.length?"Also fetched to locate selected records: "+parents.join(", ")+".":"No additional parent resources are declared for this selection."));
      rows.append(el("p","caption","This comes from the connector declaration. Review access to all listed resources before syncing; it does not prove field permissions or actual fetch coverage."));
    }else rows.append(el("p","hint","This connector's fetched resource scope cannot be determined from its catalog declaration. Review its runtime and permissions before syncing."));
    rows.append(artifact(plan,"Inspect selected outputs and fetch dependencies"));replace(body,rows);renderParameters();
  }
  function renderParameters() {
    const previous={};for(const node of all("[data-param]",$("parameter-fields")))previous[node.dataset.resource+"."+node.dataset.param]=node.value;
    replace($("parameter-fields"));const metadata=state.connector?.resource_parameters||{};
    const selected=selectedResources(),needed=state.connectorPlan?.required_fetch_resources||selected;
    for(const resource of needed) {
      const definition=metadata[resource]||{},required=list(definition.required),dependency=!selected.includes(resource);
      for(const key of list(definition.allowed)) {
        const control=input(previous[resource+"."+key]||"","Value");control.dataset.resource=resource;control.dataset.param=key;control.required=required.includes(key);
        $("parameter-fields").append(field(resource+" / "+key+(control.required?" (required)":"")+(dependency?" · parent dependency":""),control));
      }
    }
  }
  function inspectResult(result) {
    state.inspection=result;const body=$("inspection-summary");replace(body,el("p","hint","Observed committed schema. Samples are bounded; omitted cells are not empty values."));
    for(const source of list(result.sources))for(const table of list(source.tables)) {
      const row=el("div","inspection-table");row.append(el("h3","",table.table+" · "+table.rows+" rows"));
      const chips=el("div","column-chips");for(const column of list(table.columns))chips.append(el("span","",column.name+" · "+column.type));row.append(chips);
      row.append(artifact(table.samples,"Inspect bounded observed rows"));body.append(row);
    }
    if(!$("binding-list").childNodes.length)addBinding();
  }
  function observedTables() {
    return list(state.inspection?.sources).flatMap(source=>list(source.tables).map(table=>({...table,connection:source.name})));
  }
  const measureUnits=[{value:"unknown",label:"Unknown — not confirmed"},{value:"number",label:"Nonmonetary number"},{value:"currency",label:"Currency — major units"},{value:"minor_currency",label:"Currency — minor units"}];
  const measureAggregations=[{value:"unknown",label:"Unknown — not confirmed"},{value:"none",label:"Do not sum"},{value:"sum",label:"Sum permitted"}];
  const measureUnitLabel=value=>measureUnits.find(item=>item.value===value)?.label||"Unknown unit";
  const measureAggregationLabel=value=>measureAggregations.find(item=>item.value===value)?.label||"Unknown aggregation";
  function addBinding() {
    const tables=observedTables();if(!tables.length)throw new Error("Inspect source fields before adding a mapping.");
    const row=el("div","binding"),heading=el("div","binding-heading");heading.append(el("strong","","Table binding"),button("Remove",()=>row.remove(),"button text-button small"));row.append(heading);
    const grid=el("div","form-grid"),table=select(tables.map(item=>item.table)),entity=input("","accounts");
    const kind=select(["customer","person","commercial","billing","support","usage","conversation"]);
    const record=select([]),refConnection=select(list(state.workspace.sources).map(item=>item.name)),refEntity=input("","accounts"),refColumn=select([]);
    table.dataset.bindingTable=entity.dataset.bindingEntity=kind.dataset.bindingKind=record.dataset.bindingRecordId=refConnection.dataset.bindingRefConnection=refEntity.dataset.bindingRefEntity=refColumn.dataset.bindingRefColumn="true";
    entity.required=refEntity.required=true;entity.pattern=refEntity.pattern="[a-z][a-z0-9_]*";
    grid.append(field("Observed table",table),field("Source entity",entity,"The entity used in reviewed identity links."),field("Record kind",kind),field("Record ID column",record),field("Customer reference connection",refConnection),field("Customer reference entity",refEntity),field("Customer reference column",refColumn));row.append(grid);
    const roles=el("div"),measureRows=el("div"),measureNotice=el("p","hint"),addRole=button("+ Expose a field",()=>role(),"button text-button");
    addRole.dataset.action="add-role";
    function mappedRoles(){return [...new Set(all("[data-role]",roles).map(node=>node.value.trim()).filter(value=>/^[a-z][a-z0-9_]{0,62}$/.test(value)))];}
    function updateMeasureRoles(){
      const names=mappedRoles();
      for(const measure of all("[data-measure-row]",measureRows)){
        for(const [attribute,placeholder]of [["data-measure-role","Choose a mapped value role"],["data-measure-currency","Not specified"]]){
          const control=measure.querySelector("["+attribute+"]"),previous=control.value;
          setSelect(control,names,previous,placeholder);if(!names.includes(previous))control.value="";
        }
      }
    }
    function role() {
      const roleRow=el("div","role-row"),name=input("","text / amount_minor / status"),column=select(list(tables.find(item=>item.table===table.value)?.columns).map(item=>item.name));
      name.required=true;name.pattern="[a-z][a-z0-9_]*";name.dataset.role="true";column.dataset.column="true";
      name.addEventListener("input",updateMeasureRoles);
      roleRow.append(field("Logical role",name),field("Observed column",column),button("×",()=>{roleRow.remove();updateMeasureRoles();},"icon-button"));roles.append(roleRow);
    }
    function addMeasure(){
      if(all("[data-measure-row]",measureRows).length>=64)throw new Error("Use at most 64 measure policies per binding.");
      const measure=el("div","panel");measure.dataset.measureRow="true";
      const valueRole=select(mappedRoles(),"","Choose a mapped value role"),unit=select(measureUnits,"unknown"),currencyRole=select(mappedRoles(),"","Not specified"),aggregation=select(measureAggregations,"unknown"),meaning=input("","What does this value represent, and what establishes its unit and allowed aggregation?");
      valueRole.required=true;meaning.required=true;meaning.maxLength=2000;
      valueRole.dataset.measureRole=unit.dataset.measureUnit=currencyRole.dataset.measureCurrency=aggregation.dataset.measureAggregation=meaning.dataset.measureMeaning="true";
      function unitChanged(){currencyRole.disabled=unit.value==="number";currencyRole.required=["currency","minor_currency"].includes(unit.value);if(currencyRole.disabled)currencyRole.value="";}
      unit.addEventListener("change",unitChanged);unitChanged();
      const controls=el("div","form-grid");controls.append(field("Mapped value role",valueRole),field("Unit",unit,"Choose from reviewed source meaning, not the column's numeric type."),field("Currency role",currencyRole,"Required for money; optional when scale is unknown. Use a different mapped role."),field("Allowed aggregation",aggregation,"Sum permitted is a reviewed policy, not a metric definition."));
      const remove=button("Remove measure policy",()=>measure.remove(),"button text-button small");remove.dataset.action="remove-measure";
      measure.append(controls,field("Meaning and review basis",meaning,"Required, single line, at most 2,000 characters. Keep unresolved meaning explicit."),remove);measureRows.append(measure);measureNotice.textContent="Policies are proposals until this exact mapping is approved. Unknown units or aggregation do not permit new model sums.";
    }
    function update() {const selected=tables.find(item=>item.table===table.value);const columns=list(selected?.columns).map(item=>item.name);setSelect(record,columns);setSelect(refColumn,columns);refConnection.value=selected?.connection||"";replace(roles);if(measureRows.childNodes.length){replace(measureRows);measureNotice.textContent="The table changed. Previous measure policies were removed; review policies for the newly mapped fields.";}role();}
    table.addEventListener("change",update);update();row.append(roles,addRole);
    const measureSection=el("section","panel"),addMeasureButton=button("+ Add measure policy",addMeasure,"button secondary small");addMeasureButton.dataset.action="add-measure";
    measureNotice.textContent="Optional: explicitly review a mapped value's unit, currency and aggregation. Without a policy these remain unknown; the form does not infer them from names or types.";
    measureSection.append(el("h3","","Measure policies"),measureNotice,measureRows,addMeasureButton);row.append(measureSection);
    const temporal=el("details"),temporalInput=el("textarea");temporalInput.rows=5;temporalInput.maxLength=16000;temporalInput.placeholder='{"passages": [], "attachments": []}';
    temporalInput.dataset.bindingTemporal="true";
    temporal.append(el("summary","","Passage timestamps (advanced)"),field("Reviewed passage and related-metadata mappings",temporalInput,"Optional JSON with passages and attachments. Use selected source tables and exact customer keys; the proposal is checked before review."));row.append(temporal);
    row.readBinding=()=>{
      const fields={};for(const roleRow of all(".role-row",roles)){const name=roleRow.querySelector("[data-role]").value.trim();if(!name||Object.hasOwn(fields,name))throw new Error("Use unique, nonempty logical field roles.");fields[name]=roleRow.querySelector("[data-column]").value;}
      const measures=Object.create(null);
      for(const measure of all("[data-measure-row]",measureRows)){
        const role=measure.querySelector("[data-measure-role]").value,unit=measure.querySelector("[data-measure-unit]").value,currency=measure.querySelector("[data-measure-currency]").value||null,aggregation=measure.querySelector("[data-measure-aggregation]").value,meaning=measure.querySelector("[data-measure-meaning]").value.trim();
        if(!Object.hasOwn(fields,role)||Object.hasOwn(measures,role))throw new Error("Choose a unique mapped value role for each measure policy.");
        if(!measureUnits.some(item=>item.value===unit)||!measureAggregations.some(item=>item.value===aggregation))throw new Error("Choose a declared measure unit and aggregation policy.");
        if((currency!==null&&(!Object.hasOwn(fields,currency)||currency===role))||(unit==="number"&&currency!==null)||(["currency","minor_currency"].includes(unit)&&currency===null))throw new Error("Monetary measures need a different mapped currency role; nonmonetary measures have none.");
        if(!meaning||[...meaning].length>2000||/[\u0000-\u001f\u007f]/.test(meaning))throw new Error("Supply a single-line measure meaning and review basis of at most 2,000 characters.");
        measures[role]={unit,currency_field:currency,aggregation,meaning};
      }
      const extra=temporalInput.value.trim()?JSON.parse(temporalInput.value):{};
      if(!extra||typeof extra!=="object"||Array.isArray(extra)||Object.keys(extra).some(key=>!["passages","attachments"].includes(key))||Object.values(extra).some(value=>!Array.isArray(value)))throw new Error("Supply only passages and attachments arrays in the timestamp mapping.");
      return {connection:tables.find(item=>item.table===table.value).connection,entity:entity.value.trim(),table:table.value,kind:kind.value,record_id:record.value,customer_ref:{connection:refConnection.value,entity:refEntity.value.trim(),column:refColumn.value},fields,...(Object.keys(measures).length?{measures}:{}),...extra};
    };
    $("binding-list").append(row);
  }
  function renderDefinitionDraft() {
    replace($("definition-draft"),...state.definitions.map((definition,index)=>{const row=definitionSummary(definition,{draft:true});row.append(button("Remove",()=>{state.definitions.splice(index,1);renderDefinitionDraft();},"button text-button small"));return row;}));
  }

  function selectedSample() {return document.querySelector("input[name=sample-profile]:checked").value;}
  async function connectSample(phase) {
    await run("sample.connect",{profile:selectedSample(),phase},result=>{
      for(const name of list(result.source_names))state.selectedSources.add(name);
      state.plan=null;state.snapshot=null;state.inspection=null;renderScope();renderSources();navigate("review");
      $("prepare-sample").hidden=false;$("sample-mapping").hidden=true;
      $("review-empty").querySelector("h2").textContent="Your sample data is extracted.";
      $("review-empty").querySelector("p").textContent="Prepare its review to inspect explicit identity links. Nothing has been approved.";
    });
  }
  function togglePanel(id,buttonId) {
    const panel=$(id);panel.hidden=!panel.hidden;$(buttonId).setAttribute("aria-expanded",String(!panel.hidden));if(!panel.hidden)panel.scrollIntoView({block:"nearest"});
  }
  function attach() {
    all("[data-view]").forEach(node=>node.addEventListener("click",()=>navigate(node.dataset.view,true)));
    all("[data-go]").forEach(node=>node.addEventListener("click",()=>navigate(node.dataset.go,true)));
    all("[data-review]").forEach(node=>{
      node.addEventListener("click",()=>reviewTab(node.dataset.review));
      node.addEventListener("keydown",event=>{const tabs=["identity","mapping","context"];let index=tabs.indexOf(node.dataset.review);if(event.key==="ArrowRight")index=(index+1)%3;else if(event.key==="ArrowLeft")index=(index+2)%3;else if(event.key==="Home")index=0;else if(event.key==="End")index=2;else return;event.preventDefault();reviewTab(tabs[index],true);});
    });
    all("[data-review-go]").forEach(node=>node.addEventListener("click",()=>reviewTab(node.dataset.reviewGo,true)));
    $("reload-state").addEventListener("click",()=>guarded(async()=>{clearError();await refreshState();}));
    $("check-job").addEventListener("click",()=>guarded(pollJob));
    $("toggle-connect").addEventListener("click",()=>togglePanel("connect-panel","toggle-connect"));
    $("close-connect").addEventListener("click",()=>{$("connect-panel").hidden=true;$("toggle-connect").setAttribute("aria-expanded","false");$("toggle-connect").focus();});
    $("source-type").addEventListener("change",()=>guarded(loadConnector));
    $("sample-form").addEventListener("submit",event=>{event.preventDefault();guarded(()=>connectSample("initial"));});
    $("refresh-sample").addEventListener("click",()=>guarded(()=>connectSample("refreshed")));
    $("source-form").addEventListener("submit",event=>{
      event.preventDefault();guarded(async()=>{
        if(!state.connector)throw new Error("Choose a connector and wait for its requirements.");
        if(list(state.connector.resources).length&&!selectedResources().length)throw new Error("Select at least one resource to save this connection.");
        const submittedConnector=state.connector,submittedSelection=selectionKey();
        if(state.connectorPlanKey!==selectionKey())await loadSelectionPlan();
        if(submittedConnector!==state.connector||submittedSelection!==selectionKey())throw new Error("The connector selection changed while checking dependencies. Review it and save again.");
        if(state.connectorPlanKey!==selectionKey())throw new Error("Wait for the selected resource dependencies before saving.");
        if(!$("source-form").reportValidity())return;
        const credentials={};for(const node of all("[data-credential]"))if(node.value.trim())credentials[node.dataset.credential]=node.value.trim();
        let params={};if($("source-params").value.trim()){try{params=JSON.parse($("source-params").value);}catch{throw new Error("Advanced parameters must be valid JSON.");}if(!params||typeof params!=="object"||Array.isArray(params))throw new Error("Advanced parameters must be an object.");}
        for(const node of all("[data-param]",$("parameter-fields")))if(node.value.trim()){if(!params[node.dataset.resource])params[node.dataset.resource]={};if(typeof params[node.dataset.resource]!=="object"||Array.isArray(params[node.dataset.resource]))throw new Error("Resource parameters must be objects.");params[node.dataset.resource][node.dataset.param]=node.value.trim();}
        const resources=selectedResources();
        const args={source_type:$("source-type").value,name:$("source-name").value.trim(),credential_env:credentials,params};
        if(list(state.connector.resources).length)args.resources=resources;
        if(["csv","parquet"].includes(args.source_type))args.path=$("source-path").value.trim();
        await run("source.add",args,()=>{$("connect-panel").hidden=true;$("toggle-connect").setAttribute("aria-expanded","false");});
      });
    });
    $("capture-snapshot").addEventListener("click",()=>guarded(()=>{
      const connections=[...state.selectedSources];if(!connections.length)throw new Error("Select at least one committed source in the source list.");
      return run("snapshot",{connections},result=>{adoptSnapshot(result.snapshot_id);$("snapshot-summary").textContent="Snapshot captured: "+result.snapshot_id;$("snapshot-summary").hidden=false;navigate("review");});
    }));
    $("prepare-sample").addEventListener("click",()=>guarded(()=>run("sample.prepare",{profile:state.workspace.sample?.profile||selectedSample()},showPlan)));
    $("customer-form").addEventListener("submit",event=>{event.preventDefault();guarded(()=>run("customer.create",{label:$("customer-label").value.trim()},()=>{$("customer-label").value="";}));});
    $("link-form").addEventListener("submit",event=>{
      event.preventDefault();guarded(()=>run("links.propose",{links:[{connection:$("link-connection").value,entity:$("link-entity").value.trim(),source_id:$("link-source-id").value.trim(),customer_id:$("link-customer").value,basis:$("link-basis").value.trim()}]}));
    });
    $("profile-select").addEventListener("change",()=>{
      state.profile=$("profile-select").value;invalidateAnswer();
      const profile=list(state.workspace.profiles).find(item=>item.name===state.profile);if(profile?.materialization?.snapshot_id)adoptSnapshot(profile.materialization.snapshot_id);
    });
    $("inspect-sources").addEventListener("click",()=>guarded(()=>run("source.inspect",{snapshot_id:scope().snapshot_id,sample_rows:3},inspectResult)));
    $("add-binding").addEventListener("click",()=>guarded(addBinding));
    $("mapping-form").addEventListener("submit",event=>{
      event.preventDefault();guarded(()=>{
        const bindings=all(".binding",$("binding-list")).map(node=>node.readBinding());
        if(!bindings.length)throw new Error("Add at least one observed table binding.");
        const profile={spec_version:"oriel.customer360.profile/v1",name:$("mapping-name").value.trim(),description:$("mapping-description").value.trim(),bindings};
        state.profile=profile.name;return run("profile.propose",{profile,snapshot_id:scope().snapshot_id});
      });
    });
    $("materialize").addEventListener("click",()=>guarded(()=>run("materialize",scope(),result=>{
      $("materialization-status").textContent="Customer view built from this snapshot · "+(result.row_count??"")+" projected records. Review definitions next.";
      $("materialization-status").hidden=false;reviewTab("context");
    })));
    $("bootstrap-context").addEventListener("click",()=>guarded(()=>{const intent=$("bootstrap-intent").value.trim();return run("context.bootstrap",{...scope(),...modelOptions(),...(intent?{intent}: {})},result=>{
      const usage=result.usage||{};
      $("job-detail").textContent="Candidate only · "+(usage.model_calls??"?")+" model calls · reported cost "+(usage.reported_cost_usd===null?"unknown":"$"+(usage.reported_cost_usd??usage.known_cost_usd??"?"))+". Review the proposal below.";
    });}));
    $("sample-context").addEventListener("click",()=>guarded(()=>{
      if(!state.plan)throw new Error("Prepare the sample review first to inspect its authored definitions.");
      return run("context.propose",{...scope(),definitions:state.plan.definitions,gaps:state.plan.gaps||[]});
    }));
    $("show-context").addEventListener("click",()=>guarded(()=>run("context.show",scope(),renderContext)));
    $("toggle-definition").addEventListener("click",()=>togglePanel("definition-builder","toggle-definition"));
    $("definition-operation").addEventListener("change",()=>{
      const count=$("definition-operation").value==="count";$("definition-field").disabled=count;$("definition-currency").disabled=count;
      $("definition-field").required=!count;if(count){$("definition-field").value="";$("definition-currency").value="";$("definition-unit").value="records";}else if($("definition-unit").value==="records")$("definition-unit").value="number";
    });
    $("definition-builder").addEventListener("submit",event=>{
      event.preventDefault();guarded(()=>{
        const operation=$("definition-operation").value,filters=[],filter=$("filter-field").value.trim();
        if(filter){
          let value=$("filter-value").value;
          if($("filter-type").value==="number")value=numericFilter(value);
          if($("filter-type").value==="boolean"){if(!["true","false"].includes(value))throw new Error("Boolean filters must be exactly true or false.");value=value==="true";}
          filters.push({field:filter,operator:"eq",value});
        }
        const definition={name:$("definition-name").value.trim(),description:$("definition-description").value.trim(),kind:$("definition-kind").value,operation,field:operation==="count"?null:$("definition-field").value.trim(),filters,unit:operation==="count"?"records":$("definition-unit").value,currency_field:operation==="count"?null:($("definition-currency").value.trim()||null)};
        if(state.definitions.some(item=>item.name===definition.name))throw new Error("Use a unique definition name.");
        state.definitions.push(definition);renderDefinitionDraft();$("definition-name").value="";$("definition-description").value="";
      });
    });
    $("propose-definitions").addEventListener("click",()=>guarded(()=>{
      if(!state.definitions.length)throw new Error("Add at least one definition to the draft.");
      return run("context.propose",{...scope(),definitions:state.definitions,gaps:[]});
    }));
    $("customer-search").addEventListener("input",renderCustomers);
    $("load-brief").addEventListener("click",()=>guarded(()=>run("brief",{...scope(true),fields:["text","updates"],quote_limit:5},result=>renderAnswer(result))));
    $("save-review").addEventListener("click",()=>guarded(()=>run("review.save",{...scope(true),fields:["text","updates"],quote_limit:5},result=>{
      renderReviews([result]);$("job-detail").textContent="Saved a newly validated brief. It remains a historical review after sources change.";
    })));
    $("list-reviews").addEventListener("click",()=>guarded(()=>run("review.list",{profile_name:scope(true).profile_name,customer_id:state.customer},renderReviews)));
    $("ask-settings").addEventListener("click",()=>{navigate("review");reviewTab("context");$("model-settings").scrollIntoView({block:"center"});$("model-consent").focus();});
    $("model-consent").addEventListener("change",()=>{
      $("ask-consent-status").textContent=$("model-consent").checked?"Model permission is on for an explicitly started run. Review the budget before asking.":"Model permission is off. Your question will not be sent until you opt in.";
    });
    $("ask-form").addEventListener("submit",event=>{
      event.preventDefault();guarded(()=>run("ask",{...scope(true),question:$("question").value.trim(),...modelOptions()},result=>{
        if(!["answered","abstained"].includes(result.status)||result.receipt_persisted!==true||!result.answer)throw new Error("No accepted, retained answer was returned. Inspect the run receipt before trying again.");
        renderAnswer(result.answer,{runResult:result});
      }));
    });
    $("close-evidence").addEventListener("click",()=>$("evidence-dialog").close());
    $("definition-operation").dispatchEvent(new Event("change"));
  }
  async function initialize() {
    attach();navigate(location.hash.slice(1)||"sources");
    try {
      const session=await api("/api/session");state.csrf=session.csrf;
      $("model-origin").textContent=session.model_origin==="scripted_agent_test"?"Scripted test transport · no actual model":"Configured Claude CLI · selected data leaves this workspace";
      $("model-budget").max="1";
      const [catalog]=await Promise.all([api("/api/catalog"),refreshState()]);
      state.catalog=list(catalog.connectors);setSelect($("source-type"),state.catalog.map(item=>({value:item.name,label:item.name})),null,"Choose a connector");
      $("toggle-connect").textContent="+ Connect a tool · "+state.catalog.length;
    } catch(error){$("session-status").textContent="Local session unavailable";showError(error);}
  }
  initialize();
})();
