// The matrix "models x task types": sorting, filters and colours happen in the browser, on the
// data the server embedded in #matrix-data. A click loads the examples of a cell from the server.
document.addEventListener("alpine:init", function () {
  var METRICS = {
    pass_rate: { label: "Pass rate", get: function (c) { return c.pass_rate; }, higher: true },
    quality: { label: "Quality", get: function (c) { return c.quality_mean; }, higher: true },
    cost: { label: "Cost / request", get: function (c) { return c.cost_per_request_usd === null ? null : Number(c.cost_per_request_usd); }, higher: false },
    p95: { label: "Latency p95", get: function (c) { return c.latency_p95_ms; }, higher: false },
    error: { label: "Error rate", get: function (c) { return c.error_rate; }, higher: false },
  };

  function pct(v) { return v === null || v === undefined ? "–" : (v * 100).toFixed(0) + "%"; }
  function money(v) {
    if (v === null || v === undefined) return "–";
    var n = Number(v);
    return "$" + (n >= 0.01 ? n.toFixed(4) : n.toFixed(6));
  }
  function ms(v) { return v === null || v === undefined ? "–" : (v >= 10000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms"); }

  Alpine.data("matrix", function () {
    return {
      data: JSON.parse(document.getElementById("matrix-data").textContent),
      search: "",
      costType: "",
      onlyEligible: false,
      onlyMeasured: true,
      metric: "pass_rate",
      sortTask: "",
      desc: true,
      panel: false,
      metrics: METRICS,

      get rows() {
        var self = this;
        var q = this.search.trim().toLowerCase();
        var rows = this.data.models.filter(function (m) {
          if (q && m.name.toLowerCase().indexOf(q) === -1) return false;
          if (self.costType && m.cost_type !== self.costType) return false;
          if (self.onlyMeasured && !m.measured && !m.is_reference) return false;
          if (self.onlyEligible) {
            return Object.values(m.cells).some(function (c) { return c && c.eligible; });
          }
          return true;
        });
        if (this.sortTask) {
          var metric = METRICS[this.metric];
          var dir = (this.desc ? 1 : -1) * (metric.higher ? 1 : -1) * -1;
          var task = this.sortTask;
          rows = rows.slice().sort(function (a, b) {
            var x = a.cells[task] ? metric.get(a.cells[task]) : null;
            var y = b.cells[task] ? metric.get(b.cells[task]) : null;
            if (x === null || x === undefined) return 1;   // rows without a number go last
            if (y === null || y === undefined) return -1;
            return (x - y) * dir * -1;
          });
        } else {
          rows = rows.slice().sort(function (a, b) { return a.tier - b.tier || a.name.localeCompare(b.name); });
        }
        return rows;
      },

      value: function (c) {
        if (!c) return "–";
        switch (this.metric) {
          case "pass_rate": return pct(c.pass_rate);
          case "quality": return c.quality_mean === null ? "–" : c.quality_mean.toFixed(2);
          case "cost": return money(c.cost_per_request_usd);
          case "p95": return ms(c.latency_p95_ms);
          default: return pct(c.error_rate);
        }
      },
      detail: function (c) {
        if (!c) return "";
        return money(c.cost_per_request_usd) + " · p95 " + ms(c.latency_p95_ms) + " · " + c.n_evaluated + " rated";
      },
      cellClass: function (c) {
        if (!c) return "border-dashed border-slate-200 bg-slate-50 text-slate-400";
        if (c.insufficient_data) return "border-amber-300 bg-amber-50 text-amber-900";
        return c.eligible
          ? "border-emerald-300 bg-emerald-50 text-emerald-900"
          : "border-red-300 bg-red-50 text-red-900";
      },
      title: function (c) {
        if (!c) return "No numbers for this model and task type";
        var lines = [];
        if (c.insufficient_data) lines.push("Too few examples for a conclusion");
        if (!c.eligible && c.reasons.length) lines.push("Rejected: " + c.reasons.join("; "));
        if (c.eligible) lines.push("Meets the thresholds");
        return lines.join("\n");
      },
      sortBy: function (task) {
        if (this.sortTask === task) this.desc = !this.desc; else { this.sortTask = task; this.desc = true; }
      },
      open: function (row, task, c) {
        if (!c) return;
        var url = "/matrix/cell?run_id=" + encodeURIComponent(c.run_id) + "&model_id=" + encodeURIComponent(row.id) +
          "&task_type=" + encodeURIComponent(task.name);
        var self = this;
        document.getElementById("cell-panel-body").innerHTML = "<p class='p-4 text-sm text-slate-500'>Loading…</p>";
        this.panel = true;
        htmx.ajax("GET", url, { target: "#cell-panel-body", swap: "innerHTML" }).catch(function () { self.panel = true; });
      },
    };
  });
});
