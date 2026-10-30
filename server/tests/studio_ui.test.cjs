// Run with node --test server/tests/studio_ui.test.cjs. No browser or hardware IO.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname,"../hidagent/static/studio.js"),"utf8");
const html = fs.readFileSync(path.join(__dirname,"../hidagent/static/studio.html"),"utf8");
const flashID = "a".repeat(32), olderID = "b".repeat(32), taskID = "c".repeat(32);
function flash(id, created=1, status="succeeded") {
  return {id,created,kind:"flash",status,stage:"完成",backup_ready:true,backup_available:true,board_mac:"00:11:22:33:44:55"};
}
function page(state, {player=false,controller="usb-s3"} = {}) {
  const elements = new Map();
  function addElements(markup) {
    for(const match of markup.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
      const listeners = {};
      const element = {
        value:"",hidden:/\bhidden\b/.test(match[0]),disabled:/\bdisabled\b/.test(match[0]),style:{},textContent:"",
        addEventListener(event,fn){listeners[event]=fn},
        async fire(event="click"){await listeners[event]?.({target:this})},
        replaceChildren(...children){this.options=children;this.value=children[0]?.value || ""},
        add(option){this.options.push(option)},
        insertRow(){return {insertCell(){return {append(){}}}}},
      };
      Object.defineProperty(element,"innerHTML",{set:addElements});
      elements.set(match[1],element);
    }
  }
  addElements(html);
  const get=id=>{assert.ok(elements.has(id),`missing DOM id ${id}`);return elements.get(id)};
  get("controller").value=controller;
  let fails=state.failJobReads || 0;
  const requests=[];
  const fetch=async(url,options={})=>{
    requests.push({url,options});
    if(state.beforeResponse)await state.beforeResponse(url,options);
    let data;
    if(url==="/api/studio/status") data={projects:[],ports:[{port:"/dev/MOCK-ONLY",description:"mock"}],tools:{},device_port:7400};
    else if(url==="/api/status") data=state.status || {screen:false,device:false,macro:{}};
    else if(url==="/api/studio/jobs") data=state.jobs;
    else if(url.startsWith("/api/studio/jobs/")) {
      if(fails-- > 0)throw new Error("temporary disconnect");
      data=state.details[url.split("/").pop()];
      assert.ok(data,`unexpected job ${url}`);
    } else if(url==="/api/studio/restore") {
      data={id:taskID,kind:"restore",status:"running",stage:"恢复中"};
      state.details[taskID]=data;
    } else if(url==="/api/studio/build") {
      data=state.details[taskID];
    } else if(url==="/api/studio/flash") {
      data={id:taskID,kind:"flash",status:"queued",stage:"排队"};
      state.details[taskID]=data;
    } else if(url.startsWith("/api/studio/projects/") && options.method==="PUT") {
      data=JSON.parse(options.body);
    } else if(url==="/api/studio/player/upload" || url==="/api/studio/player/control") {
      const payload=JSON.parse(options.body);
      data={id:taskID,controller:payload.controller,command:payload.command,
        kind:url.endsWith("upload")?"upload":"player-control",status:"queued",stage:"排队"};
      state.details[taskID]=data;
    } else throw new Error(`Unexpected request ${url}`);
    return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
  };
  const context=vm.createContext({document:{getElementById:get,addEventListener(){},querySelectorAll(){return []},
    createElement(){return {addEventListener(){},setAttribute(){}}}},fetch,
    Option:class {constructor(text,value){this.text=text;this.value=value}},
    setInterval(){},setTimeout(){},clearTimeout(){}});
  vm.runInContext(source,context);
  if(player)vm.runInContext(fs.readFileSync(path.join(__dirname,"../hidagent/static/player.js"),"utf8"),context);
  return {get,requests,run:code=>vm.runInContext(code,context),ready:()=>new Promise(resolve=>setImmediate(resolve))};
}

test("reload recovers backups, permits failed flash backups, restores the selected original task",async()=>{
  const state={jobs:[flash(flashID,2,"failed"),flash(olderID)],details:{}};
  const first=page(state);await first.ready();
  assert.equal(first.run("lastFlash"),flashID);
  assert.equal(first.get("restore").disabled,false);
  const fresh=page(state);await fresh.ready();
  assert.equal(fresh.run("lastFlash"),flashID);
  assert.equal(fresh.get("restore").hidden,false);
  await fresh.get("restore").fire();
  assert.ok(!fresh.requests.some(r=>r.url==="/api/studio/restore"),"must explicitly select COM");
  fresh.get("job-history").value=olderID;await fresh.get("job-history").fire("change");
  fresh.get("port").value="/dev/MOCK-ONLY";
  await fresh.get("restore").fire();
  const request=fresh.requests.find(r=>r.url==="/api/studio/restore");
  assert.deepEqual(JSON.parse(request.options.body),{flash_id:olderID,port:"/dev/MOCK-ONLY"});
  assert.equal(fresh.run("activeJob"),taskID);
  assert.equal(fresh.get("restore").disabled,true);
});

test("refresh resumes a live job and retries after a transient polling failure",async()=>{
  const job={id:taskID,kind:"flash",created:2,status:"running",stage:"备份中",running_here:true};
  const state={jobs:[job],details:{[taskID]:job},failJobReads:1};
  const app=page(state);await app.ready();
  assert.equal(app.run("activeJob"),taskID);
  assert.match(app.get("job-state").textContent,/自动重试/);
  state.details[taskID]=flash(taskID,2);state.jobs=[flash(taskID,2)];
  await app.run("pollJob()");
  assert.equal(app.run("activeJob"),null);
  assert.equal(app.run("lastFlash"),taskID);
  assert.equal(app.get("restore").disabled,false);
  assert.equal(app.requests.filter(r=>r.url===`/api/studio/jobs/${taskID}`).length,2);
});

test("missing backup files disable recovery; stale running records are not auto-resumed",async()=>{
  const state={jobs:[{...flash(flashID),backup_available:false,status:"running",running_here:false}],details:{}};
  const app=page(state);await app.ready();
  assert.equal(app.run("activeJob"),null);
  assert.equal(app.run("lastFlash"),null);
  assert.equal(app.get("restore").disabled,true);
  assert.match(app.get("history-detail").textContent,/文件不完整/);
  assert.ok(!app.requests.some(r=>r.url===`/api/studio/jobs/${flashID}`));
});

function universal(state={}) {
  const job={id:taskID,kind:"build",status:"succeeded",stage:"完成",manifest:{mode:"offline",recording_protocol:2,
    project_id:null,application_sha256:"f".repeat(64),steps:0,cycle_ms:0}};
  return {jobs:[],details:{[taskID]:job},...state};
}
test("universal player builds without a project or Wi-Fi; summary does not claim zero-step playback",async()=>{
  const app=page(universal());await app.ready();await app.run('launchBuild("offline")');
  const request=app.requests.find(r=>r.url==="/api/studio/build");
  assert.deepEqual(JSON.parse(request.options.body),{mode:"offline",project_id:null,controller:"usb-s3"});
  assert.match(app.get("firmware-summary").textContent,/通用播放器 v2 · 录制单独上传/);
  assert.doesNotMatch(app.get("firmware-summary").textContent,/0步|0\.000秒/);
  assert.match(app.get("job-state").textContent,/无需重新编译/);
  assert.equal(app.get("flash").disabled,false);
});

test("optional Wi-Fi is included for the first offline build and password is cleared",async()=>{
  const app=page(universal());await app.ready();
  app.get("ssid").value="test-wifi";app.get("password").value="test-secret";app.get("host").value="192.0.2.10";
  await app.run('launchBuild("offline")');
  const body=JSON.parse(app.requests.find(r=>r.url==="/api/studio/build").options.body);
  assert.deepEqual(body.network,{ssid:"test-wifi",password:"test-secret",host:"192.0.2.10",port:7400});
  assert.equal(app.get("password").value,"");
});

test("legacy builds with unverified editor snapshot cannot enable flash after reload",async()=>{
  const state=universal();Object.assign(state.details[taskID].manifest,{recording_protocol:null,project_id:flashID});
  const app=page(state);await app.ready();await app.run(`activeJob='${taskID}';pollJob()`);
  assert.equal(app.run("currentBuild"),null);
  assert.equal(app.get("flash").disabled,true);
  assert.match(app.get("job-state").textContent,/重新生成/);
});

for(const [kind,command,expected] of [["upload",null,/逐字节校验完成.*保持待机/],
  ["player-control","arm",/已启用.*插入USB\/HID/], ["player-control","desktop",/录制模式.*插入USB\/HID/]]) {
  test(`completed ${kind} ${command || ""} shows its actual outcome`,async()=>{
    const job={id:taskID,kind,command,status:"succeeded",stage:"完成",created:1};
    const app=page({jobs:[job],details:{[taskID]:job}});await app.ready();
    await app.run(`activeJob='${taskID}';pollJob()`);
    assert.match(app.get("job-state").textContent,expected);
  });
}

test("upload has its own region and script follows the shared workbench API",()=>{
  assert.match(html,/id="recording-upload-region"/);
  assert.ok(html.indexOf('src="/static/player.js"')>html.indexOf('src="/static/studio.js"'));
});

test("player script shares workbench state and initializes without hardware actions",async()=>{
  const app=page({jobs:[flash(flashID)],details:{}},{player:true});await app.ready();
  assert.equal(app.get("recording-upload-region").hidden,false);
  assert.equal(app.get("player-mac").value,"00:11:22:33:44:55");
  assert.equal(app.run("typeof playerRequest"),"function");
  assert.equal(app.run("lastFlash"),flashID);
  assert.ok(app.requests.every(request=>request.options.method==="GET"));
});

test("player buttons reject unconfirmed HID or missing MAC without any request",async()=>{
  const app=page({jobs:[],details:{}},{player:true});await app.ready();
  app.get("port").value="/dev/MOCK-ONLY";
  for(const button of ["upload-recording","arm-player","desktop-player"]) {
    app.get("player-mac").value="00:11:22:33:44:55";
    app.get("player-detached").checked=false;
    const before=app.requests.length;
    await app.get(button).fire();
    assert.match(app.get("message").textContent,/勾选确认/);
    assert.equal(app.requests.length,before);
    app.get("player-detached").checked=true;
    app.get("player-mac").value="";
    await app.get(button).fire();
    assert.match(app.get("message").textContent,/核对板卡 MAC/);
    assert.equal(app.requests.length,before);
  }
});

for(const [button,route,command] of [["upload-recording","upload",null],["arm-player","control","arm"]]) {
  test(`${button} saves the current project before queuing its task with the project ID`,async()=>{
    const app=page({jobs:[],details:{}},{player:true});await app.ready();
    const plan={id:flashID,name:"old name",loops:1,startup_ms:10000,
      device:{name:"mock iPad",family:"ipad",width:754,height:1080},
      steps:[{do:"tap",x:0.3,y:0.4,wait_ms:500}]};
    app.run(`project=${JSON.stringify(plan)}`);
    app.get("name").value="edited recording";app.get("loops").value="2";app.get("startup").value="5";
    app.get("port").value="/dev/MOCK-ONLY";app.get("player-mac").value="00:11:22:33:44:55";
    app.get("player-detached").checked=true;
    const before=app.requests.length;
    await app.get(button).fire();
    const calls=app.requests.slice(before);
    assert.deepEqual(calls.map(request=>request.url),[
      `/api/studio/projects/${flashID}`,`/api/studio/player/${route}`,`/api/studio/jobs/${taskID}`]);
    assert.equal(calls[0].options.method,"PUT");
    const saved=JSON.parse(calls[0].options.body);
    assert.equal(saved.name,"edited recording");assert.equal(saved.loops,2);assert.equal(saved.startup_ms,5000);
    const payload={port:"/dev/MOCK-ONLY",expected_mac:"00:11:22:33:44:55",controller:"usb-s3",hid_disconnected:true,project_id:flashID};
    if(command)payload.command=command;
    assert.equal(calls[1].options.method,"POST");
    assert.deepEqual(JSON.parse(calls[1].options.body),payload);
    assert.equal(app.run("activeJob"),taskID);
    assert.equal(app.run("playerPending"),false);
    assert.equal(app.get("job-state").textContent,"排队");
  });
}

test("BLE build carries its controller and flash request matches the generated binary",async()=>{
  const state=universal();state.details[taskID].manifest.controller="ble-esp32";
  const app=page(state,{controller:"ble-esp32"});await app.ready();
  await app.run('launchBuild("offline")');
  const build=app.requests.find(request=>request.url==="/api/studio/build");
  assert.deepEqual(JSON.parse(build.options.body),{mode:"offline",project_id:null,controller:"ble-esp32"});
  assert.match(app.get("firmware-summary").textContent,/BLE/);
  app.get("port").value="/dev/MOCK-ONLY";
  await app.get("flash").fire();
  const write=app.requests.find(request=>request.url==="/api/studio/flash");
  assert.equal(JSON.parse(write.options.body).controller,"ble-esp32");
});

test("USB flash requests explicitly carry usb-s3",async()=>{
  const app=page(universal());await app.ready();await app.run('launchBuild("offline")');
  app.get("port").value="/dev/MOCK-ONLY";await app.get("flash").fire();
  assert.equal(JSON.parse(app.requests.find(r=>r.url==="/api/studio/flash").options.body).controller,"usb-s3");
});

function preparePlayer(app) {
  const plan={id:flashID,name:"BLE clicks",loops:1,startup_ms:10000,
    device:{name:"mock iPad",family:"ipad",width:754,height:1080},
    steps:[{do:"tap",x:0.3,y:0.4,wait_ms:500}]};
  app.run(`project=${JSON.stringify(plan)}`);
  app.get("name").value=plan.name;app.get("loops").value="1";app.get("startup").value="10";
  app.get("port").value="/dev/MOCK-ONLY";app.get("player-mac").value="00:11:22:33:44:55";
  app.get("player-detached").checked=true;
}

for(const [button,route,command] of [["upload-recording","upload",null],["arm-player","control","arm"],
  ["desktop-player","control","desktop"]]) {
  test(`BLE ${button} requires persistent maintenance and sends the BLE contract`,async()=>{
    const app=page({jobs:[],details:{}},{player:true,controller:"ble-esp32"});await app.ready();
    preparePlayer(app);app.get("player-detached").checked=false;
    const before=app.requests.length;await app.get(button).fire();
    assert.equal(app.requests.length,before);
    assert.match(app.get("message").textContent,/BOOT 2 秒.*持久维护/);
    app.get("player-detached").checked=true;await app.get(button).fire();
    const request=app.requests.find(r=>r.url===`/api/studio/player/${route}`);
    const expected={port:"/dev/MOCK-ONLY",expected_mac:"00:11:22:33:44:55",controller:"ble-esp32",maintenance_confirmed:true};
    if(command)expected.command=command;
    if(command !== "desktop")expected.project_id=flashID;
    assert.deepEqual(JSON.parse(request.options.body),expected);
    const saved=app.requests.find(r=>r.options.method==="PUT");
    if(saved) {
      assert.ok(!("controller" in JSON.parse(saved.options.body).device));
      assert.ok(!("controller" in JSON.parse(saved.options.body)));
    }
    assert.equal(app.run("activeJob"),taskID);
  });
}

for(const controller of ["usb-s3","ble-esp32"]) {
  for(const command of ["arm","desktop"]) {
    test(`${controller} successful ${command} invalidates the physical confirmation`,async()=>{
      const job={id:taskID,kind:"player-control",controller,command,status:"succeeded",stage:"完成",created:1};
      const state={jobs:[job],details:{[taskID]:job}};
      const app=page(state,{player:true,controller});await app.ready();
      preparePlayer(app);await app.run(`activeJob='${taskID}';pollJob()`);
      assert.equal(app.get("player-detached").checked,false);
      if(controller === "ble-esp32") {
        assert.match(app.get("player-result").textContent,command === "arm"?/自动蓝牙连接.*倒计时.*启用前.*起始页/:/退出维护.*蓝牙广播/);
        const before=app.requests.length;await app.get("upload-recording").fire();
        assert.equal(app.requests.length,before);
        assert.match(app.get("message").textContent,/持久维护/);
      } else assert.match(app.get("player-result").textContent,/USB\/HID/);
    });
  }
}

test("switching controllers clears MAC, confirmation and generic build without probing hardware",async()=>{
  const app=page(universal(),{player:true});await app.ready();await app.run('launchBuild("offline")');
  preparePlayer(app);const originalProfile=app.run("JSON.stringify(project.device)");
  const before=app.requests.length;
  app.get("controller").value="ble-esp32";await app.get("controller").fire("change");
  assert.equal(app.get("player-mac").value,"");assert.equal(app.get("player-detached").checked,false);
  assert.equal(app.run("currentBuild"),null);assert.equal(app.get("flash").disabled,true);
  assert.equal(app.get("download").hidden,true);assert.equal(app.get("firmware-summary").hidden,true);
  assert.match(app.get("player-confirmation").textContent,/持久维护/);
  assert.match(app.get("controller-preview-hint").textContent,/BLE.*不提供键盘/);
  assert.equal(app.run("JSON.stringify(project.device)"),originalProfile);
  await app.get("flash").fire();assert.equal(app.requests.length,before);
  app.get("player-mac").value="aa:bb:cc:dd:ee:ff";app.get("player-detached").checked=true;
  app.get("controller").value="usb-s3";await app.get("controller").fire("change");
  assert.equal(app.get("player-mac").value,"");assert.equal(app.get("player-detached").checked,false);
  assert.match(app.get("player-confirmation").textContent,/USB\/HID/);
});

test("old USB build completing after a controller switch cannot re-enable flash",async()=>{
  const state=universal();let resume;
  state.beforeResponse=async url=>{if(url==="/api/studio/build")await new Promise(resolve=>resume=resolve)};
  const app=page(state);await app.ready();const build=app.run('launchBuild("offline")');
  await app.ready();app.get("controller").value="ble-esp32";await app.get("controller").fire("change");
  resume();await build;
  assert.equal(app.get("flash").disabled,true);assert.equal(app.run("currentBuild"),null);
  assert.match(app.get("job-state").textContent,/控制器已切换/);
});

test("controller switch during project save cancels the upload before posting to a board",async()=>{
  let resume;const state={jobs:[],details:{},beforeResponse:async(url,options)=>{
    if(options.method==="PUT")await new Promise(resolve=>resume=resolve);
  }};
  const app=page(state,{player:true});await app.ready();preparePlayer(app);
  const upload=app.get("upload-recording").fire();await app.ready();
  app.get("controller").value="ble-esp32";await app.get("controller").fire("change");
  resume();await upload;
  assert.ok(!app.requests.some(request=>request.url==="/api/studio/player/upload"));
  assert.match(app.get("message").textContent,/控制器或维护确认已改变/);
});

test("MAC autofill uses only the most recent matching controller; untagged history is USB only",async()=>{
  const jobs=[{...flash(flashID),board_mac:"00:00:00:00:00:01",created:10},
    {...flash(olderID),controller:"ble-esp32",board_mac:"00:00:00:00:00:02",created:20},
    {...flash(taskID),manifest:{controller:"ble-esp32"},board_mac:"00:00:00:00:00:03",created:30}];
  const usb=page({jobs,details:{}},{player:true});await usb.ready();
  assert.equal(usb.get("player-mac").value,"00:00:00:00:00:01");
  const ble=page({jobs,details:{}},{player:true,controller:"ble-esp32"});await ble.ready();
  assert.equal(ble.get("player-mac").value,"00:00:00:00:00:03");
  const unknown=page({jobs:[jobs[0]],details:{}},{player:true,controller:"ble-esp32"});await unknown.ready();
  assert.equal(unknown.get("player-mac").value,"");
});

test("MAC lookup started before controller change cannot repopulate the cleared MAC",async()=>{
  let resume;const app=page({jobs:[],details:{}},{player:true});await app.ready();preparePlayer(app);
  const original=app.run("controllerRevision");
  const pending=new Promise(resolve=>resume=resolve).then(()=>app.run(`fillPlayerMac(${JSON.stringify([flash(flashID)])},${original})`));
  app.get("controller").value="ble-esp32";await app.get("controller").fire("change");
  app.get("controller").value="usb-s3";await app.get("controller").fire("change");
  resume();await pending;assert.equal(app.get("player-mac").value,"");
});

test("recording matches actual control transport and old status without a field remains USB",async()=>{
  const state={jobs:[],details:{},status:{screen:true,device:true,pointer_mode:"absolute",aspect:754/1080,macro:{}}};
  const app=page(state);await app.ready();app.get("width").value="754";app.get("height").value="1080";
  assert.equal(app.run("canRecord()"),true);assert.match(app.get("connection").textContent,/USB/);
  app.get("controller").value="ble-esp32";await app.get("controller").fire("change");
  assert.equal(app.run("canRecord()"),false);assert.equal(app.get("record").disabled,true);
  await app.run("pollStatus()");assert.match(app.get("connection").textContent,/不匹配/);
  const before=app.requests.length;await app.get("play").fire();assert.equal(app.requests.length,before);
  state.status.control_transport="ble";await app.run("pollStatus()");
  assert.equal(app.run("canRecord()"),true);assert.equal(app.get("record").disabled,false);
  assert.match(app.get("connection").textContent,/BLE 蓝牙/);
  app.get("controller").value="usb-s3";await app.get("controller").fire("change");
  assert.equal(app.run("canRecord()"),false);
});

test("BLE arm clears maintenance confirmation before waiting for history refresh",async()=>{
  const job={id:taskID,kind:"player-control",controller:"ble-esp32",command:"arm",status:"succeeded",stage:"完成"};
  const state={jobs:[],details:{[taskID]:job}};
  const app=page(state,{player:true,controller:"ble-esp32"});await app.ready();preparePlayer(app);
  let resume;state.beforeResponse=async url=>{if(url==="/api/studio/jobs")await new Promise(resolve=>resume=resolve)};
  const pending=app.run(`activeJob='${taskID}';pollJob()`);await app.ready();
  assert.equal(app.get("player-detached").checked,false);
  const before=app.requests.length;await app.get("upload-recording").fire();
  assert.equal(app.requests.length,before);assert.match(app.get("message").textContent,/持久维护/);
  resume();await pending;
});
