import { ESPLoader, Transport, HardReset } from '/lib/esp32.js';
import { Dfu } from '/lib/dfu.js';

const dfuBtn       = document.getElementById('dfu-btn');
const flashBtn     = document.getElementById('flash-btn');
const flashSection = document.getElementById('flash-section');
const flashBar     = document.getElementById('flash-bar');
const flashStatus  = document.getElementById('flash-status');
const flashLog     = document.getElementById('flash-log');

const serialSupported = 'serial' in navigator;
if (!serialSupported) {
  for (const btn of [dfuBtn, flashBtn]) {
    btn.disabled = true;
    btn.title = 'Requires a browser with Web Serial API support';
  }
}

async function blobToBinaryString(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

function setStatus(msg) {
  flashStatus.textContent = msg;
}

function appendLog(msg) {
  flashLog.hidden = false;
  flashLog.textContent += msg;
  flashLog.scrollTop = flashLog.scrollHeight;
}

function setProgress(pct) {
  flashBar.value = pct;
}

async function flashEsp32(port, blob) {
  const transport = new Transport(port, true);
  const esploader = new ESPLoader({
    transport,
    terminal: { clean() { flashLog.textContent = ''; }, write: appendLog, writeLine: (s) => appendLog(s + '\n') },
    compress: true,
    eraseAll: false,
    flashSize: 'keep',
    flashMode: 'keep',
    flashFreq: 'keep',
    baudrate: 921600,
    romBaudrate: 115200,
    enableTracing: false,
    fileArray: [{ data: await blobToBinaryString(blob), address: 0x10000 }],
    reportProgress: (_, written, total) => setProgress((written / total) * 100),
  });
  esploader.hr = new HardReset(transport);

  try {
    setStatus('Connecting…');
    await esploader.main();
    setStatus('Writing firmware…');
    await esploader.writeFlash({
      fileArray: [{ data: await blobToBinaryString(blob), address: 0x10000 }],
      flashSize: 'keep', flashMode: 'keep', flashFreq: 'keep',
      compress: true, eraseAll: false,
      reportProgress: (_, written, total) => setProgress((written / total) * 100),
    });
    await esploader.after('hard_reset');
  } finally {
    await transport.disconnect();
  }
}

async function flashNrf52(port, blob) {
  const dfu = new Dfu(port);
  setStatus('Flashing via DFU…');
  await dfu.dfuUpdate(blob, (pct) => {
    setProgress(pct);
    setStatus(`Flashing… ${pct}%`);
  }, 60000);
}

dfuBtn.addEventListener('click', async () => {
  if (!serialSupported) return;
  dfuBtn.disabled = true;
  setStatus('');
  flashSection.hidden = false;
  try {
    setStatus('Select the device serial port…');
    await Dfu.forceDfuMode(await navigator.serial.requestPort({}));
    setStatus('Device is now in DFU mode — ready to flash.');
  } catch (e) {
    setStatus(`Failed: ${e.message ?? e}`);
  } finally {
    dfuBtn.disabled = !serialSupported;
  }
});

flashBtn.addEventListener('click', async () => {
  if (!serialSupported) return;
  const state = window._flashState;
  if (!state) return;

  flashBtn.disabled = true;
  dfuBtn.disabled = true;
  flashSection.hidden = false;
  flashLog.hidden = true;
  flashLog.textContent = '';
  setProgress(0);
  setStatus('Downloading firmware…');

  try {
    const resp = await fetch(state.downloadUrl);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();

    const port = await navigator.serial.requestPort({});

    if (state.filename.endsWith('.bin')) {
      await flashEsp32(port, blob);
    } else if (state.filename.endsWith('.zip')) {
      await flashNrf52(port, blob);
    }

    setProgress(100);
    setStatus('Done — device is rebooting.');
  } catch (e) {
    setStatus(`Failed: ${e.message ?? e}`);
  } finally {
    flashBtn.disabled = false;
    dfuBtn.disabled = !serialSupported;
  }
});
