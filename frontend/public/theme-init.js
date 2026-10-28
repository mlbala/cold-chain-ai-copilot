// Applies the saved theme before first paint (external file: the CSP forbids inline scripts).
(function () {
  try {
    var preference = window.localStorage.getItem("cct-theme");
    if (preference === "light" || preference === "dark") {
      document.documentElement.setAttribute("data-theme", preference);
    }
  } catch (error) {
    // Storage unavailable (private mode, blocked cookies): fall back to the system theme.
  }
})();
