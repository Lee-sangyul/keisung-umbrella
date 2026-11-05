/* App installation is optional; core kiosk functions work without it. */
(() => {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" })
        .catch((err) => console.warn("PWA service worker registration failed", err));
    });
  }
  if (window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone) return;
  let promptEvent = null;
  let installButton = null;
  function removeButton() {
    if (installButton) installButton.remove();
    installButton = null;
  }
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    promptEvent = event;
    if (installButton) return;
    installButton = document.createElement("button");
    installButton.type = "button";
    installButton.textContent = "앱 설치";
    installButton.setAttribute("aria-label", "계성 우산 웹앱 설치");
    Object.assign(installButton.style, {
      position: "fixed", right: "14px", bottom: "14px", zIndex: "900",
      minHeight: "45px", padding: "10px 18px", border: "2px solid #fff",
      borderRadius: "999px", boxShadow: "0 4px 16px #0003",
      background: "#ff6541", color: "#fff", fontWeight: "800",
      fontSize: "14px", cursor: "pointer"
    });
    installButton.addEventListener("click", async () => {
      if (!promptEvent) return;
      const eventToPrompt = promptEvent;
      promptEvent = null;
      removeButton();
      try { await eventToPrompt.prompt(); } catch (err) {
        console.warn("PWA install prompt failed", err);
      }
    });
    document.body.appendChild(installButton);
  });
  window.addEventListener("appinstalled", () => {
    promptEvent = null;
    removeButton();
  });
})();
