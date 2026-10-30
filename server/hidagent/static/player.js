"use strict";
// No automatic port opening or browser persistence. All hardware actions require a click.
const playerRegion = document.getElementById("recording-upload-region");
playerRegion.hidden = false;
playerRegion.innerHTML = `<div class="section-head"><h2>上传录制 · 无需重新烧录</h2><span class="badge">通用播放器 v2</span></div>
<p class="hint">首次安装通用播放器后，修改录制只需上传数据。先拔下连接 iPhone / iPad 的 USB/HID，COM 保持接电脑。上传后保持待机。</p>
<label>核对板卡 MAC<input id="player-mac" placeholder="如 7c:e8:b1:a4:85:94" autocomplete="off" spellcheck="false"></label>
<p class="hint">首次烧录后自动填入已识别的 MAC；换板请核对后修改。操作使用上方选定的 COM 口。</p>
<label class="check"><input type="checkbox" id="player-detached">USB/HID 已从 iPhone / iPad 拔下</label>
<div class="controls"><button id="upload-recording" class="primary">保存并上传录制</button><button id="arm-player">启用：下次插入执行</button><button id="desktop-player">切回电脑录制</button><button id="download-recording">下载录制文件</button></div>
<p id="player-result" class="hint">上传与启用分开。未点“启用”前不会自动循环；启用后插回 USB/HID，等待倒计时。</p>`;
let playerPending = false;
async function playerRequest(command) {
  if (playerPending || activeJob) throw new Error("请等待当前任务结束");
  if (!document.getElementById("player-detached").checked) throw new Error("请先拔下 USB/HID，并勾选确认");
  const port = $("port").value, expected_mac = document.getElementById("player-mac").value.trim().toLowerCase();
  if (!port || !/^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$/.test(expected_mac)) throw new Error("请选择 COM 口并核对板卡 MAC");
  if (command === "upload" || command === "arm") await save();
  playerPending = true;
  try {
    const payload = {port, expected_mac, hid_disconnected:true};
    if (command === "upload" || command === "arm") payload.project_id = project.id;
    if (command !== "upload") payload.command = command;
    const result = document.getElementById("player-result");
    result.textContent = command === "upload" ? "正在上传与读回校验；完成后保持待机。" : "正在核对板内状态…";
    const job = await api(`/api/studio/player/${command === "upload" ? "upload" : "control"}`, payload);
    activeJob = job.id;
    await pollJob();
  } finally { playerPending = false; }
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
api("/api/studio/jobs", undefined, "GET").then(history => {
  const known = history.find(job => job.board_mac);
  if (known && !document.getElementById("player-mac").value) document.getElementById("player-mac").value = known.board_mac;
}).catch(() => {});
