"use strict";
// All editor state is in memory or saved explicitly to a project. No browser storage.
const $ = id => document.getElementById(id);
let project = null, status = {}, currentBuild = null, activeJob = null, frameSeq = -1, lastFlash = null;
let actionBusy = false, pollingJob = false, loadingFrame = false, saving = false;
let buildSnapshot = null;
let projectRevision = 0, projectRequest = 0, projectLoading = false, projectLock = null, refreshRequest = 0;
let networkRevision = 0, buildContext = null, currentBuildContext = null;
const projectControls = ["projects","import","import-file","capture","record","save","build","build-desktop","play","export","name","loops","startup","device-name","family","connector","orientation","width","height","upload-recording","arm-player","desktop-player","download-recording"];
let runtimeReady = false, runtimeIdentity = null, runtimeRevision = 0;
const runtimeHint="后台版本未通过核验：请重启本项目后台服务后刷新；停止电脑回放仍可使用。";
const runtimeActions=["record","capture","import","save","play","export","build","build-desktop","flash","restore","upload-recording","arm-player","desktop-player","download-recording"];
const runtimeDisabled=new Map();
function applyRuntimeGate() {
  for(const id of runtimeActions) {
    const control=$(id);if(!control)continue;
    const locked=projectLock?.controls.find(([element])=>element === control);
    if(!runtimeReady) {
      // Store the state underneath the project lock, not its temporary disabled=true.
      if(!runtimeDisabled.has(control))runtimeDisabled.set(control,locked?locked[1]:control.disabled);
      control.disabled=true;
    } else if(runtimeDisabled.has(control)) {
      control.disabled=Boolean(locked) || runtimeDisabled.get(control);runtimeDisabled.delete(control);
    }
  }
  if(!runtimeReady) $("tools").textContent=runtimeHint;
}
function acceptRuntime(info) {
  const runtime=info?.runtime;
  runtimeReady=Boolean(runtime && runtime.api_version === 1 && runtime.installed_matches_running === true &&
    /^[a-f0-9]{64}$/.test(runtime.build_fingerprint) && /^[a-f0-9]{64}$/.test(runtime.source_root_fingerprint) &&
    Array.isArray(runtime.capabilities) && ["runtime-version-handshake","controller-selection","player-upload","player-control"].every(capability=>runtime.capabilities.includes(capability)));
  const identity=runtimeReady?`${runtime.build_fingerprint}:${runtime.source_root_fingerprint}`:null;
  if(identity !== runtimeIdentity){runtimeIdentity=identity;runtimeRevision++;invalidateBuild();}
  if(!runtimeReady) {invalidateBuild();$("job-state").textContent=runtimeHint;}
  applyRuntimeGate();return runtimeReady;
}
function assertRuntime() { if(!runtimeReady) throw new Error(runtimeHint); }
function projectStamp() { return {revision:projectRevision, snapshot:editorSnapshot()}; }
function projectMatches(stamp) { return !projectLoading && stamp.revision === projectRevision && stamp.snapshot === editorSnapshot(); }
function assertProject(stamp) { if(!projectMatches(stamp)) throw new Error("项目或参数已改变，已取消后续操作，请重新核对并保存"); }
function projectEdited() { projectRevision++; invalidateBuild(); }
function releaseProjectLock(lock) {
  if(projectLock !== lock) return;
  projectLock=null;
  lock.controls.forEach(([control, disabled])=>control.disabled=disabled);
  for(const id of ["device-name","family","connector","orientation","width","height"]) $(id).disabled=Boolean(project);
  // renderSteps may have replaced the controls captured when the lock was acquired.
  document.querySelectorAll("#steps input,#steps button").forEach(control=>control.disabled=false);
  $("record").disabled=!runtimeReady || Boolean(projectLock) || actionBusy || Boolean(status.macro?.playing) || (!status.macro?.recording && !canRecord());
  applyRuntimeGate();
}
async function withProjectLock(fn) {
  assertRuntime();
  if(projectLock || projectLoading) throw new Error("项目正在保存或切换，请等待当前操作结束");
  const controls=[...document.querySelectorAll("#steps input,#steps button"),...projectControls.map(id=>$(id)).filter(Boolean)];
  const lock={controls:controls.map(control=>[control,control.disabled]), job:null};
  projectLock=lock;
  controls.forEach(control=>control.disabled=true);
  try { return await fn(lock); }
  finally { if(!lock.job || activeJob !== lock.job) releaseProjectLock(lock); }
}
function networkSnapshot() { return JSON.stringify({ssid:$("ssid").value,password:$("password").value,host:$("host").value,port:Number($("device-port").value)}); }
function networkMatches(context) { return context && context.networkRevision === networkRevision && context.networkSnapshot === networkSnapshot(); }
function networkEdited() { networkRevision++; invalidateBuild(); }
let historyJobs = [], historyRequest = 0;
let controllerRevision = 0, currentBuildController = null, buildRequestRevision = null;
function selectedController() { return $("controller").value || "usb-s3"; }
function jobController(job) { return job.manifest?.controller || job.controller || "usb-s3"; }
function controllerLabel(value) { return value === "ble-esp32" ? "经典 ESP32 · BLE" : "ESP32-S3 · USB"; }
function transportMatches() { return (status.control_transport || "usb") === (selectedController() === "ble-esp32" ? "ble" : "usb"); }
function updateControllerUI() {
  const ble=selectedController() === "ble-esp32";
  $("controller-badge").textContent=controllerLabel(selectedController())+" · 绝对定位";
  $("controller-preview-hint").textContent=ble?"投屏设备必须是蓝牙控制器配对的同一台设备。BLE 支持点击录制，不提供键盘。":"投屏和 USB/HID 必须连接同一台设备。";
  $("controller-install-hint").textContent="播放器首次安装后，录制单独上传、启用。"+(ble?"离线蓝牙执行不需要 Wi-Fi；电脑录制仍使用下方 Wi-Fi 连接配置。":"启用后，下次插入 USB/HID 执行。");
  $("controller-note").textContent=ble?"新播放器默认没有录制。保持供电，长按 BOOT 2 秒进入持久维护模式后，才能开 COM 上传。校验后先把 iPad 放在录制起始页，再点启用；播放器将退出维护、自动蓝牙连接并倒计时开始。":"新播放器默认没有录制。保持 COM 接电脑，拔下 USB/HID；上传校验后单独点启用，再接入 USB/HID 倒计时执行。修改录制只需重新上传。";
  $("controller-stop-hint").textContent="离线不会识别页面变化或业务成功；网页停止只管电脑回放。"+(ble?"再次操作 COM 前，须保持供电长按 BOOT 2 秒进入维护；打开 COM 可能复位，必须先物理进入持久维护模式。":"离线执行请按 BOOT 或拔掉 USB/HID；不要用 EN/RESET 当停止键。");
}
function message(text) { $("message").textContent = text; $("message").hidden = false; clearTimeout(message.timer); message.timer = setTimeout(() => { $("message").hidden = true; }, 9000); }
async function api(path, data, method = "POST") {
  if(method !== "GET" && !["/api/macro/stop","/api/agent/stop"].includes(path)) assertRuntime();
  const options = {method};
  if (data !== undefined) { options.headers = {"Content-Type":"application/json"}; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText)); }
  return response.json();
}
function on(id, fn) { $(id).addEventListener("click", async () => { try { await fn(); } catch (e) { message(e.message); } }); }
function profile() { return {name:$("device-name").value, family:$("family").value, connector:$("connector").value, orientation:$("orientation").value, width:Number($("width").value), height:Number($("height").value)}; }
function canRecord() { const p = profile(); return status.device && status.screen && transportMatches() && status.pointer_mode === "absolute" && Math.abs((status.aspect || 0) / (p.width / p.height) - 1) <= .03; }
function invalidateBuild() { currentBuild=null;currentBuildController=null;currentBuildContext=null;$("flash").disabled=true;$("download").hidden=true;$("firmware-summary").hidden=true; }
function editorSnapshot(){return project?JSON.stringify({...project,name:$("name").value,loops:Number($("loops").value),startup_ms:Math.round(Number($("startup").value)*1000)}):null;}
function renderSteps() {
  const body = $("steps"); body.replaceChildren();
  if (!project) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 4; cell.textContent = "录制或导入后，在这里逐步改时间。"; return; }
  project.steps.forEach((step, i) => {
    const row = body.insertRow(); row.insertCell().textContent = String(i + 1);
    row.insertCell().textContent = `${(step.x*100).toFixed(1)}%, ${(step.y*100).toFixed(1)}%`;
    const input = document.createElement("input"); input.type = "number"; input.min = ".5"; input.max = "3600"; input.step = ".001"; input.value = String(step.wait_ms/1000); input.setAttribute("aria-label",`第${i+1}步执行前间隔`);
    input.disabled=Boolean(projectLock);
    input.addEventListener("change", () => { if(projectLock)return;step.wait_ms = Math.round(Number(input.value)*1000); projectEdited(); updatePeriod(); });
    row.insertCell().append(input);
    const remove = document.createElement("button"); remove.textContent = "删除"; remove.disabled=Boolean(projectLock); remove.addEventListener("click", () => { if(projectLock)return;project.steps.splice(i,1); projectEdited(); renderSteps(); updatePeriod(); }); row.insertCell().append(remove);
  }); updatePeriod();
}
function updatePeriod() { $("period").textContent = project ? `每轮 ${(project.steps.reduce((sum,s)=>sum+s.wait_ms,0)/1000).toFixed(3)} 秒` : "—"; }
function fillProject(value, lock=null) {
  if(projectLock && projectLock !== lock) throw new Error("项目正在保存，请等待当前操作结束");
  projectRequest++;projectLoading=false;projectEdited();
  project = value; $("name").value = value.name; $("device-name").value = value.device.name;
  for (const key of ["family","connector","orientation","width","height"]) $(key).value = String(value.device[key]);
  $("loops").value = String(value.loops); $("startup").value = String(value.startup_ms/1000); renderSteps();
  for(const id of ["device-name","family","connector","orientation","width","height"]) $(id).disabled=true;
}
async function save(lock=null) {
  if(!lock) return withProjectLock(held=>save(held));
  if(projectLock !== lock) throw new Error("项目锁已失效");
  if (!project) throw new Error("请先保存录制或导入项目");
  if(saving)throw new Error("参数正在保存，请稍候");
  const stamp=projectStamp(), snapshot=JSON.parse(stamp.snapshot);
  saving=true;
  try {
    const saved=await api(`/api/studio/projects/${snapshot.id}`,snapshot,"PUT");
    assertProject(stamp);
    if(saved.id !== snapshot.id) throw new Error("保存响应项目不一致，已取消后续操作");
    project=saved;projectRevision++;renderSteps();
    return JSON.parse(JSON.stringify(saved));
  } finally { saving=false; }
}
async function refresh() {
  const request=++refreshRequest, revision=projectRevision, selection=projectRequest;
  let info;
  try { info=await api("/api/studio/status",undefined,"GET"); }
  catch(error) {if(request === refreshRequest)acceptRuntime(null);throw error;}
  if(request !== refreshRequest) return;
  if(!acceptRuntime(info)) return;
  const selected = project?.id || $("projects").value, port = $("port").value;
  if(revision === projectRevision && selection === projectRequest && !projectLoading) {
  $("projects").replaceChildren(new Option("新建录制", ""));
  info.projects.forEach(p => $("projects").add(new Option(`${p.device.name} · ${p.name} · ${p.steps}步`,p.id)));
  if(project && !info.projects.some(p=>p.id===selected)) $("projects").add(new Option(`${project.device.name} · ${project.name}`,project.id));
  if (project || info.projects.some(p=>p.id===selected)) $("projects").value = selected;
  }
  $("port").replaceChildren(new Option("请选择 COM 口", ""));
  info.ports.forEach(p => $("port").add(new Option(`${p.port} · ${p.description}`,p.port)));
  if (info.ports.some(p=>p.port===port)) $("port").value = port;
  $("tools").textContent = info.tools.arduino && info.tools.esptool ? "编译 / 烧录工具就绪" : "请先运行环境安装脚本";
  if(!$("host").value && info.lan_host){$("host").value=info.lan_host;networkEdited();}
  if($("device-port").value !== String(info.device_port)){ $("device-port").value=String(info.device_port);networkEdited(); }$("device-port").readOnly=true;
  $("empty").textContent=`先切换到电脑录制模式，在目标设备的「屏幕镜像」中选择 ${info.airplay_name || "Apple Recorder"}`;
  if (revision === projectRevision && selection === projectRequest && !projectLock && info.frame && !project) { $("width").value=info.frame.width; $("height").value=info.frame.height; $("orientation").value=info.frame.height>info.frame.width?"portrait":"landscape"; }
  await refreshHistory();applyRuntimeGate();
}
function historySelection() {
  const job = historyJobs.find(item => item.id === $("job-history").value);
  lastFlash = job?.kind === "flash" && job.backup_available ? job.id : null;
  $("restore").disabled = !runtimeReady || !lastFlash || Boolean(activeJob) || historyJobs.some(item => item.running_here);
  $("view-job").disabled = !job;
  const names = {build:"生成固件",flash:"烧录",restore:"恢复",upload:"上传录制", "player-control":"播放器控制"};
  const states = {queued:"排队",running:"执行中",succeeded:"完成",failed:"失败"};
  if (!job) { $("history-detail").textContent="尚无任务。生成或烧录后，记录和可用备份会显示在这里。"; return; }
  const pending = ["queued","running"].includes(job.status) && !job.running_here;
  $("history-detail").textContent = `${names[job.kind] || job.kind} · ${pending ? "未确认结束，请核对任务日志" : states[job.status] || job.status} · ${job.id}${job.board_mac ? " · 板卡 "+job.board_mac : ""}${job.port ? " · "+job.port : ""}。${job.backup_available ? "烧录前备份文件齐全；恢复时还会核对备份和原板。" : job.backup_ready ? "备份文件不完整，无法恢复。" : "此任务没有可用的烧录前备份。"}`;
  if(typeof playerJobWarning === "function") {const warning=playerJobWarning(job);if(warning)$("history-detail").textContent+=" "+warning;}
}
async function refreshHistory({select=null, resume=true} = {}) {
  const request = ++historyRequest;
  const revision = controllerRevision;
  const items = await api("/api/studio/jobs",undefined,"GET");
  if (request !== historyRequest) return;
  historyJobs=items;
  if(typeof fillPlayerMac === "function") fillPlayerMac(items,revision);
  const previous = select || $("job-history").value;
  const chosen = items.find(item=>item.id===previous) || items.find(item=>item.id===activeJob) || items.find(item=>item.backup_available) || items[0];
  $("job-history").replaceChildren(new Option(items.length ? "请选择任务" : "暂无任务", ""));
  const names = {build:"生成",flash:"烧录",restore:"恢复",upload:"上传录制", "player-control":"播放器控制"};
  const states = {queued:"排队",running:"执行中",succeeded:"完成",failed:"失败"};
  for (const job of items) {
    const time = job.created ? new Date(job.created*1000).toLocaleString() : "时间未知";
    $("job-history").add(new Option(`${time} · ${controllerLabel(jobController(job))} · ${names[job.kind] || job.kind} · ${states[job.status] || job.status}${job.backup_available ? " · 有备份" : ""} · ${job.id.slice(0,8)}`,job.id));
  }
  if(chosen) $("job-history").value=chosen.id;
  if (resume && !activeJob) {
    const running=items.find(item=>item.running_here);
    if(running) activeJob=running.id;
  }
  historySelection();
  if(resume && activeJob) await pollJob();
}
function showJob(job) {
  $("job-state").textContent = `${job.stage}${job.error ? "："+job.error : ""}`;
  $("job-log").hidden=!job.log; $("job-log").textContent=job.log || "";
  if(typeof playerJobCompleted === "function")playerJobCompleted(job,true);
}
async function launchBuild(mode) {
  return withProjectLock(async lock=>{
    if(activeJob) throw new Error("请等待当前任务结束");
    const controller=selectedController(), revision=controllerRevision;
    const context={networkRevision,networkSnapshot:networkSnapshot(),runtimeRevision,project:null,job:null};
    if (mode === "offline" && project) await save(lock);
    if(revision !== controllerRevision) throw new Error("控制器已切换，请重新生成对应固件");
    if(!networkMatches(context)) throw new Error("网络配置已改变，请重新生成");
    context.project=projectStamp();
    buildSnapshot=mode === "offline"?editorSnapshot():null;
    const body = {mode, controller, project_id:mode === "offline" && project ? project.id : null};
    if (mode === "desktop" || $("ssid").value) body.network = JSON.parse(context.networkSnapshot);
    invalidateBuild();buildContext=context;buildRequestRevision=revision;
    const job = await api("/api/studio/build",body);
    context.job=job.id;activeJob=job.id;
    // Clearing the submitted secret is programmatic, not a network edit. Never erase a newer password.
    if(body.network && networkMatches(context)) { $("password").value="";context.networkSnapshot=networkSnapshot(); }
    historySelection();await pollJob();
  });
}
async function pollJob() {
  if (!activeJob || pollingJob) return;
  const identity=activeJob;
  pollingJob=true;
  try {
    const job = await api(`/api/studio/jobs/${identity}`,undefined,"GET");
    if(activeJob !== identity) return;
    showJob(job);
    if (["succeeded","failed","unknown"].includes(job.status)) {
      if(typeof playerJobCompleted === "function") playerJobCompleted(job,true);
      await refreshHistory({select:job.kind === "flash" ? job.id : null,resume:false}).catch(e=>message(e.message));
      if(activeJob !== identity)return;
      activeJob=null;historySelection();
      if (job.kind === "build" && job.status === "succeeded") {
        if(!runtimeReady || buildContext?.runtimeRevision !== runtimeRevision) {
          invalidateBuild();$("job-state").textContent="后台版本或生成快照已改变，请重新核验服务并重新生成。";return;
        }
        if(jobController(job) !== selectedController() || (buildRequestRevision !== null && buildRequestRevision !== controllerRevision)) {
          invalidateBuild();$("job-state").textContent="控制器已切换，旧固件不可烧录；请为当前控制器重新生成。";return;
        }
        if(!buildContext || buildContext.job !== job.id || !networkMatches(buildContext)) {
          invalidateBuild();$("job-state").textContent="网络配置已改变或生成快照无法核对，旧固件不可烧录；请重新生成。";return;
        }
        if(!projectMatches(buildContext.project)) {
          invalidateBuild();$("job-state").textContent="生成期间项目或参数已改变，请重新生成。";return;
        }
        if(job.manifest.recording_protocol !== 2 && job.manifest.project_id && (!project || buildSnapshot !== editorSnapshot())){
          invalidateBuild();$("job-state").textContent="生成期间参数已改变，请保存后重新生成，旧固件不会被当作新参数烧录。";return;
        }
        currentBuildContext={...buildContext};currentBuild=job.id;currentBuildController=jobController(job); $("flash").disabled=false; $("download").hidden=false; $("download").href=`/api/studio/jobs/${job.id}/application.bin`;
        $("download").textContent="下载固件";
        $("firmware-summary").hidden=false;$("firmware-summary").textContent=job.manifest.recording_protocol === 2?`通用播放器 v2 · 录制单独上传 · ${controllerLabel(jobController(job))} · SHA256 ${job.manifest.application_sha256.slice(0,12)}…`:job.manifest.mode === "offline" || job.manifest.mode === "legacy-offline"?`${job.manifest.steps}步 · 每轮${(job.manifest.cycle_ms/1000).toFixed(3)}秒 · ${job.manifest.loops || "无限"}轮 · SHA256 ${job.manifest.application_sha256.slice(0,12)}…`:`录制模式 · SHA256 ${job.manifest.application_sha256.slice(0,12)}…`;
        $("job-state").textContent=job.manifest.recording_protocol === 2?"已生成通用播放器 v2。录制单独上传，修改录制无需重新编译固件；核对COM口后再烧录。":`已生成${job.manifest.mode === "offline" || job.manifest.mode === "legacy-offline" ? "离线" : "录制模式"}固件。核对COM口后再烧录。`;
      } else if (job.kind === "flash" && job.status === "succeeded") {
        $("job-state").textContent="烧录和哈希校验成功，烧录前备份可在任务历史中选择恢复。";
      } else if (job.kind === "upload" && job.status === "succeeded") {
        $("job-state").textContent=jobController(job) === "ble-esp32"?"上传并逐字节校验完成，保持维护待机；先把 iPad 放在起始页，再点启用，自动蓝牙连接并倒计时。":"上传并逐字节校验完成，保持待机；点启用后再插USB/HID。";
      } else if (job.kind === "player-control" && job.status === "succeeded") {
        const ble=jobController(job) === "ble-esp32";
        $("job-state").textContent=job.command === "arm"?(ble?"录制已启用并退出维护；将自动蓝牙连接并倒计时开始（启用前须放好 iPad 起始页）。":"录制已启用；现在插入USB/HID，按录制的倒计时开始执行。"):job.command === "desktop"?(ble?"已退出维护并切回电脑录制，开始蓝牙广播；下次操作 COM 前须重新进入维护并确认。":"已切换录制模式，可以插入USB/HID并连接电脑录制。") :"播放器已停止并保持待机。";
      }
      if(typeof playerJobCompleted === "function") playerJobCompleted(job);
    }
  } catch(e) {
    $("job-state").textContent=`任务状态暂时无法读取，将自动重试查询：${e.message}`;
    if(typeof playerJobUnknown === "function") playerJobUnknown(identity);
  }
  finally {
    pollingJob=false;applyRuntimeGate();
    if(projectLock?.job === identity && activeJob !== identity) releaseProjectLock(projectLock);
  }
}
on("refresh-history",()=>refreshHistory());
$("controller").addEventListener("change",()=>{
  controllerRevision++;invalidateBuild();updateControllerUI();
  if(typeof updatePlayerController === "function") updatePlayerController(true);
  $("record").disabled=!runtimeReady || Boolean(projectLock) || actionBusy || Boolean(status.macro?.playing) || (!status.macro?.recording && !canRecord());
});
$("job-history").addEventListener("change",historySelection);
on("view-job",async()=>{const identity=$("job-history").value;if(identity)showJob(await api(`/api/studio/jobs/${identity}`,undefined,"GET"));});
on("refresh",refresh); on("save",async()=>{await save();message("参数已保存，下次上传会使用新参数");await refresh();});
on("record",async()=>{
  if(projectLock || projectLoading) throw new Error("项目正在保存或切换，请稍候");
  const recording=Boolean(status.macro?.recording);
  if (!recording && !canRecord()) throw new Error("请检查录制模式、投屏和设备画面尺寸；必须使用绝对定位");
  await api(recording?"/api/macro/record/stop":"/api/macro/record/start");
});
on("capture",()=>withProjectLock(async lock=>{
  const stamp=projectStamp(), device=profile(), name=$("name").value, loops=Number($("loops").value);
  if (status.macro?.recording) await api("/api/macro/record/stop");
  assertProject(stamp);
  const value=await api("/api/studio/capture",{name,device,loops});
  assertProject(stamp);fillProject(value,lock);await refresh();message("录制已按此设备单独保存");
}));
$("projects").addEventListener("change",async()=>{
  if(projectLock){$("projects").value=project?.id || "";message("项目正在保存，请等待当前操作结束");return;}
  const identity=$("projects").value, request=++projectRequest;
  projectEdited();const revision=projectRevision;projectLoading=true;
  try {
    if(identity) {
      const value=await api(`/api/studio/projects/${identity}`,undefined,"GET");
      if(request !== projectRequest) return;
      if(revision !== projectRevision || projectLock || $("projects").value !== identity) {
        $("projects").value=project?.id || "";return;
      }
      if(value.id !== identity) throw new Error("读取响应项目不一致");
      fillProject(value);
    } else {project=null;projectLoading=false;for(const id of ["device-name","family","connector","orientation","width","height"]) $(id).disabled=false;renderSteps();updatePeriod();}
  } catch(e){if(request === projectRequest){$("projects").value=project?.id || "";message(e.message);}}
  finally {if(request === projectRequest)projectLoading=false;}
});
on("play",()=>withProjectLock(async lock=>{
  const revision=controllerRevision;
  if(!canRecord())throw new Error("请连接所选控制器的电脑录制模式，并核对投屏");
  const plan=await save(lock), stamp=projectStamp();
  if(revision !== controllerRevision || !canRecord())throw new Error("控制器或连接方式已改变，请重新核对后试跑");
  assertProject(stamp);await api(`/api/studio/projects/${plan.id}/play`);message("电脑试跑1轮，保留图像和实际指针检查");
}));
on("stop",()=>api("/api/macro/stop")); document.addEventListener("keydown",e=>{if(e.key==="Escape")api("/api/macro/stop").catch(x=>message(x.message));});
on("build",()=>launchBuild("offline"));on("build-desktop",()=>launchBuild("desktop"));
on("flash",async()=>{
  if(activeJob || projectLock)throw new Error("请等待当前任务结束");
  if (!currentBuild || !$("port").value) throw new Error("请先生成固件，再明确选择ESP32的COM口");
  if(!networkMatches(currentBuildContext)){invalidateBuild();throw new Error("网络配置已改变，请重新生成固件");}
  if(!projectMatches(currentBuildContext.project)){invalidateBuild();throw new Error("项目或参数已改变，请重新生成固件");}
  if(currentBuildController !== selectedController()){invalidateBuild();throw new Error("固件与所选控制器不一致，请重新生成");}
  const job=await api("/api/studio/flash",{build_id:currentBuild,controller:selectedController(),port:$("port").value,initialize:$("initialize").checked});
  activeJob=job.id;$("flash").disabled=true;historySelection();await pollJob();
});
on("restore",async()=>{if(activeJob)throw new Error("请等待当前任务结束");if(!lastFlash || !$("port").value)throw new Error("请选择带备份的烧录任务和原板COM口");const job=await api("/api/studio/restore",{flash_id:lastFlash,port:$("port").value});activeJob=job.id;historySelection();await pollJob();});
for(const id of ["name","loops","startup"]) $(id).addEventListener("input",projectEdited);
for(const id of ["ssid","password","host","device-port"]) {
  $(id).addEventListener("input",networkEdited);$(id).addEventListener("change",networkEdited);
}
on("fullscreen",()=>$("stage").requestFullscreen());
on("export",async()=>{const p=await save();const url=URL.createObjectURL(new Blob([JSON.stringify(p,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download=`${p.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
on("import",()=>{if(projectLock || projectLoading)throw new Error("项目正在保存或切换，请稍候");$("import-file").click();});
$("import-file").addEventListener("change",async e=>{
  try { await withProjectLock(async lock=>{
    const file=e.target.files[0];if(!file)return;if(file.size>2e6)throw new Error("项目文件过大");
    const stamp=projectStamp(), text=await file.text();assertProject(stamp);
    const p=await api("/api/studio/import",JSON.parse(text));assertProject(stamp);
    fillProject(p,lock);await refresh();
  }); } catch(x){message(x.message);} finally {e.target.value="";}
});
$("screen").addEventListener("click",async e=>{
  if(!runtimeReady || projectLock || projectLoading || actionBusy || !canRecord() || status.macro?.playing)return;
  const image=e.currentTarget,box=image.getBoundingClientRect();
  const scale=Math.min(box.width/image.naturalWidth,box.height/image.naturalHeight);
  const width=image.naturalWidth*scale,height=image.naturalHeight*scale;
  const x=(e.clientX-box.left-(box.width-width)/2)/width,y=(e.clientY-box.top-(box.height-height)/2)/height;
  if(!Number.isFinite(x) || !Number.isFinite(y) || x<0 || x>1 || y<0 || y>1)return;
  actionBusy=true;
  try{await api("/api/tap",{x,y,precise:true});}catch(error){message(error.message);}finally{actionBusy=false;}
});
async function pollStatus(){
  try{status=await api("/api/status",undefined,"GET");const transport=(status.control_transport || "usb") === "ble"?"BLE 蓝牙":"USB";$("connection").textContent=status.device?`${transport} · ${!transportMatches()?"与所选控制器不匹配":status.pointer_mode === "absolute"?"录制模式 · 绝对定位":"板子需切换绝对定位"}`:"板子未连接电脑服务";
    $("record").textContent=status.macro?.recording?"完成录制":"开始录制";$("record").disabled=!runtimeReady || Boolean(projectLock) || actionBusy || Boolean(status.macro?.playing) || (!status.macro?.recording && !canRecord());
    $("record-state").textContent=status.macro?.last_error || `${status.macro?.recording?"正在录制":"当前录制"} · ${status.macro?.steps||0} 步`;
    $("empty").hidden=Boolean(status.screen);$("screen").style.display=status.screen?"block":"none";
  }catch(e){$("connection").textContent="服务连接中断";}
}
async function frame(){
  if(loadingFrame || !status.screen)return;loadingFrame=true;
  try{const response=await fetch(`/api/frame.jpg?after=${Math.max(frameSeq,0)}&wait=1`);if(response.ok && response.status!==204){frameSeq=Number(response.headers.get("X-Frame-Seq"));const url=URL.createObjectURL(await response.blob());const old=$("screen").src;$("screen").src=url;if(old.startsWith("blob:"))URL.revokeObjectURL(old);}}
  catch(e){/* Status conveys the disconnected state. */}finally{loadingFrame=false;}
}
$("screen").addEventListener("load",()=>{if(!projectLock && !projectLoading && !project && !status.macro?.recording){$("width").value=$("screen").naturalWidth;$("height").value=$("screen").naturalHeight;$("orientation").value=$("screen").naturalHeight>$("screen").naturalWidth?"portrait":"landscape";}});
applyRuntimeGate();updateControllerUI();refresh().catch(e=>message(e.message));pollStatus();setInterval(pollStatus,1000);setInterval(frame,35);setInterval(pollJob,1000);
