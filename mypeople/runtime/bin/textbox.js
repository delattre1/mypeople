"use strict";
// The message box Access and Plugins share: a real textarea, so select, copy, cut and paste all work, and a message can go to another AI
// and come back. The colours are drawn behind it from the same text. Every value is text: textContent and .value only, never markup.
(function(){
  const css=document.createElement("style");
  css.textContent=".ed{position:relative;margin-top:8px}.ed .hl,.ed .tx{display:block;box-sizing:border-box;width:100%;margin:0;padding:10px 96px 10px 12px;border:1px solid #ffffff26;border-radius:9px;font:13px/1.65 var(--mono);letter-spacing:0;white-space:pre-wrap;overflow-wrap:anywhere;tab-size:8}.ed .hl{position:absolute;inset:0;overflow:hidden;pointer-events:none;background:#0f0f0e;color:var(--ink)}.ed .tx{position:relative;min-height:72px;background:transparent;color:transparent;caret-color:var(--ink);resize:none;overflow:hidden;outline-offset:1px}.ed .tx:focus-visible{outline:2px solid var(--volt)}.ed .tx::selection{background:rgba(213,239,138,.32);color:transparent}.ed .cp{position:absolute;z-index:2;top:6px;right:6px;height:28px;padding:0 10px;border-radius:7px;border:1px solid #ffffff26;background:#1a1a18;color:var(--ink);font:500 12.5px var(--sans);cursor:pointer}.v{border-radius:3px;background:rgba(255,255,255,.16)}.v.person{background:var(--amber);color:#111110}.v.bad{background:none;text-decoration:underline wavy #ff8a82;text-underline-offset:3px}.chip{height:24px;margin:2px 4px 2px 0;padding:0 7px;border:0;border-radius:5px;background:rgba(255,255,255,.16);color:var(--ink);font:12px var(--mono);cursor:pointer}.chip.person{background:var(--amber);color:#111110}.chip:focus-visible,.lk:focus-visible,.ed .cp:focus-visible{outline:2px solid var(--volt);outline-offset:1px}.legend{margin:10px 0 0;font-size:13px;color:var(--muted)}.st{margin:8px 0 0;font-size:13px}.st.stop{color:#ff8a82}.st.warn{color:var(--amber)}.lk{background:none;border:0;padding:0;color:inherit;font:inherit;text-decoration:underline;text-underline-offset:2px;cursor:pointer}.box-acts{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}.hint{margin:6px 0 0;font-size:12.5px;color:var(--muted)}[hidden]{display:none!important}";
  if(document.head)document.head.append(css);
  const VARS=/\{[a-zA-Z0-9_]+\}/g,mk=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x)e.textContent=x;return e},bt=(c,x,f)=>{const b=mk("button",c,x);b.type="button";b.onclick=f;return b};
  function paint(hl,text,known,person){hl.replaceChildren();let at=0;for(const m of text.matchAll(VARS)){hl.append(document.createTextNode(text.slice(at,m.index)));const v=m[0];hl.append(mk("span",known.includes(v)?(person.includes(v)?"v person":"v"):"v bad",v));at=m.index+v.length}hl.append(document.createTextNode(text.slice(at)+"\n"))}
  // o: key, label, base (the text as saved), defaults (the text the variables come from), required, warned, person, drafts, redraw, onSave(text)
  window.textBox=function(o){
    const wrap=mk("div"),brace=x=>"{"+x+"}",person=o.person.map(brace),required=o.required.map(brace),warned=o.warned.map(brace),known=[...new Set((o.defaults.match(VARS)||[]))];
    const lg=mk("p","legend","Yellow is what the person said, grey is filled in by MyPlow. Click one to put it where your cursor is: ");
    const ed=mk("div","ed"),hl=mk("pre","hl"),ta=mk("textarea","tx");hl.setAttribute("aria-hidden","true");ta.spellcheck=false;ta.setAttribute("aria-label",o.label);ta.value=o.key in o.drafts?o.drafts[o.key]:o.base;
    known.forEach(v=>{lg.append(bt("chip"+(person.includes(v)?" person":""),v,()=>{const s=ta.selectionStart,e=ta.selectionEnd;ta.setRangeText(v,s,e,"end");ta.focus();sync()}))});
    const copy=bt("cp","Copy",async()=>{try{await navigator.clipboard.writeText(ta.value);copy.textContent="Copied"}catch(e){ta.focus();ta.select();copy.textContent="Press ⌘C"}});
    ed.append(hl,ta,copy);if(known.length)wrap.append(lg);wrap.append(ed);
    const status=mk("div"),acts=mk("div","box-acts"),dis=bt("b","Discard",()=>{delete o.drafts[o.key];o.redraw()}),sv=bt("b go","Save",async()=>{if(await o.onSave(ta.value))delete o.drafts[o.key]});acts.append(dis,sv);wrap.append(status,acts);
    function sync(){
      if(ta.value!==o.base)o.drafts[o.key]=ta.value;else delete o.drafts[o.key];
      paint(hl,ta.value,known,person);ta.style.height="auto";ta.style.height=(ta.scrollHeight+2)+"px";
      const have=new Set(ta.value.match(VARS)||[]),dirty=ta.value!==o.base,lost=required.filter(v=>!have.has(v));status.replaceChildren();
      const back=(line,v,cls)=>{const l=mk("p","st "+cls,line+" ");l.append(bt("lk","Put it back at the end",()=>{ta.value=ta.value.replace(/\s*$/,"")+(cls==="stop"?"\n":" ")+v;sync()}));status.append(l)};
      lost.forEach(v=>back(person.includes(v)?"Without "+v+" the agent gets none of what the person said, so this cannot be saved.":"Without "+v+" the agent cannot tell who this is or how to answer, so this cannot be saved.",v,"stop"));
      warned.filter(v=>!have.has(v)).forEach(v=>back("Without "+v+" the agent is not told that. You can still save it.",v,"warn"));
      [...have].filter(v=>!known.includes(v)).forEach(v=>status.append(mk("p","st warn",v+" is not a variable here, so it goes out exactly as written.")));
      acts.hidden=!dirty;sv.disabled=lost.length>0;
    }
    ta.oninput=sync;sync();if(window.requestAnimationFrame)requestAnimationFrame(sync);
    return wrap;
  };
})();
