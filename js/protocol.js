/**
 * protocol.js — SCPI command constants, ScpiQueue, and response parsers
 * for the Siglent SPS5000X power supply.
 */

/** SCPI command string constants and builder functions. */
export const SCPI = {
  // Queries
  IDN: '*IDN?',
  MEAS_VOLT_CH1: 'MEASure:VOLTage? CH1',
  MEAS_CURR_CH1: 'MEASure:CURRent? CH1',
  MEAS_MODE_CH1: 'MEASure:RUN:MODE? CH1',
  MEAS_VOLT_CH2: 'MEASure:VOLTage? CH2',
  MEAS_CURR_CH2: 'MEASure:CURRent? CH2',
  MEAS_MODE_CH2: 'MEASure:RUN:MODE? CH2',
  MEAS_VOLT_CH3: 'MEASure:VOLTage? CH3',
  MEAS_CURR_CH3: 'MEASure:CURRent? CH3',
  MEAS_MODE_CH3: 'MEASure:RUN:MODE? CH3',

  // Setters (return builder functions)
  setVoltage(ch, value) {
    return `:SOURce:VOLTage:SET CH${ch},${value.toFixed(3)}`;
  },
  setCurrent(ch, value) {
    return `:SOURce:CURRent:SET CH${ch},${value.toFixed(3)}`;
  },
  setOutput(on) {
    return `OUTPut ${on ? 'ON' : 'OFF'}`;
  },
};

/**
 * ScpiQueue — serializes SCPI queries over a single channel.
 *
 * Sends one command at a time, waits for a response line, then sends the next.
 * Commands without '?' (setters) are sent without waiting for a response.
 */
export class ScpiQueue {
  #sendFn;       // function(cmdString) — sends raw text to transport
  #pending = []; // FIFO of { resolve, reject, timeoutId }
  #timeout;

  /**
   * @param {Function} sendFn — called with the raw command string to transmit
   * @param {number} timeout — ms to wait for a response before rejecting
   */
  constructor(sendFn, timeout = 2000) {
    this.#sendFn = sendFn;
    this.#timeout = timeout;
  }

  /**
   * Enqueue a SCPI query (must contain '?'). Returns a Promise that resolves
   * with the response string.
   */
  query(cmd) {
    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        const idx = this.#pending.findIndex(p => p.timeoutId === timeoutId);
        if (idx !== -1) this.#pending.splice(idx, 1);
        reject(new Error(`SCPI timeout: ${cmd}`));
      }, this.#timeout);

      this.#pending.push({ resolve, reject, timeoutId });
      this.#sendFn(cmd);
    });
  }

  /**
   * Send a command that expects no response (setter).
   */
  send(cmd) {
    this.#sendFn(cmd);
  }

  /**
   * Feed a received line from the transport. Resolves the oldest pending query.
   */
  feedLine(line) {
    if (this.#pending.length === 0) return false;
    const entry = this.#pending.shift();
    clearTimeout(entry.timeoutId);
    entry.resolve(line);
    return true;
  }

  /** Number of pending queries. */
  get pendingCount() {
    return this.#pending.length;
  }

  /** Clear all pending queries (e.g. on disconnect). */
  clear() {
    for (const entry of this.#pending) {
      clearTimeout(entry.timeoutId);
      entry.reject(new Error('Queue cleared'));
    }
    this.#pending = [];
  }
}

/**
 * Parse a numeric SCPI response like "2.991442\n" → 2.991442
 */
export function parseNumeric(response) {
  const trimmed = response.trim();
  const value = parseFloat(trimmed);
  return isNaN(value) ? null : value;
}

/**
 * Request a full reading from all three channels (9 sequential queries).
 * Returns voltage, current, and CV/CC mode for each channel.
 * @param {ScpiQueue} queue
 * @returns {Promise<{ch1: {voltage, current, mode}, ch2: {...}, ch3: {...}}>}
 */
export async function requestReading(queue) {
  const [v1, i1, m1, v2, i2, m2, v3, i3, m3] = await Promise.all([
    queue.query(SCPI.MEAS_VOLT_CH1),
    queue.query(SCPI.MEAS_CURR_CH1),
    queue.query(SCPI.MEAS_MODE_CH1),
    queue.query(SCPI.MEAS_VOLT_CH2),
    queue.query(SCPI.MEAS_CURR_CH2),
    queue.query(SCPI.MEAS_MODE_CH2),
    queue.query(SCPI.MEAS_VOLT_CH3),
    queue.query(SCPI.MEAS_CURR_CH3),
    queue.query(SCPI.MEAS_MODE_CH3),
  ]);

  return {
    ch1: { voltage: parseNumeric(v1), current: parseNumeric(i1), mode: m1.trim() },
    ch2: { voltage: parseNumeric(v2), current: parseNumeric(i2), mode: m2.trim() },
    ch3: { voltage: parseNumeric(v3), current: parseNumeric(i3), mode: m3.trim() },
  };
}
