const MARKET_NAMES = {
  lonavala: "Lonavala & Khandala Luxury Circuit",
  karjat: "Karjat Private Villa Belt",
  goa: "North Goa Beachfront & Villas",
};

const HotelIQ = {
  market: localStorage.getItem("hoteliq_market") || "lonavala",

  inr(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return "₹" + Math.round(n).toLocaleString("en-IN");
  },

  async api(path, options = {}) {
    const res = await fetch(path, {
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      window.location.href = "/index.html";
      throw new Error("Authentication required");
    }
    if (!res.ok) throw new Error(data.error || "Request failed");
    return data;
  },

  stateBox(kind, text) {
    return `<div class="p-6 text-center text-xs text-slate-400">${text}${
      kind === "error"
        ? ' <button class="text-cyan-400 underline" onclick="location.reload()">Retry</button>'
        : ""
    }</div>`;
  },

  demoBanner(meta) {
    const last = meta?.last_collection?.finished_at || meta?.last_collection?.started_at || "n/a";
    return `<div class="mb-4 px-3 py-2 rounded-lg border border-amber-500/30 bg-amber-500/10 text-[11px] text-amber-200 flex flex-wrap gap-3 items-center">
      <span class="badge-tag badge-demo">DEMO MODE</span>
      <span>Data Source: ${meta?.data_source || "Controlled demo dataset"}</span>
      <span>Last collection: ${last}</span>
    </div>`;
  },

  toggleTheme() {
    const html = document.documentElement;
    const dark = html.classList.toggle("dark");
    localStorage.setItem("hoteliq_theme", dark ? "dark" : "light");
    const icon = document.getElementById("theme-icon");
    if (icon) icon.className = dark ? "fa-solid fa-moon text-sm" : "fa-solid fa-sun text-sm text-amber-400";
  },

  updateMarket(val) {
    this.market = val;
    localStorage.setItem("hoteliq_market", val);
    location.reload();
  },

  async logout() {
    await this.api("/api/auth/logout", { method: "POST", body: "{}" });
    window.location.href = "/index.html";
  },
};

function toggleTheme() { HotelIQ.toggleTheme(); }
function updateMarket(val) { HotelIQ.updateMarket(val); }

document.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem("hoteliq_theme");
  if (saved === "light") {
    document.documentElement.classList.remove("dark");
    const icon = document.getElementById("theme-icon");
    if (icon) icon.className = "fa-solid fa-sun text-sm text-amber-400";
  }
  const sel = document.getElementById("market-select");
  if (sel) sel.value = HotelIQ.market;
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", () => sidebar.classList.toggle("hidden"));
  }
});
