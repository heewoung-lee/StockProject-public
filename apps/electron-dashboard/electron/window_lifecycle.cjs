function shouldHideMainWindowOnClose({
  platform,
  isQuitting,
  trayAvailable,
}) {
  return platform === "win32" && !isQuitting && Boolean(trayAvailable);
}

function createDesktopQuitLifecycle({
  cleanup,
  gracefulQuit,
  forceExit,
  scheduleForceExit = setTimeout,
  cancelForceExit = clearTimeout,
  forceExitDelayMs = 1500,
}) {
  let quitRequested = false;
  let forceExitTimer = null;

  return {
    requestQuit() {
      if (quitRequested) {
        return false;
      }
      quitRequested = true;
      cleanup();
      forceExitTimer = scheduleForceExit(() => forceExit(0), forceExitDelayMs);
      if (forceExitTimer && typeof forceExitTimer.unref === "function") {
        forceExitTimer.unref();
      }
      gracefulQuit();
      return true;
    },
    completeQuit() {
      if (forceExitTimer !== null) {
        cancelForceExit(forceExitTimer);
        forceExitTimer = null;
      }
    },
    isQuitRequested() {
      return quitRequested;
    },
  };
}

module.exports = {
  createDesktopQuitLifecycle,
  shouldHideMainWindowOnClose,
};
