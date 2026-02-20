/**
 * ConnectionManager — WebSocket-only transport manager.
 * Re-emits events through a single interface.
 *
 * Events: 'connected', 'disconnected', 'line', 'log', 'error'
 */
import { WebSocketTransport } from './websocket.js';

export class ConnectionManager extends EventTarget {
  #transport = null;
  #connected = false;

  get isConnected() {
    return this.#connected;
  }

  async connectWebSocket(url) {
    if (this.#connected) await this.disconnect();
    this.#transport = new WebSocketTransport();
    this.#wire();
    await this.#transport.connect(url);
  }

  async disconnect() {
    if (this.#transport) {
      await this.#transport.disconnect();
      this.#transport = null;
    }
  }

  async send(cmd) {
    if (this.#transport) {
      await this.#transport.send(cmd);
    }
  }

  #wire() {
    const events = ['connected', 'disconnected', 'line', 'log', 'error'];
    for (const name of events) {
      this.#transport.addEventListener(name, (e) => {
        if (name === 'connected') this.#connected = true;
        if (name === 'disconnected') this.#connected = false;
        this.dispatchEvent(new CustomEvent(name, { detail: e.detail }));
      });
    }
  }
}
