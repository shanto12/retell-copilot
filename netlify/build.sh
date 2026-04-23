#!/usr/bin/env bash
# Extract the LANDING_HTML from handler.py and write it as Netlify's index.html.
# Also writes netlify.toml with API proxy rules pointing at the Lambda API GW.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HANDLER="$HERE/../lambda/handler.py"
LAMBDA_URL="${LAMBDA_URL:-https://g6o8u1x16j.execute-api.us-east-1.amazonaws.com}"

OUT_DIR="$HERE/dist"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

python3 <<PY
import re, pathlib
handler = pathlib.Path("$HANDLER").read_text()
m = re.search(r'LANDING_HTML\s*=\s*r"""(.*?)"""', handler, re.DOTALL)
if not m:
    raise SystemExit("LANDING_HTML not found")
html = m.group(1)
pathlib.Path("$OUT_DIR/index.html").write_text(html)
print(f"wrote index.html ({len(html)} bytes)")
PY

cat > "$OUT_DIR/_redirects" <<REDIRECTS
# API + dynamic routes -> Lambda (behind API Gateway)
/provision-agent       $LAMBDA_URL/provision-agent       200
/create-web-call       $LAMBDA_URL/create-web-call       200
/copilot-webhook       $LAMBDA_URL/copilot-webhook       200
/healthz               $LAMBDA_URL/healthz               200
/t/*                   $LAMBDA_URL/t/:splat              200!
/test/*                $LAMBDA_URL/test/:splat           200
REDIRECTS
echo "wrote _redirects"

cat > "$HERE/netlify.toml" <<'TOML'
[build]
  publish = "dist"
  command = "./build.sh"

# ─── Security headers (apply to every response) ────────────────────────────
[[headers]]
  for = "/*"
  [headers.values]
    X-Content-Type-Options = "nosniff"
    X-Frame-Options = "DENY"
    Referrer-Policy = "strict-origin-when-cross-origin"
    Permissions-Policy = "camera=(), microphone=(self), geolocation=(), payment=(), usb=()"
    Strict-Transport-Security = "max-age=63072000; includeSubDomains; preload"
    # CSP: allow Retell Web SDK + its analytics host + Google Fonts. 'unsafe-inline'
    # is required for the inline <style> and <script> blocks; we keep it scoped tightly.
    Content-Security-Policy = "default-src 'self'; script-src 'self' 'unsafe-inline' https://esm.sh https://*.esm.sh; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; connect-src 'self' https://esm.sh https://*.esm.sh https://api.retellai.com https://*.retellai.com wss://*.retellai.com wss://*.livekit.cloud https://*.livekit.cloud; img-src 'self' data:; media-src 'self' blob: mediastream:; frame-ancestors 'none'; base-uri 'self'; form-action 'self';"
TOML
echo "wrote netlify.toml"

echo ""
echo "✓ Netlify bundle ready at $OUT_DIR"
echo "  - To deploy: cd $HERE && NETLIFY_AUTH_TOKEN=… netlify deploy --dir=dist --prod --site retell-copilot"
