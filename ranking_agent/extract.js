() => {
  // Conservative, versioned adapter. Unknown headings fail the page closed.
  const root = document.querySelector('#rso');
  if (!root) return {results: [], issues: ['Missing #rso search container']};
  const results = [], issues = [], seen = new Set();
  for (const heading of root.querySelectorAll('h3')) {
    if (!heading.getClientRects().length) continue;
    if (heading.closest('#tads, #bottomads, [data-text-ad], [data-ad-client], [data-attrid], [data-local-pack], [data-ai-overview]')) continue;
    const link = heading.closest('a[href]');
    if (!link) { issues.push('Unclassified result heading without a link'); continue; }
    let card = heading.closest('.tF2Cxc, .g');
    if (!card) {
      const candidate = heading.closest('.MjjYud');
      if (candidate && candidate.querySelectorAll('h3').length === 1 && candidate.querySelector('cite')) card = candidate;
    }
    if (!card) { issues.push('Unrecognized result block'); continue; }
    if (seen.has(card)) continue;
    seen.add(card);
    let url;
    try {
      url = new URL(link.href);
      if (url.hostname === 'www.google.com' && url.pathname === '/url') {
        url = new URL(url.searchParams.get('q') || url.searchParams.get('url'));
      }
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error();
      if (url.hostname === 'google.com' || url.hostname.endsWith('.google.com')) {
        issues.push('Unclassified Google-owned result'); continue;
      }
    } catch { issues.push('Invalid result URL'); continue; }
    results.push({title: heading.innerText.trim(), url: url.href});
  }
  if (!results.length) issues.push('No supported organic blocks found');
  return {results, issues};
}
