'use strict';

// Code inspector: a modal that browses the run's generated-code workspace.
// The header "Generated code" button fetches /api/files (a flat list of
// workspace-relative paths), renders it as a collapsible tree, and loads a
// file's contents from /api/file?path=... into the viewer pane on click.

const codeModal      = document.getElementById('code-modal');
const codeBtn        = document.getElementById('code-btn');
const codeModalClose = document.getElementById('code-modal-close');
const codeModalCopy  = document.getElementById('code-modal-copy');
const codeModalRoot  = document.getElementById('code-modal-root');
const codeTree       = document.getElementById('code-tree');
const codeView       = document.getElementById('code-view');

let _codeFileText = '';        // contents of the currently shown file (for Copy)
let _codeSelected = null;      // path of the selected file element

// Build a nested {dirs, files} tree from flat relative paths.
function buildCodeTree(paths) {
  const root = { dirs: new Map(), files: [] };
  for (const p of paths) {
    const parts = p.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      if (!node.dirs.has(seg)) node.dirs.set(seg, { dirs: new Map(), files: [] });
      node = node.dirs.get(seg);
    }
    node.files.push({ name: parts[parts.length - 1], path: p });
  }
  return root;
}

function renderCodeTree(node) {
  let html = '';
  for (const [name, child] of [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    html += `<div class="ct-folder">`
          +   `<div class="ct-folder-label"><span class="ct-caret">&#9656;</span>${esc(name)}</div>`
          +   `<div class="ct-children">${renderCodeTree(child)}</div>`
          + `</div>`;
  }
  for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
    html += `<div class="ct-file" data-path="${esc(f.path)}">${esc(f.name)}</div>`;
  }
  return html;
}

async function openCodeModal() {
  codeModal.hidden = false;
  codeTree.innerHTML = '<div class="code-tree-msg">Loading&#8230;</div>';
  codeView.innerHTML = '<div class="code-view-empty">Select a file to view its contents.</div>';
  codeModalRoot.textContent = '';
  codeModalCopy.hidden = true;
  _codeSelected = null;

  let payload;
  try {
    const resp = await fetch('/api/files');
    payload = await resp.json();
  } catch (_) {
    codeTree.innerHTML = '<div class="code-tree-msg">Failed to load workspace files.</div>';
    return;
  }

  if (!payload.available) {
    codeTree.innerHTML = '<div class="code-tree-msg">No generated-code workspace is available for this source.</div>';
    return;
  }
  if (!payload.files.length) {
    codeTree.innerHTML = '<div class="code-tree-msg">Workspace is empty &#8212; no files yet.</div>';
    codeModalRoot.textContent = payload.root || '';
    return;
  }

  codeModalRoot.textContent = payload.root || '';
  codeTree.innerHTML = renderCodeTree(buildCodeTree(payload.files));
}

async function loadCodeFile(relPath, el) {
  if (_codeSelected) _codeSelected.classList.remove('selected');
  if (el) { el.classList.add('selected'); _codeSelected = el; }
  codeView.innerHTML = '<div class="code-view-empty">Loading&#8230;</div>';
  codeModalCopy.hidden = true;

  let data;
  try {
    const resp = await fetch('/api/file?path=' + encodeURIComponent(relPath));
    if (!resp.ok) throw new Error('not found');
    data = await resp.json();
  } catch (_) {
    codeView.innerHTML = '<div class="code-view-empty">Failed to load file.</div>';
    return;
  }

  if (data.binary) {
    _codeFileText = '';
    codeView.innerHTML = `<div class="code-view-empty">Binary file (${data.size.toLocaleString()} bytes) &#8212; not shown.</div>`;
    return;
  }

  _codeFileText = data.content;
  const note = data.truncated
    ? `<div class="code-view-note">Truncated &#8212; showing first part of ${data.size.toLocaleString()} bytes.</div>`
    : '';
  codeView.innerHTML =
    `<div class="code-view-path">${esc(data.path)}</div>` +
    note +
    `<pre class="code-view-pre"><code>${esc(data.content)}</code></pre>`;
  codeView.scrollTop = 0;
  codeModalCopy.hidden = false;
  codeModalCopy.textContent = 'Copy';
}

// ── Event wiring ─────────────────────────────────────────────────────────
codeBtn.addEventListener('click', openCodeModal);
codeModalClose.addEventListener('click', () => { codeModal.hidden = true; });
codeModal.addEventListener('click', e => { if (e.target === codeModal) codeModal.hidden = true; });

codeTree.addEventListener('click', e => {
  const folder = e.target.closest('.ct-folder-label');
  if (folder) {
    folder.parentElement.classList.toggle('collapsed');
    return;
  }
  const file = e.target.closest('.ct-file');
  if (file) loadCodeFile(file.dataset.path, file);
});

codeModalCopy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(_codeFileText);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = _codeFileText;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  codeModalCopy.textContent = 'Copied!';
  setTimeout(() => { codeModalCopy.textContent = 'Copy'; }, 1500);
});
