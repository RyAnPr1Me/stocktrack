/* ── Shared utilities used across page templates ──────────────────────────── */

// Number formatting helpers available globally in all page-level scripts
window.fmtNum = function(n) {
  if (n === null || n === undefined) return '—';
  const f = parseFloat(n);
  if (isNaN(f)) return '—';
  if (Math.abs(f) >= 1000) return f.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return f.toFixed(2);
};

window.fmtPct = function(n) {
  if (n === null || n === undefined) return '—';
  const f = parseFloat(n);
  if (isNaN(f)) return '—';
  return (f >= 0 ? '+' : '') + f.toFixed(2) + '%';
};

window.truncate = function(str, len) {
  if (!str) return '';
  return str.length > len ? str.slice(0, len) + '…' : str;
};

// ── Modal helpers ────────────────────────────────────────────────────────────
window.openModal = function(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
};

window.closeModal = function(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
};

// Close modals on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'));
  }
});

// ── Sidebar toggle (mobile) ──────────────────────────────────────────────────
(function () {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  const btnTop  = document.getElementById('topbarMenuBtn');
  const btnSide = document.getElementById('sidebarToggle');

  function openSidebar()  { sidebar?.classList.add('open');  overlay?.classList.add('open'); }
  function closeSidebar() { sidebar?.classList.remove('open'); overlay?.classList.remove('open'); }

  btnTop?.addEventListener('click',  openSidebar);
  btnSide?.addEventListener('click', openSidebar);
  overlay?.addEventListener('click', closeSidebar);
})();

// ── Flash auto-dismiss ───────────────────────────────────────────────────────
(function () {
  setTimeout(() => {
    document.querySelectorAll('.flash').forEach(el => {
      el.style.transition = 'opacity 0.4s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 400);
    });
  }, 5000);
})();

// ── Market status indicator ──────────────────────────────────────────────────
(function () {
  const dot   = document.querySelector('.status-dot');
  const label = document.querySelector('.status-label');
  if (!dot || !label) return;

  const now = new Date();
  const et  = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const day = et.getDay();  // 0=Sun, 6=Sat
  const hr  = et.getHours();
  const min = et.getMinutes();
  const mins = hr * 60 + min;
  const isWeekday = day >= 1 && day <= 5;
  // Regular hours: 9:30–16:00 ET
  const isRegular = isWeekday && mins >= 9 * 60 + 30 && mins < 16 * 60;
  // Pre-market: 4:00–9:30 ET weekdays
  const isPre = isWeekday && mins >= 4 * 60 && mins < 9 * 60 + 30;
  // After-hours: 16:00–20:00 ET weekdays
  const isAfter = isWeekday && mins >= 16 * 60 && mins < 20 * 60;

  if (isRegular) {
    dot.style.background = '#10b981';
    dot.style.boxShadow  = '0 0 6px #10b981';
    label.textContent = 'Market Open';
  } else if (isPre) {
    dot.style.background = '#f59e0b';
    dot.style.boxShadow  = '0 0 6px #f59e0b';
    label.textContent = 'Pre-Market';
  } else if (isAfter) {
    dot.style.background = '#6366f1';
    dot.style.boxShadow  = '0 0 6px #6366f1';
    label.textContent = 'After Hours';
  } else {
    dot.style.background = '#64748b';
    dot.style.boxShadow  = 'none';
    dot.style.animation  = 'none';
    label.textContent = 'Market Closed';
  }
})();

// ── Chart.js global defaults (dark theme) ────────────────────────────────────
if (window.Chart) {
  Chart.defaults.color = '#64748b';
  Chart.defaults.font.family = "'Inter', sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';
  Chart.defaults.plugins.tooltip.backgroundColor = '#1c1c30';
  Chart.defaults.plugins.tooltip.titleColor = '#e2e8f0';
  Chart.defaults.plugins.tooltip.bodyColor = '#94a3b8';
  Chart.defaults.plugins.tooltip.borderColor = 'rgba(99,102,241,0.35)';
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.plugins.tooltip.cornerRadius = 8;
}
