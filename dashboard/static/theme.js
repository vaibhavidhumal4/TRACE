// Runs before first paint. Dark is the default; the viewer's choice is remembered when storage is available.
(function () {
  var theme = "dark";
  try {
    var saved = window.localStorage.getItem("trace-theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) {
    /* private window or blocked storage: keep the default */
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
