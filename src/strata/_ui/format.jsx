// Shared formatting helpers for the Console's proof surfaces.
function humanAgo(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 45) return "just now";
  const units = [
    [60, "minute"], [3600, "hour"], [86400, "day"],
    [604800, "week"], [2592000, "month"], [31536000, "year"],
  ];
  let value = secs, label = "second";
  for (const [size, name] of units) {
    if (secs < size) break;
    value = Math.floor(secs / size);
    label = name;
  }
  return `${value} ${label}${value === 1 ? "" : "s"} ago`;
}

function absoluteTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function formatNumber(n) {
  return new Intl.NumberFormat().format(n || 0);
}

window.humanAgo = humanAgo;
window.absoluteTime = absoluteTime;
window.formatNumber = formatNumber;
