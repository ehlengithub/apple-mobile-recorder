"use strict";
// No automatic port opening or browser persistence. All hardware actions require a click.
const playerRegion = document.getElementById("recording-upload-region");
playerRegion.hidden = false;
playerRegion.innerHTML = `<div class="section-head"><h2>上传录制 · 无需重新烧录</h2><span class="badge">通用播放器 v2</span></div>
<p id="player-instructions" class="hint"></p>
<label>核对板卡 MAC<input id="player-mac" placeholder="如 7c:e8:b1:a4:85:94" autocomplete="off" spellcheck="false"></label>
<p class="hint">只从相同控制器的历史任务填入已识别的 MAC；换板请核对后修改。操作使用上方选定的 COM 口，不自动探测串口。</p>
<label class="check"><input type="checkbox" id="player-detached"><span id="player-confirmation"></span></label>
<div class="controls"><button id="upload-recording" class="primary">保存并上传录制</button><button id="arm-player">启用：下次插入执行</button><button id="desktop-player">切回电脑录制</button><button id="download-recording">下载录制文件</button></div>
<p id="player-result" class="hint">上传与启用分开。未点“启用”前不会自动循环；启用后插回 USB/HID，等待倒计时。</p>`;
let playerPending = false;
const playerAttempts = new Map();
function playerUncertain(command, controller) {
  const ble=controller === "ble-esp32";
  return `状态未知：${command === "arm"?"启用":"切回电脑录制"}命令可能已执行，录制可能已运行。`+(ble?
    "请按物理 BOOT 停止，再保持供电长按 BOOT 2 秒进入持久维护模式，重新核验板卡状态后勾选确认；不要盲目重试命令或用 EN/RESET 停止。":
    "请断开 USB/HID 或按物理 BOOT 停止，重新核验板卡状态后勾选确认；不要盲目重试命令或用 EN/RESET 停止。");
}
function playerJobUnknown(identity) {
  const attempt=playerAttempts.get(identity);
  if(!attempt || attempt.revision !== controllerRevision || attempt.controller !== selectedController())return;
  $("player-detached").checked=false;
  $("job-state").textContent=$("player-result").textContent=playerUncertain(attempt.command,attempt.controller);
}
function updatePlayerController(clear=false) {
  const ble=selectedController() === "ble-esp32";
  if(clear){$("player-mac").value="";$("player-detached").checked=false;}
  $("player-instructions").textContent=ble?"保持供电，长按 BOOT 2 秒，确认进入持久维护模式后再操作 COM；打开 COM 可能复位，必须先完成物理维护操作。上传校验后保持维护待机。离线 BLE 不需要 Wi-Fi。":"修改录制只需上传数据。先拔下连接 iPhone / iPad 的 USB/HID，COM 保持接电脑。上传后保持待机。";
  $("player-confirmation").textContent=ble?"已保持供电长按 BOOT 2 秒，确认进入持久维护模式":"USB/HID 已从 iPhone / iPad 拔下";
  $("arm-player").textContent=ble?"启用：退出维护并蓝牙执行":"启用：下次插入执行";
  $("player-result").textContent=ble?"先把 iPad 放在录制起始页，再点启用；将退出维护、自动蓝牙连接并倒计时开始。启用或切回电脑录制后，再次开 COM 前必须重新进入维护并勾选确认。":"上传与启用分开。未点启用前不会自动循环；启用后插回 USB/HID，等待倒计时。";
}
function fillPlayerMac(history, revision=controllerRevision) {
  if(revision !== controllerRevision || $("player-mac").value) return;
  const known=history.filter(job=>jobController(job) === selectedController() && job.board_mac)
    .sort((a,b)=>(b.updated || b.created || 0)-(a.updated || a.created || 0))[0];
  if(known)$("player-mac").value=known.board_mac;
}
function playerJobWarning(job) {
  const command=job.command || playerAttempts.get(job.id)?.command;
  if(job.kind !== "player-control" || !["arm","desktop"].includes(command))return "";
  const uncertain=job.control_uncertain || job.operation_state === "unknown" || ["failed","unknown"].includes(job.status);
  if(!uncertain)return "";
  if(job.reconciliation?.status === "stopped" && job.may_be_running === false) {
    return "原命令未确认，已核验软件停止；仍需重新物理维护确认。"+(jobController(job) === "ble-esp32"?
      "保持供电长按 BOOT 2 秒进入持久维护模式，重新核验后再勾选；不要盲目重试启用。":"确认 USB/HID 断开并重新核验后再勾选；不要盲目重试启用。");
  }
  if(job.command_sent === false && !job.maintenance_confirmation_invalidated && !job.control_uncertain) return "";
  return playerUncertain(command,jobController(job));
}
function playerJobCompleted(job, confirmationOnly=false) {
  const attempt=playerAttempts.get(job.id), command=job.command || attempt?.command;
  if(jobController(job) !== selectedController() || (attempt && attempt.revision !== controllerRevision)) return;
  if(job.kind === "player-control" && ["arm","desktop"].includes(command)) {
    if(job.maintenance_confirmation_invalidated || job.command_sent || job.status === "succeeded" || job.command_sent !== false) $("player-detached").checked=false;
    const warning=playerJobWarning(job);
    if(warning) { $("job-state").textContent=$("player-result").textContent=warning;return; }
  }
  if(!confirmationOnly && (job.kind === "upload" || job.kind === "player-control")) $("player-result").textContent=$("job-state").textContent;
}
async function playerRequest(command) {
  assertRuntime();
  const controller=selectedController(), revision=controllerRevision, ble=controller === "ble-esp32";
  if (!$("player-detached").checked) throw new Error(ble?"请先保持供电长按 BOOT 2 秒进入持久维护模式，并勾选确认":"请先拔下 USB/HID，并勾选确认");
  if (playerPending || activeJob) throw new Error("请等待当前任务结束");
  const port = $("port").value, expected_mac = document.getElementById("player-mac").value.trim().toLowerCase();
  if (!port || !/^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$/.test(expected_mac)) throw new Error("请选择 COM 口并核对板卡 MAC");
  playerPending = true;
  try { return await withProjectLock(async lock=>{
    const plan=(command === "upload" || command === "arm") ? await save(lock) : null;
    const stamp=projectStamp();
    if(revision !== controllerRevision || !$("player-detached").checked) throw new Error("控制器或维护确认已改变，请重新核对后操作");
    if(port !== $("port").value || expected_mac !== $("player-mac").value.trim().toLowerCase()) throw new Error("COM 口或板卡 MAC 已改变，请重新核对");
    assertProject(stamp);
    if(activeJob)throw new Error("请等待当前任务结束");
    const payload = {port, expected_mac, controller, ...(ble?{maintenance_confirmed:true}:{hid_disconnected:true})};
    if (plan) payload.project_id = plan.id;
    if (command !== "upload") payload.command = command;
    const result = document.getElementById("player-result");
    result.textContent = command === "upload" ? "正在上传与读回校验；完成后保持待机。" : ble && command === "arm" ? "请先放好 iPad 起始页；启用将退出维护，自动蓝牙连接并倒计时。" : "正在核对板内状态…";
    const exitsMaintenance=["arm","desktop"].includes(command);
    // Once POST can have reached the server, its physical side effect is uncertain even if the response is lost.
    if(exitsMaintenance) $("player-detached").checked=false;
    let job;
    try { job=await api(`/api/studio/player/${command === "upload" ? "upload" : "control"}`,payload); }
    catch(error) {
      if(exitsMaintenance) {
        $("player-detached").checked=false;
        const warning=playerUncertain(command,controller);
        $("job-state").textContent=result.textContent=warning;
        throw new Error(`${warning} 请求未确认：${error.message}`);
      }
      throw error;
    }
    if(exitsMaintenance) playerAttempts.set(job.id,{command,controller,revision});
    activeJob = job.id;lock.job=job.id;
    await pollJob();
  }); } finally { playerPending = false; }
}
on("upload-recording", () => playerRequest("upload"));
on("arm-player", () => playerRequest("arm"));
on("desktop-player", () => playerRequest("desktop"));
on("download-recording", async () => {
  const plan = await save();
  const anchor = document.createElement("a");
  anchor.href = `/api/studio/projects/${plan.id}/recording.amr`;
  anchor.download = "recording.amr"; anchor.click();
});
updatePlayerController();applyRuntimeGate();
const playerInitialRevision=controllerRevision;
api("/api/studio/jobs", undefined, "GET").then(history=>fillPlayerMac(history,playerInitialRevision)).catch(() => {});
