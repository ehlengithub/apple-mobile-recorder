"use strict";
// All editor state is in memory or saved explicitly to a project. No browser storage.
const $ = id => document.getElementById(id);
let project = null, status = {}, currentBuild = null, activeJob = null, frameSeq = -1, lastFlash = null;
let actionBusy = false, pollingJob = false, loadingFrame = false, saving = false;
let buildSnapshot = null;
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
function canRecord() { const p = profile(); return status.device && status.screen && status.pointer_mode === "absolute" && Math.abs((status.aspect || 0) / (p.width / p.height) - 1) <= .03; }
function invalidateBuild() { currentBuild=null;$("flash").disabled=true;$("download").hidden=true;$("firmware-summary").hidden=true; }
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
  $("empty").textContent=`先切换到录制模式固件，在目标设备的「屏幕镜像」中选择 ${info.airplay_name || "Apple Recorder"}`;
  if (info.frame && !project) { $("width").value=info.frame.width; $("height").value=info.frame.height; $("orientation").value=info.frame.height>info.frame.width?"portrait":"landscape"; }
}
async function launchBuild(mode) {
  if (mode === "offline") await save();
  buildSnapshot=mode === "offline"?editorSnapshot():null;
  const body = {mode, project_id:mode === "offline" ? project.id : null};
  if (mode === "desktop") body.network = {ssid:$("ssid").value, password:$("password").value, host:$("host").value, port:Number($("device-port").value)};
  const job = await api("/api/studio/build",body); activeJob=job.id; currentBuild=null; $("flash").disabled=true; $("download").hidden=true;
  if (mode === "desktop") $("password").value="";
  await pollJob();
}
async function pollJob() {
  if (!activeJob || pollingJob) return;
  pollingJob=true;
  try {
    const job = await api(`/api/studio/jobs/${activeJob}`,undefined,"GET");
    $("job-state").textContent = `${job.stage}${job.error ? "："+job.error : ""}`;
    $("job-log").hidden=!job.log; $("job-log").textContent=job.log || "";
    if (job.status === "succeeded" || job.status === "failed") {
      activeJob=null;
      if (job.kind === "flash" && job.backup_ready) { lastFlash=job.id;$("restore").hidden=false; }
      if (job.kind === "build" && job.status === "succeeded") {
        if(job.manifest.mode === "offline" && buildSnapshot !== editorSnapshot()){
          invalidateBuild();$("job-state").textContent="生成期间参数已改变，请保存后重新生成，旧固件不会被当作新参数烧录。";return;
        }
        currentBuild=job.id; $("flash").disabled=false; $("download").hidden=false; $("download").href=`/api/studio/jobs/${job.id}/application.bin`;
        $("download").textContent="下载固件";
        $("firmware-summary").hidden=false;$("firmware-summary").textContent=job.manifest.mode === "offline"?`${job.manifest.steps}步 · 每轮${(job.manifest.cycle_ms/1000).toFixed(3)}秒 · ${job.manifest.loops || "无限"}轮 · SHA256 ${job.manifest.application_sha256.slice(0,12)}…`:`录制模式 · SHA256 ${job.manifest.application_sha256.slice(0,12)}…`;
        $("job-state").textContent=`已生成${job.manifest.mode === "offline" ? "离线" : "录制模式"}固件。核对COM口后再烧录。`;
      } else if (job.kind === "flash" && job.status === "succeeded") {
        lastFlash=job.id;$("restore").hidden=false;
        $("job-state").textContent="烧录和哈希校验成功，应用已备份。离线固件保持首次待机：保持COM供电，重插USB/HID后启用。";
      }
    }
  } catch(e) { message(e.message); activeJob=null; }
  finally { pollingJob=false; }
}
on("refresh",refresh); on("save",async()=>{await save();message("参数已保存，下次生成会使用新参数");await refresh();});
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
on("play",async()=>{await save();await api(`/api/studio/projects/${project.id}/play`);message("电脑试跑1轮，保留图像和实际指针检查");});
on("stop",()=>api("/api/macro/stop")); document.addEventListener("keydown",e=>{if(e.key==="Escape")api("/api/macro/stop").catch(x=>message(x.message));});
on("build",()=>launchBuild("offline"));on("build-desktop",()=>launchBuild("desktop"));
on("flash",async()=>{
  if (!currentBuild || !$("port").value) throw new Error("请先生成固件，再明确选择ESP32的COM口");
  const job=await api("/api/studio/flash",{build_id:currentBuild,port:$("port").value,initialize:$("initialize").checked});
  activeJob=job.id;$("flash").disabled=true;await pollJob();
});
on("restore",async()=>{if(!lastFlash || !$("port").value)throw new Error("请选择原来的COM口");const job=await api("/api/studio/restore",{flash_id:lastFlash,port:$("port").value});activeJob=job.id;await pollJob();});
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
  try{status=await api("/api/status",undefined,"GET");$("connection").textContent=status.device?status.pointer_mode==="absolute"?"录制模式 · 绝对定位":"板子需切换绝对定位":"板子未连接电脑服务";
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
refresh().catch(e=>message(e.message));pollStatus();setInterval(pollStatus,1000);setInterval(frame,35);setInterval(pollJob,1000);
