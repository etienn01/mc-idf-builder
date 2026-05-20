/* MeshCore Firmware Builder — frontend */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allEnvs = [];   // [{id, name, envs: [{env_name, role, label, platform}]}]
let currentBuildId = null;
let eventSource = null;

// ---------------------------------------------------------------------------
// Suggested PRs
// ---------------------------------------------------------------------------
const SUGGESTED_PRS = {
  repeater: [
    { number: 1687, title: 'Power saving for ESP32 repeaters', platform: 'espressif32' },
    { number: 2140, title: 'CLI control for LoRa FEM LNA', boards: ['heltec_v4', 'heltec_t096', 'heltec_tracker_v2'] },
  ],
  companion: [
    { number: 1686, title: 'Short sleeps when phone disconnects' },
    { number: 2286, title: 'Power saving for nRF52 companions (+30% battery)', platform: 'nordicnrf52' },
  ],
};

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const refSelect    = document.getElementById('ref-select');
const boardSel     = document.getElementById('board-select');
const typeSel      = document.getElementById('type-select');
const patchSec       = document.getElementById('patch-section');
const suggestedPRs   = document.getElementById('suggested-prs');
const prList         = document.getElementById('pr-list');
const customPrInput  = document.getElementById('custom-pr-input');
const addPrBtn       = document.getElementById('add-pr-btn');
const regionSec     = document.getElementById('region-section');
const regionsEnable = document.getElementById('regions-enable');
const regionBody    = document.getElementById('region-body');
const regionRows    = document.getElementById('region-rows');
const resetBtn      = document.getElementById('reset-regions-btn');
const locationSec  = document.getElementById('location-section');
const wifiSec      = document.getElementById('wifi-section');
const buildBtn     = document.getElementById('build-btn');
const cancelBtn    = document.getElementById('cancel-btn');
const logSec       = document.getElementById('log-section');
const logOut       = document.getElementById('log-output');
const dlLink       = document.getElementById('download-link');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function regionNameValid(name) {
  return /^[a-zA-Z0-9\-\$\#]+$/.test(name) && name.length > 0 && name.length <= 30;
}

function currentBoardEnvs() {
  const board = boardSel.value;
  const group = allEnvs.find(g => g.id === board);
  return group ? group.envs : [];
}

function currentEnvName() {
  return typeSel.value;
}

function isRepeater() {
  const env = currentBoardEnvs().find(e => e.env_name === typeSel.value);
  return env && env.role === 'repeater';
}

function isWifiCompanion() {
  const env = currentBoardEnvs().find(e => e.env_name === typeSel.value);
  return env && env.role === 'companion_wifi';
}

// ---------------------------------------------------------------------------
// Region table
// ---------------------------------------------------------------------------

const DEFAULT_REGIONS = [
  { name: 'fr',     parent: '',   flood: 'allow' },
  { name: 'fr-idf', parent: 'fr', flood: 'allow' },
];

function getRegionSnapshot() {
  return Array.from(regionRows.querySelectorAll('tr')).map(row => ({
    name:   row.querySelector('.region-name').value.trim(),
    parent: row.querySelector('.region-parent').value,
    flood:  row.querySelector('.region-flood').value,
  })).filter(r => r.name);
}

function regionsMatchDefaults() {
  const rows = getRegionSnapshot();
  if (rows.length !== DEFAULT_REGIONS.length) return false;
  return rows.every((r, i) => {
    const d = DEFAULT_REGIONS[i];
    return r.name === d.name && r.parent === d.parent && r.flood === d.flood;
  });
}

function updateResetBtn() {
  resetBtn.hidden = regionsMatchDefaults();
}

function loadDefaultRegions() {
  regionRows.innerHTML = '';
  for (const { name, parent, flood } of DEFAULT_REGIONS) {
    addRegionRow(name, parent, flood);
  }
  updateResetBtn();
}

function rebuildParentDropdowns() {
  const rows = Array.from(regionRows.querySelectorAll('tr'));

  // name → current parent value
  const parentOf = {};
  rows.forEach(row => {
    const name = row.querySelector('.region-name').value.trim();
    const par  = row.querySelector('.region-parent').value;
    if (name) parentOf[name] = par;
  });

  // name → [children]
  const childrenOf = {};
  for (const [name, par] of Object.entries(parentOf)) {
    if (par) (childrenOf[par] ??= []).push(name);
  }

  function descendants(name, out = new Set()) {
    for (const child of (childrenOf[name] ?? [])) {
      if (!out.has(child)) { out.add(child); descendants(child, out); }
    }
    return out;
  }

  const allNames = Object.keys(parentOf);

  rows.forEach(row => {
    const myName   = row.querySelector('.region-name').value.trim();
    const sel      = row.querySelector('.region-parent');
    const current  = sel.value;
    const excluded = new Set([myName, ...descendants(myName)]);

    sel.innerHTML = '<option value="">(root)</option>' +
      allNames
        .filter(n => !excluded.has(n))
        .map(n => `<option value="${n}"${n === current ? ' selected' : ''}>${n}</option>`)
        .join('');
  });
}

function addRegionRow(name = '', parent = '', flood = 'allow') {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input class="region-name" type="text" placeholder="e.g. eu" value="${name}" maxlength="30"></td>
    <td><select class="region-parent"><option value="">(root)</option></select></td>
    <td><select class="region-flood">
      <option value="allow"${flood === 'allow' ? ' selected' : ''}>Allow</option>
      <option value="deny"${flood === 'deny' ? ' selected' : ''}>Deny</option>
    </select></td>
    <td><button class="btn-remove" type="button" title="Remove">✕</button></td>
  `;
  tr.querySelector('.btn-remove').addEventListener('click', () => {
    tr.remove();
    rebuildParentDropdowns();
    updateResetBtn();
  });
  tr.querySelector('.region-name').addEventListener('input', () => {
    rebuildParentDropdowns();
    updateResetBtn();
  });
  tr.querySelector('.region-parent').addEventListener('change', () => {
    const sel = tr.querySelector('.region-parent');
    const myName = tr.querySelector('.region-name').value.trim();
    const newParent = sel.value;
    if (newParent && myName) {
      // Walk up from newParent to check for a cycle
      const rows = Array.from(regionRows.querySelectorAll('tr'));
      const parentOf = Object.fromEntries(rows.map(r => [
        r.querySelector('.region-name').value.trim(),
        r.querySelector('.region-parent').value,
      ]));
      let cur = newParent;
      const visited = new Set([myName]);
      while (cur) {
        if (visited.has(cur)) { sel.value = ''; break; }
        visited.add(cur);
        cur = parentOf[cur] ?? '';
      }
    }
    rebuildParentDropdowns();
    updateResetBtn();
  });
  tr.querySelector('.region-flood').addEventListener('change', updateResetBtn);
  regionRows.appendChild(tr);
  rebuildParentDropdowns();
  if (parent) {
    tr.querySelector('.region-parent').value = parent;
    rebuildParentDropdowns();
  }
}

document.getElementById('add-region-btn').addEventListener('click', () => {
  addRegionRow();
  updateResetBtn();
});

resetBtn.addEventListener('click', loadDefaultRegions);

regionsEnable.addEventListener('change', () => {
  regionBody.hidden = !regionsEnable.checked;
  if (regionsEnable.checked && regionRows.querySelectorAll('tr').length === 0) {
    loadDefaultRegions();
  }
});

// ---------------------------------------------------------------------------
// PR section
// ---------------------------------------------------------------------------

function currentPlatform() {
  const group = allEnvs.find(g => g.id === boardSel.value);
  if (!group) return '';
  const env = group.envs.find(e => e.env_name === typeSel.value);
  return env ? env.platform : '';
}

function renderSuggestedPRs() {
  const ftype = isRepeater() ? 'repeater' : 'companion';
  const platform = currentPlatform();
  const board = boardSel.value.toLowerCase();
  const list = SUGGESTED_PRS[ftype] || [];
  const visible = list.filter(pr =>
    (!pr.platform || pr.platform === platform) &&
    (!pr.boards || pr.boards.some(b => board.includes(b)))
  );

  suggestedPRs.innerHTML = '';
  for (const pr of visible) {
    const url = `https://github.com/meshcore-dev/MeshCore/pull/${pr.number}`;
    const lbl = document.createElement('label');
    lbl.className = 'pr-suggested';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = pr.number;

    const title = document.createElement('span');
    title.className = 'pr-suggested-title';
    title.textContent = `#${pr.number} ${pr.title}`;

    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.className = 'pr-ext-link';
    link.title = 'Open on GitHub';
    link.textContent = '↗';
    link.addEventListener('click', e => e.stopPropagation());

    lbl.append(cb, title, link);
    suggestedPRs.appendChild(lbl);
  }
}

function addCustomPr(num) {
  const existing = new Set([
    ...Array.from(suggestedPRs.querySelectorAll('input[type="checkbox"]')).map(cb => parseInt(cb.value)),
    ...Array.from(prList.querySelectorAll('li')).map(li => parseInt(li.dataset.pr)),
  ]);
  if (existing.has(num)) return;
  const li = document.createElement('li');
  li.dataset.pr = num;
  const label = document.createElement('span');
  label.className = 'pr-label';
  label.textContent = `#${num}`;
  const up = document.createElement('button');
  up.type = 'button'; up.className = 'pr-move'; up.title = 'Move up'; up.textContent = '▲';
  up.addEventListener('click', () => { const prev = li.previousElementSibling; if (prev) prList.insertBefore(li, prev); });
  const dn = document.createElement('button');
  dn.type = 'button'; dn.className = 'pr-move'; dn.title = 'Move down'; dn.textContent = '▼';
  dn.addEventListener('click', () => { const next = li.nextElementSibling; if (next) prList.insertBefore(next, li); });
  const rm = document.createElement('button');
  rm.type = 'button'; rm.className = 'btn-remove'; rm.title = 'Remove'; rm.textContent = '✕';
  rm.addEventListener('click', () => li.remove());
  li.append(label, up, dn, rm);
  prList.appendChild(li);
}

addPrBtn.addEventListener('click', () => {
  const num = parseInt(customPrInput.value);
  if (num > 0) { addCustomPr(num); customPrInput.value = ''; }
});

customPrInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addPrBtn.click();
});

function collectPRs() {
  const suggested = Array.from(suggestedPRs.querySelectorAll('input[type="checkbox"]:checked'))
    .map(cb => parseInt(cb.value));
  const custom = Array.from(prList.querySelectorAll('li'))
    .map(li => parseInt(li.dataset.pr));
  return [...suggested, ...custom];
}

// ---------------------------------------------------------------------------
// Board / type selectors
// ---------------------------------------------------------------------------

function populateBoardSelect() {
  boardSel.innerHTML = allEnvs
    .map(g => `<option value="${g.id}">${g.name}</option>`)
    .join('');
  onBoardChange();
}

function onBoardChange() {
  const envs = currentBoardEnvs();
  typeSel.innerHTML = envs
    .map(e => `<option value="${e.env_name}">${e.label}</option>`)
    .join('');
  onTypeChange();
}

function onTypeChange() {
  const rep = isRepeater();
  regionSec.hidden = !rep;
  locationSec.hidden = !rep;
  wifiSec.hidden = !isWifiCompanion();
  if (rep) {
    regionsEnable.checked = false;
    regionBody.hidden = true;
    regionRows.innerHTML = '';
  }
  renderSuggestedPRs();
}

boardSel.addEventListener('change', onBoardChange);
typeSel.addEventListener('change', onTypeChange);

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------

function collectRegions() {
  if (!regionsEnable.checked) return [];
  const rows = Array.from(regionRows.querySelectorAll('tr'));
  const result = [];
  for (const row of rows) {
    const name   = row.querySelector('.region-name').value.trim();
    const parent = row.querySelector('.region-parent').value || null;
    const flood  = row.querySelector('.region-flood').value;
    if (!name) continue;
    if (!regionNameValid(name)) {
      alert(`Invalid region name: "${name}"\nUse letters, digits, -, $, # only (max 30 chars).`);
      return null;
    }
    result.push({ name, parent, flood });
  }

  // Detect cycles
  const parentOf = Object.fromEntries(result.map(r => [r.name, r.parent]));
  for (const r of result) {
    let cur = r.parent;
    const visited = new Set([r.name]);
    while (cur) {
      if (visited.has(cur)) {
        alert(`Region cycle detected: "${r.name}" is part of a circular parent chain.`);
        return null;
      }
      visited.add(cur);
      cur = parentOf[cur] ?? null;
    }
  }

  return result;
}

function appendLog(text) {
  logOut.textContent += text + '\n';
  logOut.scrollTop = logOut.scrollHeight;
}

function startBuild() {
  const regions = collectRegions();
  if (regions === null) return;

  const body = {
    env: currentEnvName(),
    ref: refSelect.value,
    prs: collectPRs(),
    advert_name: document.getElementById('advert-name').value || undefined,
    admin_password: document.getElementById('admin-password').value || undefined,
    wifi_ssid: document.getElementById('wifi-ssid').value || undefined,
    wifi_pwd: document.getElementById('wifi-pwd').value || undefined,
    regions,
  };

  const latVal = document.getElementById('lat').value;
  const lonVal = document.getElementById('lon').value;
  if (latVal !== '') body.advert_lat = parseFloat(latVal);
  if (lonVal !== '') body.advert_lon = parseFloat(lonVal);

  // Reset UI
  logOut.textContent = '';
  dlLink.hidden = true;
  logSec.hidden = false;
  buildBtn.disabled = true;
  cancelBtn.hidden = false;

  if (eventSource) { eventSource.close(); eventSource = null; }

  fetch('/api/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject({ status: r.status, ...e })))
    .then(({ build_id }) => {
      currentBuildId = build_id;
      localStorage.setItem('buildId', build_id);
      streamLogs(build_id);
    })
    .catch(err => {
      appendLog(err.status === 429
        ? 'Build queue is full — try again in a moment.'
        : 'Error: ' + (err.detail ?? JSON.stringify(err)));
      buildBtn.disabled = false;
      cancelBtn.hidden = true;
    });
}

function streamLogs(buildId) {
  eventSource = new EventSource(`/api/builds/${buildId}/logs`);

  eventSource.onmessage = (e) => {
    if (e.data === '[DONE]') {
      eventSource.close();
      eventSource = null;
      checkStatus(buildId);
      return;
    }
    appendLog(JSON.parse(e.data));
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    buildBtn.disabled = false;
    cancelBtn.hidden = true;
  };
}

function checkStatus(buildId) {
  fetch(`/api/builds/${buildId}`)
    .then(r => r.json())
    .then(({ status, download_url, filename }) => {
      buildBtn.disabled = false;
      cancelBtn.hidden = true;
      localStorage.removeItem('buildId');
      if (status === 'completed' && download_url) {
        dlLink.href = download_url;
        dlLink.textContent = `Download ${filename ?? currentEnvName() + '.bin'}`;
        dlLink.hidden = false;
      }
    });
}

cancelBtn.addEventListener('click', () => {
  if (!currentBuildId) return;
  fetch(`/api/builds/${currentBuildId}`, { method: 'DELETE' });
});

buildBtn.addEventListener('click', startBuild);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// Reconnect to an in-progress build after page reload
const savedBuildId = localStorage.getItem('buildId');
if (savedBuildId) {
  fetch(`/api/builds/${savedBuildId}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || !['pending', 'running'].includes(data.status)) {
        localStorage.removeItem('buildId');
        return;
      }
      currentBuildId = savedBuildId;
      logSec.hidden = false;
      buildBtn.disabled = true;
      cancelBtn.hidden = false;
      appendLog(`Reconnected to build ${savedBuildId.slice(0, 8)}…`);
      streamLogs(savedBuildId);
    });
}

fetch('/api/environments')
  .then(r => r.json())
  .then(data => {
    allEnvs = data;
    populateBoardSelect();
  })
  .catch(() => appendLog('Failed to load board list.'));

fetch('/api/versions')
  .then(r => r.json())
  .then(versions => {
    refSelect.innerHTML = versions
      .map(v => `<option value="${v.value}">${v.label}</option>`)
      .join('');
  })
  .catch(() => {});  // non-critical
