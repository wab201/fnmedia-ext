#!/usr/bin/env python3
"""飞牛影视·在线扩展 管理页（单文件 HTML，无外部依赖）。"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>飞牛影视 · 在线资源扩展</title>
<style>
  :root{--bg:#0f1115;--card:#181b22;--line:#262b35;--fg:#e8eaed;--mut:#9aa3b2;
        --ac:#4f8cff;--ok:#33c66a;--warn:#f0a020;--bad:#ff5d5d;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1000px;margin:0 auto;padding:24px 18px 80px}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--mut);margin-bottom:20px;font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:16px 18px;margin-bottom:18px}
  .card h2{font-size:15px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
  .badge{font-size:11px;padding:1px 8px;border-radius:99px;background:#20304d;color:#9fc0ff}
  .row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  .muted{color:var(--mut);font-size:12px}
  input[type=text],input[type=number]{background:#12151b;border:1px solid var(--line);
        border-radius:8px;color:var(--fg);padding:8px 10px;width:100%}
  .field{margin-bottom:10px}.field label{display:block;color:var(--mut);font-size:12px;margin-bottom:4px}
  button{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:8px 16px;
         cursor:pointer;font-size:13px}
  button.ghost{background:#222732;border:1px solid var(--line)}
  button.sm{padding:5px 10px;font-size:12px}
  button:disabled{opacity:.5;cursor:default}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--mut);font-weight:500;font-size:12px}
  .nm{font-weight:500}.meta{color:var(--mut);font-size:12px;word-break:break-all}
  .tag{display:inline-block;font-size:10px;color:#9fc0ff;background:#20304d;border-radius:5px;padding:0 5px;margin-left:6px}
  .tag.cu{color:#ffd089;background:#3a2f16}
  .pill{font-size:11px;padding:1px 7px;border-radius:99px;white-space:nowrap}
  .pill.on{color:#9ff0bf;background:#10331f}
  .pill.off{color:#c9ced8;background:#262b35}
  .st{font-size:12px;white-space:nowrap;min-width:120px}
  .st.ok{color:var(--ok)}.st.bad{color:var(--bad)}.st.wait{color:var(--mut)}
  .del{color:#ff8a8a;background:transparent;border:0;cursor:pointer;font-size:12px;padding:4px 6px}
  .del:hover{color:#ff5d5d}
  #toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);
         background:#222;color:#fff;padding:9px 18px;border-radius:99px;font-size:13px;
         opacity:0;transition:.25s;pointer-events:none;border:1px solid var(--line)}
  #toast.show{opacity:1}
  .ok{color:var(--ok)}.warn{color:var(--warn)}
  .chip{display:inline-flex;align-items:center;gap:5px;background:#2a2130;border:1px solid #45303f;
        color:#e8b7cf;border-radius:99px;padding:2px 6px 2px 11px;font-size:12px}
  .chip b{cursor:pointer;font-weight:700;color:#c98aa6;padding:0 3px;border-radius:99px}
  .chip b:hover{color:#ff5d5d}
  textarea:focus,input:focus{outline:none;border-color:var(--ac)}
  .addrow{display:grid;grid-template-columns:160px 1fr auto;gap:10px;margin-top:12px}
  @media(max-width:640px){.addrow{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🎬 飞牛影视 · 在线资源扩展</h1>
  <div class="sub">在原生搜索中注入在线影视并直接播放；状态：<span id="st" class="pill off">检测中…</span>
    <span id="pb" class="muted"></span></div>

  <div class="card">
    <h2>资源站 <span class="badge" id="srcCount"></span>
      <button class="ghost sm" style="margin-left:auto" onclick="testAll()">全部测速</button>
      <button class="ghost sm" onclick="restoreBuiltin()">恢复内置源</button>
    </h2>
    <table>
      <thead><tr><th style="width:42px">启用</th><th style="width:74px">独立媒体库</th><th>资源站</th><th style="width:150px">连通性</th>
        <th style="width:80px">测试</th><th style="width:48px"></th></tr></thead>
      <tbody id="srcRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
    </table>
    <div class="addrow">
      <input type="text" id="addName" placeholder="名称（可选）" style="max-width:150px"/>
      <input type="text" id="addApi" placeholder="苹果CMS JSON/XML 接口，如 https://xxx.com/api.php/provide/vod/at/json"/>
      <label style="display:flex;align-items:center;gap:4px;font-size:12px;white-space:nowrap;cursor:pointer">
        <input type="checkbox" id="addStandalone" style="width:15px;height:15px;accent-color:var(--ac)"/>独立库</label>
      <button onclick="addSource()">添加</button>
    </div>
    <div class="muted" style="margin-top:8px">「库」= 作为独立媒体库（混合电影/剧集/动漫/综艺，无限滚动、点开即播）；「放宽」= 私有源不按标题/备注过滤解说、预告、花絮等非正片词（适合自建、命名不规范的库），但成人等分类仍会被排除。国内直连已规避代理；删除内置源只是隐藏，可点“恢复内置源”找回。</div>
  </div>

  <div class="card">
    <h2>导入 TVBox / 影视仓 / OK影视 配置</h2>
    <div class="muted" style="margin-bottom:10px">
      粘贴订阅地址或整段配置 JSON，自动识别并导入其中<strong>标准苹果CMS（type 0/1，JSON/XML）</strong>影视站；
      Spider 爬虫源（type 3，依赖 jar/JS）、网盘、嗅探解析与直播源会自动跳过。
    </div>
    <div class="addrow">
      <input type="text" id="tvboxUrl" placeholder="配置订阅 URL（http(s)，支持 base64 与多订阅聚合）" style="flex:2"/>
      <button onclick="importTvbox('url')">按 URL 导入</button>
    </div>
    <div class="field" style="margin:10px 0">
      <textarea id="tvboxText" rows="5" style="width:100%;font-family:monospace;font-size:12px"
        placeholder="或直接粘贴 TVBox / 影视仓 / OK影视 配置 JSON（含 sites 数组）…"></textarea>
    </div>
    <div class="addrow">
      <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer">
        <input type="checkbox" id="tvboxStandalone" style="width:15px;height:15px;accent-color:var(--ac)"/>
        导入的源同时「作为独立媒体库」
      </label>
      <button onclick="importTvbox('text')">解析并导入</button>
    </div>
    <div id="tvboxResult" class="muted" style="margin-top:10px;white-space:pre-wrap"></div>
  </div>

  <div class="card">
    <h2>搜索设置</h2>
    <div class="row" style="margin-bottom:12px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="onlineFirst" style="width:16px;height:16px;accent-color:var(--ac)"/>
        在线资源结果<strong style="margin-left:4px">前排展示</strong>（在线片库更齐全；关闭则本地优先）
      </label>
    </div>
    <div class="field" style="max-width:220px">
      <label>每个关键词最多注入条数</label>
      <input type="number" id="maxn" min="1" max="50"/>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="junk" style="width:16px;height:16px;accent-color:var(--ac)"/>
        按关键词过滤噪音（标题或备注命中则不注入）
      </label>
    </div>
    <div class="field">
      <label>噪音关键词 <span id="kwN" class="muted"></span>
        <button class="ghost sm" style="float:right" onclick="toggleKwEdit()">批量编辑</button>
        <button class="ghost sm" style="float:right;margin-right:6px" onclick="resetKw()">恢复默认</button>
      </label>
      <div id="kwChips" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
      <div id="kwEdit" style="display:none;margin-bottom:8px">
        <textarea id="kwArea" rows="4" style="width:100%;background:#12151b;border:1px solid var(--line);
          border-radius:8px;color:var(--fg);padding:8px 10px;font:inherit;resize:vertical"
          placeholder="每行一个，或用逗号/顿号分隔，如：解说,预告片,花絮,枪版"></textarea>
        <button class="ghost sm" style="margin-top:6px" onclick="applyKwText()">应用文本</button>
      </div>
      <div class="row">
        <input type="text" id="kwAdd" placeholder="添加关键词后回车" style="flex:1"/>
        <button class="ghost" onclick="addKw()">添加</button>
      </div>
    </div>
    <button onclick="saveCfg()">保存设置</button>
    <span id="cfgMsg" class="muted"></span>
  </div>

  <div class="card">
    <h2>热门榜单媒体库 <span class="badge" id="hotState">检测中</span>
      <button class="ghost sm" style="margin-left:auto" onclick="refreshHot()">立即刷新</button>
    </h2>
    <div class="row" style="margin-bottom:12px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="hotEnabled" style="width:16px;height:16px;accent-color:var(--ac)"/>
        在飞牛影视媒体库中显示四个真实榜单库（豆瓣周榜/推荐，点击时按需搜源播放）：
        <strong style="margin-left:4px">🎬热门电影 · 📺热门剧集 · 🌸动漫新番 · 🎤热门综艺</strong>
      </label>
    </div>
    <div class="addrow" style="grid-template-columns:1fr">
      <input type="number" id="hotInterval" min="1" max="24" title="榜单自动刷新间隔（小时）" placeholder="榜单刷新小时"/>
    </div>
    <div class="muted" style="margin:6px 0 10px">
      海报/评分/排名来自豆瓣榜单（经 60s 公开接口），后台每数小时刷新一次、打开媒体库秒读缓存；
      点进某部片时才用片名实时搜索内置资源站解析播放，找不到片源会提示「暂无片源」。
    </div>
    <div id="hotStatus" class="meta">等待加载状态…</div>
    <button onclick="saveHotCfg()" style="margin-top:10px">保存榜单设置</button>
    <span id="hotMsg" class="muted"></span>
  </div>

  <div class="card">
    <h2>已缓存在线条目 <span class="badge" id="cacheN"></span></h2>
    <div class="row">
      <input type="text" id="kw" placeholder="按标题过滤（先在飞牛搜索过的会出现在这里）" style="flex:1"/>
      <button class="ghost" onclick="loadOnline()">刷新</button>
    </div>
    <table style="margin-top:10px">
      <thead><tr><th>标题</th><th style="width:90px">来源</th><th style="width:70px">集数</th></tr></thead>
      <tbody id="rows"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody>
    </table>
  </div>
</div>
<div id="toast"></div>

<script>
const $=(id)=>document.getElementById(id);
let CFG=null,KWS=[];
function toast(m,ok=true){const t=$("toast");t.textContent=m;t.className="show "+(ok?"ok":"warn");
  setTimeout(()=>t.className="",2200)}
async function jget(p){const r=await fetch(p);if(!r.ok)throw new Error(p+" "+r.status);return r.json()}
async function jpost(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})});return r.json()}

async function init(){
  const s=await jget("/v/api/ext/status");CFG=s.config;
  $("st").textContent="接管运行中";$("st").className="pill on";
  $("pb").textContent="  已缓存条目："+s.cached_titles;
  $("maxn").value=CFG.max_per_keyword;
  $("junk").checked=CFG.filter_junk;
  $("onlineFirst").checked=CFG.online_first!==false;
  renderHot(s.charts||{});
  KWS=Array.isArray(CFG.junk_keywords)?CFG.junk_keywords.slice():[];
  renderKw();
  renderSources();
  loadOnline();
}

function fmtTs(ts){if(!ts)return "尚未刷新";const d=new Date(ts*1000);const p=(n)=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
function renderHot(h){
  $("hotEnabled").checked=CFG.hot_library_enabled!==false;
  $("hotInterval").value=Math.round((CFG.hot_refresh_interval||21600)/3600);
  const c=h.counts||{};
  const n=(c.movie||0)+(c.tv||0)+(c.anime||0)+(c.variety||0);
  $("hotState").textContent=n+" 条";
  $("hotStatus").textContent=(h.refreshing?"正在后台刷新榜单…":"最近刷新："+fmtTs(h.updated_at))
    +` · 数据源 ${h.provider_ok||0}/${h.provider_total||0}`
    +` · 电影${c.movie||0}/剧${c.tv||0}/动漫${c.anime||0}/综艺${c.variety||0}`
    +(h.last_error?(" · "+h.last_error):"");
}
async function saveHotCfg(){
  const body={
    hot_library_enabled:$("hotEnabled").checked,
    hot_refresh_interval:Math.max(1,Math.min(24,parseInt($("hotInterval").value)||6))*3600
  };
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("热门设置已保存")}
  else toast(r.msg||"保存失败",false);
}
async function refreshHot(){
  toast("开始刷新榜单…");
  const r=await jpost("/v/api/ext/hot/refresh",{});
  const st=(await jget("/v/api/ext/status"));
  CFG=st.config; renderHot(st.charts||r.data||{});
  if(r.code===0) toast("榜单已刷新"); else toast(r.msg||"刷新失败",false);
}
function renderKw(){
  $("kwN").textContent="（共 "+KWS.length+" 个）";
  $("kwChips").innerHTML=KWS.map((k,i)=>
    `<span class="chip">${esc(k)}<b data-i="${i}" title="删除">×</b></span>`).join("");
  $("kwChips").querySelectorAll("b").forEach(b=>b.onclick=()=>{
    KWS.splice(+b.dataset.i,1);saveKw()});
}
async function saveKw(){
  const r=await jpost("/v/api/ext/junk-keywords",{keywords:KWS});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();renderKw();toast("关键词已保存")}
  else toast(r.msg||"保存失败",false);
}
async function addKw(){
  const v=$("kwAdd").value.trim();if(!v)return;
  // 支持一次粘入多个（逗号/顿号/换行分隔）
  v.split(/[,，、\s]+/).filter(Boolean).forEach(k=>{if(!KWS.includes(k))KWS.push(k)});
  $("kwAdd").value="";await saveKw();
}
function toggleKwEdit(){const box=$("kwEdit");
  const show=box.style.display==="none";
  box.style.display=show?"block":"none";
  if(show)$("kwArea").value=KWS.join("\n");}
async function applyKwText(){
  KWS=$("kwArea").value.split(/[,，、\n\r;；\s]+/).map(s=>s.trim()).filter(Boolean)
    .filter((k,i,a)=>a.indexOf(k)===i);
  box=$("kwEdit");box.style.display="none";await saveKw();
}
async function resetKw(){
  if(!confirm("恢复为默认关键词列表？"))return;
  const r=await jpost("/v/api/ext/junk-keywords",{reset:true});
  if(r.code===0){KWS=r.data.junk_keywords;CFG.junk_keywords=KWS.slice();
    renderKw();$("kwEdit").style.display="none";toast("已恢复默认")}
  else toast(r.msg||"失败",false);
}
$("kwAdd").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addKw()}});
function renderSources(){
  const srcs=CFG.sources;
  $("srcCount").textContent=srcs.filter(x=>x.enabled).length+"/"+srcs.length+" 启用";
  $("srcRows").innerHTML=srcs.map(src=>`
    <tr data-id="${src.id}">
      <td><input type="checkbox" class="en" ${src.enabled?"checked":""}/></td>
      <td style="text-align:center;white-space:nowrap">
        <label title="把该资源站单独生成为一张媒体库卡片" style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer">
          <input type="checkbox" class="sa" ${src.standalone?"checked":""}
           ${src.enabled?"":"disabled"} style="width:15px;height:15px;accent-color:var(--ac)"/>库
        </label>
        <label title="私有源放宽：独立库内不按标题/备注过滤解说/预告/花絮等（命名不规范的自抓片更友好）；成人分类仍会被排除"
           style="display:inline-flex;align-items:center;gap:2px;font-size:11px;cursor:pointer;margin-left:4px">
          <input type="checkbox" class="lf" ${src.loose_filter?"checked":""}
           ${(src.enabled&&src.standalone)?"":"disabled"} style="width:15px;height:15px;accent-color:#d97706"/>放宽
        </label>
      </td>
      <td><div class="nm">${esc(src.name)}${src.builtin?'':'<span class="tag cu">自定义</span>'}${src.standalone?'<span class="tag cu">媒体库</span>':''}</div>
          <div class="meta">${esc(src.api)}</div></td>
      <td class="st wait" data-st>未测</td>
      <td><button class="ghost sm" data-test>测试</button></td>
      <td><button class="del" data-del title="删除">✕</button></td>
    </tr>`).join("");
  $("srcRows").querySelectorAll("tr").forEach(tr=>{
    const id=tr.dataset.id;
    tr.querySelector(".en").addEventListener("change",()=>toggleEnable(id,tr.querySelector(".en").checked));
    const sa=tr.querySelector(".sa");
    sa.addEventListener("change",()=>toggleStandalone(id,sa.checked));
    const lf=tr.querySelector(".lf");
    if(lf) lf.addEventListener("change",()=>toggleLoose(id,lf.checked));
    tr.querySelector("[data-test]").addEventListener("click",()=>testOne(id,tr));
    tr.querySelector("[data-del]").addEventListener("click",()=>delSource(id,srcName(id)));
  });
}
async function toggleStandalone(id,on){
  const r=await jpost("/v/api/ext/source/standalone",{id,standalone:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s){s.standalone=on;if(!on)s.loose_filter=false;}
    renderSources();toast(on?"已生成独立媒体库（后台建库中，稍后刷新页面可见）":"已取消独立媒体库");}
  else{toast(r.msg||"操作失败",false);renderSources();}
}
async function toggleLoose(id,on){
  const r=await jpost("/v/api/ext/source/loose-filter",{id,loose_filter:on});
  if(r.code===0){const s=CFG.sources.find(x=>x.id===id);if(s)s.loose_filter=on;
    renderSources();toast(on?"已放宽正片过滤（后台刷新中）":"已恢复严格过滤");}
  else{toast(r.msg||"操作失败",false);renderSources();}
}
function srcName(id){const s=CFG.sources.find(x=>x.id===id);return s?s.name:id}

async function toggleEnable(id,on){
  const sources=CFG.sources.map(s=>({id:s.id,enabled:s.id===id?on:s.enabled}));
  const r=await jpost("/v/api/ext/config",{sources});
  if(r.code===0){CFG.sources.find(x=>x.id===id).enabled=on;
    $("srcCount").textContent=CFG.sources.filter(x=>x.enabled).length+"/"+CFG.sources.length+" 启用";
    toast(on?"已启用":"已停用")}
  else toast(r.msg||"保存失败",false);
}

function setSt(tr,html,cls){const el=tr.querySelector("[data-st]");el.className="st "+cls;el.innerHTML=html}
async function testOne(id,tr){
  setSt(tr,"测试中…","wait");tr.querySelector("[data-test]").disabled=true;
  try{
    const r=await jpost("/v/api/ext/source/test",{id});
    const d=r.data;
    if(d.ok) setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
    else setSt(tr,"✕ "+(d.error||"失败")+(d.elapsed_ms?(" · "+d.elapsed_ms+"ms"):""),"bad");
  }catch(e){setSt(tr,"✕ "+e,"bad")}
  tr.querySelector("[data-test]").disabled=false;
}
async function testAll(){
  toast("正在并发测速…");
  $("srcRows").querySelectorAll("[data-st]").forEach(el=>{el.className="st wait";el.textContent="测试中…"});
  const r=await jpost("/v/api/ext/source/test-all",{});
  (r.data||[]).forEach(d=>{
    const tr=$("srcRows").querySelector(`tr[data-id="${d.id}"]`);if(!tr)return;
    if(d.ok) setSt(tr,`✓ ${d.elapsed_ms}ms · ${d.count}条${d.direct?(' · '+d.direct+'直链'):''}`,"ok");
    else setSt(tr,"✕ "+(d.error||"失败"),"bad");
  });
  toast("测速完成");
}

async function addSource(){
  const name=$("addName").value.trim(),api=$("addApi").value.trim();
  const standalone=$("addStandalone").checked;
  if(!api){toast("请填写接口地址",false);return}
  const r=await jpost("/v/api/ext/source/add",{name,api,standalone});
  if(r.code===0){toast("已添加"+(standalone?"，正在生成独立媒体库…":"，正在测速…"));
    $("addName").value="";$("addApi").value="";$("addStandalone").checked=false;
    await refreshCfg();
    const tr=$("srcRows").querySelector(`tr[data-id="${r.data.id}"]`);
    if(tr&&!standalone) testOne(r.data.id,tr);}
  else toast(r.msg||"添加失败",false);
}
async function importTvbox(kind){
  const standalone=$("tvboxStandalone").checked;
  const payload={standalone};
  if(kind==="url"){payload.url=$("tvboxUrl").value.trim();if(!payload.url){toast("请填写订阅 URL",false);return}}
  else{payload.text=$("tvboxText").value.trim();if(!payload.text){toast("请粘贴配置 JSON",false);return}}
  const out=$("tvboxResult");out.textContent="正在解析并探测，请稍候…";
  try{
    const r=await jpost("/v/api/ext/source/import-tvbox",payload);
    if(r.code!==0){out.textContent="导入失败："+(r.msg||"");return}
    const d=r.data||{};
    const lines=[];
    lines.push(`共识别 ${d.scanned||0} 个站点，可导入苹果CMS源 ${d.added||0} 个，已存在 ${d.duplicate||0} 个，跳过不支持 ${d.skipped||0} 个。`);
    (d.added_names||[]).forEach(n=>lines.push("  + "+n));
    if((d.skipped_detail||[]).length)lines.push("跳过（爬虫/网盘/直播等）："+d.skipped_detail.slice(0,12).join("、")+(d.skipped_detail.length>12?" 等":""));
    out.textContent=lines.join("\n");
    toast("导入完成");await refreshCfg();
  }catch(e){out.textContent="导入出错："+e}
}
async function delSource(id,name){
  if(!confirm("确定删除资源站「"+name+"」？"))return;
  const r=await jpost("/v/api/ext/source/delete",{id});
  if(r.code===0){toast("已删除");await refreshCfg()}else toast(r.msg||"删除失败",false);
}
async function restoreBuiltin(){
  const r=await jpost("/v/api/ext/source/restore",{});
  if(r.code===0){toast("已恢复内置源");await refreshCfg()}else toast(r.msg||"失败",false);
}
async function refreshCfg(){const s=await jget("/v/api/ext/status");CFG=s.config;renderSources()}

async function saveCfg(){
  const body={max_per_keyword:parseInt($("maxn").value)||12,
    filter_junk:$("junk").checked, online_first:$("onlineFirst").checked};
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){toast("已保存");CFG.max_per_keyword=body.max_per_keyword;
    CFG.filter_junk=body.filter_junk;CFG.online_first=body.online_first}
  else toast("保存失败 "+r.msg,false);
}

async function loadOnline(){
  const kw=$("kw").value.trim();
  const r=await jget("/v/api/ext/online?limit=500"+(kw?("&q="+encodeURIComponent(kw)):""));
  const list=r.data||[];$("cacheN").textContent=list.length;
  const tb=$("rows");
  if(!list.length){tb.innerHTML='<tr><td colspan="3" class="muted">暂无，先去飞牛搜索任意中文片名</td></tr>';return}
  tb.innerHTML=list.map(x=>`<tr>
    <td><div class="nm">${esc(x.title)}</div>
      <div class="meta">${x.year||""} ${x.area||""} · ${esc(x.remarks||"")}</div></td>
    <td>${esc(x.provider)}</td><td>${x.episodes}</td>
  </tr>`).join("");
}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
$("kw").addEventListener("input",()=>{clearTimeout(window._t);window._t=setTimeout(loadOnline,250)});
init().catch(e=>{$("st").textContent="未接管/接口不可用";$("st").className="pill off";console.error(e)});
</script>
</body>
</html>
"""
