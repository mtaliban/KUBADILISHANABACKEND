"""
Simple HTTPS reverse proxy — Python tu, hakuna dependency.
Proxies HTTPS requests to backend HTTP on port 8080.

Run:
    python3 backend/https_proxy.py
"""
import ssl
import http.server
import urllib.request
import subprocess
import os
import sys

BACKEND = "http://127.0.0.1:8080"
PORT = 443
CERT_DIR = "/tmp/ssl_certs"
CERT_FILE = os.path.join(CERT_DIR, "server.pem")

def generate_self_signed_cert():
    os.makedirs(CERT_DIR, exist_ok=True)
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", os.path.join(CERT_DIR, "key.pem"),
        "-out", CERT_FILE,
        "-days", "365", "-nodes",
        "-subj", "/CN=api.196-249-120-2.sslip.io",
    ], check=True, capture_output=True)
    # Combine key + cert into single PEM for ssl.wrap_socket
    with open(os.path.join(CERT_DIR, "key.pem")) as k, \
         open(CERT_FILE) as c:
        combined = k.read() + c.read()
    with open(CERT_FILE, "w") as f:
        f.write(combined)
    print(f"✅ Certificate generated: {CERT_FILE}")

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _proxy(self):
        url = BACKEND + self.path
        body = None
        if 'Content-Length' in self.headers:
            body = self.rfile.read(int(self.headers['Content-Length']))
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ('host', 'transfer-encoding')}
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ('transfer-encoding', 'connection'):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())

    do_GET = _proxy
    do_POST = _proxy
    do_PATCH = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy
    do_HEAD = _proxy

    def log_message(self, format, *args):
        pass  # Silent

if __name__ == "__main__":
    generate_self_signed_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE)
    server = http.server.HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"🔒 HTTPS proxy running on https://0.0.0.0:{PORT} → {BACKEND}")
    print(f"   SSLip URL: https://api.196-249-120-2.sslip.io")
    server.serve_forever()
