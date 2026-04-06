const https = require('https');
const workerToken = 'cfut_pjEgkRcv4FUjJHge3QQEbGqGJymyYQEMlTezvEOh6fb44082'; // Edit Workers
const dnsToken    = 'cfut_BuTCsrHN3M8C8ZeXLW3I9dVlCvKJ6XzBP5i85Jxp551314a6'; // Edit zone DNS
const zoneId = '98ca5b0b945ae77292e6b7755ab7c3be'; // drunks.app

// Create CNAME: bet.drunks.app → kalishi-edge-dashboard.pages.dev (proxied)
const body = JSON.stringify({ type: 'CNAME', name: 'bet', content: 'kalishi-edge-dashboard.pages.dev', proxied: true, ttl: 1 });
const opts = {
  hostname: 'api.cloudflare.com',
  path: `/client/v4/zones/${zoneId}/dns_records`,
  method: 'POST',
  headers: { 'Authorization': `Bearer ${dnsToken}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
};
const rq = https.request(opts, res => {
  let d = ''; res.on('data', c => d += c);
  res.on('end', () => {
    const r = JSON.parse(d);
    console.log('CNAME created:', r.success);
    if (r.result) console.log(`  ${r.result.name} → ${r.result.content}  proxied=${r.result.proxied}`);
    if (r.errors?.length) console.log('  errors:', r.errors);
  });
});
rq.on('error', e => console.error(e.message));
rq.write(body); rq.end();


