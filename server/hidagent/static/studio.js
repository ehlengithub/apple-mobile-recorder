"use strict";
// All editor state is in memory or saved explicitly to a project. No browser storage.
const $ = id => document.getElementById(id);
let project = null, status = {}, currentBuild = null, activeJob = null, frameSeq = -1, lastFlash = null;
let actionBusy = false, pollingJob = false, loadingFrame = false, saving = false;
let buildSnapshot = null;
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
  const options = {method};
  if (data !== undefined) { options.headers = {"Content-Type":"application/json"}; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || response.statusText)); }
  return response.json();
}
function on(id, fn) { $(id).addEventListener("click", async () => { try { await fn(); } catch (e) { message(e.message); } }); }
function profile() { return {name:$("device-name").value, family:$("family").value, connector:$("connector").value, orientation:$("orientation").value, width:Number($("width").value), height:Number($("height").value)}; }
function canRecord() { const p = profile(); return status.device && status.screen && transportMatches() && status.pointer_mode === "absolute" && Math.abs((status.aspect || 0) / (p.width / p.height) - 1) <= .03; }
function invalidateBuild() { currentBuild=null;currentBuildController=null;$("flash").disabled=true;$("download").hidden=true;$("firmware-summary").hidden=true; }
function editorSnapshot(){return project?JSON.stringify({...project,name:$("name").value,loops:Number($("loops").value),startup_ms:Math.round(Number($("startup").value)*1000)}):null;}
function renderSteps() {
  const body = $("steps"); body.replaceChildren();
  if (!project) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 4; cell.textContent = "录制或导入后，在这里逐步改时间。"; return; }
  project.steps.forEach((step, i) => {
    const row = body.insertRow(); row.insertCell().textContent = String(i + 1);
    row.insertCell().textContent = `${(step.x*100).toFixed(1)}%, ${(step.y*100).toFixed(1)}%`;
    const input = document.createElement("input"); input.type = "number"; input.min = ".5"; input.max = "3600"; input.step = ".001"; input.value = String(step.wait_ms/1000); input.setAttribute("aria-label",`第${i+1}步执行前间隔`);
    input.addEventListener("change", () => { step.wait_ms = Math.round(Number(input.value)*1000); invalidateBuild(); updatePeriod(); });
    row.insertCell().append(input);
    const remove = document.createElement("button"); remove.textContent = "删除"; remove.addEventListener("click", () => { project.steps.splice(i,1); invalidateBuild(); renderSteps(); updatePeriod(); }); row.insertCell().append(remove);
  }); updatePeriod();
}
function updatePeriod() { $("period").textContent = project ? `每轮 ${(project.steps.reduce((sum,s)=>sum+s.wait_ms,0)/1000).toFixed(3)} 秒` : "—"; }
function fillProject(value) {
  invalidateBuild();
  project = value; $("name").value = value.name; $("device-name").value = value.device.name;
  for (const key of ["family","connector","orientation","width","height"]) $(key).value = String(value.device[key]);
  $("loops").value = String(value.loops); $("startup").value = String(value.startup_ms/1000); renderSteps();
  for(const id of ["device-name","family","connector","orientation","width","height"]) $(id).disabled=true;
}
async function save() {
  if (!project) throw new Error("请先保存录制或导入项目");
  if(saving)throw new Error("参数正在保存，请稍候");
  saving=true;
  const controls=[...document.querySelectorAll("#steps input,#steps button"),...(["save","build","play","export","name","loops","startup"].map($))];
  controls.forEach(x=>x.disabled=true);
  try{
    project.name = $("name").value; project.loops = Number($("loops").value); project.startup_ms = Math.round(Number($("startup").value)*1000);
    project = await api(`/api/studio/projects/${project.id}`, project, "PUT");
    renderSteps();
    return project;
  }finally{saving=false;controls.forEach(x=>x.disabled=false);}
}
async function refresh() {
  const info = await api("/api/studio/status",undefined,"GET");
  const selected = $("projects").value, port = $("port").value;
  $("projects").replaceChildren(new Option("新建录制", ""));
  info.projects.forEach(p => $("projects").add(new Option(`${p.device.name} · ${p.name} · ${p.steps}步`,p.id)));
  if (info.projects.some(p=>p.id===selected)) $("projects").value = selected;
  $("port").replaceChildren(new Option("请选择 COM 口", ""));
  info.ports.forEach(p => $("port").add(new Option(`${p.port} · ${p.description}`,p.port)));
  if (info.ports.some(p=>p.port===port)) $("port").value = port;
  $("tools").textContent = info.tools.arduino && info.tools.esptool ? "编译 / 烧录工具就绪" : "请先运行环境安装脚本";
  if(!$("host").value && info.lan_host)$("host").value=info.lan_host;
  $("device-port").value=String(info.device_port);$("device-port").readOnly=true;
  $("empty").textContent=`先切换到电脑录制模式，在目标设备的「屏幕镜像」中选择 ${info.airplay_name || "Apple Recorder"}`;
  if (info.frame && !project) { $("width").value=info.frame.width; $("height").value=info.frame.height; $("orientation").value=info.frame.height>info.frame.width?"portrait":"landscape"; }
  await refreshHistory();
}
function historySelection() {
  const job = historyJobs.find(item => item.id === $("job-history").value);
  lastFlash = job?.kind === "flash" && job.backup_available ? job.id : null;
  $("restore").disabled = !lastFlash || Boolean(activeJob) || historyJobs.some(item => item.running_here);
  $("view-job").disabled = !job;
  const names = {build:"生成固件",flash:"烧录",restore:"恢复",upload:"上传录制", "player-control":"播放器控制"};
  const states = {queued:"排队",running:"执行中",succeeded:"完成",failed:"失败"};
  if (!job) { $("history-detail").textContent="尚无任务。生成或烧录后，记录和可用备份会显示在这里。"; return; }
  const pending = ["queued","running"].includes(job.status) && !job.running_here;
  $("history-detail").textContent = `${names[job.kind] || job.kind} · ${pending ? "未确认结束，请核对任务日志" : states[job.status] || job.status} · ${job.id}${job.board_mac ? " · 板卡 "+job.board_mac : ""}${job.port ? " · "+job.port : ""}。${job.backup_available ? "烧录前备份文件齐全；恢复时还会核对备份和原板。" : job.backup_ready ? "备份文件不完整，无法恢复。" : "此任务没有可用的烧录前备份。"}`;
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
}
async function launchBuild(mode) {
  const controller=selectedController(), revision=controllerRevision;
  if (mode === "offline" && project) await save();
  if(revision !== controllerRevision) throw new Error("控制器已切换，请重新生成对应固件");
  buildSnapshot=mode === "offline"?editorSnapshot():null;
  const body = {mode, controller, project_id:mode === "offline" && project ? project.id : null};
  if (mode === "desktop" || $("ssid").value) body.network = {ssid:$("ssid").value, password:$("password").value, host:$("host").value, port:Number($("device-port").value)};
  const job = await api("/api/studio/build",body); activeJob=job.id; currentBuild=null; $("flash").disabled=true; $("download").hidden=true; historySelection();
  buildRequestRevision=revision;
  if (body.network) $("password").value="";
  await pollJob();
}
async function pollJob() {
  if (!activeJob || pollingJob) return;
  const identity=activeJob;
  pollingJob=true;
  try {
    const job = await api(`/api/studio/jobs/${identity}`,undefined,"GET");
    if(activeJob !== identity) return;
    showJob(job);
    if (job.status === "succeeded" || job.status === "failed") {
      if(typeof playerJobCompleted === "function") playerJobCompleted(job,true);
      activeJob=null;
      await refreshHistory({select:job.kind === "flash" ? job.id : null,resume:false}).catch(e=>message(e.message));
      if (job.kind === "build" && job.status === "succeeded") {
        if(jobController(job) !== selectedController() || (buildRequestRevision !== null && buildRequestRevision !== controllerRevision)) {
          invalidateBuild();$("job-state").textContent="控制器已切换，旧固件不可烧录；请为当前控制器重新生成。";return;
        }
        if(job.manifest.recording_protocol !== 2 && job.manifest.project_id && (!project || buildSnapshot !== editorSnapshot())){
          invalidateBuild();$("job-state").textContent="生成期间参数已改变，请保存后重新生成，旧固件不会被当作新参数烧录。";return;
        }
        currentBuild=job.id;currentBuildController=jobController(job); $("flash").disabled=false; $("download").hidden=false; $("download").href=`/api/studio/jobs/${job.id}/application.bin`;
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
  } catch(e) { $("job-state").textContent=`任务状态暂时无法读取，将自动重试：${e.message}`; }
  finally { pollingJob=false; }
}
on("refresh-history",()=>refreshHistory());
$("controller").addEventListener("change",()=>{
  controllerRevision++;invalidateBuild();updateControllerUI();
  if(typeof updatePlayerController === "function") updatePlayerController(true);
  $("record").disabled=actionBusy || Boolean(status.macro?.playing) || (!status.macro?.recording && !canRecord());
});
$("job-history").addEventListener("change",historySelection);
on("view-job",async()=>{const identity=$("job-history").value;if(identity)showJob(await api(`/api/studio/jobs/${identity}`,undefined,"GET"));});
on("refresh",refresh); on("save",async()=>{await save();message("参数已保存，下次上传会使用新参数");await refresh();});
on("record",async()=>{
  const recording=Boolean(status.macro?.recording);
  if (!recording && !canRecord()) throw new Error("请检查录制模式、投屏和设备画面尺寸；必须使用绝对定位");
  await api(recording?"/api/macro/record/stop":"/api/macro/record/start");
});
on("capture",async()=>{
  if (status.macro?.recording) await api("/api/macro/record/stop");
  const value=await api("/api/studio/capture",{name:$("name").value,device:profile(),loops:Number($("loops").value)});
  fillProject(value);await refresh();$("projects").value=value.id;message("录制已按此设备单独保存");
});
$("projects").addEventListener("change",async()=>{
  try { invalidateBuild();if ($("projects").value) fillProject(await api(`/api/studio/projects/${$("projects").value}`,undefined,"GET")); else {project=null;for(const id of ["device-name","family","connector","orientation","width","height"]) $(id).disabled=false;renderSteps();updatePeriod();} }
  catch(e){message(e.message);}
});
on("play",async()=>{const revision=controllerRevision;if(!canRecord())throw new Error("请连接所选控制器的电脑录制模式，并核对投屏");await save();if(revision !== controllerRevision || !canRecord())throw new Error("控制器或连接方式已改变，请重新核对后试跑");await api(`/api/studio/projects/${project.id}/play`);message("电脑试跑1轮，保留图像和实际指针检查");});
on("stop",()=>api("/api/macro/stop")); document.addEventListener("keydown",e=>{if(e.key==="Escape")api("/api/macro/stop").catch(x=>message(x.message));});
on("build",()=>launchBuild("offline"));on("build-desktop",()=>launchBuild("desktop"));
on("flash",async()=>{
  if (!currentBuild || !$("port").value) throw new Error("请先生成固件，再明确选择ESP32的COM口");
  if(currentBuildController !== selectedController()){invalidateBuild();throw new Error("固件与所选控制器不一致，请重新生成");}
  const job=await api("/api/studio/flash",{build_id:currentBuild,controller:selectedController(),port:$("port").value,initialize:$("initialize").checked});
  activeJob=job.id;$("flash").disabled=true;historySelection();await pollJob();
});
on("restore",async()=>{if(activeJob)throw new Error("请等待当前任务结束");if(!lastFlash || !$("port").value)throw new Error("请选择带备份的烧录任务和原板COM口");const job=await api("/api/studio/restore",{flash_id:lastFlash,port:$("port").value});activeJob=job.id;historySelection();await pollJob();});
for(const id of ["name","loops","startup"]) $(id).addEventListener("input",invalidateBuild);
on("fullscreen",()=>$("stage").requestFullscreen());
on("export",async()=>{const p=await save();const url=URL.createObjectURL(new Blob([JSON.stringify(p,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download=`${p.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
on("import",()=>$("import-file").click());
$("import-file").addEventListener("change",async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>2e6)throw new Error("项目文件过大");const p=await api("/api/studio/import",JSON.parse(await file.text()));fillProject(p);await refresh();$("projects").value=p.id;}catch(x){message(x.message);}finally{e.target.value="";}});
$("screen").addEventListener("click",async e=>{
  if(actionBusy || !canRecord() || status.macro?.playing)return;
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
    $("record").textContent=status.macro?.recording?"完成录制":"开始录制";$("record").disabled=actionBusy || Boolean(status.macro?.playing) || (!status.macro?.recording && !canRecord());
    $("record-state").textContent=status.macro?.last_error || `${status.macro?.recording?"正在录制":"当前录制"} · ${status.macro?.steps||0} 步`;
    $("empty").hidden=Boolean(status.screen);$("screen").style.display=status.screen?"block":"none";
  }catch(e){$("connection").textContent="服务连接中断";}
}
async function frame(){
  if(loadingFrame || !status.screen)return;loadingFrame=true;
  try{const response=await fetch(`/api/frame.jpg?after=${Math.max(frameSeq,0)}&wait=1`);if(response.ok && response.status!==204){frameSeq=Number(response.headers.get("X-Frame-Seq"));const url=URL.createObjectURL(await response.blob());const old=$("screen").src;$("screen").src=url;if(old.startsWith("blob:"))URL.revokeObjectURL(old);}}
  catch(e){/* Status conveys the disconnected state. */}finally{loadingFrame=false;}
}
$("screen").addEventListener("load",()=>{if(!project && !status.macro?.recording){$("width").value=$("screen").naturalWidth;$("height").value=$("screen").naturalHeight;$("orientation").value=$("screen").naturalHeight>$("screen").naturalWidth?"portrait":"landscape";}});
updateControllerUI();refresh().catch(e=>message(e.message));pollStatus();setInterval(pollStatus,1000);setInterval(frame,35);setInterval(pollJob,1000);
