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
function page(state, {player=false} = {}) {
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
  let fails=state.failJobReads || 0;
  const requests=[];
  const fetch=async(url,options={})=>{
    requests.push({url,options});
    let data;
    if(url==="/api/studio/status") data={projects:[],ports:[{port:"/dev/MOCK-ONLY",description:"mock"}],tools:{},device_port:7400};
    else if(url==="/api/status") data={screen:false,device:false,macro:{}};
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
    } else if(url.startsWith("/api/studio/projects/") && options.method==="PUT") {
      data=JSON.parse(options.body);
    } else if(url==="/api/studio/player/upload" || url==="/api/studio/player/control") {
      data={id:taskID,kind:url.endsWith("upload")?"upload":"player-control",status:"queued",stage:"排队"};
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
  assert.deepEqual(JSON.parse(request.options.body),{mode:"offline",project_id:null});
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
    const payload={port:"/dev/MOCK-ONLY",expected_mac:"00:11:22:33:44:55",hid_disconnected:true,project_id:flashID};
    if(command)payload.command=command;
    assert.equal(calls[1].options.method,"POST");
    assert.deepEqual(JSON.parse(calls[1].options.body),payload);
    assert.equal(app.run("activeJob"),taskID);
    assert.equal(app.run("playerPending"),false);
    assert.equal(app.get("job-state").textContent,"排队");
  });
}
