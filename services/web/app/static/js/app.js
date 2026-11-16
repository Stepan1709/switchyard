// Charts: any <canvas data-chart="{...Chart.js config...}"> becomes a chart. The config is
// produced on the server; this file only draws it (and redraws after HTMX swaps content in).
(function () {
  function draw(root) {
    root.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      if (canvas.dataset.drawn || typeof Chart === "undefined") return;
      canvas.dataset.drawn = "1";
      var config = JSON.parse(canvas.dataset.chart);
      config.options = Object.assign({ responsive: true, maintainAspectRatio: false }, config.options || {});
      new Chart(canvas, config);
    });
  }
  document.addEventListener("DOMContentLoaded", function () { draw(document); });
  document.body && document.addEventListener("htmx:afterSettle", function (e) { draw(e.target); });
  document.addEventListener("htmx:load", function (e) { draw(e.target); });
})();
