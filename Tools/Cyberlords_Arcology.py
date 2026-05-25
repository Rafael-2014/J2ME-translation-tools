#!/usr/bin/env python3
"""
Editor Web para arquivos .LNG (Cyberlords - Arcology)
Formato: 9 bytes de cabeçalho + sequência de entradas:
  - String curta : 00 <len:1byte> <utf8_bytes>   (len = nº de BYTES utf-8)
  - Bloco grande : <len_hi:1byte> <len_lo:1byte> <utf8_bytes>  (big-endian 16-bit, primeiro byte != 00)

Uso: python app_web.py [arquivo.lng]
"""

import sys
import os
import struct
import copy
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Parser / Serializer
# ─────────────────────────────────────────────────────────────────────────────

HEADER_SIZE = 9

def parse_lng(data: bytes) -> dict:
    """Parseia os bytes e retorna estrutura com cabeçalho e lista de entradas."""
    if len(data) < HEADER_SIZE:
        raise ValueError("Arquivo muito pequeno para ser um .lng válido")

    header = data[:HEADER_SIZE]
    items = []
    i = HEADER_SIZE

    while i < len(data) - 1:
        b0 = data[i]
        b1 = data[i + 1] if i + 1 < len(data) else 0

        # Bloco grande: primeiro byte != 0x00  →  comprimento 16-bit big-endian
        if b0 != 0x00:
            big_len = (b0 << 8) | b1
            if 1 <= big_len <= 0x7FFF and i + 2 + big_len <= len(data):
                raw = data[i + 2: i + 2 + big_len]
                try:
                    text = raw.decode('utf-8')
                    items.append({
                        'type': 'BIG',
                        'offset': i,
                        'text': text,
                    })
                    i += 2 + big_len
                    continue
                except UnicodeDecodeError:
                    pass

        # String curta: primeiro byte == 0x00, segundo é o comprimento em bytes
        if b0 == 0x00 and 1 <= b1 <= 0xFF and i + 2 + b1 <= len(data):
            raw = data[i + 2: i + 2 + b1]
            try:
                text = raw.decode('utf-8')
                items.append({
                    'type': 'STR',
                    'offset': i,
                    'text': text,
                })
                i += 2 + b1
                continue
            except UnicodeDecodeError:
                pass

        # Byte não reconhecido – não deve ocorrer em arquivo válido
        raise ValueError(f"Byte inesperado no offset 0x{i:06x}: 0x{b0:02x} 0x{b1:02x}")

    return {'header': header, 'items': items}


def serialize_lng(parsed: dict) -> bytes:
    """Serializa a estrutura de volta para bytes, recalculando os comprimentos."""
    out = bytearray(parsed['header'])

    for item in parsed['items']:
        raw = item['text'].encode('utf-8')
        length = len(raw)

        if item['type'] == 'STR':
            if length > 0xFF:
                raise ValueError(
                    f"String muito longa ({length} bytes) para o formato STR (máx 255).\n"
                    f"Texto: {item['text'][:60]!r}"
                )
            out += b'\x00' + bytes([length]) + raw

        elif item['type'] == 'BIG':
            if length > 0x7FFF:
                raise ValueError(
                    f"Bloco grande muito longo ({length} bytes, máx 32767).\n"
                    f"Texto: {item['text'][:60]!r}"
                )
            hi = (length >> 8) & 0xFF
            lo = length & 0xFF
            if hi == 0x00:
                # Se o comprimento couber em 1 byte, o primeiro byte seria 0x00
                # e seria lido como STR — forçar hi=0x01 não faz sentido.
                # Neste caso, salvar como STR automaticamente é mais seguro.
                # Porém mantemos o tipo original para não mudar a semântica.
                # Na prática, blocos grandes têm length >= 256 ou o jogo diferencia pelo contexto.
                # Usamos 2 bytes sempre para BIG.
                pass
            out += bytes([hi, lo]) + raw

    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Estado global (sessão simples – uso local/Termux)
# ─────────────────────────────────────────────────────────────────────────────

state = {
    'filepath': None,
    'parsed': None,
    'original_data': None,
}

# ─────────────────────────────────────────────────────────────────────────────
# HTML / JS (single-file SPA)
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Editor LNG – Cyberlords</title>
<style>
  :root {
    --bg: #0d0f14;
    --surface: #161b25;
    --surface2: #1e2636;
    --accent: #00bfff;
    --accent2: #7b5ea7;
    --danger: #e05252;
    --ok: #52e07b;
    --text: #c8d6e5;
    --muted: #6b7b8d;
    --border: #2a3548;
    --radius: 6px;
    --mono: 'Courier New', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: sans-serif; font-size: 14px; }

  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  header h1 { font-size: 16px; color: var(--accent); flex: 1; white-space: nowrap; }

  .btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: var(--radius);
    cursor: pointer;
    font-size: 13px;
    transition: border-color .15s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary { background: var(--accent); color: #000; border-color: var(--accent); font-weight: bold; }
  .btn.primary:hover { background: #009fdf; }
  .btn.danger { background: var(--danger); color: #fff; border-color: var(--danger); }

  #status-bar {
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    padding: 5px 16px;
    font-size: 12px;
    color: var(--muted);
    display: flex;
    gap: 20px;
    align-items: center;
    flex-wrap: wrap;
  }
  #status-bar span { white-space: nowrap; }
  #status-msg { color: var(--accent); }

  #toolbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 8px 16px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  #search-input, #filter-select {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 10px;
    border-radius: var(--radius);
    font-size: 13px;
  }
  #search-input { width: 220px; }
  #search-input:focus, #filter-select:focus { outline: none; border-color: var(--accent); }

  #main { display: flex; height: calc(100vh - 110px); overflow: hidden; }

  #list-panel {
    width: 48%;
    overflow-y: auto;
    border-right: 1px solid var(--border);
  }

  #edit-panel {
    width: 52%;
    overflow-y: auto;
    padding: 16px;
    background: var(--surface);
  }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    background: var(--surface2);
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .5px;
    padding: 6px 10px;
    text-align: left;
    position: sticky;
    top: 0;
    z-index: 1;
    border-bottom: 1px solid var(--border);
  }
  tr.row { cursor: pointer; border-bottom: 1px solid var(--border); }
  tr.row:hover td { background: var(--surface2); }
  tr.row.selected td { background: #1a2d4a !important; border-left: 3px solid var(--accent); }
  tr.row td { padding: 6px 10px; vertical-align: top; }
  .tag-str { color: var(--accent); font-size: 11px; font-family: var(--mono); }
  .tag-big { color: var(--accent2); font-size: 11px; font-family: var(--mono); }
  .cell-idx { color: var(--muted); font-size: 11px; font-family: var(--mono); width: 40px; }
  .cell-offset { color: var(--muted); font-size: 11px; font-family: var(--mono); width: 70px; }
  .cell-len { font-family: var(--mono); font-size: 11px; width: 50px; }
  .len-ok { color: var(--ok); }
  .len-warn { color: #f5c518; }
  .len-err { color: var(--danger); }
  .cell-text { font-size: 12px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cell-text.changed { color: #f5c518; }

  /* Edit panel */
  #edit-panel h2 { font-size: 14px; color: var(--accent); margin-bottom: 12px; }
  .meta { font-size: 11px; color: var(--muted); margin-bottom: 10px; font-family: var(--mono); }
  .meta span { color: var(--text); }

  .field-group { margin-bottom: 14px; }
  .field-label { font-size: 11px; color: var(--muted); text-transform: uppercase; margin-bottom: 4px; }

  #edit-text {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px;
    border-radius: var(--radius);
    font-family: var(--mono);
    font-size: 13px;
    resize: vertical;
    min-height: 120px;
    line-height: 1.5;
  }
  #edit-text:focus { outline: none; border-color: var(--accent); }

  #len-display {
    font-family: var(--mono);
    font-size: 12px;
    margin-top: 4px;
  }

  #hex-preview {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 8px;
    border-radius: var(--radius);
    font-family: var(--mono);
    font-size: 11px;
    word-break: break-all;
    max-height: 80px;
    overflow-y: auto;
  }

  .edit-actions { display: flex; gap: 8px; margin-top: 12px; }

  #no-selection {
    color: var(--muted);
    text-align: center;
    margin-top: 60px;
    font-size: 13px;
  }

  .loading { color: var(--muted); padding: 20px; text-align: center; }

  /* File load overlay */
  #load-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,.85);
    display: flex; align-items: center; justify-content: center;
    z-index: 100;
  }
  #load-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 30px 36px;
    text-align: center;
    min-width: 320px;
  }
  #load-box h2 { color: var(--accent); margin-bottom: 16px; }
  #load-box p { color: var(--muted); font-size: 12px; margin-top: 8px; }
  #file-input { display: none; }
  #filepath-input {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px;
    border-radius: var(--radius);
    font-size: 13px;
    margin-top: 12px;
  }
  .drop-zone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 20px;
    cursor: pointer;
    transition: border-color .2s;
    margin-bottom: 10px;
  }
  .drop-zone:hover, .drop-zone.drag-over { border-color: var(--accent); color: var(--accent); }
  .separator { color: var(--muted); font-size: 11px; margin: 10px 0; }
</style>
</head>
<body>

<!-- LOAD OVERLAY -->
<div id="load-overlay">
  <div id="load-box">
    <h2>🎮 Editor LNG</h2>
    <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
      📂 Clique ou arraste um arquivo .lng
    </div>
    <input type="file" id="file-input" accept=".lng,.bin">
    <div class="separator">── ou informe o caminho ──</div>
    <input type="text" id="filepath-input" placeholder="Ex: /sdcard/EN_MAIN.lng">
    <button class="btn primary" style="margin-top:10px;width:100%" onclick="loadByPath()">Carregar pelo caminho</button>
    <p id="load-error" style="color:var(--danger);margin-top:8px"></p>
  </div>
</div>

<!-- MAIN UI -->
<header>
  <h1>🎮 Editor LNG – Cyberlords</h1>
  <button class="btn" onclick="showLoadOverlay()">📂 Abrir</button>
  <button class="btn primary" onclick="saveFile()">💾 Salvar</button>
  <button class="btn" onclick="revertAll()">↩ Reverter Tudo</button>
</header>

<div id="status-bar">
  <span>Arquivo: <b id="sb-file">—</b></span>
  <span>Entradas: <b id="sb-count">0</b></span>
  <span>Modificadas: <b id="sb-changed">0</b></span>
  <span id="status-msg"></span>
</div>

<div id="toolbar">
  <input type="text" id="search-input" placeholder="🔍 Buscar texto..." oninput="filterList()">
  <select id="filter-select" onchange="filterList()">
    <option value="all">Todos os tipos</option>
    <option value="STR">Strings curtas (STR)</option>
    <option value="BIG">Blocos grandes (BIG)</option>
    <option value="changed">Modificadas</option>
  </select>
  <button class="btn" onclick="clearSearch()">✕ Limpar</button>
  <span style="color:var(--muted);font-size:12px" id="filter-count"></span>
</div>

<div id="main">
  <div id="list-panel">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Offset</th>
          <th>Tipo</th>
          <th>Bytes</th>
          <th>Texto</th>
        </tr>
      </thead>
      <tbody id="list-body">
        <tr><td colspan="5" class="loading">Carregue um arquivo .lng para começar.</td></tr>
      </tbody>
    </table>
  </div>

  <div id="edit-panel">
    <div id="no-selection">← Selecione uma entrada para editar</div>
    <div id="edit-form" style="display:none">
      <h2>Editar entrada</h2>
      <div class="meta">
        Índice: <span id="ei-idx">—</span> &nbsp;|&nbsp;
        Offset: <span id="ei-offset">—</span> &nbsp;|&nbsp;
        Tipo: <span id="ei-type">—</span>
      </div>

      <div class="field-group">
        <div class="field-label">Texto (UTF-8 | usa \\n para nova linha)</div>
        <textarea id="edit-text" oninput="onEditInput()"></textarea>
        <div id="len-display"></div>
      </div>

      <div class="field-group">
        <div class="field-label">Pré-visualização hex</div>
        <div id="hex-preview"></div>
      </div>

      <div class="edit-actions">
        <button class="btn primary" onclick="applyEdit()">✔ Aplicar</button>
        <button class="btn" onclick="revertEntry()">↩ Reverter entrada</button>
        <button class="btn" id="prev-btn" onclick="navigate(-1)">◀ Anterior</button>
        <button class="btn" id="next-btn" onclick="navigate(1)">▶ Próximo</button>
      </div>
    </div>
  </div>
</div>

<script>
// ─── State ───────────────────────────────────────────────────────────────────
let items = [];          // parsed items from server
let origTexts = [];      // original texts for diff
let changes = {};        // index -> new text
let selectedIdx = null;
let filteredIndices = []; // indices after filter

// ─── Load overlay ────────────────────────────────────────────────────────────
const overlay = document.getElementById('load-overlay');

function showLoadOverlay() { overlay.style.display = 'flex'; }
function hideLoadOverlay() { overlay.style.display = 'none'; }

// Drag & drop
const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) handleFileInput(e.dataTransfer.files[0]);
});
document.getElementById('file-input').addEventListener('change', e => {
  if (e.target.files[0]) handleFileInput(e.target.files[0]);
});

function handleFileInput(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    const bytes = new Uint8Array(ev.target.result);
    uploadBytes(bytes, file.name);
  };
  reader.readAsArrayBuffer(file);
}

function uploadBytes(bytes, filename) {
  setStatus('Carregando…');
  fetch('/api/load_bytes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': filename },
    body: bytes
  })
  .then(r => r.json())
  .then(handleLoadResponse)
  .catch(e => setLoadError('Erro: ' + e));
}

function loadByPath() {
  const path = document.getElementById('filepath-input').value.trim();
  if (!path) return setLoadError('Informe um caminho.');
  setStatus('Carregando…');
  fetch('/api/load_path', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path })
  })
  .then(r => r.json())
  .then(handleLoadResponse)
  .catch(e => setLoadError('Erro: ' + e));
}

function handleLoadResponse(data) {
  if (data.error) { setLoadError(data.error); return; }
  items = data.items;
  origTexts = items.map(it => it.text);
  changes = {};
  selectedIdx = null;
  hideLoadOverlay();
  document.getElementById('sb-file').textContent = data.filename || '(buffer)';
  renderList();
  showEditPanel(false);
  setStatus('Carregado: ' + items.length + ' entradas.');
}

function setLoadError(msg) {
  document.getElementById('load-error').textContent = msg;
}

// ─── List rendering ───────────────────────────────────────────────────────────
function filterList() {
  const q = document.getElementById('search-input').value.toLowerCase();
  const type = document.getElementById('filter-select').value;
  filteredIndices = [];
  items.forEach((it, i) => {
    const text = (changes[i] !== undefined ? changes[i] : it.text).toLowerCase();
    if (type === 'changed' && changes[i] === undefined) return;
    if (type === 'STR' && it.type !== 'STR') return;
    if (type === 'BIG' && it.type !== 'BIG') return;
    if (q && !text.includes(q)) return;
    filteredIndices.push(i);
  });
  document.getElementById('filter-count').textContent =
    filteredIndices.length === items.length ? '' : filteredIndices.length + ' resultado(s)';
  renderList();
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  document.getElementById('filter-select').value = 'all';
  filterList();
}

function renderList() {
  if (filteredIndices.length === 0 && items.length > 0) {
    // init filteredIndices
    filteredIndices = items.map((_, i) => i);
  }
  const tbody = document.getElementById('list-body');
  const rows = filteredIndices.map(i => {
    const it = items[i];
    const currentText = changes[i] !== undefined ? changes[i] : it.text;
    const byteLen = new TextEncoder().encode(currentText).length;
    const isChanged = changes[i] !== undefined;
    const maxLen = it.type === 'STR' ? 255 : 32767;
    const lenClass = byteLen > maxLen ? 'len-err' : byteLen > maxLen * 0.9 ? 'len-warn' : 'len-ok';
    const typeTag = it.type === 'BIG'
      ? `<span class="tag-big">[BIG]</span>`
      : `<span class="tag-str">[STR]</span>`;
    const preview = currentText.replace(/\n/g, '↵').slice(0, 55);
    const selClass = i === selectedIdx ? ' selected' : '';
    return `<tr class="row${selClass}" data-idx="${i}" onclick="selectEntry(${i})">
      <td class="cell-idx">${i}</td>
      <td class="cell-offset">0x${it.offset.toString(16).padStart(5,'0')}</td>
      <td>${typeTag}</td>
      <td class="cell-len ${lenClass}">${byteLen}</td>
      <td class="cell-text${isChanged?' changed':''}" title="${escHtml(currentText)}">${escHtml(preview)}</td>
    </tr>`;
  }).join('');
  tbody.innerHTML = rows || '<tr><td colspan="5" style="color:var(--muted);padding:14px">Nenhum resultado.</td></tr>';

  // Update status
  const changedCount = Object.keys(changes).length;
  document.getElementById('sb-count').textContent = items.length;
  document.getElementById('sb-changed').textContent = changedCount;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── Entry selection / editing ────────────────────────────────────────────────
function selectEntry(i) {
  selectedIdx = i;
  showEditPanel(true);
  const it = items[i];
  const currentText = changes[i] !== undefined ? changes[i] : it.text;

  document.getElementById('ei-idx').textContent = i;
  document.getElementById('ei-offset').textContent = '0x' + it.offset.toString(16).padStart(5,'0');
  document.getElementById('ei-type').textContent = it.type;

  const ta = document.getElementById('edit-text');
  ta.value = currentText;
  ta.rows = Math.min(20, Math.max(4, currentText.split('\n').length + 2));

  updateLenDisplay();
  updateHexPreview(currentText);

  // Highlight row
  document.querySelectorAll('tr.row').forEach(tr => tr.classList.remove('selected'));
  const row = document.querySelector(`tr.row[data-idx="${i}"]`);
  if (row) { row.classList.add('selected'); row.scrollIntoView({block:'nearest'}); }
}

function showEditPanel(show) {
  document.getElementById('no-selection').style.display = show ? 'none' : 'block';
  document.getElementById('edit-form').style.display = show ? 'block' : 'none';
}

function onEditInput() {
  updateLenDisplay();
  updateHexPreview(document.getElementById('edit-text').value);
}

function updateLenDisplay() {
  if (selectedIdx === null) return;
  const it = items[selectedIdx];
  const text = document.getElementById('edit-text').value;
  const byteLen = new TextEncoder().encode(text).length;
  const maxLen = it.type === 'STR' ? 255 : 32767;
  const color = byteLen > maxLen ? 'var(--danger)' : byteLen > maxLen * 0.9 ? '#f5c518' : 'var(--ok)';
  document.getElementById('len-display').innerHTML =
    `<span style="color:${color}">Bytes UTF-8: <b>${byteLen}</b> / ${maxLen}</span>` +
    (byteLen > maxLen ? ' ⚠ EXCEDE O LIMITE!' : '');
}

function updateHexPreview(text) {
  const bytes = new TextEncoder().encode(text);
  const it = selectedIdx !== null ? items[selectedIdx] : null;
  let prefix = '';
  if (it) {
    if (it.type === 'STR') {
      prefix = `00 ${bytes.length.toString(16).padStart(2,'0').toUpperCase()} `;
    } else {
      const hi = (bytes.length >> 8) & 0xFF;
      const lo = bytes.length & 0xFF;
      prefix = `${hi.toString(16).padStart(2,'0').toUpperCase()} ${lo.toString(16).padStart(2,'0').toUpperCase()} `;
    }
  }
  const hex = Array.from(bytes).map(b => b.toString(16).padStart(2,'0').toUpperCase()).join(' ');
  document.getElementById('hex-preview').textContent = prefix + hex;
}

function applyEdit() {
  if (selectedIdx === null) return;
  const it = items[selectedIdx];
  const newText = document.getElementById('edit-text').value;
  const byteLen = new TextEncoder().encode(newText).length;
  const maxLen = it.type === 'STR' ? 255 : 32767;
  if (byteLen > maxLen) {
    setStatus(`⚠ String excede ${maxLen} bytes (${byteLen} bytes). Reduza o texto.`, true);
    return;
  }
  changes[selectedIdx] = newText;
  items[selectedIdx].text = newText;
  renderList();
  selectEntry(selectedIdx);
  setStatus('Salvando...');
  fetch('/api/update_item', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: selectedIdx, text: newText })
  })
  .then(r => r.json())
  .then(d => {
    if (d.error) setStatus('Erro ao salvar: ' + d.error, true);
    else setStatus('Entrada #' + selectedIdx + ' salva.');
  })
  .catch(e => setStatus('Erro: ' + e, true));
}

function revertEntry() {
  if (selectedIdx === null) return;
  delete changes[selectedIdx];
  items[selectedIdx].text = origTexts[selectedIdx];
  selectEntry(selectedIdx);
  renderList();
  setStatus(`↩ Entrada #${selectedIdx} revertida.`);
}

function revertAll() {
  if (!confirm('Reverter TODAS as alterações?')) return;
  changes = {};
  items.forEach((it, i) => it.text = origTexts[i]);
  renderList();
  if (selectedIdx !== null) selectEntry(selectedIdx);
  setStatus('↩ Todas as alterações revertidas.');
}

function navigate(dir) {
  if (selectedIdx === null) return;
  const pos = filteredIndices.indexOf(selectedIdx);
  const newPos = pos + dir;
  if (newPos >= 0 && newPos < filteredIndices.length) {
    // Apply current edit first
    applyEdit();
    selectEntry(filteredIndices[newPos]);
  }
}

// ─── Save / Download ──────────────────────────────────────────────────────────
function saveFile() {
  if (!items.length) return;
  setStatus('Salvando…');
  fetch('/api/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items: items })
  })
  .then(r => r.json())
  .then(d => {
    if (d.error) setStatus('Erro: ' + d.error, true);
    else setStatus('💾 Arquivo salvo: ' + d.path);
  });
}



// ─── Status ───────────────────────────────────────────────────────────────────
let statusTimer = null;
function setStatus(msg, isError=false) {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.style.color = isError ? 'var(--danger)' : 'var(--accent)';
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => el.textContent = '', 5000);
}

// ─── Init ─────────────────────────────────────────────────────────────────────
filterList();

// Check if server already has a file loaded (CLI arg)
fetch('/api/status').then(r => r.json()).then(d => {
  if (d.loaded) {
    handleLoadResponse(d);
  }
});
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return HTML


@app.route('/api/status')
def api_status():
    if state['parsed']:
        return jsonify({
            'loaded': True,
            'filename': os.path.basename(state['filepath'] or 'buffer'),
            'items': state['parsed']['items'],
        })
    return jsonify({'loaded': False})


@app.route('/api/load_bytes', methods=['POST'])
def api_load_bytes():
    data = request.get_data()
    filename = request.headers.get('X-Filename', 'arquivo.lng')
    try:
        parsed = parse_lng(data)
        state['parsed'] = parsed
        state['original_data'] = data
        state['filepath'] = None
        return jsonify({
            'filename': filename,
            'items': parsed['items'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/load_path', methods=['POST'])
def api_load_path():
    body = request.get_json()
    path = body.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Caminho vazio.'}), 400
    if not os.path.exists(path):
        return jsonify({'error': f'Arquivo não encontrado: {path}'}), 404
    try:
        data = open(path, 'rb').read()
        parsed = parse_lng(data)
        state['parsed'] = parsed
        state['original_data'] = data
        state['filepath'] = path
        return jsonify({
            'filename': os.path.basename(path),
            'items': parsed['items'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/update_item', methods=['POST'])
def api_update_item():
    body = request.get_json()
    idx = body.get('index')
    text = body.get('text')
    if state['parsed'] is None:
        return jsonify({'error': 'Nenhum arquivo carregado.'}), 400
    items = state['parsed']['items']
    if idx is None or not (0 <= idx < len(items)):
        return jsonify({'error': f'Índice inválido: {idx}'}), 400
    try:
        raw = text.encode('utf-8')
        max_len = 255 if items[idx]['type'] == 'STR' else 0x7FFF
        if len(raw) > max_len:
            return jsonify({'error': f'String excede {max_len} bytes ({len(raw)} bytes).'}), 400
        items[idx]['text'] = text
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/save', methods=['POST'])
def api_save():
    body = request.get_json()
    items_in = body.get('items', [])
    if state['parsed'] is None:
        return jsonify({'error': 'Nenhum arquivo carregado.'}), 400
    try:
        parsed_copy = copy.deepcopy(state['parsed'])
        parsed_copy['items'] = items_in
        out_bytes = serialize_lng(parsed_copy)

        # Determine save path
        if state['filepath']:
            save_path = state['filepath']
        else:
            save_path = 'EN_MAIN_edited.lng'

        with open(save_path, 'wb') as f:
            f.write(out_bytes)

        return jsonify({'path': save_path, 'size': len(out_bytes)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# CLI auto-load
# ─────────────────────────────────────────────────────────────────────────────

def preload_file(path):
    if not os.path.exists(path):
        print(f"[AVISO] Arquivo não encontrado: {path}")
        return
    try:
        data = open(path, 'rb').read()
        parsed = parse_lng(data)
        state['parsed'] = parsed
        state['original_data'] = data
        state['filepath'] = path
        print(f"[OK] Arquivo carregado: {path} ({len(parsed['items'])} entradas)")
    except Exception as e:
        print(f"[ERRO] Falha ao carregar {path}: {e}")


if __name__ == '__main__':
    port = 5000

    # Auto-load apenas se o caminho for passado explicitamente como argumento
    if len(sys.argv) > 1:
        preload_file(sys.argv[1])

    print(f"\n{'='*50}")
    print(f"  Editor LNG – Cyberlords")
    print(f"  Acesse: http://localhost:{port}")
    print(f"  Uso: python app_web.py [arquivo.lng]")
    print(f"{'='*50}\n")

    app.run(host='0.0.0.0', port=port, debug=False)
