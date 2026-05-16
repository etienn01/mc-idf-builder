/* MeshCore Firmware Builder — frontend */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allEnvs = [];   // [{board, envs: [{env_name, firmware_type}]}]
let currentBuildId = null;
let eventSource = null;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const refSelect   = document.getElementById('ref-select');
const boardSel    = document.getElementById('board-select');
const typeSel     = document.getElementById('type-select');
const regionSec   = document.getElementById('region-section');
const regionRows  = document.getElementById('region-rows');
const resetBtn    = document.getElementById('reset-regions-btn');
const locationSec = document.getElementById('location-section');
const wifiSec     = document.getElementById('wifi-section');
const buildBtn    = document.getElementById('build-btn');
const logSec      = document.getElementById('log-section');
const logOut      = document.getElementById('log-output');
const dlLink      = document.getElementById('download-link');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function regionNameValid(name) {
  return /^[a-z0-9\-\$\#]+$/.test(name) && name.length > 0 && name.length <= 30;
}

function currentBoardEnvs() {
  const board = boardSel.value;
  const group = allEnvs.find(g => g.board === board);
  return group ? group.envs : [];
}

function currentEnvName() {
  return typeSel.value;
}

function isRepeater() {
  const env = currentBoardEnvs().find(e => e.env_name === typeSel.value);
  return env && env.firmware_type === 'repeater';
}

function isWifiCompanion() {
  const env = currentBoardEnvs().find(e => e.env_name === typeSel.value);
  return env && env.firmware_type === 'companion_wifi';
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
  tr.querySelector('.region-parent').addEventListener('change', updateResetBtn);
  tr.querySelector('.region-flood').addEventListener('change', updateResetBtn);
  regionRows.appendChild(tr);
  rebuildParentDropdowns();
  if (parent) tr.querySelector('.region-parent').value = parent;
}

document.getElementById('add-region-btn').addEventListener('click', () => {
  addRegionRow();
  updateResetBtn();
});

resetBtn.addEventListener('click', loadDefaultRegions);

// ---------------------------------------------------------------------------
// Board / type selectors
// ---------------------------------------------------------------------------

function populateBoardSelect() {
  boardSel.innerHTML = allEnvs
    .map(g => `<option value="${g.board}">${g.board}</option>`)
    .join('');
  onBoardChange();
}

function onBoardChange() {
  const envs = currentBoardEnvs();
  const LABELS = {
    repeater: 'Repeater',
    companion_usb: 'Companion radio (USB)',
    companion_ble: 'Companion radio (BLE)',
    companion_wifi: 'Companion radio (WiFi)',
  };
  typeSel.innerHTML = envs
    .map(e => `<option value="${e.env_name}">${LABELS[e.firmware_type] ?? e.firmware_type}</option>`)
    .join('');
  onTypeChange();
}

function onTypeChange() {
  const rep = isRepeater();
  regionSec.hidden = !rep;
  locationSec.hidden = !rep;
  wifiSec.hidden = !isWifiCompanion();
  if (rep) loadDefaultRegions();
}

boardSel.addEventListener('change', onBoardChange);
typeSel.addEventListener('change', onTypeChange);

// ---------------------------------------------------------------------------
// Build
// ---------------------------------------------------------------------------

function collectRegions() {
  const rows = Array.from(regionRows.querySelectorAll('tr'));
  const result = [];
  for (const row of rows) {
    const name   = row.querySelector('.region-name').value.trim();
    const parent = row.querySelector('.region-parent').value || null;
    const flood  = row.querySelector('.region-flood').value;
    if (!name) continue;
    if (!regionNameValid(name)) {
      alert(`Invalid region name: "${name}"\nUse lowercase letters, digits, -, $, # only.`);
      return null;
    }
    result.push({ name, parent, flood });
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

  if (eventSource) { eventSource.close(); eventSource = null; }

  fetch('/api/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
    .then(({ build_id }) => {
      currentBuildId = build_id;
      streamLogs(build_id);
    })
    .catch(err => {
      appendLog('Error: ' + (err.detail ?? JSON.stringify(err)));
      buildBtn.disabled = false;
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
    appendLog(e.data);
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    buildBtn.disabled = false;
  };
}

function checkStatus(buildId) {
  fetch(`/api/builds/${buildId}`)
    .then(r => r.json())
    .then(({ status, download_url }) => {
      buildBtn.disabled = false;
      if (status === 'completed' && download_url) {
        dlLink.href = download_url;
        dlLink.textContent = `Download ${currentEnvName()}.bin`;
        dlLink.hidden = false;
      }
    });
}

buildBtn.addEventListener('click', startBuild);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

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
