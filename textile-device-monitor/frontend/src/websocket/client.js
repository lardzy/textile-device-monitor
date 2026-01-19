class WebSocketClient {
  constructor() {
    this.ws = null;
    this.handlers = {};
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 5;
    this.reconnectDelay = 3000;
  }

  connect(url) {
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
    };

    this.ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        const handler = this.handlers[message.type];
        if (handler) {
          handler(message.data);
        }
      } catch (error) {
        console.error('WebSocket message error:', error, event.data);
      }
    };

    this.ws.onclose = () => {
      console.log('WebSocket disconnected');
      this.attemptReconnect(url);
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
  }

  attemptReconnect(url) {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++;
      console.log(`Attempting to reconnect... (${this.reconnectAttempts}/${this.maxReconnectAttempts})`);
      setTimeout(() => this.connect(url), this.reconnectDelay);
    }
  }

  on(type, handler) {
    this.handlers[type] = handler;
  }

  off(type) {
    delete this.handlers[type];
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
    }
  }
}

const wsClient = new WebSocketClient();

export default wsClient;
