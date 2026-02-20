/**
 * Recorder — records timestamped 3-channel power supply readings and exports as CSV.
 */
export class Recorder {
  #data = [];
  #recording = false;

  get isRecording() {
    return this.#recording;
  }

  get count() {
    return this.#data.length;
  }

  start() {
    this.#data = [];
    this.#recording = true;
  }

  stop() {
    this.#recording = false;
  }

  /**
   * @param {{ch1: {voltage, current}, ch2: {voltage, current}, ch3: {voltage, current}}} reading
   */
  addReading(reading) {
    if (!this.#recording) return;
    this.#data.push({
      timestamp: new Date().toISOString(),
      ch1v: reading.ch1.voltage,
      ch1i: reading.ch1.current,
      ch2v: reading.ch2.voltage,
      ch2i: reading.ch2.current,
      ch3v: reading.ch3.voltage,
      ch3i: reading.ch3.current,
    });
  }

  download() {
    if (this.#data.length === 0) return false;
    const header = 'Timestamp,CH1_V,CH1_I,CH2_V,CH2_I,CH3_V,CH3_I\n';
    const rows = this.#data.map(r =>
      `${r.timestamp},${r.ch1v},${r.ch1i},${r.ch2v},${r.ch2i},${r.ch3v},${r.ch3i}`
    ).join('\n');
    const csv = header + rows + '\n';
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const ts = new Date().toISOString().replace(/[:\-]/g, '').replace(/\..+/, '');
    a.download = `sps5000x_recording_${ts}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    return true;
  }
}
