// SUIT Kiosk Extension - Background Service Worker
// Communicates with native host com.suit.kiosk via Chrome Native Messaging API
// and manages tab operations (opening board config, closing tab to return)

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (!request || !request.action) {
    sendResponse({ status: "error", error: "No action specified" });
    return false;
  }

  // 1. Handle Tab Operations
  if (request.action === "close_tab") {
    if (sender && sender.tab && sender.tab.id) {
      chrome.tabs.remove(sender.tab.id, () => {
        sendResponse({ status: "ok" });
      });
    } else {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs && tabs[0] && tabs[0].id) {
          chrome.tabs.remove(tabs[0].id, () => sendResponse({ status: "ok" }));
        } else {
          sendResponse({ status: "error", error: "No active tab found" });
        }
      });
    }
    return true;
  }

  if (request.action === "open_tab") {
    const targetUrl = request.url || "http://localhost:3180/config";
    chrome.tabs.create({ url: targetUrl }, (tab) => {
      sendResponse({ status: "ok", tabId: tab ? tab.id : null });
    });
    return true;
  }

  // 2. Handle System Operations via Native Messaging
  try {
    chrome.runtime.sendNativeMessage(
      "com.suit.kiosk",
      request,
      (response) => {
        if (chrome.runtime.lastError) {
          console.error("SUIT Kiosk Native Message error:", chrome.runtime.lastError.message);
          sendResponse({
            status: "error",
            error: chrome.runtime.lastError.message
          });
        } else {
          sendResponse(response || { status: "ok" });
        }
      }
    );
  } catch (err) {
    console.error("Failed sending native message:", err);
    sendResponse({ status: "error", error: String(err) });
  }

  return true; // Required to keep sendResponse valid asynchronously
});
