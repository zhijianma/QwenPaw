// src/qwenpaw/pawapp/frontend_sdk.js
/**
 * PawApp Frontend SDK - JavaScript SDK for frontend apps to communicate with kernel
 */

class PawAppSDK {
  constructor() {
    this.appId = null;
    this.kernelOrigin = '*'; // Will be set during initialization
    this.pendingRequests = new Map();
    this.eventListeners = new Map();

    // Set up message listener
    window.addEventListener('message', this._handleMessage.bind(this));
  }

  /**
   * Initialize the SDK with app context
   */
  init(appId, kernelOrigin = '*') {
    this.appId = appId;
    this.kernelOrigin = kernelOrigin;
  }

  /**
   * Send a message to the kernel
   */
  _sendMessage(type, scope, method, params = {}) {
    return new Promise((resolve, reject) => {
      const messageId = this._generateId();

      // Store the resolver for the response
      this.pendingRequests.set(messageId, { resolve, reject });

      // Create the message
      const message = {
        id: messageId,
        type: type,
        scope: scope,
        method: method,
        params: params,
        appId: this.appId,
        timestamp: Date.now()
      };

      // Send to parent window (kernel)
      window.parent.postMessage(message, this.kernelOrigin);

      // Set timeout to reject if no response
      setTimeout(() => {
        if (this.pendingRequests.has(messageId)) {
          const { reject } = this.pendingRequests.get(messageId);
          this.pendingRequests.delete(messageId);
          reject(new Error(`Request ${messageId} timed out`));
        }
      }, 30000); // 30 second timeout
    });
  }

  /**
   * Handle incoming messages from kernel
   */
  _handleMessage(event) {
    const { id, success, data, error, type } = event.data;

    // Handle responses to our requests
    if (type === 'response' && id) {
      if (this.pendingRequests.has(id)) {
        const { resolve, reject } = this.pendingRequests.get(id);
        this.pendingRequests.delete(id);

        if (success) {
          resolve(data);
        } else {
          reject(new Error(error || 'Unknown error'));
        }
      }
    }
    // Handle events from kernel
    else if (type === 'event' && event.data.eventName) {
      const listeners = this.eventListeners.get(event.data.eventName) || [];
      listeners.forEach(listener => {
        try {
          listener(event.data.payload);
        } catch (e) {
          console.error('Error in event listener:', e);
        }
      });
    }
  }

  /**
   * Generate a unique ID
   */
  _generateId() {
    return Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
  }

  /**
   * Subscribe to events
   */
  subscribe(event, callback) {
    if (!this.eventListeners.has(event)) {
      this.eventListeners.set(event, []);
    }
    this.eventListeners.get(event).push(callback);
  }

  /**
   * Unsubscribe from events
   */
  unsubscribe(event, callback) {
    if (this.eventListeners.has(event)) {
      const listeners = this.eventListeners.get(event);
      const index = listeners.indexOf(callback);
      if (index > -1) {
        listeners.splice(index, 1);
      }
    }
  }

  // paw.self.* APIs (App-private services)
  get self() {
    return {
      // Storage - App-private storage
      storage: {
        get: (key) => this._sendMessage('request', 'self', 'storage.get', { key }),
        set: (key, value) => this._sendMessage('request', 'self', 'storage.set', { key, value }),
        search: (query, options = {}) => this._sendMessage('request', 'self', 'storage.search', { query, ...options })
      },

      // Configuration - App-specific settings
      config: {
        get: () => this._sendMessage('request', 'self', 'config.get', {})
      },

      // Notifications - App-private notifications
      notify: {
        toast: (text) => this._sendMessage('request', 'self', 'notification.toast', { text })
      }
    };
  }

  // paw.host.* APIs (Host services with permission levels)
  get host() {
    return {
      // Chat services
      chat: {
        sendMessage: (message) => this._sendMessage('request', 'host', 'chat.send_message', { message }),
        getHistory: (options = {}) => this._sendMessage('request', 'host', 'chat.get_history', { options })
      },

      // Storage - Host-shared storage
      storage: {
        get: (key) => this._sendMessage('request', 'host', 'storage.get', { key }),
        set: (key, value) => this._sendMessage('request', 'host', 'storage.set', { key, value }),
        search: (query, options = {}) => this._sendMessage('request', 'host', 'storage.search', { query, ...options })
      },

      // Notifications
      notification: {
        sendCard: (options) => this._sendMessage('request', 'host', 'notification.send_card', { ...options }),
        toast: (text) => this._sendMessage('request', 'host', 'notification.toast', { text })
      },

      // Scheduling
      schedule: {
        createTask: (options) => this._sendMessage('request', 'host', 'schedule.create_task', { ...options })
      },

      // Skills
      skill: {
        invoke: (name, params = {}) => this._sendMessage('request', 'host', 'skill.invoke', { name, params })
      },

      // MCP (Model Context Protocol)
      mcp: {
        call: (server, tool, params = {}) => this._sendMessage('request', 'host', 'mcp.call', { server, tool, params })
      },

      // File operations
      file: {
        read: (path) => this._sendMessage('request', 'host', 'file.read', { path }),
        write: (path, data) => this._sendMessage('request', 'host', 'file.write', { path, data })
      },

      // User information
      user: {
        getInfo: () => this._sendMessage('request', 'host', 'user.info', {})
      },

      // App-specific operations
      app: {
        settings: {
          get: () => this._sendMessage('request', 'host', 'app.settings.get', {}),
          update: (settings) => this._sendMessage('request', 'host', 'app.settings.update', { settings })
        }
      }
    };
  }
}

// Create global instance
const paw = new PawAppSDK();

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { paw, PawAppSDK };
}

// Make available globally
window.paw = paw;

console.log('PawApp Frontend SDK loaded');