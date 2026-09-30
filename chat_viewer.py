"""
chat_viewer.py - build a single self-contained, offline "WhatsApp Web"-style HTML page
from a folder of WhatsApp chat-export .zip files (each zip = one exported chat).

Usage:
    python chat_viewer.py <folder-of-zips> [-o viewer.html] [--max-embed-mb 20] [--owner NAME]

- Parses each zip's _chat.txt (iOS "[date, time] Sender:" and Android "date, time - Sender:").
- Embeds media as base64 data URIs so the output is ONE portable .html (no external files).
- Media larger than --max-embed-mb is shown as a labelled placeholder instead of bloating the
  file (use --max-embed-mb 0 to embed everything, regardless of size).
- Fully offline: no CDNs, pure Python standard library + vanilla JS. Read-only viewer.
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import html
import json
import os
import re
import sys
import zipfile

# ---- WhatsApp export line formats ---------------------------------------------------------
# iOS:     [06/09/2024, 10:30:45] John Doe: message
# Android: 06/09/2024, 10:30 - John Doe: message      (or "9/6/24, 10:30 AM - John: ...")
_LTR = "\u200e\u200f"  # RTL/LTR marks WhatsApp sprinkles in
_RE_IOS = re.compile(
    r"^[%s]*\[(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\]\s*(.*)$" % _LTR
)
_RE_AND = re.compile(
    r"^[%s]*(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\s+-\s+(.*)$" % _LTR
)
# media reference forms inside a message
_RE_ATTACHED = re.compile(r"<attached:\s*([^>]+)>", re.IGNORECASE)
_RE_FILEATT = re.compile(r"([^\s]+\.[A-Za-z0-9]{2,4})\s*\(file attached\)", re.IGNORECASE)

_IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VID = {".mp4", ".3gp", ".mov", ".mkv", ".webm"}
_AUD = {".opus", ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".amr"}
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".mp4": "video/mp4", ".3gp": "video/3gpp",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".amr": "audio/amr", ".pdf": "application/pdf",
}


def _kind(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMG:
        return "image"
    if ext in _VID:
        return "video"
    if ext in _AUD:
        return "audio"
    return "file"


def _split_sender(rest):
    """From the text after the timestamp, split 'Sender: message'. Returns (sender, text).
    System messages (no 'Name: ') come back as (None, rest)."""
    # a real sender line has 'Name: ' fairly early and the name has no newline
    idx = rest.find(": ")
    if idx == -1:
        # could be 'Name:' at end, or a system line
        if rest.endswith(":"):
            return rest[:-1].strip(), ""
        return None, rest.strip()
    sender = rest[:idx].strip()
    # sender names don't contain these; if they do, it's probably a system line with a colon
    if len(sender) > 60 or "\n" in sender:
        return None, rest.strip()
    return sender, rest[idx + 2:]


def parse_chat_text(text):
    """Parse a _chat.txt into a list of message dicts: {ts, sender, text, media_refs:[names]}."""
    msgs = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = _RE_IOS.match(line) or _RE_AND.match(line)
        if m:
            date, tm, rest = m.group(1), m.group(2), m.group(3)
            sender, body = _split_sender(rest)
            cur = {"ts": f"{date}, {tm}", "sender": sender, "text": body, "media_refs": []}
            msgs.append(cur)
        elif cur is not None:
            cur["text"] += "\n" + line              # continuation of the previous message
        # else: preamble before the first dated line -> ignore
    # normalise senders: WhatsApp writes the OWNER's own messages with the name replaced by a
    # lone LTR mark (e.g. "- ‎: Yes"), so a sender that's only direction marks/space == "You".
    for mo in msgs:
        if mo["sender"] is not None:
            s = mo["sender"].strip(_LTR + " \t")
            mo["sender"] = s if s else "You"
    # extract media references out of each message's text
    for mo in msgs:
        refs = []
        for rx in (_RE_ATTACHED, _RE_FILEATT):
            for mm in rx.finditer(mo["text"]):
                refs.append(os.path.basename(mm.group(1).strip()))
        # strip the media tokens from the visible text
        t = _RE_ATTACHED.sub("", mo["text"])
        t = _RE_FILEATT.sub("", t)
        t = t.replace("<Media omitted>", "").strip(" \t" + _LTR)
        mo["text"] = t
        mo["media_refs"] = refs
    return msgs


def load_zip(path, max_bytes):
    """Return a chat dict {name, messages, ...} from one export zip, or None if it has no chat."""
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        print(f"  skip {os.path.basename(path)}: {e}")
        return None
    names = zf.namelist()
    txt_name = next((n for n in names if n.lower().endswith("_chat.txt")), None) \
        or next((n for n in names if n.lower().endswith(".txt")), None)
    if not txt_name:
        print(f"  skip {os.path.basename(path)}: no _chat.txt")
        return None
    raw = zf.read(txt_name)
    text = raw.decode("utf-8", "replace")
    msgs = parse_chat_text(text)

    # index media files in the zip by basename
    files = {os.path.basename(n): n for n in names if not n.lower().endswith(".txt")}
    used, embedded, skipped, embed_bytes = set(), 0, 0, 0
    for mo in msgs:
        media = []
        for ref in mo["media_refs"]:
            inzip = files.get(ref)
            item = {"name": ref, "kind": _kind(ref), "data": None, "size": 0}
            if inzip is not None:
                used.add(inzip)
                info = zf.getinfo(inzip)
                item["size"] = info.file_size
                if max_bytes == 0 or info.file_size <= max_bytes:
                    b = zf.read(inzip)
                    mime = _MIME.get(os.path.splitext(ref)[1].lower(), "application/octet-stream")
                    item["data"] = f"data:{mime};base64," + base64.b64encode(b).decode("ascii")
                    embedded += 1
                    embed_bytes += info.file_size
                else:
                    skipped += 1
            media.append(item)
        mo["media"] = media
        mo.pop("media_refs", None)

    # derive the chat name from the file name: "WhatsApp Chat with X.zip" -> "X"
    base = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^WhatsApp Chat with\s*", "", base).strip() or base
    senders = [m["sender"] for m in msgs if m["sender"]]
    last = next((m for m in reversed(msgs) if m["text"] or m.get("media")), None)
    preview = ""
    if last:
        preview = last["text"] or (last["media"][0]["kind"] if last.get("media") else "")
    return {
        "name": name,
        "file": os.path.basename(path),
        "sha256": _sha256(path),
        "messages": msgs,
        "count": len(msgs),
        "senders": sorted(set(senders)),
        "preview": preview[:80],
        "embedded": embedded,
        "skipped": skipped,
        "embed_bytes": embed_bytes,
    }


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(folder, out_path, max_embed_mb, owner):
    zips = sorted(glob.glob(os.path.join(folder, "*.zip")))
    if not zips:
        print("No .zip files found in:", folder)
        return 1
    max_bytes = int(max_embed_mb * 1024 * 1024)
    print(f"Building viewer from {len(zips)} zip(s) in {folder}")
    chats = []
    for z in zips:
        print(f"- {os.path.basename(z)}")
        c = load_zip(z, max_bytes)
        if c:
            chats.append(c)
    if not chats:
        print("No parseable chats.")
        return 1
    chats.sort(key=lambda c: c["name"].lower())
    data = {"owner": owner or "", "chats": chats,
            "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    payload = json.dumps(data, ensure_ascii=False)
    htmldoc = HTML_TEMPLATE.replace("/*__DATA__*/", "window.__CHATS__ = " + payload + ";")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(htmldoc)
    total_embed = sum(c["embed_bytes"] for c in chats)
    total_skip = sum(c["skipped"] for c in chats)
    size = os.path.getsize(out_path)
    print(f"\nWrote {out_path}  ({size/1e6:.1f} MB)")
    print(f"chats={len(chats)}  media embedded={sum(c['embedded'] for c in chats)} "
          f"({total_embed/1e6:.1f} MB)  media too-large-skipped={total_skip}")
    if size > 800e6:
        print("WARNING: the HTML is very large and may be slow to open. Lower --max-embed-mb.")
    return 0


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Viewer</title>
<style>
  :root{--bg:#111b21;--panel:#202c33;--panel2:#111b21;--in:#202c33;--out:#005c4b;
        --txt:#e9edef;--muted:#8696a0;--line:#2a3942;--accent:#00a884;--hl:#f6c344;}
  *{box-sizing:border-box}
  body{margin:0;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--panel2);
       color:var(--txt);height:100vh;overflow:hidden}
  .app{display:flex;height:100vh}
  .side{width:360px;min-width:300px;border-right:1px solid var(--line);display:flex;flex-direction:column;background:var(--panel2)}
  .side h1{font-size:15px;margin:0;padding:14px 16px;background:var(--panel);color:var(--muted);font-weight:600}
  .search{padding:8px;background:var(--panel2)}
  .search input{width:100%;padding:9px 12px;border-radius:8px;border:none;background:var(--panel);
                color:var(--txt);font-size:14px;outline:none}
  .chats{overflow-y:auto;flex:1}
  .chat{padding:11px 14px;border-bottom:1px solid var(--line);cursor:pointer;display:flex;gap:12px;align-items:center}
  .chat:hover{background:var(--panel)}
  .chat.active{background:var(--line)}
  .avatar{width:44px;height:44px;border-radius:50%;background:var(--accent);color:#04231d;
          display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}
  .chat .meta{overflow:hidden}
  .chat .nm{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .chat .pv{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .main{flex:1;display:flex;flex-direction:column;background:#0b141a;
        background-image:linear-gradient(rgba(11,20,26,.95),rgba(11,20,26,.95))}
  .top{padding:12px 16px;background:var(--panel);border-bottom:1px solid var(--line);
       display:flex;align-items:center;gap:12px}
  .top .nm{font-weight:600}.top .sub{font-size:12px;color:var(--muted)}
  .top .cs{margin-left:auto;display:flex;gap:6px;align-items:center}
  .top .cs input{padding:6px 10px;border-radius:6px;border:none;background:var(--panel2);color:var(--txt);outline:none}
  .msgs{flex:1;overflow-y:auto;padding:18px 8%;display:flex;flex-direction:column;gap:3px}
  .row{display:flex}.row.me{justify-content:flex-end}
  .bub{max-width:70%;padding:6px 9px 8px;border-radius:8px;background:var(--in);position:relative;
       font-size:14.2px;line-height:1.35;white-space:pre-wrap;word-wrap:break-word}
  .row.me .bub{background:var(--out)}
  .bub .snd{font-size:12.5px;font-weight:600;color:#53bdeb;margin-bottom:2px}
  .bub .tm{font-size:11px;color:var(--muted);float:right;margin:6px 0 -2px 10px}
  .bub img,.bub video{max-width:100%;border-radius:6px;display:block;margin:2px 0}
  .bub audio{width:240px;margin:3px 0}
  .bub .file{display:inline-block;padding:8px 10px;background:rgba(255,255,255,.06);border-radius:6px;color:var(--txt);text-decoration:none;margin:2px 0}
  .bub .ph{font-size:12px;color:var(--muted);font-style:italic;padding:4px 0}
  .sys{align-self:center;background:rgba(255,255,255,.05);color:var(--muted);font-size:12.5px;
       padding:5px 12px;border-radius:8px;margin:6px 0}
  mark{background:var(--hl);color:#000;border-radius:2px}
  .empty{margin:auto;color:var(--muted);text-align:center}
  .hit{font-size:12px;color:var(--muted);padding:2px 8px}
  .gres{position:absolute;top:52px;left:0;right:0;background:var(--panel);max-height:60vh;overflow:auto;
        border-bottom:1px solid var(--line);z-index:5}
  .gres .g{padding:8px 14px;border-bottom:1px solid var(--line);cursor:pointer}
  .gres .g:hover{background:var(--line)}
  .gres .g .c{font-size:12px;color:var(--accent)}
  .side{position:relative}
</style></head>
<body>
<div class="app">
  <div class="side">
    <h1>Chats <span id="cinfo" style="float:right;font-weight:400"></span></h1>
    <div class="search"><input id="q" placeholder="Search chats or messages..."></div>
    <div id="gres" class="gres" style="display:none"></div>
    <div id="chats" class="chats"></div>
  </div>
  <div class="main">
    <div class="top" id="top" style="display:none">
      <div><div class="nm" id="tnm"></div><div class="sub" id="tsub"></div></div>
      <div class="cs">
        <select id="owner" title="Which sender is 'you' (right side)"></select>
        <input id="cs" placeholder="Find in chat">
        <span class="hit" id="hit"></span>
      </div>
    </div>
    <div class="msgs" id="msgs"><div class="empty">Select a chat on the left</div></div>
  </div>
</div>
<script>
/*__DATA__*/
const DATA = window.__CHATS__ || {chats:[]};
const chats = DATA.chats;
let active = -1, ownerByChat = {};
const $ = s => document.querySelector(s);
const esc = s => (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function initial(n){ return (n||"?").trim().charAt(0).toUpperCase(); }

document.getElementById('cinfo').textContent = chats.length + ' chats';

function guessOwner(c){
  // heuristic: in a 1:1, the contact (chat name) is the OTHER person -> "me" = the other sender
  if (ownerByChat[c.name] !== undefined) return ownerByChat[c.name];
  if (DATA.owner && c.senders.includes(DATA.owner)) return DATA.owner;
  if (c.senders.includes("You")) return "You";
  const others = c.senders.filter(s => s.toLowerCase() !== c.name.toLowerCase());
  return (c.senders.length === 2 && others.length === 1) ? others[0] : "";
}

function renderChatList(filter){
  const box = $('#chats'); box.innerHTML='';
  const f = (filter||"").toLowerCase();
  chats.forEach((c,i)=>{
    if(f && !c.name.toLowerCase().includes(f)) return;
    const d=document.createElement('div'); d.className='chat'+(i===active?' active':'');
    d.innerHTML=`<div class="avatar">${esc(initial(c.name))}</div>
      <div class="meta"><div class="nm">${esc(c.name)}</div>
      <div class="pv">${esc(c.preview||'')}</div></div>`;
    d.onclick=()=>openChat(i);
    box.appendChild(d);
  });
}

function openChat(i, scrollToIdx){
  active=i; renderChatList($('#q').value);
  const c=chats[i];
  $('#top').style.display='flex';
  $('#tnm').textContent=c.name;
  $('#tsub').textContent=`${c.count} messages · ${c.senders.join(', ')}`;
  const sel=$('#owner'); sel.innerHTML='<option value="">(auto)</option>'+
    c.senders.map(s=>`<option${guessOwner(c)===s?' selected':''}>${esc(s)}</option>`).join('');
  sel.onchange=()=>{ownerByChat[c.name]=sel.value; renderMsgs(c);};
  $('#cs').value=''; $('#hit').textContent='';
  renderMsgs(c, scrollToIdx);
}

function mediaHtml(m){
  if(m.data){
    if(m.kind==='image') return `<img loading="lazy" src="${m.data}">`;
    if(m.kind==='video') return `<video controls preload="none" src="${m.data}"></video>`;
    if(m.kind==='audio') return `<audio controls preload="none" src="${m.data}"></audio>`;
    return `<a class="file" href="${m.data}" download="${esc(m.name)}">📎 ${esc(m.name)}</a>`;
  }
  const kb=(m.size/1024).toFixed(0);
  return `<div class="ph">📎 ${esc(m.name)} — not embedded (${kb} KB, over the size cap)</div>`;
}

function renderMsgs(c, scrollToIdx){
  const owner=guessOwner(c);
  const box=$('#msgs'); box.innerHTML='';
  c.messages.forEach((m,idx)=>{
    if(m.sender===null){
      const s=document.createElement('div'); s.className='sys'; s.textContent=m.text; box.appendChild(s); return;
    }
    const me = owner && m.sender===owner;
    const row=document.createElement('div'); row.className='row'+(me?' me':'');
    let media=(m.media||[]).map(mediaHtml).join('');
    const txt = m.text ? esc(m.text) : '';
    row.innerHTML=`<div class="bub" data-i="${idx}">${me?'':`<div class="snd">${esc(m.sender)}</div>`}
      ${media}${txt?`<span class="t">${txt}</span>`:''}<span class="tm">${esc(m.ts.split(', ').pop())}</span></div>`;
    box.appendChild(row);
  });
  if(scrollToIdx!=null){
    const el=box.querySelector(`.bub[data-i="${scrollToIdx}"]`);
    if(el){el.scrollIntoView({block:'center'}); el.style.outline='2px solid var(--accent)';}
  } else box.scrollTop=box.scrollHeight;
}

// in-chat search
$('#cs').addEventListener('input', e=>{
  if(active<0) return;
  const q=e.target.value.toLowerCase(); const c=chats[active];
  renderMsgs(c);
  if(!q){$('#hit').textContent='';return;}
  let n=0, first=null;
  document.querySelectorAll('#msgs .bub .t').forEach(sp=>{
    const t=sp.textContent; if(t.toLowerCase().includes(q)){
      n++; if(first===null) first=sp.closest('.bub');
      sp.innerHTML=esc(t).replace(new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig'),'<mark>$1</mark>');
    }
  });
  $('#hit').textContent=n?`${n} match${n>1?'es':''}`:'no matches';
  if(first) first.scrollIntoView({block:'center'});
});

// global search (chats + messages)
$('#q').addEventListener('input', e=>{
  const q=e.target.value.trim().toLowerCase();
  renderChatList(q);
  const g=$('#gres');
  if(q.length<2){g.style.display='none';return;}
  const hits=[];
  chats.forEach((c,ci)=>c.messages.forEach((m,mi)=>{
    if(m.text && m.text.toLowerCase().includes(q)){
      hits.push({ci,mi,name:c.name,sender:m.sender||'system',text:m.text});
    }
  }));
  if(!hits.length){g.style.display='none';return;}
  g.style.display='block';
  g.innerHTML=hits.slice(0,200).map(h=>{
    const i=h.text.toLowerCase().indexOf(q);
    const snip=esc(h.text.substring(Math.max(0,i-25),i+45));
    return `<div class="g" data-ci="${h.ci}" data-mi="${h.mi}">
      <div class="c">${esc(h.name)} · ${esc(h.sender)}</div><div>…${snip}…</div></div>`;
  }).join('') + (hits.length>200?`<div class="hit">${hits.length-200} more…</div>`:'');
  g.querySelectorAll('.g').forEach(el=>el.onclick=()=>{
    g.style.display='none'; $('#q').value='';
    openChat(+el.dataset.ci, +el.dataset.mi);
  });
});

renderChatList('');
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Build a self-contained WhatsApp-Web-style HTML viewer from export zips.")
    ap.add_argument("folder", help="folder containing WhatsApp export .zip files")
    ap.add_argument("-o", "--out", default=None, help="output .html (default: <folder>/chat_viewer.html)")
    ap.add_argument("--max-embed-mb", type=float, default=20.0,
                    help="embed media up to this size each (MB); 0 = embed everything (default 20)")
    ap.add_argument("--owner", default="", help="your sender name (right-aligned as 'you')")
    args = ap.parse_args()
    out = args.out or os.path.join(args.folder, "chat_viewer.html")
    sys.exit(build(args.folder, out, args.max_embed_mb, args.owner))


if __name__ == "__main__":
    main()
