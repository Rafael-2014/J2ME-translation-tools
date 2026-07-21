"""
Editor Flask para arquivos .mu8 — Ratchet & Clank (J2ME)
"""
import io
import json
from flask import Flask, request, jsonify, send_file, render_template_string

# ── parser.py embutido ──────────────────────────────────────────────
import types as _types
mu8 = _types.ModuleType('mu8')
exec(compile(r'''
"""
Motor de parsing do formato .mu8 — Ratchet & Clank (J2ME/JAVA)
===============================================================

Estrutura do arquivo:
─────────────────────
  [0x0000–0x0001]  Header 2 bytes big-endian = total de entradas
                   ex: 0x0141 = 321 entradas (incluindo nulls)

  [0x0002 … EOF]   Sequência contínua de blocos:

  ┌─ BLOCO NORMAL ≤255 bytes (ponteiro 1 byte) ─────────────────┐
  │  00 NN TEXTO[NN]                                             │
  │  ├─ 0x00  → flag: ponteiro de 1 byte                        │
  │  ├─ NN    → tamanho em bytes (1–255)                        │
  │  └─ TEXTO → UTF-8, exatamente NN bytes                      │
  └──────────────────────────────────────────────────────────────┘

  ┌─ BLOCO VAZIO / NULL (ponteiro 1 byte, size=0) ──────────────┐
  │  00 00                                                       │
  │  Representa uma string vazia; DEVE ser preservado no rebuild │
  └──────────────────────────────────────────────────────────────┘

  ┌─ BLOCO GRANDE >255 bytes (ponteiro 2 bytes big-endian) ─────┐
  │  HH LL TEXTO[HH*256+LL]                                     │
  │  ├─ HH ≠ 0x00  → MSB do tamanho                            │
  │  ├─ LL         → LSB do tamanho                             │
  │  └─ TEXTO      → UTF-8, exatamente HH*256+LL bytes         │
  └──────────────────────────────────────────────────────────────┘

Encoding: UTF-8 (confirmado pelos acentos do francês)

Casos encontrados no arquivo FR original (15842 bytes, 321 entradas):
  - 317 blocos normais ≤255 bytes (ponteiro 1 byte)
  -   3 blocos grandes >255 bytes (ponteiro 2 bytes): tamanhos 312, 346, 384
  -   1 bloco null/vazio (00 00) no índice 198
"""


def parse(data: bytes) -> dict:
    """
    Faz o parse completo do arquivo .mu8.

    Retorna:
        {
          'header_count': int,
          'strings': [
            {
              'index':      int,   # posição na sequência (0-based)
              'offset':     int,   # offset no arquivo em bytes
              'size_bytes': int,   # 1 ou 2 (bytes do campo de tamanho)
              'size':       int,   # tamanho do texto em bytes UTF-8
              'size_hex':   str,   # representação hex do campo de tamanho
              'text':       str,   # texto decodificado
              'null':       bool,  # True se for bloco 00 00 (string vazia)
            }, ...
          ]
        }
    """
    if len(data) < 2:
        raise ValueError("Arquivo muito pequeno (mínimo 2 bytes para o header)")

    header_count = data[0] * 256 + data[1]
    strings = []
    i = 2
    idx = 0
    total = len(data)

    while i < total:
        b0 = data[i]

        if b0 == 0x00:
            # ── Ponteiro 1 byte (≤255) ou bloco null ──
            if i + 1 >= total:
                break
            sz = data[i + 1]
            text_start = i + 2
            text_end   = text_start + sz
            if text_end > total:
                break  # dados truncados

            chunk = data[text_start:text_end]
            try:
                text = chunk.decode('utf-8')
            except UnicodeDecodeError:
                text = chunk.decode('latin-1')

            strings.append({
                'index':      idx,
                'offset':     i,
                'size_bytes': 1,
                'size':       sz,
                'size_hex':   f'00{sz:02x}',
                'text':       text,
                'null':       sz == 0,   # bloco 00 00 → string vazia
            })
            idx += 1
            i = text_end

        else:
            # ── Ponteiro 2 bytes big-endian (>255) ──
            if i + 1 >= total:
                break
            b1 = data[i + 1]
            sz = b0 * 256 + b1

            if sz == 0 or sz > 10000:
                # Valor absurdo → não é um ponteiro válido aqui
                i += 1
                continue

            text_start = i + 2
            text_end   = text_start + sz
            if text_end > total:
                i += 1
                continue

            chunk = data[text_start:text_end]
            try:
                text = chunk.decode('utf-8')
                printable = sum(1 for c in text if c.isprintable())
                if printable < sz * 0.70:
                    i += 1
                    continue
            except UnicodeDecodeError:
                i += 1
                continue

            strings.append({
                'index':      idx,
                'offset':     i,
                'size_bytes': 2,
                'size':       sz,
                'size_hex':   f'{b0:02x}{b1:02x}',
                'text':       text,
                'null':       False,
            })
            idx += 1
            i = text_end

    return {
        'header_count': header_count,
        'strings':      strings,
    }


def build(header_count: int, strings: list) -> bytes:
    """
    Reconstrói o arquivo .mu8 a partir da lista de entradas.

    ─ Blocos null (null=True) são preservados como 00 00.
    ─ Strings ≤255 bytes UTF-8  → ponteiro 1 byte:  00 NN texto
    ─ Strings >255 bytes UTF-8  → ponteiro 2 bytes: HH LL texto
    ─ O tamanho é sempre recalculado a partir do texto atual.
    ─ O header_count é mantido como estava no original.
    """
    result = bytearray()

    # Header 2 bytes big-endian
    result.append((header_count >> 8) & 0xFF)
    result.append(header_count & 0xFF)

    for entry in strings:
        # Bloco null: preservar como 00 00
        if entry.get('null'):
            result.append(0x00)
            result.append(0x00)
            continue

        text    = entry['text']
        encoded = text.encode('utf-8')
        sz      = len(encoded)

        if sz <= 255:
            # Ponteiro 1 byte
            result.append(0x00)
            result.append(sz)
        else:
            # Ponteiro 2 bytes big-endian
            result.append((sz >> 8) & 0xFF)
            result.append(sz & 0xFF)

        result.extend(encoded)

    return bytes(result)


def validate(original: bytes, edited_strings: list) -> list:
    """
    Valida a lista de strings editadas contra o original.
    Retorna lista de dicts com 'index', 'type' ('warning'|'error'), 'msg'.
    """
    warnings = []
    parsed = parse(original)
    orig   = {s['index']: s for s in parsed['strings']}

    for entry in edited_strings:
        idx  = entry.get('index', -1)
        text = entry.get('text', '')

        # Ignorar nulls
        if entry.get('null'):
            continue

        if not text and not entry.get('null'):
            warnings.append({
                'index': idx,
                'type':  'error',
                'msg':   f'String #{idx} está vazia'
            })
            continue

        enc_sz = len(text.encode('utf-8'))
        orig_e = orig.get(idx)
        if orig_e:
            orig_sz = orig_e['size']
            # Aviso se ficou mais que 3× o tamanho original
            if enc_sz > orig_sz * 3 and enc_sz > 50:
                warnings.append({
                    'index': idx,
                    'type':  'warning',
                    'msg':   f'String #{idx} muito maior que original ({enc_sz} vs {orig_sz} bytes)'
                })

    return warnings

''', 'parser.py', 'exec'), mu8.__dict__)
# ────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Estado de sessão simples (processo único)
_state = {
    'original_data': None,
    'parsed':        None,
    'filename':      'output.mu8',
}

# ──────────────────────────────────────────────────────────────────
# TEMPLATE HTML
# ──────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Ratchet & Clank — Editor .mu8</title>
<style>
:root{
  --bg:#0d1117;--surf:#161b22;--surf2:#21262d;--bdr:#30363d;
  --acc:#f78166;--blue:#79c0ff;--text:#c9d1d9;--muted:#8b949e;
  --green:#56d364;--yellow:#e3b341;--red:#f85149;--purple:#d2a8ff;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}

/* ── HEADER ── */
header{
  background:var(--surf);border-bottom:1px solid var(--bdr);
  padding:12px 20px;display:flex;align-items:center;gap:12px;
  position:sticky;top:0;z-index:200;flex-wrap:wrap;
}
header h1{font-size:17px;color:var(--acc);flex:1;white-space:nowrap}
.badge{
  background:var(--surf2);border:1px solid var(--bdr);border-radius:20px;
  padding:2px 10px;font-size:11px;color:var(--muted);white-space:nowrap;
}

/* ── TOOLBAR ── */
.toolbar{
  background:var(--surf);border-bottom:1px solid var(--bdr);
  padding:8px 20px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
}
.btn{
  padding:5px 13px;border-radius:6px;border:1px solid var(--bdr);
  cursor:pointer;font-size:13px;font-weight:600;transition:all .15s;
  white-space:nowrap;
}
.btn-green{background:#1a3a20;border-color:#2ea043;color:var(--green)}
.btn-green:hover{background:#238636}
.btn-blue{background:#0d2340;border-color:var(--blue);color:var(--blue)}
.btn-blue:hover{background:#1f4068}
.btn-red{background:#3d0000;border-color:var(--red);color:var(--red)}
.btn-red:hover{background:#5a0000}
.btn-gray{background:var(--surf2);border-color:var(--bdr);color:var(--text)}
.btn-gray:hover{background:#30363d}
.btn-yellow{background:#2d1f00;border-color:var(--yellow);color:var(--yellow)}
.btn-yellow:hover{background:#3d2a00}
.btn:disabled{opacity:.35;cursor:not-allowed!important}

input[type=text]{
  padding:5px 11px;border-radius:6px;border:1px solid var(--bdr);
  background:var(--bg);color:var(--text);font-size:13px;
}
input[type=text]:focus{outline:none;border-color:var(--blue)}
input[type=text]:disabled{opacity:.35}

.stats-bar{font-size:12px;color:var(--muted);margin-left:auto;display:flex;gap:12px;flex-wrap:wrap}
.stat-item{white-space:nowrap}
.stat-item b{color:var(--text)}

/* ── FILTERS ── */
.filter-bar{
  background:var(--bg);border-bottom:1px solid var(--bdr);
  padding:6px 20px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
}
.filter-btn{
  padding:3px 10px;border-radius:12px;border:1px solid var(--bdr);
  background:transparent;color:var(--muted);cursor:pointer;font-size:12px;
  transition:all .15s;
}
.filter-btn.active{background:var(--surf2);color:var(--text);border-color:#484f58}
.filter-btn:hover{border-color:#484f58;color:var(--text)}

/* ── MAIN ── */
main{padding:16px 20px;max-width:1440px;margin:0 auto}

/* ── DROPZONE ── */
#dropzone{
  border:2px dashed var(--bdr);border-radius:12px;
  padding:60px 40px;text-align:center;color:var(--muted);
  margin:30px 0;transition:border-color .2s,color .2s;cursor:pointer;
}
#dropzone:hover,#dropzone.drag{border-color:var(--blue);color:var(--blue)}
#dropzone h2{font-size:22px;margin-bottom:10px}
#dropzone p{font-size:13px;line-height:1.8}
#dropzone code{
  background:var(--surf2);padding:1px 6px;border-radius:4px;
  font-family:monospace;font-size:12px;color:var(--purple);
}

/* ── PROGRESS ── */
#progress{display:none;text-align:center;padding:40px;color:var(--muted)}
.spinner{
  width:36px;height:36px;border:3px solid var(--bdr);
  border-top-color:var(--blue);border-radius:50%;
  animation:spin .8s linear infinite;margin:0 auto 12px;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── STRING LIST ── */
#string-list{display:none}

.group-label{
  font-size:11px;text-transform:uppercase;letter-spacing:1px;
  color:var(--muted);padding:12px 4px 6px;
  border-bottom:1px solid var(--bdr);margin-bottom:6px;margin-top:10px;
}
.group-label:first-child{margin-top:0}

.card{
  background:var(--surf);border:1px solid var(--bdr);border-radius:8px;
  margin-bottom:6px;overflow:hidden;transition:border-color .15s;
}
.card:hover{border-color:#484f58}
.card.mod{border-left:3px solid var(--yellow)}
.card.null-card{opacity:.5}
.card.err{border-left:3px solid var(--red)}

.card-head{
  display:flex;align-items:center;gap:8px;
  padding:7px 12px;cursor:pointer;user-select:none;
}
.card-head:hover{background:#0d1117}

.ci{font-size:11px;color:var(--muted);font-family:monospace;min-width:36px}
.co{font-size:11px;color:#6e7681;font-family:monospace;min-width:56px}
.ptr{
  font-size:10px;font-family:monospace;padding:1px 6px;
  border-radius:4px;border:1px solid;min-width:24px;text-align:center;
}
.ptr1{border-color:#1f6feb;color:var(--blue);background:#0d2340}
.ptr2{border-color:#9e6a03;color:var(--yellow);background:#2d1f00}
.ptr0{border-color:#3d0f0f;color:#8b3030;background:#1a0000}

.cprev{
  flex:1;font-size:13px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;
}
.cprev.orig{color:var(--text)}
.cprev.trl{color:var(--green)}
.cprev.mt{color:var(--red);font-style:italic}

.csz{font-size:11px;min-width:72px;text-align:right;font-family:monospace}
.csz.ok{color:var(--green)}
.csz.warn{color:var(--yellow)}
.csz.danger{color:var(--red)}

/* ── CARD BODY ── */
.card-body{display:none;padding:14px;border-top:1px solid var(--bdr)}
.card-body.open{display:block}

.meta{
  display:flex;gap:8px;flex-wrap:wrap;font-size:11px;
  color:var(--muted);margin-bottom:10px;font-family:monospace;
}
.meta span{
  background:var(--bg);padding:2px 8px;border-radius:4px;
  border:1px solid var(--bdr);
}
.meta span b{color:var(--blue)}

.field{margin-bottom:10px}
.field label{
  display:block;font-size:10px;text-transform:uppercase;
  letter-spacing:.8px;color:var(--muted);margin-bottom:5px;
}
textarea{
  width:100%;background:var(--bg);color:var(--text);
  border:1px solid var(--bdr);border-radius:6px;
  padding:8px 10px;font-size:13px;font-family:Consolas,monospace;
  resize:vertical;line-height:1.55;transition:border-color .15s;
}
textarea:focus{outline:none;border-color:var(--blue)}
textarea.changed{border-color:var(--yellow)}
textarea[readonly]{opacity:.45;resize:none}

.byte-bar{
  display:flex;gap:14px;flex-wrap:wrap;font-size:11px;
  color:var(--muted);margin-top:5px;font-family:monospace;
}
.byte-bar .ok{color:var(--green)}
.byte-bar .warn{color:var(--yellow)}
.byte-bar .danger{color:var(--red)}

/* ── TOAST ── */
#toast{
  position:fixed;bottom:20px;right:20px;
  background:#238636;color:#fff;padding:11px 18px;
  border-radius:8px;font-size:13px;font-weight:600;
  opacity:0;transition:opacity .25s;pointer-events:none;z-index:999;
  max-width:360px;
}
#toast.show{opacity:1}
#toast.err{background:var(--red)}
#toast.warn{background:#9e6a03}

/* ── MODAL DIFF ── */
#modal-bg{
  display:none;position:fixed;inset:0;background:#000a;
  z-index:500;align-items:center;justify-content:center;
}
#modal-bg.open{display:flex}
#modal{
  background:var(--surf);border:1px solid var(--bdr);border-radius:12px;
  padding:24px;max-width:700px;width:90%;max-height:80vh;overflow-y:auto;
}
#modal h3{font-size:16px;margin-bottom:16px;color:var(--acc)}
.diff-row{
  display:grid;grid-template-columns:1fr 1fr;gap:10px;
  margin-bottom:12px;font-size:13px;
}
.diff-row div{
  background:var(--bg);border:1px solid var(--bdr);border-radius:6px;
  padding:8px 10px;font-family:monospace;white-space:pre-wrap;word-break:break-word;
}
.diff-row .fr{border-color:#21262d}
.diff-row .pt{border-color:var(--yellow)}
.modal-close{
  float:right;background:none;border:none;color:var(--muted);
  cursor:pointer;font-size:18px;line-height:1;
}
.modal-close:hover{color:var(--text)}
</style>
</head>
<body>

<!-- HEADER -->
<header id="header" style="display:none">
  <h1>🔧 Ratchet &amp; Clank — Editor de Diálogos (.mu8)</h1>
  <span class="badge" id="b-file">Nenhum arquivo</span>
  <span class="badge" id="b-hdr">—</span>
</header>

<!-- TOOLBAR -->
<div class="toolbar" id="toolbar" style="display:none">
  <button class="btn btn-gray" onclick="$('f-in').click()">📂 Abrir .mu8</button>
  <input type="file" id="f-in" accept=".mu8,.bin" style="display:none" onchange="loadFile(this.files[0])"/>

  <button class="btn btn-green" id="b-save" onclick="downloadMU8()" disabled>💾 Salvar</button>

  <button class="btn btn-blue" id="b-xjson" onclick="exportJSON()" disabled>📤 JSON</button>
  <button class="btn btn-blue" id="b-ijson" onclick="$('j-in').click()" disabled>📥 Importar JSON</button>
  <input type="file" id="j-in" accept=".json" style="display:none" onchange="importJSON(this.files[0])"/>

  <button class="btn btn-yellow" id="b-diff" onclick="showDiff()" disabled>📊 Ver diff</button>
  <button class="btn btn-red" id="b-reset" onclick="resetAll()" disabled>↩ Resetar</button>

  <input type="text" id="b-search" placeholder="🔍 Buscar…" oninput="applyFilters()" disabled style="width:200px"/>

  <div class="stats-bar" id="stats-bar">
    <span class="stat-item">Total: <b id="st-total">—</b></span>
    <span class="stat-item">Traduzidas: <b id="st-mod">—</b></span>
    <span class="stat-item">Pendentes: <b id="st-pend">—</b></span>
    <span class="stat-item">%: <b id="st-pct">—</b></span>
  </div>
</div>

<!-- FILTER BAR -->
<div class="filter-bar" id="filter-bar" style="display:none">
  <span style="font-size:11px;color:var(--muted)">Filtro:</span>
  <button class="filter-btn active" data-f="all"    onclick="setFilter('all',this)">Todas</button>
  <button class="filter-btn"        data-f="pend"   onclick="setFilter('pend',this)">Pendentes</button>
  <button class="filter-btn"        data-f="mod"    onclick="setFilter('mod',this)">Traduzidas</button>
  <button class="filter-btn"        data-f="big"    onclick="setFilter('big',this)">Blocos >255</button>
  <button class="filter-btn"        data-f="long"   onclick="setFilter('long',this)">>80 chars</button>
  <button class="filter-btn"        data-f="short"  onclick="setFilter('short',this)">≤20 chars</button>
</div>

<!-- MAIN -->
<main>
  <div id="dropzone"
    onclick="$('f-in').click()"
    ondragover="event.preventDefault();this.classList.add('drag')"
    ondragleave="this.classList.remove('drag')"
    ondrop="event.preventDefault();this.classList.remove('drag');loadFile(event.dataTransfer.files[0])">
    <h2>📁 Arraste o arquivo <code>.mu8</code> aqui</h2>
  </div>

  <div id="progress"><div class="spinner"></div>Carregando e parseando…</div>
  <div id="string-list"></div>
</main>

<!-- MODAL DIFF -->
<div id="modal-bg" onclick="if(event.target===this)closeDiff()">
  <div id="modal">
    <button class="modal-close" onclick="closeDiff()">✕</button>
    <h3>📊 Strings modificadas</h3>
    <div id="diff-content"></div>
  </div>
</div>

<div id="toast"></div>

<script>
// ─────────────────────────────────────
// Estado global
// ─────────────────────────────────────
let strings    = [];   // lista atual (editada)
let origStr    = [];   // cópia imutável do original
let filename   = '';
let activeFilter = 'all';

// ─────────────────────────────────────
// UTILS
// ─────────────────────────────────────
function $(id){ return document.getElementById(id) }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
function enc(s){ return new TextEncoder().encode(s).length }

function toast(msg, type='ok'){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'show' + (type==='err'?' err':type==='warn'?' warn':'');
  clearTimeout(t._timer);
  t._timer = setTimeout(()=>t.className='', 3500);
}

function enableUI(){
  $('header').style.display = 'flex';
  $('toolbar').style.display = 'flex';
  ['b-save','b-xjson','b-ijson','b-diff','b-reset'].forEach(id=>$(id).disabled=false);
  $('b-search').disabled = false;
  $('filter-bar').style.display = 'flex';
}

function updateStats(){
  const total = strings.filter(s=>!s.null).length;
  const mod   = strings.filter((s,i)=>!s.null && s.text !== origStr[s.index]?.text).length;
  const pend  = total - mod;
  const pct   = total>0 ? Math.round(mod/total*100) : 0;
  $('st-total').textContent = total;
  $('st-mod').textContent   = mod;
  $('st-pend').textContent  = pend;
  $('st-pct').textContent   = pct + '%';
}

// ─────────────────────────────────────
// LOAD
// ─────────────────────────────────────
// loadFile definida abaixo (com persistência)

// ─────────────────────────────────────
// RENDER
// ─────────────────────────────────────
function applyFilters(){
  const q   = $('b-search').value.toLowerCase();
  const f   = activeFilter;

  const list = strings.filter(s=>{
    if(s.null) return false;
    const ismod = s.text !== origStr[s.index]?.text;
    const orig_text = origStr[s.index]?.text || '';

    if(f==='pend'  && ismod)                      return false;
    if(f==='mod'   && !ismod)                     return false;
    if(f==='big'   && s.size_bytes!==2)           return false;
    if(f==='long'  && s.text.length<=80)          return false;
    if(f==='short' && s.text.length>20)           return false;

    if(q && !s.text.toLowerCase().includes(q) &&
            !orig_text.toLowerCase().includes(q) &&
            !String(s.index).includes(q))         return false;

    return true;
  });

  renderList(list);
}

function setFilter(f, btn){
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}

function renderList(list){
  const container = $('string-list');
  container.innerHTML = '';

  if(list.length === 0){
    container.innerHTML = '<p style="color:var(--muted);padding:30px;text-align:center">Nenhuma string encontrada</p>';
    return;
  }

  list.forEach(s=>{
    const orig    = origStr[s.index];
    const ismod   = s.text !== orig?.text;
    const isEmpty = s.text.trim()==='' && !s.null;
    const encsz   = enc(s.text);
    const origSz  = orig?.size ?? 0;

    const card = document.createElement('div');
    const cls  = ['card'];
    if(ismod)   cls.push('mod');
    if(isEmpty) cls.push('err');
    if(s.null)  cls.push('null-card');
    card.className = cls.join(' ');
    card.dataset.idx = s.index;

    const ptrCls = s.null ? 'ptr0' : (s.size_bytes===2 ? 'ptr2' : 'ptr1');
    const ptrLbl = s.null ? 'NULL' : (s.size_bytes===2 ? '2B' : '');
    const ptrTip = s.null ? 'Bloco vazio (00 00)' : (s.size_bytes===2 ? 'Ponteiro 2 bytes big-endian (>255)' : 'Ponteiro padrão (≤255 bytes)');

    const prevTxt = (s.null ? '(null)' : s.text).replace(/\n/g,' ').substring(0,110);
    const prevCls = s.null?'mt':(ismod?'trl':'orig');

    const szCls  = encsz>origSz*1.5?'danger':(encsz>origSz?'warn':'ok');
    const ptrWill = encsz>255?'2B':'1B';
    const ptrChg  = (!s.null && ((encsz>255)!==(s.size_bytes===2)))?'⚠':'';

    card.innerHTML = `
<div class="card-head" onclick="toggleCard(this)">
  <span class="ci">#${String(s.index).padStart(3,'0')}</span>
  <span class="co">0x${s.offset.toString(16).toUpperCase().padStart(4,'0')}</span>
  <span class="ptr ${ptrCls}" title="${ptrTip}">${ptrLbl}</span>
  <span class="cprev ${prevCls}">${esc(prevTxt)}</span>
  <span class="csz ${szCls}">${encsz}/${origSz}B ${ptrChg}</span>
</div>
<div class="card-body" id="cb-${s.index}">
  <div class="meta">
    <span>Índice: <b>${s.index}</b></span>
    <span>Offset: <b>0x${s.offset.toString(16).toUpperCase().padStart(4,'0')}</b></span>
    <span>Ptr orig: <b>${s.size_hex}</b></span>
    <span>Tam orig: <b>${origSz}</b> bytes</span>

  </div>
  ${s.null ? '<p style="color:var(--muted);font-size:12px">Bloco vazio (00 00) — preservado automaticamente no rebuild.</p>' : `
  <div class="field">
    <label>Original (Francês)</label>
    <textarea rows="${Math.max(2,Math.ceil((orig?.text||'').length/90))}" readonly>${esc(orig?.text||'')}</textarea>
  </div>
  <div class="field">
    <label>Tradução — Português do Brasil</label>
    <textarea id="ta-${s.index}"
      rows="${Math.max(2,Math.ceil(s.text.length/90))}"
      oninput="onChange(${s.index},this)">${esc(s.text)}</textarea>
    <div class="byte-bar" id="bb-${s.index}"></div>
  </div>
  `}
</div>`;
    container.appendChild(card);
    if(!s.null) updateByteBar(s.index);
  });
}

function toggleCard(hd){
  hd.nextElementSibling.classList.toggle('open');
}

// ─────────────────────────────────────
// EDIÇÃO
// ─────────────────────────────────────
function onChange(idx, ta){
  strings[idx].text = ta.value;
  updateByteBar(idx);

  const card  = ta.closest('.card');
  const ismod = strings[idx].text !== origStr[idx]?.text;
  const empty = strings[idx].text.trim()==='' && !strings[idx].null;
  card.classList.toggle('mod', ismod);
  card.classList.toggle('err', empty);
  ta.className = ismod ? 'changed' : '';

  // Atualizar preview
  const prev = card.querySelector('.cprev');
  const txt  = strings[idx].text.replace(/\n/g,' ').substring(0,110);
  prev.textContent = txt || '(vazio!)';
  prev.className   = 'cprev ' + (ismod?'trl':(empty?'mt':'orig'));

  // Atualizar byte info
  const encsz  = enc(ta.value);
  const origSz = origStr[idx]?.size ?? 0;
  const si     = card.querySelector('.csz');
  si.textContent = `${encsz}/${origSz}B`;
  si.className   = 'csz ' + (encsz>origSz*1.5?'danger':(encsz>origSz?'warn':'ok'));

  updateStats();
  saveSession();
}

function updateByteBar(idx){
  const ta = $('ta-'+idx);
  const bb = $('bb-'+idx);
  if(!ta||!bb) return;
  const sz     = enc(ta.value);
  const origSz = origStr[idx]?.size ?? 0;
  const delta  = sz - origSz;
  const ds     = delta===0?'±0':(delta>0?`+${delta}`:`${delta}`);
  const cls    = sz>origSz*1.5?'danger':(sz>origSz?'warn':'ok');
  const ptr    = sz>255?'2 bytes (>255)':'1 byte (≤255)';
  bb.innerHTML = `
    <span class="${cls}">UTF-8: ${sz}B (orig: ${origSz}B)</span>
    <span class="${cls}">Δ ${ds}</span>

    <span>Chars: ${ta.value.length}</span>`;
}

// ─────────────────────────────────────
// DOWNLOAD
// ─────────────────────────────────────
async function downloadMU8(){
  try{
    const r = await fetch('/api/build',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({strings})
    });
    if(!r.ok){
      const e=await r.json();
      toast((e.error||'Erro')+': '+(e.details||[]).map(d=>d.msg).join('; '),'err');
      return;
    }
    const warns = parseInt(r.headers.get('X-Warnings')||'0');
    if(warns>0) toast(`Arquivo gerado com ${warns} aviso(s)!`,'warn');
    else toast('✅ Arquivo .mu8 gerado com sucesso!');

    const blob = await r.blob();
    const a    = document.createElement('a');
    a.href     = URL.createObjectURL(blob);
    a.download = filename.replace(/\.mu8$/i,'_ptbr.mu8')||'output_ptbr.mu8';
    a.click();
    URL.revokeObjectURL(a.href);
  }catch(e){ toast('Erro: '+e.message,'err') }
}

// ─────────────────────────────────────
// JSON
// ─────────────────────────────────────
function exportJSON(){
  const data = {
    filename,
    generated: new Date().toISOString(),
    strings: strings.filter(s=>!s.null).map(s=>({
      index:       s.index,
      offset_hex:  '0x'+s.offset.toString(16).toUpperCase().padStart(4,'0'),
      size_ptr:    s.size_bytes+'B',
      original:    origStr[s.index]?.text||'',
      translation: s.text,
    }))
  };
  const blob = new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = filename.replace(/\.mu8$/i,'_trad.json')||'traducao.json';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('JSON exportado!');
}

// importJSON definida abaixo (com persistência)

// ─────────────────────────────────────
// DIFF
// ─────────────────────────────────────
function showDiff(){
  const mods = strings.filter(s=>!s.null && s.text !== origStr[s.index]?.text);
  if(!mods.length){ toast('Nenhuma string modificada ainda','warn'); return; }

  const rows = mods.map(s=>`
    <div style="margin-bottom:14px">
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;font-family:monospace">
        #${s.index} · 0x${s.offset.toString(16).toUpperCase().padStart(4,'0')} · ${enc(s.text)}B
      </div>
      <div class="diff-row">
        <div class="fr"><span style="font-size:10px;color:var(--muted);display:block;margin-bottom:4px">🇫🇷 ORIGINAL</span>${esc(origStr[s.index]?.text||'')}</div>
        <div class="pt"><span style="font-size:10px;color:var(--yellow);display:block;margin-bottom:4px">🇧🇷 TRADUÇÃO</span>${esc(s.text)}</div>
      </div>
    </div>`).join('');

  $('diff-content').innerHTML = `<p style="font-size:12px;color:var(--muted);margin-bottom:16px">${mods.length} string(s) modificada(s)</p>${rows}`;
  $('modal-bg').classList.add('open');
}
function closeDiff(){ $('modal-bg').classList.remove('open') }
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDiff() });

// ─────────────────────────────────────
// PERSISTÊNCIA (sessionStorage)
// ─────────────────────────────────────
function saveSession(){
  if(!strings.length) return;
  sessionStorage.setItem('mu8_strings',  JSON.stringify(strings));
  sessionStorage.setItem('mu8_origStr',  JSON.stringify(origStr));
  sessionStorage.setItem('mu8_filename', filename);
}

async function tryRestoreSession(){
  try{
    const r = await fetch('/api/info');
    const d = await r.json();
    if(!d.loaded) return; // servidor sem arquivo → mostrar dropzone

    const savedStr  = sessionStorage.getItem('mu8_strings');
    const savedOrig = sessionStorage.getItem('mu8_origStr');
    const savedFile = sessionStorage.getItem('mu8_filename');

    if(savedStr && savedOrig && savedFile === d.filename){
      // Restaurar da sessão (traduções em andamento)
      strings  = JSON.parse(savedStr);
      origStr  = JSON.parse(savedOrig);
      filename = savedFile;
    } else {
      // Servidor tem arquivo mas sessão não bate → buscar strings originais
      const r2 = await fetch('/api/strings');
      const d2 = await r2.json();
      if(!d2.strings) return;
      strings  = d2.strings.map(s=>({...s}));
      origStr  = d2.strings.map(s=>({...s}));
      filename = d.filename;
      saveSession();
    }

    $('b-file').textContent = filename;
    $('b-hdr').textContent  = `Header: 0x${d.header_count.toString(16).toUpperCase().padStart(4,'0')} = ${d.header_count} entradas`;
    $('dropzone').style.display     = 'none';
    $('string-list').style.display  = 'block';
    enableUI();
    updateStats();
    applyFilters();
  }catch(e){
    // Servidor offline → dropzone normal
  }
}

function importJSON(file){
  if(!file) return;
  const rd = new FileReader();
  rd.onload = e => {
    try{
      const d = JSON.parse(e.target.result);
      if(!d.strings) throw new Error('campo "strings" ausente');
      let n = 0;
      d.strings.forEach(entry=>{
        const idx = entry.index;
        if(idx>=0 && idx<strings.length && !strings[idx].null){
          strings[idx].text = entry.translation ?? entry.text ?? '';
          n++;
        }
      });
      applyFilters();
      updateStats();
      saveSession();
      toast(`✅ ${n} strings importadas`);
    }catch(err){ toast('JSON inválido: '+err.message,'err') }
  };
  rd.readAsText(file);
  $('j-in').value='';
}

function resetAll(){
  if(!confirm('Resetar TODAS as traduções para o original?')) return;
  strings = origStr.map(s=>({...s}));
  applyFilters();
  updateStats();
  saveSession();
  toast('↩ Todas as strings resetadas');
}

async function loadFile(file){
  if(!file) return;
  filename = file.name;
  $('b-file').textContent = file.name;
  $('dropzone').style.display = 'none';
  $('progress').style.display = 'block';
  $('string-list').style.display = 'none';

  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/parse',{method:'POST',body:fd});
    const d = await r.json();
    if(d.error){ toast(d.error,'err'); $('progress').style.display='none'; $('dropzone').style.display='block'; return; }

    strings = d.strings.map(s=>({...s}));
    origStr = d.strings.map(s=>({...s}));

    $('b-hdr').textContent = `Header: 0x${d.header_count.toString(16).toUpperCase().padStart(4,'0')} = ${d.header_count} entradas`;

    $('progress').style.display = 'none';
    $('string-list').style.display = 'block';
    enableUI();
    updateStats();
    applyFilters();
    saveSession();
    toast(`✅ ${strings.length} entradas carregadas (${strings.filter(s=>s.null).length} null)`);
  } catch(e){
    toast('Erro: '+e.message,'err');
    $('progress').style.display='none';
    $('dropzone').style.display='block';
  }
}

tryRestoreSession();

// ─────────────────────────────────────
// RESET
// ─────────────────────────────────────
// resetAll definida abaixo (com persistência)
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────
# ROTAS API
# ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/parse', methods=['POST'])
def api_parse():
    if 'file' not in request.files:
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400

    f    = request.files['file']
    data = f.read()

    if len(data) < 4:
        return jsonify({'error': 'Arquivo muito pequeno ou inválido'}), 400

    _state['original_data'] = data
    _state['filename']      = f.filename

    try:
        result = mu8.parse(data)
    except Exception as e:
        return jsonify({'error': f'Erro no parse: {e}'}), 400

    _state['parsed'] = result
    return jsonify(result)


@app.route('/api/build', methods=['POST'])
def api_build():
    body = request.get_json(force=True, silent=True)
    if not body or 'strings' not in body:
        return jsonify({'error': 'Payload inválido'}), 400

    if _state['parsed'] is None:
        return jsonify({'error': 'Nenhum arquivo carregado no servidor'}), 400

    edited       = body['strings']
    header_count = _state['parsed']['header_count']

    # Validar
    warns  = mu8.validate(_state['original_data'], edited)
    errors = [w for w in warns if w['type'] == 'error']
    if errors:
        return jsonify({'error': 'Erros encontrados', 'details': errors}), 400

    try:
        out = mu8.build(header_count, edited)
    except Exception as e:
        return jsonify({'error': f'Erro ao gerar arquivo: {e}'}), 500

    warn_count = len([w for w in warns if w['type'] == 'warning'])
    resp = send_file(
        io.BytesIO(out),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name='output_ptbr.mu8'
    )
    resp.headers['X-Warnings'] = str(warn_count)
    return resp


@app.route('/api/validate', methods=['POST'])
def api_validate():
    if _state['original_data'] is None:
        return jsonify({'error': 'Nenhum arquivo carregado'}), 400
    body  = request.get_json(force=True, silent=True) or {}
    warns = mu8.validate(_state['original_data'], body.get('strings', []))
    return jsonify({'warnings': warns, 'count': len(warns)})


@app.route('/api/info')
def api_info():
    if _state['parsed'] is None:
        return jsonify({'loaded': False})
    p = _state['parsed']
    return jsonify({
        'loaded':        True,
        'filename':      _state['filename'],
        'header_count':  p['header_count'],
        'total_entries': len(p['strings']),
        'nulls':         sum(1 for s in p['strings'] if s.get('null')),
        'ptr_1byte':     sum(1 for s in p['strings'] if s['size_bytes']==1 and not s.get('null')),
        'ptr_2byte':     sum(1 for s in p['strings'] if s['size_bytes']==2),
        'file_size':     len(_state['original_data']),
    })


@app.route('/api/strings')
def api_strings():
    """Retorna as strings parseadas do arquivo carregado no servidor."""
    if _state['parsed'] is None:
        return jsonify({'error': 'Nenhum arquivo carregado'}), 404
    return jsonify(_state['parsed'])



if __name__ == '__main__':
    print("  Acesse: http://localhost:5000")
    app.run(debug=False, host='127.0.0.1', port=5000)
