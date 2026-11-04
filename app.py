"""Bigg Boss 20 fan-voting screen (simulation). Run: python app.py  ->  open http://localhost:8000
Needs only Flask. Names live in namelist.py. Everything else (photos, votes, settings) is kept
in the browser/WebView localStorage."""
import json
import os

from flask import Flask, Response

from namelist import CONTESTANTS, VOTERS

app = Flask(__name__)


@app.get("/")
@app.get("/bb")
def index():
    data = {"contestants": [dict(c, n=i + 1) for i, c in enumerate(CONTESTANTS)], "voters": VOTERS}
    return Response(PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False)),
                    mimetype="text/html")


@app.get("/health")
def health():
    return "ok"


PAGE = r"""<!doctype html>
<html lang="hi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Bigg Boss 20 Fan Voting</title>
<style>
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
body{background:radial-gradient(60rem 30rem at 50% -5%,#fff3d6,#efe3cc);font-family:system-ui,"Noto Sans Devanagari",sans-serif;color:#1b1630;display:flex;justify-content:center}
#app{width:100%;max-width:540px;padding:10px 8px 80px}
.hd{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.live{background:#ff2d4d;color:#fff;font-weight:800;padding:7px 18px;border-radius:20px}
#clk{font:700 15px monospace}.air{margin-left:auto;font-size:11px;font-weight:700;color:#ff3366;background:#fff;padding:4px 10px;border-radius:12px}
.ttl{text-align:center;font:900 26px/1 sans-serif;color:#e8a21a;letter-spacing:1px;margin:4px 0}
.sub2{text-align:center;font-size:10px;color:#7a7390;margin-bottom:6px}
#feed{height:54px;margin-bottom:6px}
.ft{display:flex;align-items:center;gap:8px;background:#1b1233;color:#fff;border-radius:16px;padding:8px 12px;box-shadow:0 8px 24px #0005;animation:in .5s cubic-bezier(.2,1.4,.4,1);font-size:13px}
@keyframes in{from{transform:translateY(-30px) scale(.9);opacity:0}}
.ft img,.va{width:36px;height:36px;border-radius:50%;object-fit:cover;flex:none}
.va{display:grid;place-items:center;font-weight:700;color:#fff}
.ft i{opacity:.6;font-style:normal}.ft em{margin-left:auto;background:#ff7a1a;border-radius:12px;padding:3px 9px;font-style:normal;font-weight:800}
.ft.sub{background:linear-gradient(90deg,#ff3366,#ffab2e);justify-content:center;font-weight:800;height:54px}
.tot{display:flex;justify-content:space-between;align-items:center;background:#fff;border:2px solid #36e2c8;border-radius:14px;padding:8px 12px;margin-bottom:8px}
.tot small{display:block;font-size:10px;font-weight:700;color:#ff7a1a}.tot b{font-size:20px}.tot .r{text-align:right}.tot .r b{font-size:13px;color:#ff7a1a;display:block}.tot .r small{color:#7a7390}
#duo{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
#pod{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:8px 0}
.zones{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.zl{display:grid;gap:6px;align-content:start}
.zt{font-size:9px;font-weight:800;text-align:center;border-radius:10px;padding:4px;margin-bottom:6px;background:#dff5ea;color:#139464}
.zones>div:nth-child(2) .zt{background:#dbeaff;color:#2a72c8}.zones>div:nth-child(3) .zt{background:#ffdede;color:#d23a3a}
.slot{min-width:0}
.mom{position:relative;background:#fff;border-radius:12px;padding:6px 10px 14px;display:flex;justify-content:space-between;font-size:10px;font-weight:800}
.mb{position:absolute;left:10px;right:10px;bottom:5px;height:6px;border-radius:4px;background:#36c8ff}.mb i{display:block;height:100%;background:#ffab2e;border-radius:4px;transition:width .6s}
.c{position:relative;display:flex;align-items:center;gap:6px;padding:6px;background:#fff;border:2px solid #f2dcae;border-radius:14px;min-height:56px;overflow:hidden;transition:border-color .4s,box-shadow .4s}
.rk{position:absolute;top:0;left:0;background:#18b878;color:#fff;font:700 9px/1 sans-serif;padding:3px 5px;border-radius:0 0 8px 0;z-index:2}
.av{width:34px;height:34px;border-radius:50%;object-fit:cover;flex:none;border:2px solid #fff;box-shadow:0 0 0 2px #ffd27a}
.in{min-width:0;flex:1;padding-top:6px}
.nm{font-weight:700;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.chip{font-size:9px;font-weight:700;color:#fff;padding:1px 6px;border-radius:9px;background:#18b878}
.v{font-weight:800;font-size:10px;color:#1e90d6}.pc{font-size:9px;color:#7a7390}
.bar{height:4px;background:#eee;border-radius:3px;margin-top:3px}.bar i{display:block;height:100%;background:linear-gradient(90deg,#ffab2e,#ff3366);border-radius:3px;transition:width .6s}
.bdg{position:absolute;top:4px;right:4px;font:700 8px/1 sans-serif;padding:3px 5px;border-radius:6px;color:#fff}
.bdg:empty{display:none}
.c.big{padding:14px 8px 8px;gap:8px;border-color:#ffb43a;min-height:128px}
.big .av{width:66px;height:66px}.big .nm{font-size:16px}.big .v{font-size:19px;text-transform:uppercase}.big .chip{font-size:11px}.big .rk{font-size:11px}
.c.pod .av{width:42px;height:42px}.pod .nm{font-size:13px}.pod .v{font-size:11px}
.c.ev{filter:grayscale(1);opacity:.7}.c.nom{border-color:#ff3366;box-shadow:0 0 14px #ff336655}
.hit{animation:hit .7s}@keyframes hit{50%{box-shadow:0 0 18px #36c8ffaa;transform:scale(1.025)}}
.pop{position:absolute;right:8px;bottom:6px;font-weight:800;color:#18b878;animation:up 1.2s forwards;z-index:3}
@keyframes up{to{transform:translateY(-28px);opacity:0}}
.cta{margin-top:10px;text-align:center;background:#fff;border:2px solid #ffab2e;border-radius:14px;padding:10px;font-weight:800;font-size:13px}
h2{margin:8px 0 4px}.hint{font-size:12px;color:#7a7390;margin-bottom:10px}
.sr{display:flex;align-items:center;gap:10px;padding:8px;background:#fff;border-radius:14px;margin-bottom:8px;border:2px solid #f2dcae}
.sr img{width:46px;height:46px;border-radius:50%;object-fit:cover;cursor:pointer}.sr b{flex:1;font-size:13px}.sr span{font-size:11px;color:#7a7390}
.sr button{border:0;border-radius:10px;padding:7px 8px;font-size:11px;background:#eee}
.sr button.n.on{background:#ff3366;color:#fff}.sr button.e.on{background:#222;color:#fff}
.go,.ctl button{width:100%;border:0;border-radius:16px;padding:15px;font-weight:800;font-size:16px;color:#fff;background:linear-gradient(120deg,#ff3366,#ffab2e)}
.ctl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.ctl button{font-size:13px;padding:11px;background:#1b1233}
#gear{position:fixed;right:12px;bottom:14px;z-index:10;width:48px;height:48px;border-radius:50%;border:0;background:linear-gradient(135deg,#ff3366,#ffab2e);color:#fff;font-size:22px;box-shadow:0 6px 18px #0004}
#sheet{position:fixed;inset:0;background:#000a;display:none;z-index:20}#sheet.on{display:flex;align-items:flex-end;justify-content:center}
#sheet>div{background:#fff8ec;width:100%;max-width:540px;max-height:88vh;overflow:auto;border-radius:22px 22px 0 0;padding:14px}
</style></head><body><div id="app">

<div id="setup">
 <h2>Bigg Boss 20 · Contestants</h2>
 <p class="hint">Naam par tap karke gallery se photo lagao. Phir Start Voting dabao.</p>
 <div id="list"></div>
 <button class="go" id="go">▶ Start Voting</button>
</div>

<div id="board" hidden>
 <div class="hd"><span class="live">▶ LIVE</span><span id="clk">00:00:00</span><span class="air">● ON AIR</span></div>
 <div class="ttl">BIGG BOSS 20 · FAN VOTING</div>
 <div class="sub2">Entertainment simulation · not the official show vote</div>
 <div id="feed"></div>
 <div class="tot"><div><small>LIVE FAN VOTING</small><b id="tot"></b></div><div class="r"><b id="vpm"></b><small id="gap"></small></div></div>
 <div id="duo"></div>
 <div class="mom"><span id="ml"></span><span>⚔ REAL-TIME MOMENTUM CLASH ⚔</span><span id="mr"></span><div class="mb"><i id="mf"></i></div></div>
 <div id="pod"></div>
 <div class="zones">
  <div><div class="zt">✨ SAFE ZONE · RANKS 6–10</div><div class="zl"></div></div>
  <div><div class="zt">⚡ BATTLE ZONE · 11–15</div><div class="zl"></div></div>
  <div><div class="zt">🚨 DANGER · 16–20</div><div class="zl"></div></div>
 </div>
 <div class="cta">🔥 VOTE NOW! TYPE !vote &lt;1–20&gt; IN LIVE CHAT 🔥</div>
</div>

<button id="gear" hidden>⚙</button>
<div id="sheet"><div>
 <h2>Settings</h2><p class="hint">Nominated = laal border. Evicted = black &amp; white, uske vote ruk jaate hain. Photo pe tap = badlo.</p>
 <div id="rows"></div>
 <div class="ctl">
  <button id="mute">🔊 Voice ON</button><button id="subnow">🔔 Subscribe bolo</button>
  <button id="reset">♻ Votes reset</button><button id="close">✔ Done</button>
 </div>
</div></div>
<input type="file" id="file" accept="image/*" hidden>
</div>
<script>
const D=__DATA__,$=s=>document.querySelector(s);
const LS={g(k,d){try{const v=JSON.parse(localStorage.getItem('bb_'+k));return v==null?d:v}catch(e){return d}},s(k,v){try{localStorage.setItem('bb_'+k,JSON.stringify(v))}catch(e){}}};
const R=(a,b)=>a+Math.random()*(b-a),fmt=n=>Math.round(n).toLocaleString('en-US');
const photos=LS.g('photos',{}),saved=LS.g('state',[]);
const C=D.contestants.map(c=>{const s=saved.find(x=>x.id===c.id)||{};return{...c,votes:s.votes||c.start,st:s.st||'on',photo:photos[c.id]||null,shown:0,tw:0,rank:0,w:c.id==='qazi'?14:c.id==='arishfa'?10:R(3,9)}});
const by=id=>C.find(c=>c.id===id),act=()=>C.filter(c=>c.st!=='ev');
const hue=s=>[...s].reduce((h,ch)=>(h*31+ch.charCodeAt(0))%360,7);
const avatar=(t,h)=>'data:image/svg+xml;utf8,'+encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" fill="hsl(${h},70%,60%)"/><text x="48" y="64" font-size="46" text-anchor="middle" fill="#fff" font-family="sans-serif" font-weight="700">${t}</text></svg>`);
const ph=c=>c.photo||avatar(c.nm[0],hue(c.id));
let live=false,started=false,mode='normal',t0=0,nextDip=Date.now()+R(4,7)*60000,muted=false,feedLock=0,subN=0,pickId=null,vo=null;
let startTs=LS.g('start',0);if(!startTs){startTs=Date.now();LS.s('start',startTs)}
const evs=[];

/* ---------- setup + photos ---------- */
function drawSetup(){$('#list').innerHTML=C.map(c=>`<div class="sr" data-pick="${c.id}"><img src="${ph(c)}"><b>${c.nm}</b><span>${c.photo?'Photo ✓ (badlo)':'Gallery se photo'}</span></div>`).join('')}
$('#file').onchange=e=>{const f=e.target.files[0];e.target.value='';if(!f||!pickId)return;const id=pickId,im=new Image();
 im.onload=()=>{const s=220,cv=document.createElement('canvas');cv.width=cv.height=s;const k=Math.max(s/im.width,s/im.height),w=im.width*k,h=im.height*k;
  cv.getContext('2d').drawImage(im,(s-w)/2,(s-h)/2,w,h);const u=cv.toDataURL('image/jpeg',.85);
  by(id).photo=u;photos[id]=u;LS.s('photos',photos);drawSetup();if(live){render();drawSheet()}URL.revokeObjectURL(im.src)};
 im.src=URL.createObjectURL(f)};
document.addEventListener('click',e=>{
 const p=e.target.closest('[data-pick]');if(p){pickId=p.dataset.pick;$('#file').click();return}
 const t=e.target.closest('[data-t]');if(t){const[k,id]=t.dataset.t.split(':'),c=by(id);
  c.st=k==='nom'?(c.st==='nom'?'on':'nom'):(c.st==='ev'?'on':'ev');drawSheet();render();save()}});

/* ---------- board ---------- */
const slots=[];
function build(){const duo=$('#duo'),pod=$('#pod'),zl=[...document.querySelectorAll('.zl')];
 for(let r=1;r<=20;r++){const d=document.createElement('div');d.className='slot';slots.push(d);(r<=2?duo:r<=5?pod:zl[Math.floor((r-6)/5)]).appendChild(d)}
 C.forEach(c=>{const e=document.createElement('div');e.className='c';
  e.innerHTML='<b class="rk"></b><img class="av"><div class="in"><div class="nm"></div><div class="row"><span class="chip"></span><span class="v"></span></div><div class="pc"></div><div class="bar"><i></i></div></div><span class="bdg"></span>';
  c.el=e;c.q=s=>e.querySelector(s)})}
const order=()=>act().sort((x,y)=>y.votes-x.votes).concat(C.filter(c=>c.st==='ev').sort((x,y)=>y.votes-x.votes));
function tween(c){const el=c.q('.v'),from=c.shown,to=c.votes;if(from===to)return;const id=++c.tw,t1=performance.now();
 (function f(t){if(c.tw!==id)return;const k=Math.min(1,(t-t1)/600);c.shown=from+(to-from)*k;el.textContent=fmt(c.shown)+' votes';if(k<1)requestAnimationFrame(f)})(t1)}
function render(first){
 const o=order(),tot=C.reduce((s,c)=>s+c.votes,0),old=new Map(C.map(c=>[c,c.el.getBoundingClientRect()])),moved=new Set();
 o.forEach((c,i)=>{const r=i+1,e=c.el;
  if(c.rank!==r){c.rank=r;slots[i].appendChild(e);moved.add(c)}
  e.className='c '+(r<=2?'big':r<=5?'pod':'')+(c.st==='ev'?' ev':c.st==='nom'?' nom':'');
  c.q('.rk').textContent='#'+r;const im=c.q('.av'),u=ph(c);if(im.getAttribute('src')!==u)im.src=u;
  c.q('.nm').textContent=c.nm;c.q('.chip').textContent='!vote '+c.n;
  const b=c.q('.bdg');b.textContent=c.st==='ev'?'EVICTED':c.st==='nom'?'NOMINATED':'';b.style.background=c.st==='ev'?'#555':'#ff3366';
  c.q('.pc').textContent=(c.votes/tot*100).toFixed(1)+'% of house votes';c.q('.bar i').style.width=Math.min(100,c.votes/o[0].votes*100)+'%';
  if(first)c.shown=c.votes;tween(c)});
 if(!first)o.forEach(c=>{if(!moved.has(c))return;const a=old.get(c),b=c.el.getBoundingClientRect(),dx=a.left-b.left,dy=a.top-b.top;
  if(dx||dy)c.el.animate([{transform:`translate(${dx}px,${dy}px)`,zIndex:5},{transform:'none'}],{duration:650,easing:'cubic-bezier(.2,.8,.2,1)'})});
 const a=o[0],b=o[1]||o[0],now=Date.now();while(evs.length&&now-evs[0][0]>60000)evs.shift();
 $('#tot').textContent=fmt(tot)+' VOTES';$('#vpm').textContent='⚡ +'+fmt(evs.reduce((s,x)=>s+x[1],0))+' V/MIN';$('#gap').textContent='LEAD GAP: +'+fmt(a.votes-b.votes);
 $('#ml').textContent='👑 '+a.nm.split(' ')[0]+' ('+(a.votes/(a.votes+b.votes)*100).toFixed(1)+'%)';$('#mr').textContent='('+(b.votes/(a.votes+b.votes)*100).toFixed(1)+'%) '+b.nm.split(' ')[0];
 $('#mf').style.width=a.votes/(a.votes+b.votes)*100+'%'}

/* ---------- voice ---------- */
function pickVoice(){try{vo=speechSynthesis.getVoices().find(v=>/^hi/i.test(v.lang))||null}catch(e){}}
try{speechSynthesis.onvoiceschanged=pickVoice;pickVoice()}catch(e){}
function say(t,heavy,prio){if(muted||!window.speechSynthesis)return;if(prio)speechSynthesis.cancel();else if(speechSynthesis.speaking)return;
 const u=new SpeechSynthesisUtterance(t);u.lang='hi-IN';if(vo)u.voice=vo;u.pitch=heavy?.1:1.05;u.rate=heavy?.8:1;u.volume=heavy?1:.5;speechSynthesis.speak(u)}
const SUB=['दोस्तों, अगर आपको ये लाइव वोटिंग पसंद आ रही है, तो चैनल को सब्सक्राइब करें, फॉलो करें और लाइक ज़रूर करें।','सब्सक्राइब करना न भूलें! फॉलो करें और लाइक करें, ताकि हर अपडेट आपको सबसे पहले मिले।','अगर मज़ा आ रहा है तो लाइक दबाइए, चैनल को फॉलो कीजिए और सब्सक्राइब ज़रूर कीजिए।'];
function sub(){feedLock=Date.now()+9000;$('#feed').innerHTML='<div class="ft sub">🔔 सब्सक्राइब • फॉलो • लाइक 👍</div>';say(SUB[subN++%SUB.length],1,1)}

/* ---------- voting engine ---------- */
function toast(v,c,n){if(Date.now()<feedLock)return;
 $('#feed').innerHTML=`<div class="ft"><span class="va" style="background:hsl(${hue(v)},70%,55%)">${v[0]}</span><b>${v}</b><i>→</i><img src="${ph(c)}"><b>${c.nm.split(' ')[0]}</b><em>+${n}</em></div>`}
function hit(c,n){const e=c.el;e.classList.remove('hit');void e.offsetWidth;e.classList.add('hit');const p=document.createElement('span');p.className='pop';p.textContent='+'+n;e.appendChild(p);setTimeout(()=>p.remove(),1200)}
function pickC(){const A=act(),q=by('qazi'),a=by('arishfa'),duel=q.st!=='ev'&&a.st!=='ev';
 if(mode==='normal'&&q.st!=='ev'){const lead=Math.max(-1e9,...A.filter(c=>c!==q).map(c=>c.votes));if(q.votes-lead<500)return q}
 const ws=A.map(c=>{let w=c.w*(c.st==='nom'?1.3:1);
  if(mode!=='normal'&&duel){const rec=mode==='recover';w=c===a?(rec?3:60):c===q?(rec?60:1.5):w*.15}return w});
 let r=Math.random()*ws.reduce((x,y)=>x+y,0);for(let i=0;i<A.length;i++){r-=ws[i];if(r<=0)return A[i]}return A[0]}
function modeStep(){const q=by('qazi'),a=by('arishfa'),now=Date.now();
 if(q.st==='ev'||a.st==='ev'){mode='normal';return}
 if(mode==='normal'&&now>nextDip){mode='dip';t0=now}
 else if(mode==='dip'){if(a.votes>q.votes){mode='hold';t0=now;say('ब्रेकिंग! अरिश्फा अब नंबर वन पर आ गई हैं!',0,1)}else if(now-t0>150000)mode='recover'}
 else if(mode==='hold'&&now-t0>20000)mode='recover';
 else if(mode==='recover'&&q.votes>a.votes+700){mode='normal';nextDip=now+R(6,10)*60000;say('काज़ी फिर से नंबर वन पर वापस!',0,1)}}
function vote(){if(!act().length)return;const c=pickC(),v=D.voters[Math.random()*D.voters.length|0],r=Math.random(),n=r<.5?99:r<.75?50+(Math.random()*48|0):1+(Math.random()*49|0);
 c.votes+=n;evs.push([Date.now(),n]);hit(c,n);toast(v,c,n);
 const T=[`${v} ने ${c.hi} को ${n} वोट किए`,`${v} ने ${c.hi} को ${n} वोट दिए`,`${c.hi} को ${v} की तरफ़ से ${n} वोट मिले`];say(T[Math.random()*3|0],0,0);
 modeStep();render()}
function loop(){if(live)vote();setTimeout(loop,mode==='dip'||mode==='recover'?R(900,1500):R(2200,3800))}
function save(){LS.s('state',C.map(({id,votes,st})=>({id,votes,st})))}

/* ---------- settings ---------- */
function drawSheet(){$('#rows').innerHTML=C.map(c=>`<div class="sr"><img src="${ph(c)}" data-pick="${c.id}"><b>${c.nm}</b><button class="n ${c.st==='nom'?'on':''}" data-t="nom:${c.id}">Nominated</button><button class="e ${c.st==='ev'?'on':''}" data-t="ev:${c.id}">Evicted</button></div>`).join('')}
$('#gear').onclick=()=>{drawSheet();$('#sheet').classList.add('on')};
$('#close').onclick=()=>$('#sheet').classList.remove('on');
$('#mute').onclick=e=>{muted=!muted;e.target.textContent=muted?'🔇 Voice OFF':'🔊 Voice ON';if(muted)speechSynthesis.cancel()};
$('#subnow').onclick=()=>{$('#sheet').classList.remove('on');sub()};
$('#reset').onclick=()=>{if(!confirm('Sab votes starting par reset?'))return;C.forEach(c=>{c.votes=c.start});mode='normal';save();render(true)};

/* ---------- start ---------- */
$('#go').onclick=()=>{live=true;$('#setup').hidden=true;$('#board').hidden=false;$('#gear').hidden=false;
 try{speechSynthesis.speak(new SpeechSynthesisUtterance(''))}catch(e){}
 render(true);say('बिग बॉस बीस की फ़ैन वोटिंग शुरू हो गई है',0,1);
 if(!started){started=true;setTimeout(loop,2500);setInterval(sub,600000);setInterval(save,4000);
  setInterval(()=>{act().forEach(c=>{if(c.id!=='qazi'&&c.id!=='arishfa')c.w=Math.min(12,Math.max(1.5,c.w*R(.7,1.3)))})},20000);
  setInterval(()=>{const s=(Date.now()-startTs)/1000|0,p=n=>String(n).padStart(2,'0');$('#clk').textContent=p(s/3600|0)+':'+p(s%3600/60|0)+':'+p(s%60)},1000)}};
build();drawSetup();
</script></body></html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
