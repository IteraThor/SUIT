// SUIT Kiosk Controls - Content Script
// Dual Mode:
// 1. play.autodarts.com: Native header trigger button + sleek anchored dropdown popover
// 2. localhost:3180: Native Chakra UI dark "← Back to Game" return button

(function () {
  if (window.__suitKioskInitialized) return;
  window.__suitKioskInitialized = true;

  const isConfigHost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
  const isConfigPort = window.location.port === "3180" || window.location.port === "3182";

  // =========================================================================
  // SVGs matching Autodarts and Chakra design language
  // =========================================================================
  const SVG_POWER = `<svg viewBox="0 0 512 512" fill="currentColor"><path d="M288 32c0-17.7-14.3-32-32-32s-32 14.3-32 32V256c0 17.7 14.3 32 32 32s32-14.3 32-32V32zM143.5 120.6c13.6-11.3 15.4-31.5 4.1-45.1s-31.5-15.4-45.1-4.1C49.7 115.4 16 181.8 16 256c0 132.5 107.5 240 240 240s240-107.5 240-240c0-74.2-33.8-140.6-86.5-184.6c-13.6-11.3-33.8-9.5-45.1 4.1s-9.5 33.8 4.1 45.1c38.9 32.3 63.5 80.8 63.5 135.4c0 97.2-78.8 176-176 176s-176-78.8-176-176c0-54.6 24.6-103.1 63.5-135.4z"/></svg>`;
  const SVG_REBOOT = `<svg viewBox="0 0 512 512" fill="currentColor"><path d="M463.5 224H472c13.3 0 24-10.7 24-24V72c0-9.7-5.8-18.5-14.8-22.2s-19.3-1.7-26.2 5.2L413.4 96.6c-87.6-86.5-228.7-86.2-315.8 1c-87.5 87.5-87.5 229.3 0 316.8s229.3 87.5 316.8 0c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0c-62.5 62.5-163.8 62.5-226.3 0s-62.5-163.8 0-226.3c62.2-62.2 162.7-62.5 225.3-1L327 182.6c-6.9 6.9-8.9 17.2-5.2 26.2s12.5 14.8 22.2 14.8H463.5z"/></svg>`;
  const SVG_EXIT = `<svg viewBox="0 0 512 512" fill="currentColor"><path d="M377.9 105.9L500.7 228.7c7.2 7.2 11.3 17.1 11.3 27.3s-4.1 20.1-11.3 27.3L377.9 406.1c-6.4 6.4-15 9.9-24 9.9c-18.7 0-33.9-15.2-33.9-33.9l0-62.1-128 0c-17.7 0-32-14.3-32-32l0-64c0-17.7 14.3-32 32-32l128 0 0-62.1c0-18.7 15.2-33.9 33.9-33.9c9 0 17.6 3.6 24 9.9zM160 96L96 96c-17.7 0-32 14.3-32 32l0 256c0 17.7 14.3 32 32 32l64 0c17.7 0 32 14.3 32 32s-14.3 32-32 32l-64 0c-53 0-96-43-96-96L0 128C0 75 43 32 96 32l64 0c17.7 0 32 14.3 32 32s-14.3 32-32 32z"/></svg>`;
  const SVG_CONFIG = `<svg viewBox="0 0 512 512" fill="currentColor"><path d="M495.9 166.6c3.2 8.7 .5 18.4-6.4 24.6l-43.3 39.4c1.1 8.3 1.7 16.8 1.7 25.4s-.6 17.1-1.7 25.4l43.3 39.4c6.9 6.2 9.6 15.9 6.4 24.6c-4.4 11.9-9.7 23.3-15.8 34.3l-4.7 8.1c-6.6 11-14 21.4-22.1 31.2c-5.9 7.2-15.7 9.6-24.5 6.8l-55.7-17.7c-13.4 10.3-28.2 18.9-44 25.4l-12.5 57.1c-2 9.1-9 16.3-18.2 17.8c-13.8 2.3-28 3.5-42.5 3.5s-28.7-1.2-42.5-3.5c-9.2-1.5-16.2-8.7-18.2-17.8l-12.5-57.1c-15.8-6.5-30.6-15.1-44-25.4L83.1 425.9c-8.8 2.8-18.6 .3-24.5-6.8c-8.1-9.8-15.5-20.2-22.1-31.2l-4.7-8.1c-6.1-11-11.4-22.4-15.8-34.3c-3.2-8.7-.5-18.4 6.4-24.6l43.3-39.4C64.6 273.1 64 264.6 64 256s.6-17.1 1.7-25.4L22.4 191.2c-6.9-6.2-9.6-15.9-6.4-24.6c4.4-11.9 9.7-23.3 15.8-34.3l4.7-8.1c6.6-11 14-21.4 22.1-31.2c5.9-7.2 15.7-9.6 24.5-6.8l55.7 17.7c13.4-10.3 28.2-18.9 44-25.4l12.5-57.1c2-9.1 9-16.3 18.2-17.8C227.3 1.2 241.5 0 256 0s28.7 1.2 42.5 3.5c9.2 1.5 16.2 8.7 18.2 17.8l12.5 57.1c15.8 6.5 30.6 15.1 44 25.4l55.7-17.7c8.8-2.8 18.6-.3 24.5 6.8c8.1 9.8 15.5 20.2 22.1 31.2l4.7 8.1c6.1 11 11.4 22.4 15.8 34.3zM256 336a80 80 0 1 0 0-160 80 80 0 1 0 0 160z"/></svg>`;
  const SVG_ARROW_LEFT = `<svg viewBox="0 0 448 512" fill="currentColor" width="14" height="14"><path d="M9.4 233.4c-12.5 12.5-12.5 32.8 0 45.3l160 160c12.5 12.5 32.8 12.5 45.3 0s12.5-32.8 0-45.3L109.2 288 416 288c17.7 0 32-14.3 32-32s-14.3-32-32-32l-306.7 0L214.6 118.6c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0l-160 160z"/></svg>`;
  const SVG_CHEVRON = `<svg viewBox="0 0 512 512" fill="currentColor" width="10" height="10"><path d="M233.4 406.6c12.5 12.5 32.8 12.5 45.3 0l192-192c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0L256 338.7 86.6 169.4c-12.5-12.5-32.8-12.5-45.3 0s-12.5 32.8 0 45.3l192 192z"/></svg>`;
  const SVG_LIGHTBULB = `<svg viewBox="0 0 384 512" fill="currentColor"><path d="M297.2 248.9C311.6 228.3 320 203.2 320 176c0-70.7-57.3-128-128-128S64 105.3 64 176c0 27.2 8.4 52.3 22.8 72.9 3.7 5.3 8.1 11.5 12.8 18.1 11 15.4 22.4 31.4 30.7 47.9 7.4 14.8 11.7 30.6 13.7 47.1h96c2-16.5 6.3-32.3 13.7-47.1 8.3-16.5 19.7-32.5 30.7-47.9 4.7-6.6 9.1-12.8 12.8-18.1zM192 0c97.2 0 176 78.8 176 176 0 38-12 73.3-32.6 102.3-4.2 5.9-8.4 11.9-12.7 17.9-9.8 13.8-19.1 26.8-24.8 38.3-4.5 9.1-7.5 18.4-9.3 27.5H94.4c-1.8-9.1-4.8-18.4-9.3-27.5-5.7-11.5-15-24.5-24.8-38.3-4.3-6-8.5-12-12.7-17.9C28 249.3 16 214 16 176 16 78.8 94.8 0 192 0zm-64 432c0-8.8 7.2-16 16-16h96c8.8 0 16 7.2 16 16v16c0 8.8-7.2 16-16 16h-96c-8.8 0-16-7.2-16-16v-16zm24 64h80c8.8 0 16 7.2 16 16s-7.2 16-16 16h-80c-8.8 0-16-7.2-16-16s7.2-16 16-16z"/></svg>`;

  // =========================================================================
  // MODE A: Autodarts Board Configuration Page (localhost:3180)
  // =========================================================================
  if (isConfigHost && isConfigPort) {
    function setupBoardConfigReturnButton() {
      if (document.getElementById("suit-config-return-btn")) return;

      const btn = document.createElement("button");
      btn.id = "suit-config-return-btn";
      btn.className = "suit-config-back-btn";
      btn.innerHTML = `
        ${SVG_ARROW_LEFT}
        <span>Back to Game</span>
      `;
      btn.title = "Close configuration and return to Autodarts";

      btn.addEventListener("click", () => {
        try {
          chrome.runtime.sendMessage({ action: "close_tab" }, (resp) => {
            if (chrome.runtime.lastError || (resp && resp.status === "error")) {
              window.close();
              window.location.href = "https://play.autodarts.com/";
            }
          });
        } catch (e) {
          window.close();
          window.location.href = "https://play.autodarts.com/";
        }
      });

      document.body.appendChild(btn);
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", setupBoardConfigReturnButton);
    } else {
      setupBoardConfigReturnButton();
    }

    const observer = new MutationObserver(() => {
      setupBoardConfigReturnButton();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    return; // Do not execute Autodarts main navbar popover code on config page
  }

  // =========================================================================
  // MODE B: Autodarts Play Web Interface (play.autodarts.com)
  // =========================================================================
  let triggerBtn = null;
  let popover = null;
  let toastTimeout = null;

  function showToast(message, duration = 3000) {
    let toast = document.getElementById("suit-kiosk-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "suit-kiosk-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("suit-toast-visible");

    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      toast.classList.remove("suit-toast-visible");
    }, duration);
  }

  // =========================================================================
  // Dartboard Light State & Controls
  // =========================================================================
  let lightState = { enabled: false, isOn: false, isBusy: false };

  function updateLightUI() {
    const sec = document.getElementById("suit-section-light");
    const icon = document.getElementById("suit-light-icon");
    const desc = document.getElementById("suit-light-desc");
    const badge = document.getElementById("suit-light-badge");
    const item = document.getElementById("suit-item-light");
    if (!sec) return;

    if (!lightState.enabled) {
      sec.style.display = "none";
      return;
    }

    sec.style.display = "block";

    if (lightState.isBusy) {
      if (desc) desc.textContent = "Switching...";
      if (badge) {
        badge.textContent = "...";
        badge.className = "suit-light-badge suit-light-busy";
      }
      return;
    }

    if (lightState.isOn) {
      if (desc) desc.textContent = "Turned On";
      if (icon) icon.classList.add("suit-light-active");
      if (item) item.classList.add("suit-item-light-on");
      if (badge) {
        badge.textContent = "ON";
        badge.className = "suit-light-badge suit-light-on";
      }
    } else {
      if (desc) desc.textContent = "Turned Off";
      if (icon) icon.classList.remove("suit-light-active");
      if (item) item.classList.remove("suit-item-light-on");
      if (badge) {
        badge.textContent = "OFF";
        badge.className = "suit-light-badge suit-light-off";
      }
    }
  }

  function queryLightStatus() {
    try {
      chrome.runtime.sendMessage({ action: "get_light_status" }, (resp) => {
        if (!chrome.runtime.lastError && resp && resp.status === "ok") {
          lightState.enabled = Boolean(resp.enabled);
          lightState.isOn = Boolean(resp.is_on);
          lightState.isBusy = false;
          updateLightUI();
        }
      });
    } catch (e) {
      console.debug("Could not query light status:", e);
    }
  }

  function toggleDartboardLight() {
    if (lightState.isBusy) return;
    lightState.isBusy = true;
    updateLightUI();

    try {
      chrome.runtime.sendMessage({ action: "toggle_light" }, (resp) => {
        lightState.isBusy = false;
        if (chrome.runtime.lastError || !resp || resp.status !== "ok") {
          const err = (chrome.runtime.lastError && chrome.runtime.lastError.message) || (resp && resp.error) || "Failed to toggle light";
          showToast(`Light Error: ${err}`, 3500);
          queryLightStatus();
        } else {
          lightState.isOn = Boolean(resp.is_on);
          updateLightUI();
          showToast(resp.is_on ? "Dartboard light turned on" : "Dartboard light turned off", 2000);
        }
      });
    } catch (e) {
      lightState.isBusy = false;
      showToast(`Light Error: ${String(e)}`, 3500);
      updateLightUI();
    }
  }

  function getOrCreateTriggerButton() {
    if (triggerBtn && document.contains(triggerBtn)) return triggerBtn;

    triggerBtn = document.getElementById("suit-kiosk-trigger-btn");
    if (!triggerBtn) {
      triggerBtn = document.createElement("button");
      triggerBtn.id = "suit-kiosk-trigger-btn";
      triggerBtn.setAttribute("aria-label", "System Control");
      triggerBtn.title = "System Control";
      triggerBtn.innerHTML = SVG_POWER;
      triggerBtn.addEventListener("click", togglePopover);
    }
    return triggerBtn;
  }

  function getOrCreatePopover() {
    if (popover && document.contains(popover)) return popover;

    popover = document.getElementById("suit-kiosk-popover");
    if (popover) return popover;

    popover = document.createElement("div");
    popover.id = "suit-kiosk-popover";
    popover.innerHTML = `
      <!-- AUTODARTS SECTION -->
      <div class="suit-popover-section-title">Autodarts</div>
      <button class="suit-popover-item" id="suit-item-board-config">
        <div class="suit-item-icon suit-icon-config">${SVG_CONFIG}</div>
        <div class="suit-item-text">
          <span class="suit-item-label">Board Configuration</span>
          <span class="suit-item-desc">Calibration & settings</span>
        </div>
      </button>

      <!-- ACCESSORIES / DARTBOARD LIGHT (Initially hidden until confirmed enabled) -->
      <div id="suit-section-light" style="display: none;">
        <div class="suit-popover-divider"></div>
        <div class="suit-popover-section-title">Accessories</div>
        <button class="suit-popover-item" id="suit-item-light">
          <div class="suit-item-icon suit-icon-light" id="suit-light-icon">${SVG_LIGHTBULB}</div>
          <div class="suit-item-text">
            <span class="suit-item-label">Dartboard Light</span>
            <span class="suit-item-desc" id="suit-light-desc">Turned Off</span>
          </div>
          <div class="suit-light-badge suit-light-off" id="suit-light-badge">OFF</div>
        </button>
      </div>

      <!-- DIVIDER -->
      <div class="suit-popover-divider"></div>

      <!-- SYSTEM SECTION -->
      <div class="suit-popover-section-title">System</div>

      <!-- Exit Kiosk / Close Session -->
      <button class="suit-popover-item" id="suit-item-exit">
        <div class="suit-item-icon suit-icon-exit">${SVG_EXIT}</div>
        <div class="suit-item-text">
          <span class="suit-item-label" id="suit-exit-label">Exit Kiosk</span>
          <span class="suit-item-desc" id="suit-exit-desc">Return to desktop</span>
        </div>
      </button>

      <!-- Restart -->
      <button class="suit-popover-item" id="suit-item-reboot">
        <div class="suit-item-icon suit-icon-reboot">${SVG_REBOOT}</div>
        <div class="suit-item-text">
          <span class="suit-item-label">Restart</span>
          <span class="suit-item-desc">Reboot the PC</span>
        </div>
        <div class="suit-item-chevron">${SVG_CHEVRON}</div>
      </button>
      <div class="suit-inline-confirm" id="suit-confirm-reboot">
        <span class="suit-confirm-msg">Are you sure you want to reboot?</span>
        <div class="suit-confirm-btns">
          <button class="suit-confirm-btn-cancel" id="suit-cancel-reboot">Cancel</button>
          <button class="suit-confirm-btn-action action-reboot" id="suit-proceed-reboot">Restart</button>
        </div>
      </div>

      <!-- Power Off -->
      <button class="suit-popover-item" id="suit-item-poweroff">
        <div class="suit-item-icon suit-icon-poweroff">${SVG_POWER}</div>
        <div class="suit-item-text">
          <span class="suit-item-label">Power Off</span>
          <span class="suit-item-desc">Shut down the PC</span>
        </div>
        <div class="suit-item-chevron">${SVG_CHEVRON}</div>
      </button>
      <div class="suit-inline-confirm" id="suit-confirm-poweroff">
        <span class="suit-confirm-msg">Are you sure you want to shut down?</span>
        <div class="suit-confirm-btns">
          <button class="suit-confirm-btn-cancel" id="suit-cancel-poweroff">Cancel</button>
          <button class="suit-confirm-btn-action action-poweroff" id="suit-proceed-poweroff">Power Off</button>
        </div>
      </div>
    `;

    document.body.appendChild(popover);

    // Wire Item Events
    document.getElementById("suit-item-board-config").addEventListener("click", () => {
      closePopover();
      const openConfigUrl = (url) => {
        try {
          chrome.runtime.sendMessage({ action: "open_tab", url: url }, (resp) => {
            if (chrome.runtime.lastError || (resp && resp.status === "error")) {
              window.open(url, "_blank");
            }
          });
        } catch (e) {
          window.open(url, "_blank");
        }
      };

      // Try v2 port 3182 first, fallback to v1 3180
      fetch("http://localhost:3182/api/state", { method: "GET", mode: "no-cors", cache: "no-cache" })
        .then(() => openConfigUrl("http://localhost:3182/config"))
        .catch(() => openConfigUrl("http://localhost:3180/config"));
    });

    const lightBtn = document.getElementById("suit-item-light");
    if (lightBtn) {
      lightBtn.addEventListener("click", () => {
        toggleDartboardLight();
      });
    }

    document.getElementById("suit-item-exit").addEventListener("click", () => {
      closePopover();
      executeAction("exit_kiosk");
    });

    // Accordion: Restart
    const rebootBtn = document.getElementById("suit-item-reboot");
    const rebootConfirm = document.getElementById("suit-confirm-reboot");
    rebootBtn.addEventListener("click", () => {
      collapseInlineConfirm("suit-confirm-poweroff");
      toggleInlineConfirm(rebootBtn, rebootConfirm);
    });
    document.getElementById("suit-cancel-reboot").addEventListener("click", (e) => {
      e.stopPropagation();
      collapseInlineConfirm("suit-confirm-reboot");
    });
    document.getElementById("suit-proceed-reboot").addEventListener("click", (e) => {
      e.stopPropagation();
      closePopover();
      showToast("Restarting system...");
      executeAction("reboot");
    });

    // Accordion: Power Off
    const powerBtn = document.getElementById("suit-item-poweroff");
    const powerConfirm = document.getElementById("suit-confirm-poweroff");
    powerBtn.addEventListener("click", () => {
      collapseInlineConfirm("suit-confirm-reboot");
      toggleInlineConfirm(powerBtn, powerConfirm);
    });
    document.getElementById("suit-cancel-poweroff").addEventListener("click", (e) => {
      e.stopPropagation();
      collapseInlineConfirm("suit-confirm-poweroff");
    });
    document.getElementById("suit-proceed-poweroff").addEventListener("click", (e) => {
      e.stopPropagation();
      closePopover();
      showToast("Shutting down system...");
      executeAction("poweroff");
    });

    return popover;
  }

  function toggleInlineConfirm(btn, confirmEl) {
    const isOpen = confirmEl.classList.contains("suit-confirm-open");
    if (isOpen) {
      confirmEl.classList.remove("suit-confirm-open");
      btn.classList.remove("suit-item-expanded");
    } else {
      confirmEl.classList.add("suit-confirm-open");
      btn.classList.add("suit-item-expanded");
    }
  }

  function collapseInlineConfirm(id) {
    const confirmEl = document.getElementById(id);
    if (!confirmEl) return;
    confirmEl.classList.remove("suit-confirm-open");
    if (id === "suit-confirm-reboot") {
      document.getElementById("suit-item-reboot")?.classList.remove("suit-item-expanded");
    } else if (id === "suit-confirm-poweroff") {
      document.getElementById("suit-item-poweroff")?.classList.remove("suit-item-expanded");
    }
  }

  function positionPopover() {
    if (!popover || !triggerBtn) return;
    const rect = triggerBtn.getBoundingClientRect();
    const popoverWidth = 290;

    let top = rect.bottom + 8;
    let right = window.innerWidth - rect.right;

    if (right < 12) right = 12;
    if (top + 340 > window.innerHeight && rect.top > 340) {
      // If bottom edge overflow, open upwards
      top = rect.top - 8 - popover.offsetHeight;
    }

    popover.style.top = `${top}px`;
    popover.style.right = `${right}px`;
  }

  function openPopover() {
    const p = getOrCreatePopover();
    collapseInlineConfirm("suit-confirm-reboot");
    collapseInlineConfirm("suit-confirm-poweroff");

    // Adaptive copy
    const isFullscreen = Boolean(document.fullscreenElement || window.innerHeight >= screen.height - 20);
    const exitLabel = document.getElementById("suit-exit-label");
    const exitDesc = document.getElementById("suit-exit-desc");
    if (exitLabel && exitDesc) {
      if (isFullscreen) {
        exitLabel.textContent = "Exit Kiosk";
        exitDesc.textContent = "Return to desktop";
      } else {
        exitLabel.textContent = "Close Session";
        exitDesc.textContent = "Close browser window";
      }
    }

    positionPopover();
    p.classList.add("suit-popover-open");
    queryLightStatus();
  }

  function closePopover() {
    if (!popover) return;
    popover.classList.remove("suit-popover-open");
    collapseInlineConfirm("suit-confirm-reboot");
    collapseInlineConfirm("suit-confirm-poweroff");
  }

  function togglePopover(e) {
    if (e) e.stopPropagation();
    const p = getOrCreatePopover();
    if (p.classList.contains("suit-popover-open")) {
      closePopover();
    } else {
      openPopover();
    }
  }

  function executeAction(action) {
    try {
      chrome.runtime.sendMessage({ action: action }, (response) => {
        if (chrome.runtime.lastError || (response && response.status === "error")) {
          const err = (chrome.runtime.lastError && chrome.runtime.lastError.message) || (response && response.error) || "Action failed";
          showToast(`Error: ${err}`, 4500);
        }
      });
    } catch (e) {
      showToast(`Error: ${String(e)}`, 4500);
    }
  }

  // Global dismiss on click outside or Escape key
  document.addEventListener("click", (e) => {
    if (!popover || !popover.classList.contains("suit-popover-open")) return;
    if (popover.contains(e.target) || (triggerBtn && triggerBtn.contains(e.target))) {
      return;
    }
    closePopover();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closePopover();
    }
  });

  window.addEventListener("resize", () => {
    if (popover && popover.classList.contains("suit-popover-open")) {
      positionPopover();
    }
  });

  // Dock button into Autodarts header
  function attachButtonToHeader() {
    const btn = getOrCreateTriggerButton();
    getOrCreatePopover();

    const headers = document.querySelectorAll("header");
    let targetContainer = null;

    for (const header of headers) {
      const innerButtons = Array.from(header.querySelectorAll("button"));
      if (innerButtons.length > 0) {
        const nativeButtons = innerButtons.filter((b) => b.id !== "suit-kiosk-trigger-btn");
        if (nativeButtons.length > 0) {
          const lastBtn = nativeButtons[nativeButtons.length - 1];
          targetContainer = lastBtn.parentElement;
          break;
        }
      }

      if (header.lastElementChild && header.lastElementChild !== btn) {
        targetContainer = header.lastElementChild;
        break;
      }
    }

    if (targetContainer) {
      if (btn.parentElement !== targetContainer) {
        btn.classList.remove("suit-fallback-floating");
        btn.classList.add("suit-header-docked");
        targetContainer.appendChild(btn);
      }
      return;
    }

    // Fallback if header is not yet rendered
    if (!document.body.contains(btn) || btn.parentElement !== document.body) {
      btn.classList.remove("suit-header-docked");
      btn.classList.add("suit-fallback-floating");
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachButtonToHeader);
  } else {
    attachButtonToHeader();
  }

  let observerTimer = null;
  const observer = new MutationObserver(() => {
    if (observerTimer) return;
    observerTimer = setTimeout(() => {
      observerTimer = null;
      attachButtonToHeader();
    }, 150);
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
