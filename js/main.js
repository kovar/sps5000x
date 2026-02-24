/**
 * main.js — Entry point. Wires all modules together for SPS5000X power supply.
 */
import { ConnectionManager } from './connection.js';
import { DualChartManager } from './chart-manager.js';
import { Recorder } from './recorder.js';
import { StatsTracker } from './stats.js';
import { ScpiQueue, SCPI, requestReading } from './protocol.js';
import {
  setConnectionState, setMeasurementState, setRecordingState,
  updateReadout, updateStats,
  appendLog, showToast,
} from './ui.js';

// ── Instances ──────────────────────────────────────────────
const conn = new ConnectionManager();
let charts;
const recorder = new Recorder();
const statsTrackers = {
  ch1v: new StatsTracker(),
  ch1i: new StatsTracker(),
  ch2v: new StatsTracker(),
  ch2i: new StatsTracker(),
  ch3v: new StatsTracker(),
  ch3i: new StatsTracker(),
};
let queue = null;
let pollTimer = null;
let pollBusy = false;
let demoInterval = null;
let demoState = null;

// ── DOM Ready ──────────────────────────────────────────────
window._sps5000xModulesLoaded = true;

document.addEventListener('DOMContentLoaded', () => {
  wireConnection();
  wireToolbar();
  wireControls();

  setConnectionState(false);
  setMeasurementState(false);
  setRecordingState(false);

  try {
    charts = new DualChartManager(
      document.getElementById('voltageCanvas'),
      document.getElementById('currentCanvas'),
    );
    wireChart();
  } catch (err) {
    appendLog('Chart init failed: ' + err.message);
  }
});

// ── Connection Events ──────────────────────────────────────
function wireConnection() {
  conn.addEventListener('connected', () => {
    setConnectionState(true);
    appendLog('Connected');
    showToast('Connected to SPS5000X', 'success');

    queue = new ScpiQueue((cmd) => conn.send(cmd));

    queue.query(SCPI.IDN).then(response => {
      appendLog('Device: ' + response.trim());
      showToast(response.trim(), 'info');
    }).catch(() => {});
  });

  conn.addEventListener('disconnected', () => {
    stopPolling();
    if (queue) { queue.clear(); queue = null; }
    setConnectionState(false);
    appendLog('Disconnected');
    showToast('Disconnected', 'info');
  });

  conn.addEventListener('line', (e) => {
    if (queue) queue.feedLine(e.detail.line);
  });

  conn.addEventListener('log', (e) => appendLog(e.detail.message));
  conn.addEventListener('error', (e) => {
    appendLog('ERROR: ' + e.detail.message);
    showToast(e.detail.message, 'error', 6000);
  });
}

// ── Toolbar Buttons ────────────────────────────────────────
function wireToolbar() {
  document.getElementById('connectWs')?.addEventListener('click', async () => {
    const url = document.getElementById('wsUrl')?.value || undefined;
    try { await conn.connectWebSocket(url); } catch (_) {}
  });

  document.getElementById('disconnect')?.addEventListener('click', () => conn.disconnect());

  // Measurement
  document.getElementById('startMeasure')?.addEventListener('click', startPolling);
  document.getElementById('stopMeasure')?.addEventListener('click', stopPolling);

  // Recording
  document.getElementById('startRecord')?.addEventListener('click', () => {
    recorder.start();
    setRecordingState(true);
    appendLog('Recording started');
    showToast('Recording started', 'info');
  });

  document.getElementById('stopRecord')?.addEventListener('click', () => {
    recorder.stop();
    setRecordingState(false);
    if (recorder.download()) {
      const msg = 'Recording saved (' + recorder.count + ' readings)';
      appendLog(msg);
      showToast(msg, 'success');
    } else {
      appendLog('No data recorded');
      showToast('No data recorded', 'error');
    }
  });

  // Demo
  document.getElementById('demo')?.addEventListener('click', toggleDemo);
}

// ── Control Panel (setpoints + output toggle) ──────────────
function wireControls() {
  for (const ch of [1, 2, 3]) {
    document.getElementById(`setCh${ch}Voltage`)?.addEventListener('click', () => {
      const val = parseFloat(document.getElementById(`ch${ch}VoltageInput`)?.value);
      if (!isNaN(val) && queue) {
        queue.send(SCPI.setVoltage(ch, val));
        appendLog(`Set CH${ch} voltage: ${val.toFixed(3)} V`);
      }
    });

    document.getElementById(`setCh${ch}Current`)?.addEventListener('click', () => {
      const val = parseFloat(document.getElementById(`ch${ch}CurrentInput`)?.value);
      if (!isNaN(val) && queue) {
        queue.send(SCPI.setCurrent(ch, val));
        appendLog(`Set CH${ch} current: ${val.toFixed(3)} A`);
      }
    });
  }

  document.getElementById('outputOn')?.addEventListener('click', () => {
    if (queue) {
      queue.send(SCPI.setOutput(true));
      appendLog('Output ON');
    }
  });

  document.getElementById('outputOff')?.addEventListener('click', () => {
    if (queue) {
      queue.send(SCPI.setOutput(false));
      appendLog('Output OFF');
    }
  });
}

// ── Polling ────────────────────────────────────────────────
function startPolling() {
  if (pollTimer || !queue) return;
  const rate = parseInt(document.getElementById('samplingRate')?.value) || 1000;

  setMeasurementState(true);
  appendLog(`Polling started (every ${rate} ms)`);
  showToast(`Polling every ${rate} ms`, 'info');

  doPoll();
  pollTimer = setInterval(doPoll, rate);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  setMeasurementState(false);
  appendLog('Polling stopped');
}

async function doPoll() {
  if (!queue || pollBusy) return;
  pollBusy = true;
  try {
    const reading = await requestReading(queue);
    handleReading(reading);
  } catch (err) {
    appendLog('Poll error: ' + err.message);
    queue?.clear(); // flush stale pending entries to prevent queue desync
  } finally {
    pollBusy = false;
  }
}

function handleReading(reading) {
  updateReadout(reading);
  if (charts) charts.addReading(reading);

  statsTrackers.ch1v.addValue(reading.ch1.voltage);
  statsTrackers.ch1i.addValue(reading.ch1.current);
  statsTrackers.ch2v.addValue(reading.ch2.voltage);
  statsTrackers.ch2i.addValue(reading.ch2.current);
  statsTrackers.ch3v.addValue(reading.ch3.voltage);
  statsTrackers.ch3i.addValue(reading.ch3.current);
  updateStats({
    ch1v: statsTrackers.ch1v.getStats(),
    ch1i: statsTrackers.ch1i.getStats(),
    ch2v: statsTrackers.ch2v.getStats(),
    ch2i: statsTrackers.ch2i.getStats(),
    ch3v: statsTrackers.ch3v.getStats(),
    ch3i: statsTrackers.ch3i.getStats(),
  });

  recorder.addReading(reading);
}

// ── Chart Controls ─────────────────────────────────────────
function wireChart() {
  document.getElementById('timeRange')?.addEventListener('change', (e) => {
    charts.setTimeWindow(parseInt(e.target.value));
  });

  document.getElementById('clearChart')?.addEventListener('click', () => {
    charts.clear();
    for (const s of Object.values(statsTrackers)) s.reset();
    updateStats({
      ch1v: statsTrackers.ch1v.getStats(),
      ch1i: statsTrackers.ch1i.getStats(),
      ch2v: statsTrackers.ch2v.getStats(),
      ch2i: statsTrackers.ch2i.getStats(),
      ch3v: statsTrackers.ch3v.getStats(),
      ch3i: statsTrackers.ch3i.getStats(),
    });
  });
}

// ── Demo Mode ──────────────────────────────────────────────
function toggleDemo() {
  const btn = document.getElementById('demo');
  if (demoInterval) {
    stopDemo();
  } else {
    startDemo();
    if (btn) { btn.textContent = 'Stop Demo'; btn.classList.add('active'); }
  }
}

function startDemo() {
  demoState = {
    ch1: { voltage: 5.0, current: 0.5 },
    ch2: { voltage: 12.0, current: 1.0 },
    ch3: { voltage: 3.3, current: 0.3 },
    step: 0,
  };
  const rate = parseInt(document.getElementById('samplingRate')?.value) || 1000;

  setConnectionState(true);
  showToast('Demo mode \u2014 generating fake 3-channel PSU data', 'info');
  appendLog('Demo started');

  demoInterval = setInterval(() => {
    demoState.step++;

    const drift1v = 0.1 * Math.sin(demoState.step / 50 * Math.PI * 2);
    const drift2v = 0.15 * Math.sin(demoState.step / 40 * Math.PI * 2);
    const drift3v = 0.05 * Math.sin(demoState.step / 60 * Math.PI * 2);
    const noise = () => ((Math.random() + Math.random() + Math.random()) / 3 - 0.5) * 0.02;

    const reading = {
      ch1: {
        voltage: Math.round((demoState.ch1.voltage + drift1v + noise()) * 1000) / 1000,
        current: Math.round((demoState.ch1.current + noise() * 5) * 1000) / 1000,
        mode: demoState.step % 60 > 45 ? 'CC' : 'CV',
      },
      ch2: {
        voltage: Math.round((demoState.ch2.voltage + drift2v + noise()) * 1000) / 1000,
        current: Math.round((demoState.ch2.current + noise() * 5) * 1000) / 1000,
        mode: 'CV',
      },
      ch3: {
        voltage: Math.round((demoState.ch3.voltage + drift3v + noise()) * 1000) / 1000,
        current: Math.round((demoState.ch3.current + noise() * 3) * 1000) / 1000,
        mode: 'CV',
      },
    };

    handleReading(reading);

    appendLog(
      `CH1: ${reading.ch1.voltage.toFixed(3)}V ${reading.ch1.current.toFixed(3)}A [${reading.ch1.mode}] | ` +
      `CH2: ${reading.ch2.voltage.toFixed(3)}V ${reading.ch2.current.toFixed(3)}A | ` +
      `CH3: ${reading.ch3.voltage.toFixed(3)}V ${reading.ch3.current.toFixed(3)}A`
    );
  }, rate);
}

function stopDemo() {
  if (demoInterval) {
    clearInterval(demoInterval);
    demoInterval = null;
    demoState = null;
  }
  stopPolling();
  setConnectionState(false);
  const btn = document.getElementById('demo');
  if (btn) { btn.textContent = 'Demo'; btn.classList.remove('active'); }
  appendLog('Demo stopped');
  showToast('Demo stopped', 'info');
}
