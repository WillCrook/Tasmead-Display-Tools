"""Shared native controls and Google Maps 3D WebEngine preview workspace."""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from services.map_preview import PreviewScene, TraceAdjustment, preview_payload

try:  # WebEngine is optional until the user requests a preview.
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - exercised on installations without WebEngine
    QWebChannel = None
    QWebEnginePage = None
    QWebEngineView = None


WEBENGINE_AVAILABLE = QWebEngineView is not None
_CHUNK_SIZE = 128 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_MAX_CSP_DIAGNOSTICS = 32
_SHELL_READY_TIMEOUT_MS = 10_000
_PRESENTATION_REFRESH_INTERVAL_MS = 33
_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_CSP_DIRECTIVES = frozenset(
    {
        "base-uri",
        "child-src",
        "connect-src",
        "default-src",
        "font-src",
        "form-action",
        "frame-ancestors",
        "frame-src",
        "img-src",
        "manifest-src",
        "media-src",
        "object-src",
        "script-src",
        "script-src-attr",
        "script-src-elem",
        "style-src",
        "style-src-attr",
        "style-src-elem",
        "worker-src",
    }
)
_WEB_FAILURE_MESSAGES = {
    "authentication": frozenset(
        {
            "Google rejected the map configuration. Check the API key, restrictions, Maps JavaScript API access, and billing.",
            "Google rejected the API key or project configuration. Check the key, Maps JavaScript API, restrictions, and billing.",
        }
    ),
    "network": frozenset(
        {
            "Google Maps could not be downloaded. Check the network connection and try again.",
        }
    ),
    "render": frozenset(
        {
            "Google Maps 3D could not initialise. Check WebGL support and the Google project configuration.",
            "Google Maps did not reach a stable rendered state. Check WebGL support and try again.",
            "Google Maps could not render this 3D scene. Check Maps 3D availability and WebGL support.",
        }
    ),
    "transport": frozenset(
        {
            "The preview scene was not transferred completely. Try again.",
            "The preview scene data was invalid.",
        }
    ),
}
_WEB_FAILURE_FALLBACKS = {
    "authentication": "Google rejected the Maps project configuration. Check the API key, restrictions, Maps JavaScript API access, and billing.",
    "network": "Google Maps could not be downloaded. Check the network connection and try again.",
    "render": "Google Maps could not render this 3D scene. Check Maps 3D availability and WebGL support.",
    "transport": "The preview scene could not be transferred. Try again.",
}


def _validated_nonce(nonce: str) -> str:
    value = str(nonce)
    if _NONCE_PATTERN.fullmatch(value) is None:
        raise ValueError("Preview CSP nonce has an invalid format.")
    return value


def _render_preview_html(nonce: str) -> str:
    return _PREVIEW_HTML.replace("__NONCE__", _validated_nonce(nonce))


def _content_security_policy(nonce: str) -> str:
    value = _validated_nonce(nonce)
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{value}' 'strict-dynamic' https: qrc: 'unsafe-eval' blob:; "
        "script-src-attr 'none'; "
        "connect-src 'self' data: blob: https://*.googleapis.com https://*.gstatic.com https://*.google.com; "
        "img-src 'self' data: blob: https://*.googleapis.com https://*.gstatic.com "
        "https://*.google.com https://*.googleusercontent.com https://*.ggpht.com; "
        f"style-src 'nonce-{value}' https://fonts.googleapis.com; "
        f"style-src-elem 'nonce-{value}' https://fonts.googleapis.com; "
        "style-src-attr 'unsafe-inline'; "
        "font-src https://fonts.gstatic.com; frame-src https://*.google.com; "
        "worker-src blob:; child-src blob:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )


def _normalise_csp_directive(value: str) -> str:
    directive = str(value).strip().lower()
    return directive if directive in _CSP_DIRECTIVES else "other"


def _normalise_csp_disposition(value: str) -> str:
    return "report" if str(value).strip().lower() == "report" else "enforce"


def _sanitise_diagnostic_location(value: str) -> str:
    raw = str(value).strip()
    literal = raw.lower()
    if literal in {"inline", "eval", "self"}:
        return literal
    if literal.startswith("data:"):
        return "data:"
    if literal.startswith("blob:"):
        return "blob:"
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return "other"
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.hostname:
        host = parsed.hostname.lower()
        try:
            port = parsed.port
        except ValueError:
            return "other"
        default_port = 80 if scheme == "http" else 443
        port_suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{scheme}://{host}{port_suffix}"
    if scheme == "qrc":
        return "qrc:"
    return f"{scheme}:" if scheme in {"file", "ws", "wss"} else "other"


def _clamped_diagnostic_number(value: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    return min(maximum, max(minimum, number))


def _normalised_web_failure(kind: str, message: str) -> tuple[str, str]:
    safe_kind = str(kind).strip().lower()
    if safe_kind not in _WEB_FAILURE_MESSAGES:
        safe_kind = "render"
    candidate = str(message)
    safe_message = (
        candidate
        if len(candidate) <= 256 and candidate in _WEB_FAILURE_MESSAGES[safe_kind]
        else _WEB_FAILURE_FALLBACKS[safe_kind]
    )
    return safe_kind, safe_message


_PREVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tasmead 3D preview</title>
  <style nonce="__NONCE__">
    html, body, #map-host { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #0f172a; }
    gmp-map-3d { width: 100%; height: 100%; display: block; }
    #state { position: fixed; z-index: 100; inset: 16px auto auto 16px; max-width: 520px;
      padding: 10px 14px; border-radius: 8px; color: #f8fafc; background: rgba(15,23,42,.88);
      font: 14px/1.4 system-ui, sans-serif; box-shadow: 0 4px 18px rgba(0,0,0,.24); }
    #state[data-ready="true"] { display: none; }
  </style>
  <script nonce="__NONCE__" src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body>
  <div id="state">Initialising secure preview…</div>
  <div id="map-host"></div>
  <script nonce="__NONCE__">
  (() => {
    'use strict';
    const generationMatch = window.location.hash.match(/^#generation=([1-9][0-9]{0,9})$/);
    const initialGeneration = generationMatch
      ? Math.min(2147483647, Number(generationMatch[1]))
      : 0;
    const state = {
      bridge: null, googleReady: false, chunks: new Map(), pending: null,
      map: null, payload: null, latestRevision: -1, renderTimeout: null,
      generation: initialGeneration, cspFailed: false, cspDiagnostics: new Map()
    };
    const status = document.getElementById('state');
    const host = document.getElementById('map-host');
    const cspDirectives = new Set([
      'base-uri', 'child-src', 'connect-src', 'default-src', 'font-src',
      'form-action', 'frame-ancestors', 'frame-src', 'img-src', 'manifest-src',
      'media-src', 'object-src', 'script-src', 'script-src-attr',
      'script-src-elem', 'style-src', 'style-src-attr', 'style-src-elem',
      'worker-src'
    ]);

    function setStatus(message, ready=false) {
      status.textContent = message;
      status.dataset.ready = ready ? 'true' : 'false';
    }
    function fail(kind, message) {
      setStatus(message, false);
      if (state.bridge) {
        state.bridge.renderFailed(state.generation, String(kind), String(message));
      }
    }
    function diagnosticDirective(value) {
      const directive = String(value || '').trim().toLowerCase();
      return cspDirectives.has(directive) ? directive : 'other';
    }
    function diagnosticDisposition(value) {
      return String(value || '').trim().toLowerCase() === 'report' ? 'report' : 'enforce';
    }
    function diagnosticLocation(value) {
      const raw = String(value || '').trim();
      if (!raw) return 'other';
      const literal = raw.toLowerCase();
      if (literal === 'inline' || literal === 'eval' || literal === 'self') return literal;
      if (literal.startsWith('data:')) return 'data:';
      if (literal.startsWith('blob:')) return 'blob:';
      try {
        const parsed = new URL(raw, window.location.href);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.origin;
        if (parsed.protocol === 'qrc:') return 'qrc:';
        if (parsed.protocol === 'file:' || parsed.protocol === 'ws:' || parsed.protocol === 'wss:') {
          return parsed.protocol;
        }
      } catch (_) {}
      return 'other';
    }
    function diagnosticNumber(value, minimum, maximum) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return minimum;
      return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)));
    }
    function deliverCspDiagnostic(record) {
      if (!state.bridge || state.generation <= 0 || record.delivered) return;
      state.bridge.securityPolicyViolation(
        state.generation,
        record.disposition,
        record.directive,
        record.blocked,
        record.source,
        record.line,
        record.column,
        record.count
      );
      record.delivered = true;
    }
    function flushCspDiagnostics() {
      for (const record of state.cspDiagnostics.values()) deliverCspDiagnostic(record);
    }
    function recordCspViolation(event) {
      const disposition = diagnosticDisposition(event.disposition);
      const directive = diagnosticDirective(event.effectiveDirective || event.violatedDirective);
      const blocked = diagnosticLocation(event.blockedURI);
      const source = diagnosticLocation(event.sourceFile);
      const line = diagnosticNumber(event.lineNumber, 0, 2147483647);
      const column = diagnosticNumber(event.columnNumber, 0, 2147483647);
      let key = JSON.stringify([disposition, directive, blocked, source]);
      let record = state.cspDiagnostics.get(key);
      if (!record && state.cspDiagnostics.size >= 30) {
        key = 'overflow:' + disposition;
        record = state.cspDiagnostics.get(key);
        if (!record) {
          record = {
            disposition, directive: 'other', blocked: 'other', source: 'other',
            line: 0, column: 0, count: 0, delivered: false
          };
          state.cspDiagnostics.set(key, record);
        }
      } else if (!record) {
        record = {
          disposition, directive, blocked, source, line, column,
          count: 0, delivered: false
        };
        state.cspDiagnostics.set(key, record);
      }
      record.count = diagnosticNumber(record.count + 1, 1, 1000000);
      if (disposition === 'enforce') {
        state.cspFailed = true;
        if (state.renderTimeout) clearTimeout(state.renderTimeout);
        state.renderTimeout = null;
        setStatus('The embedded map was blocked by its security policy.', false);
      }
      deliverCspDiagnostic(record);
    }
    window.addEventListener('securitypolicyviolation', recordCspViolation, true);
    function altitudeMode(AltitudeMode, value) {
      return AltitudeMode[value] || value;
    }
    function allCoordinates(payload) {
      const points = [];
      for (const trace of payload.traces || []) {
        for (const geometry of trace.geometries || []) points.push(...(geometry.coordinates || []));
      }
      return points;
    }
    function fitScene() {
      if (!state.map || !state.payload) return;
      const points = allCoordinates(state.payload);
      if (!points.length) return;
      const anchor = state.payload.traces[0].anchor;
      const originLat = Number(anchor.lat);
      const originLng = Number(anchor.lng);
      let eastMin = 0, eastMax = 0, northMin = 0, northMax = 0;
      let altitudeMin = Number(anchor.altitude || 0), altitudeMax = altitudeMin;
      const latScale = 111320;
      const lngScale = Math.max(1, Math.cos(originLat * Math.PI / 180) * latScale);
      for (const point of points) {
        let deltaLng = Number(point.lng) - originLng;
        while (deltaLng > 180) deltaLng -= 360;
        while (deltaLng < -180) deltaLng += 360;
        const east = deltaLng * lngScale;
        const north = (Number(point.lat) - originLat) * latScale;
        eastMin = Math.min(eastMin, east); eastMax = Math.max(eastMax, east);
        northMin = Math.min(northMin, north); northMax = Math.max(northMax, north);
        altitudeMin = Math.min(altitudeMin, Number(point.altitude || 0));
        altitudeMax = Math.max(altitudeMax, Number(point.altitude || 0));
      }
      const centreEast = (eastMin + eastMax) / 2;
      const centreNorth = (northMin + northMax) / 2;
      let centreLng = originLng + centreEast / lngScale;
      while (centreLng > 180) centreLng -= 360;
      while (centreLng < -180) centreLng += 360;
      const centreLat = originLat + centreNorth / latScale;
      const span = Math.max(eastMax-eastMin, northMax-northMin, altitudeMax-altitudeMin, 80);
      state.map.center = { lat: centreLat, lng: centreLng, altitude: Math.max(0, altitudeMax * .25) };
      state.map.range = Math.max(250, span * 2.8);
      state.map.tilt = 60;
      state.map.heading = 0;
    }
    async function render(payload, revision) {
      if (state.cspFailed) return;
      if (!state.googleReady) { state.pending = {payload, revision}; return; }
      try {
        setStatus('Rendering WGS84 trace geometry…');
        const [maps3d, markerLibrary] = await Promise.all([
          google.maps.importLibrary('maps3d'),
          google.maps.importLibrary('marker')
        ]);
        if (state.cspFailed || Number(revision) !== state.latestRevision) return;
        const {Map3DElement, MapMode, AltitudeMode, Polyline3DElement, Polygon3DElement} = maps3d;
        if (!Map3DElement || !Polyline3DElement || !Polygon3DElement) {
          throw new Error('Required Maps 3D elements are unavailable.');
        }
        host.replaceChildren();
        const map = new Map3DElement({
          center: {lat: payload.traces[0].anchor.lat, lng: payload.traces[0].anchor.lng, altitude: 0},
          range: 1500,
          tilt: 60,
          heading: 0,
          mode: (MapMode && MapMode.HYBRID) || 'HYBRID'
        });
        map.addEventListener('gmp-error', () => {
          if (Number(revision) === state.latestRevision) {
            fail('render', 'Google Maps 3D could not initialise. Check WebGL support and the Google project configuration.');
          }
        });
        map.addEventListener('gmp-map-id-error', () => {
          if (Number(revision) === state.latestRevision) {
            fail('authentication', 'Google rejected the map configuration. Check the API key, restrictions, Maps JavaScript API access, and billing.');
          }
        });
        let acknowledged = false;
        map.addEventListener('gmp-steadychange', event => {
          if (state.cspFailed || Number(revision) !== state.latestRevision) return;
          const isSteady = Boolean(event.isSteady);
          if (state.bridge) {
            state.bridge.presentationStateChanged(
              state.generation, Number(revision), isSteady
            );
          }
          if (!isSteady || acknowledged) return;
          acknowledged = true;
          if (state.renderTimeout) clearTimeout(state.renderTimeout);
          state.renderTimeout = null;
          setStatus('', true);
          if (state.bridge) state.bridge.renderAcknowledged(state.generation, Number(revision));
        });
        host.append(map);
        state.map = map;
        state.payload = payload;

        const renderedAnchors = new Set();
        for (const trace of payload.traces) {
          for (const geometry of trace.geometries) {
            const common = {
              altitudeMode: altitudeMode(AltitudeMode, geometry.altitudeMode),
              strokeColor: geometry.style.strokeColor,
              strokeWidth: geometry.style.strokeWidth
            };
            if (geometry.type === 'polyline') {
              map.append(new Polyline3DElement({
                ...common,
                path: geometry.coordinates,
                extruded: Boolean(geometry.extrude),
                geodesic: Boolean(geometry.tessellate)
              }));
            } else if (geometry.type === 'polygon') {
              map.append(new Polygon3DElement({
                ...common,
                path: geometry.coordinates,
                // KML's default PolyStyle is opaque white when no polygon
                // colour sub-style is supplied.
                fillColor: geometry.style.fillColor || '#ffffffff'
              }));
            }
          }
          const MarkerClass = maps3d.Marker3DElement;
          const anchorKey = JSON.stringify([
            trace.anchor.lat, trace.anchor.lng, trace.anchor.altitude,
            trace.anchor.altitudeMode
          ]);
          if (MarkerClass && !renderedAnchors.has(anchorKey)) {
            renderedAnchors.add(anchorKey);
            const marker = new MarkerClass({
              position: {lat: trace.anchor.lat, lng: trace.anchor.lng, altitude: trace.anchor.altitude},
              altitudeMode: altitudeMode(AltitudeMode, trace.anchor.altitudeMode),
              label: trace.anchor.label,
              drawsWhenOccluded: true
            });
            if (markerLibrary.PinElement && marker.append) {
              marker.append(new markerLibrary.PinElement({
                background: '#ff00ff', borderColor: '#ffffff', glyphColor: '#ffffff', glyphText: 'A'
              }));
            }
            map.append(marker);
          }
        }
        fitScene();
        if (state.renderTimeout) clearTimeout(state.renderTimeout);
        state.renderTimeout = setTimeout(() => {
          if (!acknowledged && Number(revision) === state.latestRevision) {
            fail('render', 'Google Maps did not reach a stable rendered state. Check WebGL support and try again.');
          }
        }, 20000);
      } catch (_) {
        if (Number(revision) === state.latestRevision) {
          fail('render', 'Google Maps could not render this 3D scene. Check Maps 3D availability and WebGL support.');
        }
      }
    }

    window.tasmead = {
      loadGoogleMaps(apiKey) {
        if (state.googleReady || state.cspFailed) return;
        setStatus('Loading Google Maps 3D…');
        window.gm_authFailure = () => fail(
          'authentication',
          'Google rejected the API key or project configuration. Check the key, Maps JavaScript API, restrictions, and billing.'
        );
        const script = document.createElement('script');
        script.nonce = '__NONCE__';
        script.async = true;
        script.onerror = () => fail('network', 'Google Maps could not be downloaded. Check the network connection and try again.');
        script.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(apiKey)
          + '&v=quarterly&loading=async&libraries=maps3d,marker&auth_referrer_policy=origin&callback=tasmeadGoogleReady';
        document.head.append(script);
      },
      beginScene(revision, total) { state.chunks.set(Number(revision), {total: Number(total), parts: []}); },
      appendSceneChunk(revision, chunk) {
        const item = state.chunks.get(Number(revision));
        if (item) item.parts.push(String(chunk));
      },
      finishScene(revision) {
        const key = Number(revision);
        const item = state.chunks.get(key);
        if (!item || item.parts.length !== item.total) {
          fail('transport', 'The preview scene was not transferred completely. Try again.');
          return;
        }
        state.chunks.delete(key);
        state.latestRevision = key;
        if (state.cspFailed) return;
        try { render(JSON.parse(item.parts.join('')), key); }
        catch (_) { fail('transport', 'The preview scene data was invalid.'); }
      },
      fitScene
    };
    window.tasmeadGoogleReady = () => {
      state.googleReady = true;
      if (!state.cspFailed && state.pending) {
        const pending = state.pending; state.pending = null;
        render(pending.payload, pending.revision);
      }
    };
    new QWebChannel(qt.webChannelTransport, channel => {
      state.bridge = channel.objects.tasmeadBridge;
      state.bridge.shellReady(state.generation);
      flushCspDiagnostics();
    });
  })();
  </script>
</body>
</html>
"""


class _PreviewRequestHandler(BaseHTTPRequestHandler):
    server_version = "TasmeadPreview/1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        server = self.server
        if self.path != server.preview_path:  # type: ignore[attr-defined]
            self.send_error(404)
            return
        nonce = secrets.token_urlsafe(24)
        body = _render_preview_html(nonce).encode("utf-8")
        content_security_policy = _content_security_policy(nonce)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            content_security_policy,
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class PreviewLoopbackServer:
    """Serve one non-sensitive shell from an unguessable loopback URL."""

    def __init__(self) -> None:
        token = secrets.token_urlsafe(24)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewRequestHandler)
        self._server.daemon_threads = True
        self._server.preview_path = f"/{token}/preview"  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tasmead-preview-loopback",
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> QUrl:
        port = self._server.server_address[1]
        return QUrl(f"http://127.0.0.1:{port}{self._server.preview_path}")  # type: ignore[attr-defined]

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def stop(self) -> None:
        if self._server is None:
            return
        server = self._server
        self._server = None
        server.shutdown()
        server.server_close()
        self._thread.join(timeout=2)


class MapPreviewBridge(QObject):
    shell_ready = pyqtSignal(int)
    render_acknowledged = pyqtSignal(int, int)
    render_failed = pyqtSignal(int, str, str)
    presentation_state_changed = pyqtSignal(int, int, bool)
    security_policy_violation = pyqtSignal(
        int, str, str, str, str, int, int, int
    )

    @pyqtSlot(int)
    def shellReady(self, generation: int) -> None:  # noqa: N802 - called from JavaScript
        self.shell_ready.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            )
        )

    @pyqtSlot(int, int)
    def renderAcknowledged(self, generation: int, revision: int) -> None:  # noqa: N802
        self.render_acknowledged.emit(generation, revision)

    @pyqtSlot(int, str, str)
    def renderFailed(self, generation: int, kind: str, message: str) -> None:  # noqa: N802
        safe_kind, safe_message = _normalised_web_failure(kind, message)
        self.render_failed.emit(generation, safe_kind, safe_message)

    @pyqtSlot(int, int, bool)
    def presentationStateChanged(  # noqa: N802 - called from JavaScript
        self,
        generation: int,
        revision: int,
        is_steady: bool,
    ) -> None:
        self.presentation_state_changed.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            ),
            _clamped_diagnostic_number(
                revision, minimum=0, maximum=2_147_483_647
            ),
            bool(is_steady),
        )

    @pyqtSlot(int, str, str, str, str, int, int, int)
    def securityPolicyViolation(  # noqa: N802 - called from JavaScript
        self,
        generation: int,
        disposition: str,
        directive: str,
        blocked_location: str,
        source_location: str,
        line: int,
        column: int,
        duplicate_count: int,
    ) -> None:
        self.security_policy_violation.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            ),
            _normalise_csp_disposition(disposition),
            _normalise_csp_directive(directive),
            _sanitise_diagnostic_location(blocked_location),
            _sanitise_diagnostic_location(source_location),
            _clamped_diagnostic_number(line, minimum=0, maximum=2_147_483_647),
            _clamped_diagnostic_number(
                column, minimum=0, maximum=2_147_483_647
            ),
            _clamped_diagnostic_number(
                duplicate_count, minimum=1, maximum=1_000_000
            ),
        )


if WEBENGINE_AVAILABLE:
    class RestrictedPreviewPage(QWebEnginePage):
        def __init__(self, allowed_port: int, parent: QObject | None = None) -> None:
            super().__init__(parent)
            self.allowed_port = allowed_port

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            if not is_main_frame:
                return True
            return (
                url.scheme() == "http"
                and url.host() == "127.0.0.1"
                and url.port() == self.allowed_port
            )

        def createWindow(self, _window_type):
            return None

        def javaScriptConsoleMessage(
            self, _level, _message: str, _line_number: int, _source_id: str
        ) -> None:
            # Qt's default implementation prints raw third-party console text.
            # Maps script URLs may contain the API key, so diagnostics cross the
            # bridge only through the bounded, sanitised CSP event path above.
            return


class MapPreviewWidget(QWidget):
    """Full-workspace map with native, per-trace ENU controls."""

    close_requested = pyqtSignal()
    scene_applied = pyqtSignal(object)
    scene_export_requested = pyqtSignal(object)
    fullscreen_requested = pyqtSignal(bool)
    settings_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mapPreviewWorkspace")
        self._scene: PreviewScene | None = None
        self._committed_scene: PreviewScene | None = None
        self._api_key = ""
        self._revision = 0
        self._acknowledged_revision = -1
        self._page_generation = 0
        self._csp_failed_generation: int | None = None
        self._csp_diagnostics: dict[tuple[str, str, str, str], int] = {}
        self._shell_ready = False
        self._server: PreviewLoopbackServer | None = None
        self._web_view = None
        self._bridge: MapPreviewBridge | None = None
        self._loading_controls = False
        self._presentation_active = False

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(140)
        self._render_timer.timeout.connect(self._render_scene)
        self._shell_ready_timer = QTimer(self)
        self._shell_ready_timer.setSingleShot(True)
        self._shell_ready_timer.setInterval(_SHELL_READY_TIMEOUT_MS)
        self._shell_ready_timer.timeout.connect(self._on_shell_ready_timeout)
        self._presentation_timer = QTimer(self)
        self._presentation_timer.setInterval(_PRESENTATION_REFRESH_INTERVAL_MS)
        self._presentation_timer.timeout.connect(self._refresh_web_presentation)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Google Maps 3D trace preview")
        title.setObjectName("dialogTitle")
        header.addWidget(title)
        header.addStretch()
        self.fullscreen_button = QPushButton("Full screen")
        self.fullscreen_button.setCheckable(True)
        self.fullscreen_button.toggled.connect(self.fullscreen_requested)
        header.addWidget(self.fullscreen_button)
        self.close_button = QPushButton("Cancel")
        self.close_button.clicked.connect(self.close_requested)
        header.addWidget(self.close_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        self.map_host = QFrame()
        self.map_host.setObjectName("previewHost")
        self.map_layout = QVBoxLayout(self.map_host)
        self.map_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(self.map_host)

        controls = QFrame()
        controls.setObjectName("workspacePanel")
        controls.setMinimumWidth(300)
        controls.setMaximumWidth(380)
        panel = QVBoxLayout(controls)
        panel.setContentsMargins(16, 16, 16, 16)
        panel.setSpacing(10)
        panel.addWidget(QLabel("Trace", objectName="panelTitle"))
        self.trace_selector = QComboBox()
        self.trace_selector.setAccessibleName("Trace to adjust")
        self.trace_selector.currentIndexChanged.connect(self._selected_trace_changed)
        panel.addWidget(self.trace_selector)

        self.axis_controls: dict[str, QDoubleSpinBox] = {}
        specifications = (
            ("east_m", "X / East", -100_000.0, 100_000.0, " m"),
            ("north_m", "Y / North", -100_000.0, 100_000.0, " m"),
            ("up_m", "Z / Up", -20_000.0, 20_000.0, " m"),
            ("yaw_deg", "Yaw / clockwise", -180.0, 180.0, "°"),
        )
        for key, label, minimum, maximum, suffix in specifications:
            panel.addWidget(QLabel(label))
            spin = QDoubleSpinBox()
            spin.setAccessibleName(label)
            spin.setRange(minimum, maximum)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            spin.setSuffix(suffix)
            spin.valueChanged.connect(self._adjustment_changed)
            panel.addWidget(spin)
            self.axis_controls[key] = spin

        self.up_warning = QLabel(
            "A non-zero Up offset lifts ground-clamped lines and polygons by "
            "exporting them relative to the ground. Negative values may be hidden by terrain."
        )
        self.up_warning.setObjectName("mutedText")
        self.up_warning.setWordWrap(True)
        self.up_warning.hide()
        panel.addWidget(self.up_warning)

        reset_row = QHBoxLayout()
        self.reset_selected_button = QPushButton("Reset selected")
        self.reset_all_button = QPushButton("Reset all")
        self.reset_selected_button.clicked.connect(self._reset_selected)
        self.reset_all_button.clicked.connect(self._reset_all)
        reset_row.addWidget(self.reset_selected_button)
        reset_row.addWidget(self.reset_all_button)
        panel.addLayout(reset_row)

        self.fit_button = QPushButton("Recentre view")
        self.fit_button.clicked.connect(self.fit_traces)
        panel.addWidget(self.fit_button)

        self.status_label = QLabel("Waiting for a preview scene.")
        self.status_label.setObjectName("mutedText")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        panel.addWidget(self.status_label)
        self.retry_button = QPushButton("Retry map")
        self.retry_button.clicked.connect(self._reload_shell)
        self.retry_button.hide()
        panel.addWidget(self.retry_button)
        self.open_settings_button = QPushButton("Open Maps Settings")
        self.open_settings_button.clicked.connect(self.settings_requested)
        self.open_settings_button.hide()
        panel.addWidget(self.open_settings_button)
        panel.addStretch()

        self.apply_button = QPushButton("Apply offsets")
        self.apply_button.setObjectName("primaryButton")
        self.apply_export_button = QPushButton("Apply and export KML…")
        self.apply_button.clicked.connect(self._apply)
        self.apply_export_button.clicked.connect(self._apply_and_export)
        panel.addWidget(self.apply_button)
        panel.addWidget(self.apply_export_button)
        splitter.addWidget(controls)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes((900, 340))
        self._set_apply_enabled(False)

        self._idle_label = QLabel(
            "The Google Maps 3D renderer starts only when a preview is requested."
            if WEBENGINE_AVAILABLE
            else
                "PyQt6-WebEngine is not installed. Install the dependencies in "
                "requirements.txt, then restart the application."
        )
        self._idle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_label.setWordWrap(True)
        self.map_layout.addWidget(self._idle_label)

    def _ensure_web_view(self) -> bool:
        if not WEBENGINE_AVAILABLE:
            return False
        if self._web_view is not None:
            return True
        try:
            self._create_web_view()
        except Exception:
            self._show_error(
                "initialisation",
                "The secure local preview service could not be started.",
            )
            return False
        self._idle_label.hide()
        return True

    def _create_web_view(self) -> None:
        server = PreviewLoopbackServer()
        web_view = None
        try:
            web_view = QWebEngineView(self.map_host)
            page = RestrictedPreviewPage(server.port, web_view)
            web_view.setPage(page)
            bridge = MapPreviewBridge(web_view)
            channel = QWebChannel(page)
            channel.registerObject("tasmeadBridge", bridge)
            page.setWebChannel(channel)
            page._tasmead_channel = channel
            bridge.shell_ready.connect(self._on_shell_ready)
            bridge.render_acknowledged.connect(self._on_render_acknowledged)
            bridge.render_failed.connect(self._on_render_failed)
            bridge.presentation_state_changed.connect(
                self._on_presentation_state_changed
            )
            bridge.security_policy_violation.connect(
                self._on_security_policy_violation
            )
            web_view.loadFinished.connect(self._on_load_finished)
            if hasattr(page, "renderProcessTerminated"):
                page.renderProcessTerminated.connect(
                    self._on_render_process_terminated
                )
        except Exception:
            if web_view is not None:
                web_view.deleteLater()
            server.stop()
            raise
        self._server = server
        self._web_view = web_view
        self._bridge = bridge
        self.map_layout.addWidget(web_view)

    def set_scene(self, scene: PreviewScene, api_key: str) -> bool:
        if not scene.traces:
            raise ValueError("A preview scene requires at least one trace.")
        self._scene = scene
        self._committed_scene = scene
        self._api_key = str(api_key).strip()
        self.trace_selector.blockSignals(True)
        self.trace_selector.clear()
        for trace in scene.traces:
            self.trace_selector.addItem(trace.label, trace.trace_id)
        self.trace_selector.blockSignals(False)
        self.trace_selector.setCurrentIndex(0)
        self._load_selected_adjustment()
        if not self._ensure_web_view():
            self._show_error(
                "dependency",
                "PyQt6-WebEngine is unavailable or could not start. KML export remains available without preview.",
            )
            return False
        self._reload_shell()
        return True

    @property
    def scene(self) -> PreviewScene | None:
        return self._scene

    def _begin_page_generation(self) -> int:
        self._page_generation = (
            1
            if self._page_generation >= 2_147_483_647
            else self._page_generation + 1
        )
        self._csp_failed_generation = None
        self._csp_diagnostics.clear()
        return self._page_generation

    def _reload_shell(self) -> None:
        self._stop_presentation_watchdog(clear_activity=True)
        if not WEBENGINE_AVAILABLE or self._web_view is None or self._server is None:
            return
        self._begin_page_generation()
        self._shell_ready = False
        self._acknowledged_revision = -1
        self._set_apply_enabled(False)
        self.retry_button.hide()
        self.open_settings_button.hide()
        self.status_label.setText("Initialising secure local preview…")
        page_url = QUrl(self._server.url)
        page_url.setFragment(f"generation={self._page_generation}")
        self._shell_ready_timer.start()
        self._web_view.load(page_url)

    def _on_load_finished(self, succeeded: bool) -> None:
        if not succeeded:
            self._shell_ready_timer.stop()
            self._show_error(
                "initialisation",
                "The secure local preview shell could not be loaded.",
            )

    def _on_shell_ready_timeout(self) -> None:
        if self._shell_ready:
            return
        self._show_error(
            "initialisation",
            "The secure local preview shell did not initialise. Retry the preview; KML export remains available.",
        )

    def _on_shell_ready(self, generation: int) -> None:
        if generation != self._page_generation:
            return
        self._shell_ready_timer.stop()
        self._shell_ready = True
        if not self._api_key:
            self._show_error("missing-key", "No Google Maps API key is configured.")
            return
        self.status_label.setText("Loading Google Maps 3D…")
        self._run_javascript(
            f"window.tasmead.loadGoogleMaps({json.dumps(self._api_key)});"
        )
        self._schedule_render(immediate=True)

    def _schedule_render(self, *, immediate: bool = False) -> None:
        if self._csp_failed_generation == self._page_generation:
            self._set_apply_enabled(False)
            return
        self._revision += 1
        self._acknowledged_revision = -1
        self._set_apply_enabled(False)
        self.status_label.setText("Updating preview…")
        self.retry_button.hide()
        self.open_settings_button.hide()
        if immediate:
            self._render_timer.stop()
            self._render_scene()
        else:
            self._render_timer.start()

    def _render_scene(self) -> None:
        if (
            not self._shell_ready
            or self._scene is None
            or self._csp_failed_generation == self._page_generation
        ):
            return
        encoded = json.dumps(
            preview_payload(self._scene),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        byte_count = len(encoded.encode("utf-8"))
        if byte_count > _MAX_PAYLOAD_BYTES:
            self._show_error(
                "oversized",
                "This scene is too large for the embedded preview. No vertices were simplified; export remains available.",
            )
            return
        chunks = [encoded[index:index + _CHUNK_SIZE] for index in range(0, len(encoded), _CHUNK_SIZE)] or [""]
        revision = self._revision
        self._run_javascript(f"window.tasmead.beginScene({revision}, {len(chunks)});")
        for chunk in chunks:
            self._run_javascript(
                f"window.tasmead.appendSceneChunk({revision}, {json.dumps(chunk)});"
            )
        self._run_javascript(f"window.tasmead.finishScene({revision});")

    def _run_javascript(self, source: str) -> None:
        if self._web_view is not None:
            self._web_view.page().runJavaScript(source)

    def _on_render_acknowledged(self, generation: int, revision: int) -> None:
        if (
            generation != self._page_generation
            or revision != self._revision
            or self._csp_failed_generation == generation
        ):
            return
        self._stop_presentation_watchdog(clear_activity=True)
        QTimer.singleShot(0, self._refresh_web_presentation)
        self._acknowledged_revision = revision
        self.status_label.setText(
            "Preview matches the quantized WGS84 geometry that will be exported. "
            "The magenta anchor is a preview-only guide."
        )
        self.retry_button.hide()
        self.open_settings_button.hide()
        self._set_apply_enabled(True)

    def _on_render_failed(self, generation: int, kind: str, message: str) -> None:
        if (
            generation != self._page_generation
            or self._csp_failed_generation == generation
        ):
            return
        allowed = {
            "authentication", "network", "render", "transport",
            "initialisation", "dependency", "oversized", "missing-key",
            "security-policy",
        }
        self._show_error(kind if kind in allowed else "render", message)

    def _on_presentation_state_changed(
        self,
        generation: int,
        revision: int,
        is_steady: bool,
    ) -> None:
        if (
            generation != self._page_generation
            or revision != self._revision
            or self._csp_failed_generation == generation
        ):
            return
        self._presentation_active = not is_steady
        if is_steady:
            self._presentation_timer.stop()
            QTimer.singleShot(0, self._refresh_web_presentation)
        elif self.isVisible() and self._web_view is not None:
            self._presentation_timer.start()

    def _refresh_web_presentation(self) -> None:
        if self._web_view is not None and self.isVisible():
            self._web_view.update()

    def _stop_presentation_watchdog(self, *, clear_activity: bool) -> None:
        self._presentation_timer.stop()
        if clear_activity:
            self._presentation_active = False

    def showEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        super().showEvent(event)
        if self._presentation_active and self._web_view is not None:
            self._presentation_timer.start()
        QTimer.singleShot(0, self._refresh_web_presentation)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt virtual method
        self._presentation_timer.stop()
        super().hideEvent(event)

    def _on_security_policy_violation(
        self,
        generation: int,
        disposition: str,
        directive: str,
        blocked_location: str,
        source_location: str,
        line: int,
        column: int,
        duplicate_count: int,
    ) -> None:
        if generation != self._page_generation:
            return
        disposition = _normalise_csp_disposition(disposition)
        directive = _normalise_csp_directive(directive)
        blocked_location = _sanitise_diagnostic_location(blocked_location)
        source_location = _sanitise_diagnostic_location(source_location)
        line = _clamped_diagnostic_number(
            line, minimum=0, maximum=2_147_483_647
        )
        column = _clamped_diagnostic_number(
            column, minimum=0, maximum=2_147_483_647
        )
        incoming_count = _clamped_diagnostic_number(
            duplicate_count, minimum=1, maximum=1_000_000
        )
        key = (disposition, directive, blocked_location, source_location)
        if (
            key not in self._csp_diagnostics
            and len(self._csp_diagnostics) >= _MAX_CSP_DIAGNOSTICS - 2
        ):
            key = (disposition, "other", "other", "other")
            directive = blocked_location = source_location = "other"
            line = column = 0
        previous_count = self._csp_diagnostics.get(key, 0)
        observed_count = min(1_000_000, max(incoming_count, previous_count + 1))
        self._csp_diagnostics[key] = observed_count
        if disposition != "enforce":
            return

        self._csp_failed_generation = generation
        self._acknowledged_revision = -1
        details = [f"directive {directive}", f"blocked {blocked_location}"]
        if source_location != "other":
            details.append(f"source {source_location}")
        if line:
            position = f"line {line}"
            if column:
                position += f", column {column}"
            details.append(position)
        if observed_count > 1:
            details.append(f"repeated {observed_count} times")
        self._show_error(
            "security-policy",
            "The embedded map was blocked by its security policy ("
            + "; ".join(details)
            + "). Retry the preview; KML export remains available.",
        )

    def _on_render_process_terminated(self, *_args: Any) -> None:
        self._show_error(
            "render",
            "The embedded map renderer stopped unexpectedly. Check WebGL support and retry.",
        )

    def _show_error(self, kind: str, message: str) -> None:
        self._stop_presentation_watchdog(clear_activity=True)
        self._set_apply_enabled(False)
        self.status_label.setText(message)
        self.retry_button.setVisible(WEBENGINE_AVAILABLE)
        self.open_settings_button.setVisible(
            kind in {"authentication", "missing-key"}
        )

    def set_api_key_and_retry(self, api_key: str) -> None:
        key = str(api_key).strip()
        if not key:
            return
        self._api_key = key
        self._reload_shell()

    def _set_apply_enabled(self, enabled: bool) -> None:
        self.apply_button.setEnabled(enabled)
        self.apply_export_button.setEnabled(enabled)

    def _selected_trace_changed(self, _index: int) -> None:
        self._load_selected_adjustment()

    def _load_selected_adjustment(self) -> None:
        if self._scene is None or not (0 <= self.trace_selector.currentIndex() < len(self._scene.traces)):
            return
        adjustment = self._scene.traces[self.trace_selector.currentIndex()].adjustment
        self._loading_controls = True
        try:
            for key, control in self.axis_controls.items():
                control.setValue(float(getattr(adjustment, key)))
        finally:
            self._loading_controls = False
        self.up_warning.setVisible(adjustment.up_m != 0)

    def _adjustment_changed(self, _value: float) -> None:
        if self._loading_controls or self._scene is None:
            return
        index = self.trace_selector.currentIndex()
        if not 0 <= index < len(self._scene.traces):
            return
        adjustment = TraceAdjustment(
            east_m=self.axis_controls["east_m"].value(),
            north_m=self.axis_controls["north_m"].value(),
            up_m=self.axis_controls["up_m"].value(),
            yaw_deg=self.axis_controls["yaw_deg"].value(),
        )
        traces = list(self._scene.traces)
        traces[index] = traces[index].with_adjustment(adjustment)
        self._scene = PreviewScene(tuple(traces))
        self.up_warning.setVisible(adjustment.up_m != 0)
        self._schedule_render()

    def _reset_selected(self) -> None:
        if self._scene is None:
            return
        index = self.trace_selector.currentIndex()
        if not 0 <= index < len(self._scene.traces):
            return
        traces = list(self._scene.traces)
        traces[index] = traces[index].with_adjustment(TraceAdjustment())
        self._scene = PreviewScene(tuple(traces))
        self._load_selected_adjustment()
        self._schedule_render(immediate=True)

    def _reset_all(self) -> None:
        if self._scene is None:
            return
        self._scene = PreviewScene(
            tuple(trace.with_adjustment(TraceAdjustment()) for trace in self._scene.traces)
        )
        self._load_selected_adjustment()
        self._schedule_render(immediate=True)

    def fit_traces(self) -> None:
        self._run_javascript("window.tasmead.fitScene();")

    def _apply(self) -> None:
        if (
            self._scene is not None
            and self._acknowledged_revision == self._revision
            and self._csp_failed_generation != self._page_generation
        ):
            self._committed_scene = self._scene
            self.scene_applied.emit(self._scene)

    def _apply_and_export(self) -> None:
        if (
            self._scene is not None
            and self._acknowledged_revision == self._revision
            and self._csp_failed_generation != self._page_generation
        ):
            self._committed_scene = self._scene
            self.scene_export_requested.emit(self._scene)

    def set_fullscreen_state(self, fullscreen: bool) -> None:
        self.fullscreen_button.blockSignals(True)
        self.fullscreen_button.setChecked(fullscreen)
        self.fullscreen_button.setText("Exit full screen" if fullscreen else "Full screen")
        self.fullscreen_button.blockSignals(False)

    def shutdown(self) -> None:
        self._render_timer.stop()
        self._shell_ready_timer.stop()
        self._stop_presentation_watchdog(clear_activity=True)
        if self._web_view is not None:
            web_view = self._web_view
            self._web_view = None
            self._bridge = None
            web_view.stop()
            self.map_layout.removeWidget(web_view)
            web_view.deleteLater()
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._shell_ready = False
        self._csp_failed_generation = None
        self._csp_diagnostics.clear()
        self._api_key = ""
        self._scene = None
        self._committed_scene = None


__all__ = [
    "MapPreviewBridge",
    "MapPreviewWidget",
    "PreviewLoopbackServer",
    "WEBENGINE_AVAILABLE",
]
