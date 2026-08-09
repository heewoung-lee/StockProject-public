const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("stockbotBridge", {
  loadState: () => ipcRenderer.invoke("stockbot:load-state"),
  loadProfitReport: (query) => ipcRenderer.invoke("stockbot:load-profit-report", query),
  runAction: (action, payload = {}) => ipcRenderer.invoke("stockbot:run-action", action, payload),
});
