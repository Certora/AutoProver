const pptxgen = require("pptxgenjs");
const p = new pptxgen();
p.layout = "LAYOUT_WIDE";            // 13.3 x 7.5
const W = 13.3, H = 7.5;

const TEAL="028090", SEA="00A896", MINT="02C39A", INK="12303A", SLATE="5A7684",
      PAPER="FFFFFF", TINT="EAF4F5", RED="B3261E", GREEN="1E7A3C", MUTED="7B8E97";
const HEAD="Cambria", BODY="Calibri", MONO="Courier New";

const title = (s, t, sub) => {
  s.addText(t, {x:0.7,y:0.5,w:W-1.4,h:0.9,fontSize:34,bold:true,color:INK,fontFace:HEAD,isTextBox:true,margin:0});
  if (sub) s.addText(sub, {x:0.7,y:1.35,w:W-1.4,h:0.4,fontSize:14,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
};
const card = (s,x,y,w,h,fill) => s.addShape(p.ShapeType.roundRect,{x,y,w,h,fill:{color:fill||TINT},line:{color:fill||TINT},rectRadius:0.08});

/* 1 — title */
let s = p.addSlide();
s.background = {color: INK};
s.addText("AutoProver for Soroban", {x:0.9,y:2.2,w:W-1.8,h:1.0,fontSize:48,bold:true,color:PAPER,fontFace:HEAD,isTextBox:true,margin:0});
s.addText("Phase 1 progress update", {x:0.9,y:3.2,w:W-1.8,h:0.5,fontSize:22,color:MINT,fontFace:BODY,isTextBox:true,margin:0});
s.addText("Certora  ·  2026-10-01", {x:0.9,y:4.0,w:W-1.8,h:0.4,fontSize:14,color:MUTED,fontFace:BODY,isTextBox:true,margin:0});
s.addText("End-to-end run complete: specs authored, verified by Sunbeam, real defect found", {x:0.9,y:5.4,w:W-2.4,h:0.5,fontSize:15,italic:true,color:"C9DDE1",fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("Headline: the pipeline now runs end to end on a Soroban contract and found a real admin-takeover bug, with a Sunbeam proof.");

/* 2 — status vs kickoff */
s = p.addSlide();
title(s,"Where we are against Phase 1","Commitments from the 2026-09-02 kickoff deck");
const rows = [
  ["AutoSetup prepares a Soroban protocol","DONE","Built into the pipeline. Dependency pinning, harness scaffold, feature gate, compile gate — 30s on an unmodified repo."],
  ["AutoProver PoC produces CVLR specs","DONE","5 rules authored for 4 extracted properties. No human wrote CVLR."],
  ["Sunbeam report showing specs were proved","DONE","3 submissions; verdicts read back into the loop; report generated."],
  ["Specs available for each protocol","PARTIAL","Available for the contract run so far; 2-3 protocols not yet chosen with SDF."],
  ["CLI or cloud option users can try","PARTIAL","CLI works today (see later slide). Cloud option not started."],
];
let y = 2.0;
rows.forEach(([what,state,detail]) => {
  card(s,0.7,y,W-1.4,0.95, state==="DONE" ? TINT : "F4F1EC");
  s.addText(state, {x:0.9,y:y+0.14,w:1.15,h:0.3,fontSize:11,bold:true,color: state==="DONE"?GREEN:"9A6A00",fontFace:BODY,isTextBox:true,margin:0});
  s.addText(what, {x:2.15,y:y+0.1,w:4.4,h:0.4,fontSize:14,bold:true,color:INK,fontFace:BODY,isTextBox:true,margin:0});
  s.addText(detail, {x:6.7,y:y+0.1,w:5.9,h:0.75,fontSize:11.5,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
  y += 1.05;
});
s.addNotes("Three of five green. Be explicit that the two partials are protocol selection (needs SDF) and the cloud option (not started).");

/* 3 — the pipeline */
s = p.addSlide();
title(s,"One command, eight phases, 59 minutes","Run on 2026-09-30 against an unmodified Soroban contract");
const steps = [
  ["1","Design doc discovery","14s"],["2","System analysis","1m 30s"],
  ["3","Property extraction","4 components"],["4","AutoSetup / preflight","30s"],
  ["5","Rule authoring (agent)","21m 55s"],["6","Wasm build + submit","3 jobs"],
  ["7","Verdicts + counterexamples","36s prover"],["8","Report","34s"],
];
let x=0.7; y=2.1;
steps.forEach(([n,label,meta],i) => {
  const cx = 0.7 + (i%4)*3.1, cy = 2.1 + Math.floor(i/4)*2.0;
  card(s,cx,cy,2.85,1.7);
  s.addShape(p.ShapeType.ellipse,{x:cx+0.2,y:cy+0.2,w:0.45,h:0.45,fill:{color:TEAL},line:{color:TEAL}});
  s.addText(n,{x:cx+0.2,y:cy+0.24,w:0.45,h:0.37,fontSize:13,bold:true,color:PAPER,align:"center",fontFace:BODY,isTextBox:true,margin:0});
  s.addText(label,{x:cx+0.2,y:cy+0.78,w:2.45,h:0.55,fontSize:13.5,bold:true,color:INK,fontFace:BODY,isTextBox:true,margin:0});
  s.addText(meta,{x:cx+0.2,y:cy+1.3,w:2.45,h:0.3,fontSize:11,color:SEA,bold:true,fontFace:BODY,isTextBox:true,margin:0});
});
s.addText("Models: Claude Opus 5 + Sonnet 5 via API  ·  prover: certoraSorobanProver (cloud)  ·  spend capped at $25",
  {x:0.7,y:6.4,w:W-1.4,h:0.4,fontSize:12,italic:true,color:MUTED,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("Extraction ran the four components concurrently; the longest took 35 minutes. Authoring is one component (--max-properties 4).");

/* 4 — the contract */
s = p.addSlide();
title(s,"The target: a Soroban token contract","Nothing about the contract was changed — source_edits: 0 in the report");
s.addText("What the agent was given", {x:0.7,y:1.9,w:5.8,h:0.3,fontSize:13,bold:true,color:TEAL,fontFace:BODY,isTextBox:true,margin:0});
card(s,0.7,2.3,5.9,2.0,"0E2A33");
s.addText([
  {text:"pub fn initialize(e: Env, admin: Address) {\n",options:{color:"CFE9EC"}},
  {text:"    e.storage().persistent()\n        .set(&\"ADMIN\", &admin);\n",options:{color:MINT}},
  {text:"}",options:{color:"CFE9EC"}},
],{x:0.95,y:2.5,w:5.4,h:1.6,fontSize:13,fontFace:MONO,isTextBox:true,margin:0,lineSpacing:18});
s.addText("No authorization. No re-initialization guard.", {x:0.7,y:4.45,w:5.9,h:0.3,fontSize:12.5,bold:true,color:RED,fontFace:BODY,isTextBox:true,margin:0});

s.addText("What the agent wrote (5 rules)", {x:6.9,y:1.9,w:5.7,h:0.3,fontSize:13,bold:true,color:TEAL,fontFace:BODY,isTextBox:true,margin:0});
card(s,6.9,2.3,5.7,2.0,"0E2A33");
s.addText([
  {text:"#[rule]\n",options:{color:MINT}},
  {text:"fn admin_rotation_requires_current_admin_auth(\n        e: Env, b: Address) {\n",options:{color:"CFE9EC"}},
  {text:"    let a = admin_opt(&e).unwrap();\n    let authorized = is_auth(a.clone());\n    Token::initialize(e.clone(), b.clone());\n    let rotated = admin_opt(&e) == Some(b);\n",options:{color:"CFE9EC"}},
  {text:"    cvlr_assert!(!rotated || authorized);",options:{color:MINT}},
],{x:7.1,y:2.42,w:5.35,h:1.8,fontSize:10,fontFace:MONO,isTextBox:true,margin:0,lineSpacing:14});
s.addText("Properties came from the repo's own docs and code; the user wrote no CVLR.",
  {x:6.9,y:4.45,w:5.7,h:0.5,fontSize:12,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});

card(s,0.7,5.2,11.9,1.5);
s.addText("4 properties extracted → 5 rules → complete coverage, 0 skipped, 0 source edits",
  {x:1.0,y:5.45,w:11.3,h:0.4,fontSize:16,bold:true,color:INK,fontFace:HEAD,isTextBox:true,margin:0});
s.addText("The harness, the conf, the build script and the wasm were all generated.",
  {x:1.0,y:5.9,w:11.3,h:0.4,fontSize:12,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("The rule shown is lightly abridged for the slide; the real file is in the repo under demos/soroban_token.");

/* 5 — verdicts */
s = p.addSlide();
title(s,"Sunbeam verdicts","Job is public — open it during the call");
const verdicts = [
  ["initialize_persists_admin","VERIFIED",GREEN],
  ["initialize_then_mint_uses_stored_admin","VERIFIED",GREEN],
  ["unauthenticated_admin_overwrite_enables_unlimited_mint","VIOLATED",RED],
  ["initialize_is_one_shot","VIOLATED",RED],
  ["admin_rotation_requires_current_admin_auth","VIOLATED",RED],
];
y = 2.0;
verdicts.forEach(([name,v,c]) => {
  card(s,0.7,y,9.0,0.62,"F7FAFA");
  s.addText(name,{x:0.95,y:y+0.14,w:8.5,h:0.35,fontSize:13,color:INK,fontFace:MONO,isTextBox:true,margin:0});
  card(s,9.9,y,2.7,0.62, v==="VERIFIED"?"E6F4EA":"FBEAE8");
  s.addText(v,{x:9.9,y:y+0.15,w:2.7,h:0.35,fontSize:12.5,bold:true,color:c,align:"center",fontFace:BODY,isTextBox:true,margin:0});
  y += 0.72;
});
s.addText("The three violations are the deliverable, not a failure.",{x:0.7,y:5.75,w:11.9,h:0.35,fontSize:15,bold:true,color:INK,fontFace:HEAD,isTextBox:true,margin:0});
s.addText("The agent read each counterexample, judged the defect real, and marked the rule expected-to-fail — so the run completed with the bugs in it rather than weakening the rules until they passed.",
  {x:0.7,y:6.15,w:11.9,h:0.7,fontSize:12.5,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("Link: prover.certora.com/jobStatus/33158/f6379ea6... with anonymousKey — no Certora login needed.");

/* 6 — the finding */
s = p.addSlide();
s.background = {color: INK};
s.addText("One critical defect, with a proof", {x:0.7,y:0.6,w:W-1.4,h:0.8,fontSize:32,bold:true,color:PAPER,fontFace:HEAD,isTextBox:true,margin:0});
s.addText("Unauthenticated re-initialization → mint takeover", {x:0.7,y:1.4,w:W-1.4,h:0.4,fontSize:16,color:MINT,fontFace:BODY,isTextBox:true,margin:0});
const bits = [
  ["Impact","Any address can call initialize at any time and install itself as admin, displacing the legitimate one. The admin slot gates minting, so the attacker can inflate supply without bound."],
  ["Attack path","1. initialize(a) stores legitimate admin a.  2. Attacker b calls initialize(b) with no authorization from a.  3. No guard, no require_auth — the slot is overwritten. b now mints freely."],
  ["Evidence","Prover counterexample: the stored admin authorized nothing, yet initialize(b) completed. The trace shows the body contains exactly two operations — a key conversion and one put_contract_data."],
];
y = 2.1;
bits.forEach(([h,t]) => {
  s.addText(h,{x:0.7,y:y,w:2.2,h:0.35,fontSize:14,bold:true,color:MINT,fontFace:BODY,isTextBox:true,margin:0});
  s.addText(t,{x:3.0,y:y,w:9.6,h:1.1,fontSize:13,color:"DCE9EC",fontFace:BODY,isTextBox:true,margin:0});
  y += 1.45;
});
s.addText("Report includes impact, attack path, stated assumptions, and the counterexample as proof of concept.",
  {x:0.7,y:6.5,w:11.9,h:0.4,fontSize:12,italic:true,color:MUTED,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("The report emitted this as three findings sharing one root cause; the grouping step should have merged them. Known defect in the report layer, not in the Soroban work.");

/* 7 — how to run it */
s = p.addSlide();
title(s,"How you run it today","Asked on Slack: the CLI exists and this is the whole invocation");
card(s,0.7,2.0,11.9,1.45,"0E2A33");
s.addText([
  {text:"console-soroban",options:{color:MINT,bold:true}},
  {text:" ./my-protocol ",options:{color:"CFE9EC"}},
  {text:"src/lib.rs:Token",options:{color:"FFD79A"}},
  {text:" \\\n        --budget budget.json --max-properties 4",options:{color:"CFE9EC"}},
],{x:1.0,y:2.3,w:11.3,h:0.9,fontSize:15,fontFace:MONO,isTextBox:true,margin:0,lineSpacing:24});
const args = [
  ["./my-protocol","the repo — cargo workspace root, unmodified"],
  ["src/lib.rs:Token","the contract: path to its source, plus the #[contract] type"],
  ["--budget","USD cap on LLM spend; the run wraps up when it approaches it"],
  ["--max-properties","bound how much work it takes on (omit for everything)"],
];
y = 3.7;
args.forEach(([a,d]) => {
  s.addText(a,{x:0.7,y:y,w:3.3,h:0.35,fontSize:12.5,bold:true,color:TEAL,fontFace:MONO,isTextBox:true,margin:0});
  s.addText(d,{x:4.2,y:y,w:8.4,h:0.35,fontSize:12.5,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
  y += 0.52;
});
card(s,0.7,5.95,11.9,0.95,"F7FAFA");
s.addText("Also available: tui-soroban (live progress view)  ·  --interactive to review extracted properties before authoring  ·  --properties to replay a saved run cheaply",
  {x:1.0,y:6.12,w:11.3,h:0.65,fontSize:11.5,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("Prerequisites: ANTHROPIC_API_KEY, CERTORAKEY, a local postgres (docker compose), and a one-time Certora login for reading results. All open-source on the branch.");

/* 8 — asks */
s = p.addSlide();
title(s,"What we need from you","So Phase 1 lands on protocols you care about");
const asks = [
  ["Pick 2-3 protocols","From the kickoff list: soroswap, phoenix, blend, templar, kinetic, one-way-channel. Nothing is formally chosen yet, and Phase 1 says \"chosen with SF\"."],
  ["Tell us their soroban-sdk versions","We support SDK 22, 25 and 26. SDK 23 has no cvlr-soroban branch yet — if a chosen protocol is on 23, we need one cut."],
  ["Meridian logistics","Date, slot length, and audience, so we can shape the talk and the live demo."],
];
y = 2.1;
asks.forEach(([h,t],i) => {
  card(s,0.7,y,11.9,1.4);
  s.addShape(p.ShapeType.ellipse,{x:1.0,y:y+0.45,w:0.5,h:0.5,fill:{color:TEAL},line:{color:TEAL}});
  s.addText(String(i+1),{x:1.0,y:y+0.5,w:0.5,h:0.4,fontSize:15,bold:true,color:PAPER,align:"center",fontFace:BODY,isTextBox:true,margin:0});
  s.addText(h,{x:1.8,y:y+0.2,w:4.0,h:0.4,fontSize:15,bold:true,color:INK,fontFace:BODY,isTextBox:true,margin:0});
  s.addText(t,{x:5.9,y:y+0.2,w:6.5,h:1.0,fontSize:12,color:SLATE,fontFace:BODY,isTextBox:true,margin:0});
  y += 1.6;
});
s.addText("Status, stated plainly: one contract proven end to end so far, and it is a tutorial contract rather than one of yours. Everything is open-source.",
  {x:0.7,y:6.9,w:11.9,h:0.4,fontSize:12,italic:true,color:MUTED,fontFace:BODY,isTextBox:true,margin:0});
s.addNotes("If asked about greenfield: not built, and the term needs defining before we commit. If asked about multi-contract: that is Phase 2.");

p.writeFile({fileName:"AutoProver-Soroban-Update-2026-10-01.pptx"}).then(f=>console.log("wrote",f));
