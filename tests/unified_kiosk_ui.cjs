"use strict";
/* Integration-style DOM simulation for the single student/staff kiosk.
 * No network, browser, external modules or production database required.
 * Run: node tests/unified_kiosk_ui.cjs
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("kiosk.html", "utf8");
const scriptMatch = html.match(/<script>\s*([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "Unified kiosk inline script is present");
assert.ok(!html.includes('href="/staff"'), "Kiosk must not link to a separate staff page");
for(const required of ["roleChooser", "chooseStudent", "chooseStaff", "actionChooser", "staffIdentification", "staffId", "student", "pinSetup", "review"]) {
 assert.match(html, new RegExp('id="' + required + '"'), "Kiosk element " + required + " must exist");
}

class FakeClassList {
 constructor(){this.items = new Set();}
 add(name){this.items.add(name);}
 remove(name){this.items.delete(name);}
 toggle(name, value){
  if(value === undefined) value = !this.items.has(name);
  if(value) this.items.add(name); else this.items.delete(name);
  return Boolean(value);
 }
 contains(name){return this.items.has(name);}
}
class FakeElement {
 constructor(tag = "div") {
  this.tag = tag;
  this.classList = new FakeClassList();
  this.dataset = {};
  this.children = [];
  this.value = "";
  this.textContent = "";
  this.innerHTML = "";
  this.disabled = false;
  this.listeners = {};
  this.onclick = null;
 }
 addEventListener(event, listener){this.listeners[event] = listener;}
 dispatch(event){return this.listeners[event]?.({key:event, preventDefault(){}});}
 setAttribute(){}
 append(...items){this.children.push(...items);}
 getContext(){return {};}
 pause(){}
}
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);
const unique = new Set(ids);
assert.equal(unique.size, ids.length, "Duplicate element IDs are not allowed");
const elements = new Map(ids.map(id=>[id,new FakeElement()]));
const $ = id => {
 assert.ok(elements.has(id), "Element " + id + " must be defined in the HTML");
 return elements.get(id);
};
const requests=[];
async function fetchStub(url, options={}) {
 const body = options.body?JSON.parse(options.body):null;
 requests.push({url,body});
 let payload;
 switch(url) {
  case "/api/umbrellas": payload = [{id:"UMB-0001",status:"AVAILABLE"}]; break;
  case "/api/student-pin/status": payload = {has_pin:body.number!==17}; break;
  case "/api/student-pin/enroll": payload = {ok:true,student_id:"6-4-17"}; break;
  case "/api/borrow":
  case "/api/staff/borrow":
   payload = {ok:true,id:body.umbrella_id,status:"BORROWED",
     due_at:"2026-11-12T08:00:00+00:00",remaining:9};
   break;
  case "/api/return":
  case "/api/staff/return":
   payload = {ok:true,id:body.umbrella_id,status:"AVAILABLE"};
   break;
  default: throw new Error("Unexpected kiosk API request: "+url);
 }
 return {ok:true,status:200,json:async()=>payload};
}
const sandbox = {
 document:{
  getElementById: $,
  createElement:tag=>new FakeElement(tag)
 },
 window:{addEventListener(){}},
 navigator:{},
 setInterval(){},
 console,
 fetch:fetchStub,
 Date,
};
vm.createContext(sandbox);
vm.runInContext(scriptMatch[1], sandbox, {filename:"kiosk.html"});
const run = code => vm.runInContext(code, sandbox);
const pause = async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
const click=async(id)=>{assert.equal(typeof $(id).onclick,"function","Clickable "+id);$(id).onclick();await pause();};

(async()=>{
 assert.equal(run("role"),null,"first screen starts with no role");
 assert.ok(!$("roleChooser").classList.contains("hidden"));
 assert.ok($("actionChooser").classList.contains("hidden"));
 assert.ok($("back").classList.contains("hidden"));

 // Staff: role -> borrow -> staff ID -> shared QR -> shared PIN -> confirmation
 await click("chooseStaff");
 assert.equal(run("role"),"staff");
 assert.ok($("roleChooser").classList.contains("hidden"));
 assert.ok(!$("actionChooser").classList.contains("hidden"));
 await click("startBorrow");
 assert.equal(run("step"),6);
 assert.ok(!$("next").disabled===false||$("next").disabled);
 $("staffId").value="TEACH01";
 $("staffId").listeners.input();
 assert.ok(!$("next").disabled);
 await click("next");
 assert.equal(run("step"),3);
 $("umbrella").value="UMB-0001";
 $("umbrella").listeners.input();
 assert.ok(!$("next").disabled,"valid umbrella id enables shared next");
 await click("next");
 assert.equal(run("step"),4);
 assert.equal($("vRoleLabel").textContent,"교직원");
 assert.equal($("vStudent").textContent,"TEACH01");
 assert.equal($("reviewPinLabel").textContent,"교직원 PIN (숫자 6자리)");
 $("studentPin").value="123456";
 await click("next");
 assert.equal(run("step"),5);
 assert.ok(requests.some(r=>r.url==="/api/staff/borrow" && r.body.staff_id==="TEACH01"));
 assert.ok(!$("doneMessage").textContent.includes("이번 달 남은 대여 횟수"));
 assert.ok($("doneMessage").textContent.includes("반납 예정:"));
 await click("back");
 assert.equal(run("role"),null);
 assert.ok(!$("roleChooser").classList.contains("hidden"));

 // Student: same kiosk, existing ID/PIN flow is preserved
 await click("chooseStudent");await click("startBorrow");
 assert.equal(run("step"),1);
 run("grade=6;klass=4;number=16;");
 await click("next");
 assert.equal(run("step"),3,"registered student proceeds to QR");
 $("umbrella").value="UMB-0001";$("umbrella").listeners.input();
 await click("next");
 assert.equal(run("step"),4);
 assert.equal($("vRoleLabel").textContent,"학생");
 assert.equal($("reviewPinLabel").textContent,"학생 PIN (숫자 6자리)");
 $("studentPin").value="482739";
 await click("next");
 assert.ok(requests.some(r=>r.url==="/api/borrow" && r.body.grade===6 && r.body.number===16));
 assert.ok($("doneMessage").textContent.includes("이번 달 남은 대여 횟수"));
 await click("back");

 // Staff return uses the same review page and staff API, not a new URL
 await click("chooseStaff");await click("startReturn");
 $("staffId").value="TEACH01";$("staffId").listeners.input();await click("next");
 $("umbrella").value="UMB-0001";$("umbrella").listeners.input();await click("next");
 $("studentPin").value="123456";await click("next");
 assert.ok(requests.some(r=>r.url==="/api/staff/return" && r.body.staff_id==="TEACH01"));
 assert.ok($("doneMessage").textContent.includes("반납"));
 await click("back");

 // First-time student still can create a PIN inside the unified kiosk
 await click("chooseStudent");await click("startBorrow");
 run("grade=6;klass=4;number=17;");
 await click("next");
 assert.equal(run("step"),2,"unregistered student sees first PIN registration");
 $("newStudentPin").value="482739";$("confirmStudentPin").value="482739";
 await click("next");
 assert.equal(run("step"),3,"student registration is immediately usable");
 assert.ok(requests.some(r=>r.url==="/api/student-pin/enroll"&&r.body.number===17));

 console.log("Unified kiosk UI checks passed: role selection, staff/student borrow, staff return, student first PIN.");
})().catch(error=>{console.error(error);process.exitCode=1;});
