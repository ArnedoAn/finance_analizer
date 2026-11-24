"""Browser view of the enrollment QR (``POST /devices/enroll-qr`` and ``GET /enroll/view/{token}``).

The two one-time codes are *derived* from the random view token (HMAC-SHA256 keyed with the token), so the
database keeps only the usual SHA-256 hashes in ``enroll_codes``: neither the token nor a code is ever stored,
nothing lives in process memory (any worker can serve the page) and the page stops working by itself once
either code is used or expired.
"""
import hashlib
import hmac
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from html import escape

import segno

INGEST_SCOPES = ["ingest"]
INTERACTIVE_SCOPES = ["read", "review"]
MAX_TOKEN_LENGTH = 200  # a longer path segment cannot be one of ours; it is cut before hashing

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def derive_codes(token: str) -> tuple[str, str]:
    """The (ingest, interactive) enrollment codes that belong to a view token."""
    key = token[:MAX_TOKEN_LENGTH].encode()

    def code(label: bytes) -> str:
        return urlsafe_b64encode(hmac.digest(key, b"cofre-enroll-qr/" + label, hashlib.sha256)).rstrip(b"=").decode()

    return code(b"ingest"), code(b"interactive")


def build_link(api_base: str, ingest: str, interactive: str) -> str:
    """The exact link the app parses (``EnrollLink`` in app/lib/features/enroll): codes are URL-safe, no escaping needed."""
    return f"cofre://enroll?api={api_base}&ingest={ingest}&interactive={interactive}"


def render_qr_svg(link: str) -> str:
    """Inline ``<svg>`` (no CDN, no image file) with a white quiet zone so it scans on any page background."""
    return segno.make(link, error="m").svg_inline(scale=7, border=4, dark="#000", light="#fff", title="Código QR para conectar Cofre")


_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:24px;background:#f4f4f5;color:#18181b;display:flex;justify-content:center}}
main{{max-width:480px;width:100%;background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.15)}}
h1{{margin-top:0;font-size:1.5rem}} .qr{{text-align:center}} .qr svg{{max-width:100%;height:auto}}
#left{{font-weight:600;font-variant-numeric:tabular-nums}} ol{{padding-left:1.2rem;line-height:1.5}}
code{{word-break:break-all;display:block;background:#f4f4f5;padding:8px;border-radius:6px;font-size:.8rem}}
.warn{{color:#92400e;font-size:.9rem}}
</style></head><body><main>
{body}
</main></body></html>
"""

_VALID_BODY = """<h1>Conectar Cofre</h1>
<p class="qr" id="qr">{svg}</p>
<p>Caduca a las <time datetime="{iso}">{clock} UTC</time>. Tiempo restante: <span id="left">{minutes} min</span></p>
<ol>
<li>Abra la app Cofre en el teléfono y vaya a «Conectar».</li>
<li>Escanee este código QR con el escáner de la app.</li>
<li>Confirme que el servidor mostrado es el suyo y cierre esta página.</li>
</ol>
<details><summary>Ver el enlace en texto</summary><code>{link}</code></details>
<p class="warn">Este código sirve una sola vez: no lo comparta ni lo guarde. Al conectarse el teléfono, esta página deja de funcionar.</p>
<script>
(function(){{var end=Date.parse("{iso}"),left=document.getElementById("left");
function tick(){{var s=Math.max(0,Math.round((end-Date.now())/1000));
if(s===0){{document.getElementById("qr").textContent="El código caducó. Genere uno nuevo.";left.textContent="0:00";clearInterval(t);return}}
left.textContent=Math.floor(s/60)+":"+("0"+s%60).slice(-2)}}
var t=setInterval(tick,1000);tick()}})();
</script>"""

_INVALID_BODY = """<h1>Enlace no válido o caducado</h1>
<p>Este enlace no sirve. Genere uno nuevo desde el servidor.</p>"""


def render_page(link: str, expires_at: datetime, now: datetime) -> str:
    """The page for a live link. Also rendered (and discarded) for dead links so every answer costs the same."""
    minutes = max(0, int((expires_at - now).total_seconds() // 60))
    expires_at = expires_at.astimezone(UTC)
    return _PAGE.format(
        title="Conectar Cofre",
        body=_VALID_BODY.format(
            svg=render_qr_svg(link), iso=expires_at.isoformat(timespec="seconds"), clock=expires_at.strftime("%H:%M"),
            minutes=minutes, link=escape(link),
        ),
    )


def render_invalid_page() -> str:
    return _PAGE.format(title="Enlace no válido o caducado", body=_INVALID_BODY)
