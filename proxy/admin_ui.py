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
      <thead><tr><th style="width:46px">启用</th><th>资源站</th><th style="width:150px">连通性</th>
        <th style="width:90px">测试</th><th style="width:56px"></th></tr></thead>
      <tbody id="srcRows"><tr><td colspan="5" class="muted">加载中…</td></tr></tbody>
    </table>
    <div class="addrow">
      <input type="text" id="addName" placeholder="名称（可选）"/>
      <input type="text" id="addApi" placeholder="苹果CMS JSON 接口，如 https://xxx.com/api.php/provide/vod/at/json"/>
      <button onclick="addSource()">添加</button>
    </div>
    <div class="muted" style="margin-top:8px">国内直连已规避代理；删除内置源只是隐藏，可点“恢复内置源”找回。</div>
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
    <h2>在线热门媒体库 <span class="badge" id="hotState">检测中</span>
      <button class="ghost sm" style="margin-left:auto" onclick="refreshHot()">立即刷新</button>
    </h2>
    <div class="row" style="margin-bottom:12px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="hotEnabled" style="width:16px;height:16px;accent-color:var(--ac)"/>
        在飞牛影视媒体库中显示四个在线分类库：
        <strong style="margin-left:4px">🌐在线电影 · 🌐在线剧集 · 🌐在线动漫 · 🌐在线综艺</strong>
      </label>
    </div>
    <div class="addrow" style="grid-template-columns:1fr 1fr 1fr 1fr">
      <input type="number" id="hotLimit" min="60" max="800" title="四库合计保留条数上限" placeholder="总条数上限"/>
      <input type="number" id="hotPerSource" min="20" max="240" title="每个资源站抓取条数" placeholder="每源条数"/>
      <input type="number" id="hotPages" min="1" max="10" title="每个资源站翻最近更新页数（每页约20条）" placeholder="每源页数"/>
      <input type="number" id="hotInterval" min="1" max="24" title="自动刷新间隔（小时）" placeholder="刷新小时"/>
    </div>
    <div class="muted" style="margin:6px 0 10px">
      后台深度爬取各源「最近更新」多页并跨源去重，自动归入电影/剧集/动漫/综艺，数值越大内容越多、刷新越慢。
      打开媒体库只读缓存（秒开），缓存过期会在后台自动更新，失败保留旧缓存。
    </div>
    <div id="hotStatus" class="meta">等待加载状态…</div>
    <button onclick="saveHotCfg()" style="margin-top:10px">保存热门设置</button>
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
  renderHot(s.hot||{});
  KWS=Array.isArray(CFG.junk_keywords)?CFG.junk_keywords.slice():[];
  renderKw();
  renderSources();
  loadOnline();
}

function renderHot(h){
  $("hotEnabled").checked=CFG.hot_library_enabled!==false;
  $("hotLimit").value=CFG.hot_limit||300;
  $("hotPerSource").value=CFG.hot_per_source||120;
  $("hotPages").value=CFG.hot_pages_per_source||6;
  $("hotInterval").value=Math.round((CFG.hot_refresh_interval||21600)/3600);
  const n=h.count||0, ok=h.provider_ok||0, total=h.total_sources||0;
  $("hotState").textContent=n+" 条";
  $("hotStatus").textContent=(h.refreshing?"正在后台深度刷新…":"最近刷新："+(h.updated_at_text||"尚未刷新"))
    +` · 成功线路 ${ok}/${total}` + (h.last_error?(" · "+h.last_error):"");
}
async function saveHotCfg(){
  const body={
    hot_library_enabled:$("hotEnabled").checked,
    hot_limit:Math.max(60,Math.min(800,parseInt($("hotLimit").value)||300)),
    hot_per_source:Math.max(20,Math.min(240,parseInt($("hotPerSource").value)||120)),
    hot_pages_per_source:Math.max(1,Math.min(10,parseInt($("hotPages").value)||6)),
    hot_refresh_interval:Math.max(1,Math.min(24,parseInt($("hotInterval").value)||6))*3600
  };
  const r=await jpost("/v/api/ext/config",body);
  if(r.code===0){Object.assign(CFG,body);toast("热门设置已保存")}
  else toast(r.msg||"保存失败",false);
}
async function refreshHot(){
  toast("开始刷新热门缓存…");
  const r=await jpost("/v/api/ext/hot/refresh",{});
  const cfg=(await jget("/v/api/ext/status"));
  CFG=cfg.config; renderHot(cfg.hot||r.data||{});
  if(r.code===0) toast("热门缓存已刷新"); else toast(r.msg||"刷新失败",false);
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
      <td><div class="nm">${esc(src.name)}${src.builtin?'':'<span class="tag cu">自定义</span>'}</div>
          <div class="meta">${esc(src.api)}</div></td>
      <td class="st wait" data-st>未测</td>
      <td><button class="ghost sm" data-test>测试</button></td>
      <td><button class="del" data-del title="删除">✕</button></td>
    </tr>`).join("");
  $("srcRows").querySelectorAll("tr").forEach(tr=>{
    const id=tr.dataset.id;
    tr.querySelector(".en").addEventListener("change",()=>toggleEnable(id,tr.querySelector(".en").checked));
    tr.querySelector("[data-test]").addEventListener("click",()=>testOne(id,tr));
    tr.querySelector("[data-del]").addEventListener("click",()=>delSource(id,srcName(id)));
  });
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
  if(!api){toast("请填写接口地址",false);return}
  const r=await jpost("/v/api/ext/source/add",{name,api});
  if(r.code===0){toast("已添加，正在测速…");$("addName").value="";$("addApi").value="";
    await refreshCfg();
    const tr=$("srcRows").querySelector(`tr[data-id="${r.data.id}"]`);
    if(tr) testOne(r.data.id,tr);}
  else toast(r.msg||"添加失败",false);
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
