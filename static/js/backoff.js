export function backoffDelay(attempt, { baseMs = 500, maxMs = 15000, jitter = 0.3 } = {}, rand = Math.random) {
  const d = Math.min(maxMs, baseMs * 2 ** attempt);
  return Math.round(d * (1 - jitter + jitter * rand()));
}
