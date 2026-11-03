"use strict";

const $ = (id) => document.getElementById(id);
const motorNames = ["Thumb · abduction", "Thumb · flexion", "Thumb · tendon", "Index finger", "Middle finger", "Ring finger", "Pinky finger"];
const primaryPoses = ["open", "pinch", "grab", "point", "peace", "middle_finger"];
const captions = {open:"Open palm",pinch:"Thumb + index",grab:"Power grasp",point:"Index extended",peace:"Two fingers",middle_finger:"Middle extended",pinch_middle:"Thumb + middle",pinch_ring:"Thumb + ring",pinch_pinky:"Thumb + pinky"};
const stateLabels = {starting:"Starting",opening:"Opening hand",zeroing:"Zeroing sensors",ready:"Ready",active:"Moving",hold:"Holding",stopping:"Stopping",stopped:"Stopped",error:"Attention"};
let poses = new Map();
let poseButtons = new Map();
let snapshot = null;
let connected = false;
let revision = -1;
let commandGeneration = 0;
let pendingCommand = null;
let requestCount = 0;
let amountTouched = false;
let displayedIcon = null;
let terminalState = false;
let commandError = "";

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function handIcon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const flags = {open:[1,1,1,1],hold:[1,1,1,1],grab:[0,0,0,0],pinch:[0,1,1,1],pinch_middle:[1,0,1,1],pinch_ring:[1,1,0,1],pinch_pinky:[1,1,1,0],point:[1,0,0,0],peace:[1,1,0,0],middle_finger:[0,1,0,0]}[name] || [1,1,1,1];
  const add = (path) => {const p = document.createElementNS(ns,"path"); p.setAttribute("d",path); svg.append(p);};
  add("M21 35v10c0 8 5 13 15 13s17-6 17-16V31");
  const xs = [23,32,41,50];
  const tops = [13,6,10,19];
  xs.forEach((x,i) => {
    if (flags[i]) add(`M${x-3} 35V${tops[i]+3}q0-3 3-3t3 3v20`);
    else add(`M${x-3} 35v-7q0-3 3-3t3 3v7`);
  });
  if (name.startsWith("pinch")) add("M21 43l-8-10c-4-6 1-11 6-7l8 7");
  else if (name === "open" || name === "hold") add("M21 44 10 32c-4-5-1-10 4-7l10 8");
  else add("M21 43l-5-7c-3-5 0-8 4-6l12 8");
  return svg;
}

function buildPoses(items) {
  if (!Array.isArray(items)) return;
  const next = items.filter((p) => p && typeof p.name === "string" && typeof p.label === "string");
  if (JSON.stringify(next) === JSON.stringify([...poses.values()])) return;
  poses = new Map(next.map((p) => [p.name,p]));
  poseButtons = new Map();
  $("pose-grid").replaceChildren();
  const order = [...primaryPoses.filter((name) => poses.has(name)), ...next.map((p) => p.name).filter((name) => !primaryPoses.includes(name))];
  order.forEach((name) => {
    const pose = poses.get(name);
    const button = element("button","pose-button");
    button.type = "button";
    button.dataset.command = name;
    button.title = pose.description || pose.label;
    button.setAttribute("aria-pressed","false");
    const icon = element("span","pose-icon"); icon.append(handIcon(name));
    const copy = element("span","pose-button-copy");
    copy.append(element("span","pose-button-label",pose.label),element("span","pose-button-meta",captions[name] || "Goal posture"));
    button.append(icon,copy);
    const quickIndex = primaryPoses.indexOf(name);
    if (quickIndex >= 0) button.append(element("kbd",null,String(quickIndex+1)));
    button.addEventListener("click", () => sendCommand(name));
    $("pose-grid").append(button);
    poseButtons.set(name,button);
  });
}

const motorRows = motorNames.map((name) => {
  const row = element("div","motor-row unavailable");
  const heading = element("div","motor-row-heading");
  const values = element("span","motor-values");
  const measured = element("span","measured","—");
  const goal = element("span","goal","—");
  values.append(measured,element("span","arrow","→"),goal);
  heading.append(element("span","motor-label",name),values);
  const track = element("div","motor-track");
  const fill = element("span","motor-fill");
  const marker = element("span","motor-goal");
  track.append(fill,marker); row.append(heading,track); $("motor-list").append(row);
  return {row,measured,goal,fill,marker};
});

function setAmount() {
  const amount = Number($("amount").value);
  $("amount-output").replaceChildren(document.createTextNode(String(amount)),element("span",null,"%"));
  $("amount").style.setProperty("--amount-fill",`${amount}%`);
}
$("amount").addEventListener("input",() => {amountTouched = true; setAmount();});

function prettyCommand(name) {
  if (name === "hold") return "Hold";
  return poses.get(name)?.label || String(name || "Hold").replaceAll("_"," ").replace(/^./,(c) => c.toUpperCase());
}

function setConnection(isConnected) {
  connected = isConnected;
  const el = $("connection");
  el.classList.toggle("connected",isConnected);
  el.classList.toggle("disconnected",!isConnected);
  el.querySelector("span").textContent = isConnected ? "Connected" : (terminalState ? "Session ended" : "Disconnected");
  updateControls();
}

function updateControls() {
  const status = snapshot?.status || "starting";
  const ready = connected && !snapshot?.baseline_capture?.active && (snapshot?.mode === "preview" || snapshot?.tactile?.fresh) && ["ready","active","hold"].includes(status);
  poseButtons.forEach((button,name) => {
    button.disabled = !ready;
    button.classList.toggle("selected",snapshot?.command === name);
    button.classList.toggle("pending",pendingCommand === name && snapshot?.command !== name);
    button.setAttribute("aria-pressed",String(snapshot?.command === name));
  });
  $("amount").disabled = !ready;
  $("hold-button").disabled = !connected || ["stopping","stopped","error"].includes(status);
  $("stop-button").disabled = !connected || ["stopping","stopped","error"].includes(status);
  const manualReady = connected && status === "hold" && snapshot?.thumb_setup?.can_adjust
    && snapshot?.mode !== "observe" && !snapshot?.hold_pending && !snapshot?.baseline_capture?.active;
  ["thumb-angle","thumb-slider","thumb-minus","thumb-plus","thumb-apply","thumb-current"].forEach(id => $(id).disabled = !manualReady);
}

function render(next) {
  if (!next || typeof next !== "object" || !Number.isInteger(next.revision) || next.revision < revision) return;
  revision = next.revision;
  if (Number.isInteger(next.generation)) commandGeneration = Math.max(commandGeneration,next.generation);
  snapshot = next;
  const preview = next.mode === "preview";
  const status = next.status || "starting";
  terminalState = ["stopped","error"].includes(status);
  buildPoses(next.poses);
  document.body.classList.toggle("preview-mode",preview);
  $("preview-badge").hidden = !preview;
  $("device").textContent = preview ? "No hardware connected" : (next.device || "Waiting for controller");
  $("version").textContent = next.version ? `v${next.version}` : "";
  $("current-command").textContent = prettyCommand(next.command);
  $("current-command").classList.toggle("long",prettyCommand(next.command).length > 10);
  document.title = `${prettyCommand(next.command)} · Artificial Brains`;
  $("phase").textContent = preview && !terminalState && status !== "stopping" ? "Preview" : (next.at_target && status === "active" ? "At target" : stateLabels[status] || status);
  $("command-description").textContent = next.command === "hold" ? "Keep the hand at its current position." : (captions[next.command] || poses.get(next.command)?.description || "Custom target posture");
  if (displayedIcon !== next.command) {
    $("hero-icon").replaceChildren(handIcon(next.command || "hold"));
    displayedIcon = next.command;
  }
  $("command-panel").classList.toggle("is-error",status === "error");
  $("command-panel").classList.toggle("is-stopped",status === "stopped");
  $("target-badge").hidden = !next.at_target || preview || status !== "active";
  const initializing = ["opening", "zeroing"].includes(status);
  $("startup-note").hidden = !initializing;
  if (initializing) {
    $("startup-note").textContent = status === "opening"
      ? "OPENING BEFORE BRAIN START · Keep the hand empty and the pads clear. Hold or Stop cancels startup."
      : `ZEROING UNLOADED SENSORS · ${next.startup?.samples || 0} / 50 samples. Neural control has not started.`;
    $("current-command").textContent = status === "opening" ? "Open hand" : "Zero sensors";
    $("current-command").classList.add("long");
    $("command-description").textContent = status === "opening"
      ? "Opening six motors while thumb abduction stays at its manual setpoint."
      : "Measuring idle offsets and noise, not a grip-force target.";
  }

  let detail = preview ? "Interface preview · no hand or brain connected" : status === "hold" ? "Position held · choose a pose when ready" : status === "active" ? "Live neural control" : status === "stopped" ? "Session ended" : status === "stopping" ? "Holding and saving neural dynamics…" : status === "error" ? "Controller stopped with an error" : "Waiting for the controller…";
  if (initializing) detail = "Body initialization · brain not started";
  if (status === "active" && Number.isFinite(next.amount)) detail += ` · ${Math.round(next.amount*100)}% posture`;
  $("command-detail").textContent = detail;
  if (!amountTouched && Number.isFinite(next.amount)) {$("amount").value = String(Math.round(next.amount*100)); setAmount();}
  if (pendingCommand === next.command || ["hold","stopping","stopped","error"].includes(status)) pendingCommand = null;
  const error = next.error ? String(next.error) : commandError;
  $("error-banner").textContent = error;
  $("error-banner").hidden = !error;
  $("message").textContent = next.message || (preview ? "Interface preview — no hand or brain connected." : "Choose a pose to send its target posture.");
  $("session-title").textContent = preview ? "Preview session" : ({starting:"Connecting to the hand",opening:"Brain not started",zeroing:"Brain not started",ready:"Hand ready",hold:"Hand holding",active:"Neural control active",stopping:"Saving neural dynamics",stopped:"Session stopped",error:"Controller needs attention"}[status] || "Controller status");
  $("session-dot").className = `status-dot${status === "error" ? " error" : ["ready","hold","active"].includes(status) ? " active" : ""}`;
  $("stop-button").lastChild.textContent = preview ? "Close preview" : "Stop brain";
  document.querySelector(".stop-help").textContent = preview ? "Ends this interface preview." : "Holds the hand and saves neural dynamics.";
  $("feedback-note").textContent = preview ? "Motor readings appear when a real hand is connected. Preview sends no motor commands." : "Measured actuator angles; reaching a pose does not establish object contact.";
  motorRows.forEach((entry,i) => {
    const position = !preview && next.position?.[i];
    const goal = !preview && next.goal?.[i];
    const bounds = next.limits?.[i];
    const measuredValid = !preview && Number.isFinite(position);
    const goalValid = !preview && Number.isFinite(goal);
    entry.measured.textContent = measuredValid ? position.toFixed(1) : "—";
    entry.goal.textContent = goalValid ? goal.toFixed(1) : "—";
    const valid = measuredValid && goalValid && Array.isArray(bounds) && bounds.length === 2 && bounds.every(Number.isFinite) && bounds[1] > bounds[0];
    entry.row.classList.toggle("unavailable",!valid);
    const percent = (value) => Math.max(0,Math.min(100,(value-bounds[0])/(bounds[1]-bounds[0])*100));
    entry.fill.style.width = valid ? `${percent(position)}%` : "0%";
    entry.marker.style.left = valid ? `${percent(goal)}%` : "0%";
  });
  renderTactile(next);
  renderDiagnostics(next);
  renderThumb(next);
  updateControls();
}

async function sendCommand(command) {
  if (!connected) return;
  if (poses.has(command) && !["ready","active","hold"].includes(snapshot?.status)) return;
  const payload = {command};
  if (command === "calibrate_zero") payload.generation = commandGeneration;
  if (poses.has(command)) {
    payload.amount = Number($("amount").value)/100;
    payload.generation = commandGeneration;
  }
  const requestId = ++requestCount;
  if (command === "hold" || command === "quit") pendingCommand = null;
  else pendingCommand = command;
  updateControls();
  try {
    const response = await fetch("api/command",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),cache:"no-store",signal:AbortSignal.timeout(3000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The command was not accepted.");
    if (Number.isInteger(result.generation)) commandGeneration = Math.max(commandGeneration,result.generation);
    commandError = "";
    if (requestId === requestCount && !terminalState) $("message").textContent = command === "quit" ? "Stop requested. Waiting for the controller to finish." : `${prettyCommand(command)} requested.`;
  } catch (error) {
    if (requestId !== requestCount) return;
    pendingCommand = null;
    commandError = `Command not confirmed: ${error.message}`;
    $("error-banner").hidden = false;
    $("error-banner").textContent = commandError;
    updateControls();
  }
}

$("hold-button").addEventListener("click",() => sendCommand("hold"));
$("stop-button").addEventListener("click",() => sendCommand("quit"));
document.addEventListener("keydown",(event) => {
  const target = event.target;
  if (event.altKey || event.ctrlKey || event.metaKey || target instanceof HTMLElement && (target.isContentEditable || ["INPUT","TEXTAREA","SELECT"].includes(target.tagName))) return;
  if (event.code === "Space") {
    event.preventDefault();
    if (!event.repeat && !$("hold-button").disabled) sendCommand("hold");
    return;
  }
  if (event.repeat || target instanceof HTMLElement && target.tagName === "BUTTON") return;
  const index = Number(event.key)-1;
  if (event.key.length === 1 && index >= 0 && index < primaryPoses.length && poseButtons.has(primaryPoses[index]) && !poseButtons.get(primaryPoses[index]).disabled) sendCommand(primaryPoses[index]);
});
window.addEventListener("pagehide",() => {
  if (!connected || ["stopping","stopped","error"].includes(snapshot?.status)) return;
  navigator.sendBeacon("api/leave",new Blob(["{}"],{type:"application/json"}));
});

async function poll() {
  try {
    const response = await fetch("api/state",{cache:"no-store",signal:AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error("Controller state unavailable");
    const next = await response.json();
    render(next);
    setConnection(true);
  } catch (_error) {
    setConnection(false);
    if (!terminalState) {
      $("error-banner").hidden = false;
      $("error-banner").textContent = "Connection to the controller was lost. Commands are unavailable until it reconnects.";
      $("phase").textContent = "Disconnected";
    }
  }
  if (!terminalState || connected) window.setTimeout(poll,200);
}

$("hero-icon").append(handIcon("hold"));
setAmount();
poll();

// Tactile readouts display ADC and neural HOLD separately. They do not derive
// a motor command from an ADC threshold in JavaScript.
const fingers = ["thumb", "index", "middle", "ring", "little"];
const sensorCards = new Map();
for (const finger of fingers) {
  const card = element("article", "tactile-card");
  card.append(element("h4", "", finger[0].toUpperCase()+finger.slice(1)));
  const adc = element("div", "adc-number", "--"); card.append(adc);
  const bar = document.createElement("progress"); bar.max=1;bar.value=0;card.append(bar);
  const signal = element("div", "sensor-values", "Input signal: --");card.append(signal);
  const zero = element("div", "sensor-zero", "Zero: --");card.append(zero);
  const hold = element("div", "neural-hold", "Neural HOLD: not observed");card.append(hold);
  $("tactile-grid").append(card);sensorCards.set(finger,{adc,bar,signal,hold,zero});
}
const targetInputs = motorNames.map((name)=>{
  const label=element("label","",name);const input=document.createElement("input");
  input.type="number";input.step="0.1";input.placeholder="degrees";label.append(input);$("target-inputs").append(label);return input;
});
$("use-measured").addEventListener("click",()=>targetInputs.forEach((el,i)=>{
  const value = i===0 && snapshot?.thumb_setup?.locked ? snapshot.thumb_setup.target : snapshot?.position?.[i];
  if(Number.isFinite(value))el.value=value.toFixed(2);
}));
$("send-targets").addEventListener("click",()=>{
  if(targetInputs.some(el=>el.value.trim()==="")){commandError="Enter all seven target angles.";$("error-banner").textContent=commandError;$("error-banner").hidden=false;return;}
  sendPayload({command:"targets",values:targetInputs.map(el=>Number(el.value)),generation:commandGeneration});
});
async function sendPayload(payload){
  if(!connected)return;
  try{
    const response=await fetch("api/command",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),signal:AbortSignal.timeout(3000)});
    const answer=await response.json();if(!response.ok)throw new Error(answer.error||"Request rejected");
    commandError="";
    if(Number.isInteger(answer.generation)) commandGeneration=Math.max(commandGeneration,answer.generation);
    return true;
  }catch(error){commandError=error.message;$("error-banner").textContent=commandError;$("error-banner").hidden=false;return false;}
}
function renderTactile(state){
  const t=state.tactile;const fresh=!!t?.fresh;
  $("tactile-link").textContent=state.mode==="preview"?"Preview data":fresh?"Live ADC":t?.adc?"Last received ADC":"No tactile sample";
  $("send-targets").disabled=!connected||!fresh||!!state.baseline_capture?.active||!["hold","ready","active"].includes(state.status);
  $("use-measured").disabled=!Array.isArray(state.position);
  for(const [finger,card] of sensorCards){
    const raw=t?.adc?.[finger];card.adc.textContent=Number.isFinite(raw)?String(raw):"--";
    const zero=t?.baseline?.[finger];const direction=t?.directions?.[finger]??1;
    card.zero.textContent=Number.isFinite(zero)?`Zero: ${zero.toFixed(1)} · Change: ${Number.isFinite(raw)?(direction*(raw-zero)).toFixed(1):"--"}`:"Zero: --";
    const signal=t?.normalized?.[finger];card.bar.value=Number.isFinite(signal)?signal:0;
    card.signal.textContent=Number.isFinite(signal)?`Input signal: ${signal.toFixed(3)} / 1.000`:"Input signal: --";
    const active=!!t?.holds?.[finger];card.hold.textContent=state.step>=0?`Neural HOLD: ${active?"ACTIVE":"off"}`:"Neural HOLD: not observed";
    card.hold.classList.toggle("active",active);
  }
  const capture=state.baseline_capture;
  $("capture-zero").disabled=!connected||!fresh||state.status!=="hold"||!!state.hold_pending||!!capture?.active;
  $("zero-status").textContent=capture?.active?`Zeroing: ${capture.samples} / ${capture.required} fresh samples. Keep pads unloaded.`:"Select Hold. Keep all five pads free of contact. This is sensor zeroing, not a grip threshold.";
  $("tactile-note").textContent="Zero-corrected contact observations feed the brain. Its tactile pathway opposes FLEX and emits grip-HOLD. No host pressure threshold. EXTEND remains available.";
  if(!fresh)$("tactile-note").textContent+=" Waiting for a current sensor sample; readings are not replaced by zero.";
  if(t?.error)$("tactile-note").textContent+=" Sensor link: "+t.error;
}

$("capture-zero").addEventListener("click",()=>sendCommand("calibrate_zero"));
const motorKeys=["thumb_cmc_abd","thumb_cmc_flex","thumb_tendon","index_tendon","middle_tendon","ring_tendon","pinky_tendon"];
const diagnosticRows=motorKeys.map((key,i)=>{
  const row=element("tr");const cells=Array.from({length:6},()=>element("td"));
  cells[0].textContent=motorNames[i];cells.forEach(c=>row.append(c));$("diagnostic-rows").append(row);return cells;
});
function renderDiagnostics(state){
  const d=state.diagnostics;
  $("output-rate").textContent=Number.isFinite(d?.output_hz)?`${d.output_hz} output frames/s`:"No output yet";
  $("speed-note").textContent=`Since the latest command selection. Movement gain: ${Number.isFinite(state.movement_gain)?state.movement_gain.toFixed(1):"1.0"}×. FLEX and EXTEND are shown separately; both may cancel. Applied increments are target changes, not measured physical travel.`;
  motorKeys.forEach((key,i)=>{
    const m=d?.motors?.[key];const c=diagnosticRows[i];
    c[1].textContent=m?`${m.flex_count} / ${m.extend_count}`:"--";
    c[2].textContent=m?String(m.both_active_frames):"--";
    c[3].textContent=m?String(m.grip_hold_frames):"--";
    c[4].textContent=m?`${m.requested_abs_degrees.toFixed(1)} / ${m.applied_abs_degrees.toFixed(1)}`:"--";
    c[5].textContent=m?(m.last_limits?.join(", ")||m.last_disposition||"none"):"--";
  });
  const serial=state.serial_stats;
  $("serial-note").textContent=serial?`Serial reads: ${serial.reads}. Recent packets reused: ${serial.reused}. Last read: ${serial.last_read_ms.toFixed(1)} ms.`:"";
  const l=state.logging;
  $("log-path").textContent=l?.directory||"No physical session in preview";
  const drops=Object.values(l?.dropped||{}).reduce((a,b)=>a+b,0);
  $("log-status").textContent=l?`${l.recording?"Recording":"Recording stopped"} · ${(l.bytes/1048576).toFixed(1)} MiB · ${drops} records not written${l.error?" · "+l.error:""}${l.size_limit_reached?" · Run log size limit reached":""}`:"Session files are created automatically when hardware starts.";
  const n=state.neuron_logging;
  $("neuron-log-status").textContent=n?`Neuron stream: ${n.status} · ${n.frames||0} frames${n.error?" · "+n.error:""}`:"Full neuron data unavailable in preview. No data are invented.";
}


// Manual abduction setup is an operator exception, not a browser motor policy.
let thumbEdited = false;
let pendingThumbAngle = null;
function thumbInput(value) {
  $("thumb-angle").value = String(value);
  $("thumb-slider").value = String(value);
  thumbEdited = true;
}
$("thumb-angle").addEventListener("input",()=>{thumbEdited=true;$("thumb-slider").value=$("thumb-angle").value;});
$("thumb-slider").addEventListener("input",()=>thumbInput($("thumb-slider").value));
async function applyThumb(value) {
  const bounds=snapshot?.thumb_setup?.limits || [0,100];
  if(typeof value!=="number" || !Number.isFinite(value) || value<bounds[0] || value>bounds[1]) {
    commandError=`Choose a thumb angle from ${bounds[0]} to ${bounds[1]} degrees.`;
    $("error-banner").textContent=commandError;$("error-banner").hidden=false;return;
  }
  thumbInput(value);
  pendingThumbAngle=value;
  if(!await sendPayload({command:"thumb_angle",angle:value,generation:commandGeneration})) pendingThumbAngle=null;
}
$("thumb-apply").addEventListener("click",()=>applyThumb($("thumb-angle").value.trim()==="" ? NaN : Number($("thumb-angle").value)));
$("thumb-minus").addEventListener("click",()=>applyThumb(Number($("thumb-angle").value)-1));
$("thumb-plus").addEventListener("click",()=>applyThumb(Number($("thumb-angle").value)+1));
$("thumb-current").addEventListener("click",async()=>{
  pendingThumbAngle=null;thumbEdited=false;
  await sendPayload({command:"thumb_current",generation:commandGeneration});
});
function renderThumb(state) {
  const t=state.thumb_setup;
  const angle=t?.target;
  const format=(x)=>Number.isFinite(x)?`${x.toFixed(1)}°`:"--";
  $("thumb-lock-badge").textContent=t?.locked?"MANUAL · LOCKED":state.mode==="observe"?"READ ONLY":"WAITING FOR HAND";
  const bounds=t?.limits || [0,100];
  for(const id of ["thumb-angle","thumb-slider"]){$(id).min=String(bounds[0]);$(id).max=String(bounds[1]);}
  if(Number.isFinite(pendingThumbAngle) && Number.isFinite(angle) && Math.abs(angle-pendingThumbAngle)<.001){pendingThumbAngle=null;thumbEdited=false;}
  if(Number.isFinite(angle) && !thumbEdited && !["thumb-angle","thumb-slider"].includes(document.activeElement?.id)){
    $("thumb-angle").value=angle.toFixed(1);$("thumb-slider").value=String(angle);
  }
  $("thumb-readout").textContent=`Fixed target: ${format(angle)} · Measured: ${format(t?.measured)} · Saved: ${format(t?.saved)}`;
  $("thumb-setup-help").textContent=state.mode==="observe" ? "Observation mode: no manual motor writes are allowed."
    :state.status==="hold" ? "Adjust while the hand is empty. Apply & save moves the thumb using its coupled tendon geometry. Lock measured position adopts the actual angle. No abduction arrival timeout."
    :"Select Hold to adjust the thumb. Open, Grab and the neural outputs retain this fixed abduction target.";
  if(t?.locked && Number.isFinite(angle)){
    targetInputs[0].value=angle.toFixed(2);targetInputs[0].disabled=true;
    targetInputs[0].title="Controlled only by the Thumb position panel";
    motorRows[0].goal.textContent=angle.toFixed(1);
    const b=state.limits?.[0];
    if(Array.isArray(b) && b.length===2 && b.every(Number.isFinite) && b[1]>b[0]) motorRows[0].marker.style.left=`${Math.max(0,Math.min(100,(angle-b[0])/(b[1]-b[0])*100))}%`;
    diagnosticRows[0][5].textContent="manual thumb lock";
  }
}
