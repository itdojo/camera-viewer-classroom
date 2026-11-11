// Formatting shared by more than one concern.

// 812_345_000_000 -> "812.3 GB". Decimal units, as the disk thresholds are given.
export function size(bytes) {
  for (const [unit, factor] of [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['kB', 1e3]]) {
    if (bytes >= factor) return `${(bytes / factor).toFixed(1)} ${unit}`;
  }
  return `${bytes} B`;
}
