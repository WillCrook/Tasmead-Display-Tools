"""Shared native controls and Google Maps 3D WebEngine preview workspace."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from services.geodesy import inverse_distance_bearing
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
_GENERATION_QUERY_PATTERN = re.compile(r"generation=([1-9][0-9]{0,9})")
_MAX_GENERATION = 2_147_483_647
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


def _generation_from_request_target(
    request_target: str,
    preview_path: str,
) -> int | None:
    try:
        request = urlsplit(str(request_target))
    except (TypeError, ValueError):
        return None
    generation_match = _GENERATION_QUERY_PATTERN.fullmatch(request.query)
    if (
        request.scheme
        or request.netloc
        or request.fragment
        or request.path != preview_path
        or generation_match is None
    ):
        return None
    generation = int(generation_match.group(1))
    return generation if generation <= _MAX_GENERATION else None


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
    const generationMatch = window.location.search.match(/^\?generation=([1-9][0-9]{0,9})$/);
    const initialGeneration = generationMatch
      ? Math.min(2147483647, Number(generationMatch[1]))
      : 0;
    const state = {
      bridge: null, googleReady: false, chunks: new Map(), pending: null,
      map: null, payload: null, latestRevision: -1, renderTimeout: null,
      generation: initialGeneration, cspFailed: false, cspDiagnostics: new Map(),
      libraries: null, renderedTraces: new Map(), awaitingRevision: null,
      toolMode: 'navigate', selectedTraceId: null,
      measurement: {payload: {points: []}, line: null, markers: []}
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
    function selectedTrace(payload) {
      const traces = payload && Array.isArray(payload.traces) ? payload.traces : [];
      if (!traces.length) return null;
      return traces.find(trace => String(trace.id) === state.selectedTraceId)
        || traces[0];
    }
    function allCoordinates(trace) {
      const points = [];
      for (const geometry of trace.geometries || []) {
        points.push(...(geometry.coordinates || []));
      }
      return points;
    }
    function fitScene() {
      if (!state.map || !state.payload) return;
      const trace = selectedTrace(state.payload);
      if (!trace) return;
      const points = allCoordinates(trace);
      if (!points.length) return;
      const anchor = trace.anchor;
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
    function removeElement(element) {
      if (element && typeof element.remove === 'function') element.remove();
    }
    function removeRenderedTrace(rendered) {
      for (const item of rendered.geometries.values()) removeElement(item.element);
      removeElement(rendered.anchor);
    }
    function setElementAttached(element, attached) {
      if (!element || !state.map) return;
      if (attached && element.parentElement !== state.map) {
        state.map.append(element);
      } else if (!attached && element.parentElement === state.map) {
        removeElement(element);
      }
    }
    function applyTraceVisibility() {
      if (!state.map || !state.payload) return;
      const trace = selectedTrace(state.payload);
      state.selectedTraceId = trace ? String(trace.id) : null;
      for (const [traceId, rendered] of state.renderedTraces) {
        const visible = traceId === state.selectedTraceId;
        for (const item of rendered.geometries.values()) {
          setElementAttached(item.element, visible);
        }
        setElementAttached(rendered.anchor, visible);
      }
    }
    function updateGeometry(element, geometry) {
      const {AltitudeMode} = state.libraries;
      element.altitudeMode = altitudeMode(AltitudeMode, geometry.altitudeMode);
      element.strokeColor = geometry.style.strokeColor;
      element.strokeWidth = geometry.style.strokeWidth;
      element.path = geometry.coordinates;
      if (geometry.type === 'polyline') {
        element.extruded = Boolean(geometry.extrude);
        element.geodesic = Boolean(geometry.tessellate);
      } else {
        // KML's default PolyStyle is opaque white when no fill is supplied.
        element.fillColor = geometry.style.fillColor || '#ffffffff';
      }
    }
    function createGeometry(geometry) {
      const {Polyline3DElement, Polygon3DElement} = state.libraries;
      const element = geometry.type === 'polyline'
        ? new Polyline3DElement()
        : new Polygon3DElement();
      updateGeometry(element, geometry);
      state.map.append(element);
      return element;
    }
    function createPin(options) {
      const {PinElement} = state.libraries;
      return PinElement ? new PinElement(options) : null;
    }
    function updateAnchor(rendered, trace) {
      const {AltitudeMode, Marker3DElement} = state.libraries;
      if (!Marker3DElement) return;
      if (!rendered.anchor) {
        rendered.anchor = new Marker3DElement({drawsWhenOccluded: true});
        const pin = createPin({
          background: '#ff00ff', borderColor: '#ffffff',
          glyphColor: '#ffffff', glyphText: 'A'
        });
        if (pin && rendered.anchor.append) rendered.anchor.append(pin);
        state.map.append(rendered.anchor);
      }
      rendered.anchor.position = {
        lat: trace.anchor.lat,
        lng: trace.anchor.lng,
        altitude: trace.anchor.altitude
      };
      rendered.anchor.altitudeMode = altitudeMode(
        AltitudeMode, trace.anchor.altitudeMode
      );
      rendered.anchor.label = trace.anchor.label;
    }
    function reconcileScene(payload) {
      const retainedTraceIds = new Set();
      for (const trace of payload.traces) {
        const traceId = String(trace.id);
        retainedTraceIds.add(traceId);
        let rendered = state.renderedTraces.get(traceId);
        if (!rendered) {
          rendered = {geometries: new Map(), anchor: null};
          state.renderedTraces.set(traceId, rendered);
        }
        const retainedGeometryIds = new Set();
        for (const geometry of trace.geometries) {
          const geometryId = String(geometry.id);
          retainedGeometryIds.add(geometryId);
          let item = rendered.geometries.get(geometryId);
          if (!item || item.type !== geometry.type) {
            if (item) removeElement(item.element);
            item = {type: geometry.type, element: createGeometry(geometry)};
            rendered.geometries.set(geometryId, item);
          } else {
            updateGeometry(item.element, geometry);
          }
        }
        for (const [geometryId, item] of rendered.geometries) {
          if (!retainedGeometryIds.has(geometryId)) {
            removeElement(item.element);
            rendered.geometries.delete(geometryId);
          }
        }
        updateAnchor(rendered, trace);
      }
      for (const [traceId, rendered] of state.renderedTraces) {
        if (!retainedTraceIds.has(traceId)) {
          removeRenderedTrace(rendered);
          state.renderedTraces.delete(traceId);
        }
      }
      applyTraceVisibility();
    }
    function reconcileMeasurement(payload) {
      state.measurement.payload = payload && Array.isArray(payload.points)
        ? payload : {points: []};
      if (!state.map || !state.libraries) return;
      const points = state.measurement.payload.points;
      const {AltitudeMode, Marker3DElement, Polyline3DElement} = state.libraries;
      if (points.length >= 2) {
        if (!state.measurement.line) {
          state.measurement.line = new Polyline3DElement();
          state.map.append(state.measurement.line);
        }
        state.measurement.line.path = points;
        state.measurement.line.altitudeMode = altitudeMode(
          AltitudeMode, 'CLAMP_TO_GROUND'
        );
        state.measurement.line.strokeColor = '#22d3eeff';
        state.measurement.line.strokeWidth = 5;
        state.measurement.line.geodesic = true;
      } else if (state.measurement.line) {
        removeElement(state.measurement.line);
        state.measurement.line = null;
      }
      while (state.measurement.markers.length > points.length) {
        removeElement(state.measurement.markers.pop());
      }
      if (!Marker3DElement) return;
      for (let index = 0; index < points.length; index += 1) {
        let marker = state.measurement.markers[index];
        if (!marker) {
          marker = new Marker3DElement({drawsWhenOccluded: true});
          const pin = createPin({
            background: '#0891b2', borderColor: '#ffffff',
            glyphColor: '#ffffff', glyphText: String(index + 1)
          });
          if (pin && marker.append) marker.append(pin);
          state.measurement.markers.push(marker);
          state.map.append(marker);
        }
        marker.position = points[index];
        marker.altitudeMode = altitudeMode(AltitudeMode, 'CLAMP_TO_GROUND');
        marker.label = 'Measurement point ' + String(index + 1);
      }
    }
    function acknowledgeRevisionIfReady(isSteady) {
      const revision = state.latestRevision;
      if (state.bridge && revision >= 0) {
        state.bridge.presentationStateChanged(
          state.generation, revision, Boolean(isSteady)
        );
      }
      if (!isSteady || state.awaitingRevision !== revision) return;
      state.awaitingRevision = null;
      if (state.renderTimeout) clearTimeout(state.renderTimeout);
      state.renderTimeout = null;
      setStatus('', true);
      if (state.bridge) {
        state.bridge.renderAcknowledged(state.generation, revision);
      }
    }
    function ensureMap(payload) {
      if (state.map) return false;
      const {Map3DElement, MapMode} = state.libraries;
      const map = new Map3DElement({
        center: {
          lat: payload.traces[0].anchor.lat,
          lng: payload.traces[0].anchor.lng,
          altitude: 0
        },
        range: 1500,
        tilt: 60,
        heading: 0,
        mode: (MapMode && MapMode.HYBRID) || 'HYBRID'
      });
      map.addEventListener('gmp-error', () => fail(
        'render',
        'Google Maps 3D could not initialise. Check WebGL support and the Google project configuration.'
      ));
      map.addEventListener('gmp-map-id-error', () => fail(
        'authentication',
        'Google rejected the map configuration. Check the API key, restrictions, Maps JavaScript API access, and billing.'
      ));
      map.addEventListener('gmp-steadychange', event => {
        if (!state.cspFailed) acknowledgeRevisionIfReady(Boolean(event.isSteady));
      });
      map.addEventListener('gmp-click', event => {
        if (state.toolMode === 'navigate' || !event.position) return;
        if (typeof event.preventDefault === 'function') event.preventDefault();
        const latitude = Number(event.position.lat);
        const longitude = Number(event.position.lng);
        if (state.bridge && Number.isFinite(latitude) && Number.isFinite(longitude)) {
          state.bridge.mapClicked(state.generation, latitude, longitude);
        }
      });
      host.append(map);
      state.map = map;
      reconcileMeasurement(state.measurement.payload);
      return true;
    }
    async function render(payload, revision, fitRequested=false) {
      if (state.cspFailed) return;
      if (!state.googleReady) {
        state.pending = {payload, revision, fitRequested};
        return;
      }
      try {
        if (state.bridge) state.bridge.renderStarted(state.generation, Number(revision));
        if (!state.map) setStatus('Opening KML preview…');
        if (!state.libraries) {
          const [maps3d, markerLibrary] = await Promise.all([
            google.maps.importLibrary('maps3d'),
            google.maps.importLibrary('marker')
          ]);
          state.libraries = {...maps3d, PinElement: markerLibrary.PinElement};
        }
        if (state.cspFailed || Number(revision) !== state.latestRevision) return;
        const {Map3DElement, Polyline3DElement, Polygon3DElement} = state.libraries;
        if (!Map3DElement || !Polyline3DElement || !Polygon3DElement) {
          throw new Error('Required Maps 3D elements are unavailable.');
        }
        if (!payload || payload.version !== 2 || !Array.isArray(payload.traces) || !payload.traces.length) {
          throw new Error('Unsupported preview payload.');
        }
        const mapCreated = ensureMap(payload);
        state.awaitingRevision = Number(revision);
        state.payload = payload;
        reconcileScene(payload);
        if (mapCreated || Boolean(fitRequested)) fitScene();
        if (state.renderTimeout) clearTimeout(state.renderTimeout);
        state.renderTimeout = setTimeout(() => {
          if (state.awaitingRevision === Number(revision) && Number(revision) === state.latestRevision) {
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
      finishScene(revision, fitRequested=false) {
        const key = Number(revision);
        const item = state.chunks.get(key);
        if (!item || item.parts.length !== item.total) {
          fail('transport', 'The preview scene was not transferred completely. Try again.');
          return;
        }
        state.chunks.delete(key);
        state.latestRevision = key;
        if (state.cspFailed) return;
        try { render(JSON.parse(item.parts.join('')), key, Boolean(fitRequested)); }
        catch (_) { fail('transport', 'The preview scene data was invalid.'); }
      },
      setToolMode(mode) {
        const requested = String(mode);
        state.toolMode = ['navigate', 'measure', 'move-anchor'].includes(requested)
          ? requested : 'navigate';
        host.dataset.toolMode = state.toolMode;
      },
      setMeasurement(payload) { reconcileMeasurement(payload); },
      setSelectedTrace(traceId, fitRequested=false) {
        state.selectedTraceId = String(traceId);
        applyTraceVisibility();
        if (Boolean(fitRequested)) fitScene();
      },
      fitScene
    };
    window.tasmeadGoogleReady = () => {
      state.googleReady = true;
      if (!state.cspFailed && state.pending) {
        const pending = state.pending; state.pending = null;
        render(pending.payload, pending.revision, pending.fitRequested);
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
        generation = _generation_from_request_target(
            self.path,
            server.preview_path,  # type: ignore[attr-defined]
        )
        if generation is None:
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

    def url_for_generation(self, generation: int) -> QUrl:
        value = int(generation)
        if not 1 <= value <= _MAX_GENERATION:
            raise ValueError("Preview generation is outside the supported range.")
        url = QUrl(self.url)
        url.setQuery(f"generation={value}")
        return url

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
    render_started = pyqtSignal(int, int)
    render_acknowledged = pyqtSignal(int, int)
    render_failed = pyqtSignal(int, str, str)
    presentation_state_changed = pyqtSignal(int, int, bool)
    security_policy_violation = pyqtSignal(
        int, str, str, str, str, int, int, int
    )
    map_clicked = pyqtSignal(int, float, float)

    @pyqtSlot(int)
    def shellReady(self, generation: int) -> None:  # noqa: N802 - called from JavaScript
        self.shell_ready.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            )
        )

    @pyqtSlot(int, int)
    def renderStarted(self, generation: int, revision: int) -> None:  # noqa: N802
        self.render_started.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            ),
            _clamped_diagnostic_number(
                revision, minimum=0, maximum=2_147_483_647
            ),
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

    @pyqtSlot(int, float, float)
    def mapClicked(  # noqa: N802 - called from JavaScript
        self,
        generation: int,
        latitude: float,
        longitude: float,
    ) -> None:
        try:
            latitude_value = float(latitude)
            longitude_value = float(longitude)
        except (TypeError, ValueError, OverflowError):
            return
        if (
            not math.isfinite(latitude_value)
            or not math.isfinite(longitude_value)
            or not -90.0 <= latitude_value <= 90.0
            or not -180.0 <= longitude_value <= 180.0
        ):
            return
        self.map_clicked.emit(
            _clamped_diagnostic_number(
                generation, minimum=0, maximum=2_147_483_647
            ),
            latitude_value,
            longitude_value,
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
        self._failed_generation: int | None = None
        self._csp_failed_generation: int | None = None
        self._csp_diagnostics: dict[tuple[str, str, str, str], int] = {}
        self._shell_ready = False
        self._session_reusable = False
        self._web_runtime_recreation_required = False
        self._server: PreviewLoopbackServer | None = None
        self._web_view = None
        self._bridge: MapPreviewBridge | None = None
        self._loading_controls = False
        self._loading_active = False
        self._presentation_active = False
        self._tool_mode = "navigate"
        self._measurement_points: list[tuple[float, float]] = []
        self._fit_scene_on_next_render = False

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
        title = QLabel("Google Maps 3D preview")
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
        self.map_layout = QStackedLayout(self.map_host)
        self.map_layout.setContentsMargins(0, 0, 0, 0)
        self.map_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        splitter.addWidget(self.map_host)

        self.loading_screen = QFrame()
        self.loading_screen.setObjectName("previewLoadingScreen")
        loading_layout = QVBoxLayout(self.loading_screen)
        loading_layout.setContentsMargins(24, 24, 24, 24)
        loading_layout.addStretch()
        loading_card = QFrame()
        loading_card.setObjectName("previewLoadingCard")
        loading_card.setMaximumWidth(420)
        loading_card_layout = QVBoxLayout(loading_card)
        loading_card_layout.setContentsMargins(28, 24, 28, 24)
        loading_card_layout.setSpacing(10)
        self.loading_title = QLabel("Preview")
        self.loading_title.setObjectName("dialogTitle")
        self.loading_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_card_layout.addWidget(self.loading_title)
        self.loading_message = QLabel()
        self.loading_message.setObjectName("mutedText")
        self.loading_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_message.setWordWrap(True)
        loading_card_layout.addWidget(self.loading_message)
        self.loading_progress = QProgressBar()
        self.loading_progress.setAccessibleName("Preview loading progress")
        self.loading_progress.setRange(0, 0)
        self.loading_progress.setTextVisible(False)
        loading_card_layout.addWidget(self.loading_progress)
        loading_layout.addWidget(
            loading_card, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        loading_layout.addStretch()
        self.map_layout.addWidget(self.loading_screen)

        controls = QFrame()
        controls.setObjectName("workspacePanel")
        controls.setMinimumWidth(300)
        controls.setMaximumWidth(380)
        panel = QVBoxLayout(controls)
        panel.setContentsMargins(16, 16, 16, 16)
        panel.setSpacing(10)
        panel.addWidget(QLabel("KML file", objectName="panelTitle"))
        self.trace_selector = QComboBox()
        self.trace_selector.setAccessibleName("KML file to preview")
        self.trace_selector.currentIndexChanged.connect(self._selected_trace_changed)
        panel.addWidget(self.trace_selector)

        panel.addWidget(QLabel("Tools", objectName="panelTitle"))
        self.tool_mode_control = QFrame()
        self.tool_mode_control.setAccessibleName("Map tool")
        tool_row = QHBoxLayout(self.tool_mode_control)
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.setSpacing(8)
        self.navigate_tool_button = QRadioButton("Navigate")
        self.measure_tool_button = QRadioButton("Measure")
        self.move_anchor_tool_button = QRadioButton("Move anchor")
        self.tool_mode_group = QButtonGroup(self)
        self.tool_mode_group.setExclusive(True)
        for button, mode in (
            (self.navigate_tool_button, "navigate"),
            (self.measure_tool_button, "measure"),
            (self.move_anchor_tool_button, "move-anchor"),
        ):
            self.tool_mode_group.addButton(button)
            button.toggled.connect(
                lambda checked, selected_mode=mode: self._tool_mode_changed(
                    selected_mode,
                    checked,
                )
            )
            tool_row.addWidget(button)
        panel.addWidget(self.tool_mode_control)

        self.tool_help_label = QLabel("Drag the map to move around.")
        self.tool_help_label.setObjectName("mutedText")
        self.tool_help_label.setWordWrap(True)
        panel.addWidget(self.tool_help_label)

        self.measurement_label = QLabel("Click two or more points to measure distance.")
        self.measurement_label.setObjectName("mutedText")
        self.measurement_label.setWordWrap(True)
        self.measurement_label.setAccessibleName("Distance measurement")
        panel.addWidget(self.measurement_label)
        measurement_actions = QHBoxLayout()
        self.undo_measurement_button = QPushButton("Undo point")
        self.clear_measurement_button = QPushButton("Clear")
        self.undo_measurement_button.clicked.connect(self._undo_measurement_point)
        self.clear_measurement_button.clicked.connect(self._clear_measurement)
        measurement_actions.addWidget(self.undo_measurement_button)
        measurement_actions.addWidget(self.clear_measurement_button)
        panel.addLayout(measurement_actions)
        self.navigate_tool_button.setChecked(True)

        self.axis_controls: dict[str, QDoubleSpinBox] = {}
        specifications = (
            ("east_m", "East / West", -100_000.0, 100_000.0, " m"),
            ("north_m", "North / South", -100_000.0, 100_000.0, " m"),
            ("up_m", "Height", -20_000.0, 20_000.0, " m"),
            ("yaw_deg", "Rotation", -180.0, 180.0, "°"),
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
            "Changing the height raises or lowers items that normally follow the "
            "ground. Lowered items may be hidden by the terrain."
        )
        self.up_warning.setObjectName("mutedText")
        self.up_warning.setWordWrap(True)
        self.up_warning.hide()
        panel.addWidget(self.up_warning)

        reset_row = QHBoxLayout()
        self.reset_selected_button = QPushButton("Reset selected KML")
        self.reset_all_button = QPushButton("Reset all")
        self.reset_selected_button.clicked.connect(self._reset_selected)
        self.reset_all_button.clicked.connect(self._reset_all)
        reset_row.addWidget(self.reset_selected_button)
        reset_row.addWidget(self.reset_all_button)
        panel.addLayout(reset_row)

        self.fit_button = QPushButton("Recentre view")
        self.fit_button.clicked.connect(self.fit_traces)
        panel.addWidget(self.fit_button)

        self.status_label = QLabel("Choose a KML file to begin.")
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

        self.apply_button = QPushButton("Apply changes")
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
        self._render_measurement_state()

        self._idle_message = (
            "The map will open when you choose View preview."
            if WEBENGINE_AVAILABLE
            else
                "PyQt6-WebEngine is not installed. Install the dependencies in "
                "requirements.txt, then restart the application."
        )
        self._show_idle_state()

    def _show_loading(self, message: str) -> None:
        self._loading_active = True
        self.loading_title.setText("Loading preview")
        self.loading_message.setText(message)
        self.loading_progress.show()
        self.loading_screen.show()
        self.loading_screen.raise_()

    def _update_loading(self, message: str) -> None:
        if self._loading_active:
            self.loading_message.setText(message)

    def _hide_loading(self) -> None:
        self._loading_active = False
        self.loading_screen.hide()

    def _show_idle_state(self) -> None:
        self._loading_active = False
        self.loading_title.setText("Preview")
        self.loading_message.setText(self._idle_message)
        self.loading_progress.hide()
        self.loading_screen.show()
        self.loading_screen.raise_()

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
        return True

    def _create_web_view(self) -> None:
        server = PreviewLoopbackServer()
        web_view = None
        try:
            web_view = QWebEngineView(self.map_host)
            page = RestrictedPreviewPage(server.port, web_view)
            page.setBackgroundColor(QColor("#0f172a"))
            web_view.setPage(page)
            bridge = MapPreviewBridge(web_view)
            channel = QWebChannel(page)
            channel.registerObject("tasmeadBridge", bridge)
            page.setWebChannel(channel)
            page._tasmead_channel = channel
            bridge.shell_ready.connect(self._on_shell_ready)
            bridge.render_started.connect(self._on_render_started)
            bridge.render_acknowledged.connect(self._on_render_acknowledged)
            bridge.render_failed.connect(self._on_render_failed)
            bridge.presentation_state_changed.connect(
                self._on_presentation_state_changed
            )
            bridge.security_policy_violation.connect(
                self._on_security_policy_violation
            )
            bridge.map_clicked.connect(self._on_map_clicked)
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
        if not self.loading_screen.isHidden():
            self.loading_screen.raise_()

    def _dispose_web_runtime(self) -> None:
        self._shell_ready_timer.stop()
        self._stop_presentation_watchdog(clear_activity=True)
        self._fit_scene_on_next_render = False
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
        self._session_reusable = False

    def set_scene(self, scene: PreviewScene, api_key: str) -> bool:
        if not scene.traces:
            raise ValueError("A preview scene requires at least one trace.")
        previous_selected_trace_id = self.trace_selector.currentData(
            Qt.ItemDataRole.UserRole
        )
        previous_trace_ids = (
            frozenset(trace.trace_id for trace in self._scene.traces)
            if self._scene is not None
            else frozenset()
        )
        next_trace_ids = frozenset(trace.trace_id for trace in scene.traces)
        trace_identity_changed = previous_trace_ids != next_trace_ids
        selected_trace_changed = (
            isinstance(previous_selected_trace_id, str)
            and previous_selected_trace_id != scene.traces[0].trace_id
        )
        key = str(api_key).strip()
        reuse_session = (
            self._session_reusable
            and self._shell_ready
            and self._web_view is not None
            and self._server is not None
            and key == self._api_key
        )
        if not reuse_session:
            self._show_loading("Preparing preview…")
        if trace_identity_changed or selected_trace_changed:
            self._clear_measurement()
        self._scene = scene
        self._committed_scene = scene
        self._api_key = key
        self.trace_selector.blockSignals(True)
        self.trace_selector.clear()
        for trace in scene.traces:
            self.trace_selector.addItem(trace.label, trace.trace_id)
        self.trace_selector.blockSignals(False)
        self.trace_selector.setCurrentIndex(0)
        self._load_selected_adjustment()
        self._sync_selected_trace(fit_scene=False)
        if not self._ensure_web_view():
            self._show_error(
                "dependency",
                "PyQt6-WebEngine is unavailable or could not start. KML export remains available without preview.",
            )
            return False
        if reuse_session:
            self._schedule_render(
                immediate=True,
                fit_scene=trace_identity_changed or selected_trace_changed,
            )
            return True
        return self._reload_shell()

    @property
    def scene(self) -> PreviewScene | None:
        return self._scene

    def _begin_page_generation(self) -> int:
        self._page_generation = (
            1
            if self._page_generation >= _MAX_GENERATION
            else self._page_generation + 1
        )
        self._failed_generation = None
        self._csp_failed_generation = None
        self._csp_diagnostics.clear()
        return self._page_generation

    def _reload_shell(self) -> bool:
        self._stop_presentation_watchdog(clear_activity=True)
        self._session_reusable = False
        if not WEBENGINE_AVAILABLE:
            return False
        if self._web_runtime_recreation_required:
            self._dispose_web_runtime()
            try:
                self._create_web_view()
            except Exception:
                self._show_error(
                    "initialisation",
                    "The secure local preview service could not be restarted.",
                )
                return False
            self._web_runtime_recreation_required = False
        if self._web_view is None or self._server is None:
            return False
        self._begin_page_generation()
        self._shell_ready = False
        self._acknowledged_revision = -1
        self._set_apply_enabled(False)
        self.retry_button.hide()
        self.open_settings_button.hide()
        self.status_label.setText("Opening map preview…")
        self._show_loading("Preparing preview…")
        page_url = self._server.url_for_generation(self._page_generation)
        self._shell_ready_timer.start()
        self._web_view.load(page_url)
        return True

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
        if (
            generation != self._page_generation
            or self._failed_generation == generation
        ):
            return
        self._shell_ready_timer.stop()
        self._shell_ready = True
        if not self._api_key:
            self._show_error("missing-key", "No Google Maps API key is configured.")
            return
        self.status_label.setText("Loading Google Maps 3D…")
        self._update_loading("Loading Google Maps 3D…")
        self._run_javascript(
            f"window.tasmead.loadGoogleMaps({json.dumps(self._api_key)});"
        )
        self._sync_tool_mode()
        self._sync_selected_trace(fit_scene=False)
        self._schedule_render(immediate=True)

    def _schedule_render(
        self,
        *,
        immediate: bool = False,
        fit_scene: bool = False,
    ) -> None:
        if (
            self._failed_generation == self._page_generation
            or self._csp_failed_generation == self._page_generation
        ):
            self._set_apply_enabled(False)
            return
        self._session_reusable = False
        self._fit_scene_on_next_render = (
            self._fit_scene_on_next_render or fit_scene
        )
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
            or self._failed_generation == self._page_generation
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
        fit_scene = self._fit_scene_on_next_render
        self._fit_scene_on_next_render = False
        self._run_javascript(f"window.tasmead.beginScene({revision}, {len(chunks)});")
        for chunk in chunks:
            self._run_javascript(
                f"window.tasmead.appendSceneChunk({revision}, {json.dumps(chunk)});"
            )
        self._run_javascript(
            "window.tasmead.finishScene("
            f"{revision}, {'true' if fit_scene else 'false'}"
            ");"
        )

    def _run_javascript(self, source: str) -> None:
        if self._web_view is None:
            return
        page_getter = getattr(self._web_view, "page", None)
        page = page_getter() if callable(page_getter) else None
        if page is not None:
            page.runJavaScript(source)

    def _on_render_started(self, generation: int, revision: int) -> None:
        if (
            generation != self._page_generation
            or revision != self._revision
            or self._failed_generation == generation
            or self._csp_failed_generation == generation
        ):
            return
        self._update_loading("Rendering preview…")

    def _on_render_acknowledged(self, generation: int, revision: int) -> None:
        if (
            generation != self._page_generation
            or revision != self._revision
            or self._failed_generation == generation
            or self._csp_failed_generation == generation
            or self._web_runtime_recreation_required
        ):
            return
        self._hide_loading()
        self._stop_presentation_watchdog(clear_activity=True)
        QTimer.singleShot(0, self._refresh_web_presentation)
        self._acknowledged_revision = revision
        self._session_reusable = (
            self._shell_ready
            and self._web_view is not None
            and self._server is not None
        )
        self.status_label.setText(
            "Preview matches the KML that will be exported."
        )
        self.retry_button.hide()
        self.open_settings_button.hide()
        self._set_apply_enabled(True)
        self._sync_measurement_overlay()

    def _on_render_failed(self, generation: int, kind: str, message: str) -> None:
        if (
            generation != self._page_generation
            or self._failed_generation == generation
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
            or self._failed_generation == generation
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
        self._web_runtime_recreation_required = True
        self._show_error(
            "render",
            "The embedded map renderer stopped unexpectedly. Check WebGL support and retry.",
        )

    def _show_error(self, kind: str, message: str) -> None:
        self._render_timer.stop()
        self._shell_ready_timer.stop()
        self._stop_presentation_watchdog(clear_activity=True)
        self._failed_generation = self._page_generation
        self._session_reusable = False
        self._hide_loading()
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

    def _tool_mode_changed(self, mode: str, checked: bool) -> None:
        if not checked:
            return
        self._tool_mode = mode
        if mode == "measure":
            self.tool_help_label.setText(
                "Click points on the map to measure distance."
            )
        elif mode == "move-anchor":
            self.tool_help_label.setText(
                "Click the map to move the selected KML file."
            )
        else:
            self.tool_help_label.setText("Drag the map to move around.")
        self._render_measurement_state()
        self._sync_tool_mode()

    def _sync_tool_mode(self) -> None:
        self._run_javascript(
            "window.tasmead.setToolMode("
            f"{json.dumps(self._tool_mode)}"
            ");"
        )

    def _on_map_clicked(
        self,
        generation: int,
        latitude: float,
        longitude: float,
    ) -> None:
        if (
            generation != self._page_generation
            or self._failed_generation == generation
            or self._csp_failed_generation == generation
        ):
            return
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError, OverflowError):
            return
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90.0 <= latitude <= 90.0
            or not -180.0 <= longitude <= 180.0
        ):
            return
        if self._tool_mode == "measure":
            self._measurement_points.append((latitude, longitude))
            self._render_measurement_state()
            self._sync_measurement_overlay()
        elif self._tool_mode == "move-anchor":
            self._move_selected_anchor(latitude, longitude)

    def _move_selected_anchor(
        self,
        latitude: float,
        longitude: float,
    ) -> None:
        if self._scene is None:
            return
        index = self.trace_selector.currentIndex()
        if not 0 <= index < len(self._scene.traces):
            return
        try:
            moved_trace = self._scene.traces[index].with_anchor_destination(
                latitude,
                longitude,
            )
        except (TypeError, ValueError) as error:
            self.tool_help_label.setText(
                str(error) or "The selected anchor position is invalid."
            )
            return

        self._loading_controls = True
        try:
            self.axis_controls["east_m"].setValue(
                moved_trace.adjustment.east_m
            )
            self.axis_controls["north_m"].setValue(
                moved_trace.adjustment.north_m
            )
        finally:
            self._loading_controls = False
        adjustment = TraceAdjustment(
            east_m=self.axis_controls["east_m"].value(),
            north_m=self.axis_controls["north_m"].value(),
            up_m=self.axis_controls["up_m"].value(),
            yaw_deg=self.axis_controls["yaw_deg"].value(),
        )
        traces = list(self._scene.traces)
        traces[index] = traces[index].with_adjustment(adjustment)
        self._scene = PreviewScene(tuple(traces))
        self.tool_help_label.setText(
            "KML moved. Click another position to refine it, or choose Navigate."
        )
        self._schedule_render(immediate=True)

    @staticmethod
    def _format_metric_distance(distance_m: float) -> str:
        if distance_m < 1_000.0:
            return f"{distance_m:.1f} m"
        return f"{distance_m / 1_000.0:.3f} km"

    @staticmethod
    def _format_nautical_distance(distance_m: float) -> str:
        nautical_miles = distance_m / 1_852.0
        places = 3 if nautical_miles < 10.0 else 2
        return f"{nautical_miles:.{places}f} NM"

    def _measurement_distances(self) -> tuple[float, float]:
        distances = tuple(
            inverse_distance_bearing(*start, *end)[0]
            for start, end in zip(
                self._measurement_points,
                self._measurement_points[1:],
            )
        )
        return (distances[-1], sum(distances)) if distances else (0.0, 0.0)

    def _render_measurement_state(self) -> None:
        point_count = len(self._measurement_points)
        visible = self._tool_mode == "measure" or point_count > 0
        self.measurement_label.setVisible(visible)
        self.undo_measurement_button.setVisible(visible)
        self.clear_measurement_button.setVisible(visible)
        self.undo_measurement_button.setEnabled(point_count > 0)
        self.clear_measurement_button.setEnabled(point_count > 0)
        if point_count == 0:
            self.measurement_label.setText(
                "Click two or more points to measure distance."
            )
        elif point_count == 1:
            self.measurement_label.setText(
                "1 point selected. Click another point to measure a leg."
            )
        else:
            last_leg_m, total_m = self._measurement_distances()
            self.measurement_label.setText(
                f"Last leg: {self._format_nautical_distance(last_leg_m)} / "
                f"{self._format_metric_distance(last_leg_m)}\n"
                f"Total: {self._format_nautical_distance(total_m)} / "
                f"{self._format_metric_distance(total_m)}"
            )

    def _sync_measurement_overlay(self) -> None:
        payload = {
            "points": [
                {"lat": latitude, "lng": longitude, "altitude": 0.0}
                for latitude, longitude in self._measurement_points
            ]
        }
        self._run_javascript(
            "window.tasmead.setMeasurement("
            f"{json.dumps(payload, separators=(',', ':'))}"
            ");"
        )

    def _undo_measurement_point(self, _checked: bool = False) -> None:
        if self._measurement_points:
            self._measurement_points.pop()
        self._render_measurement_state()
        self._sync_measurement_overlay()

    def _clear_measurement(self, _checked: bool = False) -> None:
        self._measurement_points.clear()
        self._render_measurement_state()
        self._sync_measurement_overlay()

    def _selected_trace_changed(self, _index: int) -> None:
        self._load_selected_adjustment()
        self._clear_measurement()
        self._sync_selected_trace(fit_scene=True)

    def _sync_selected_trace(self, *, fit_scene: bool) -> None:
        trace_id = self.trace_selector.currentData(Qt.ItemDataRole.UserRole)
        if not isinstance(trace_id, str) or not trace_id:
            return
        self._run_javascript(
            "window.tasmead.setSelectedTrace("
            f"{json.dumps(trace_id)}, {'true' if fit_scene else 'false'}"
            ");"
        )

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
        answer = QMessageBox.question(
            self,
            "Reset all KML offsets?",
            "Reset the position, height and rotation changes for every KML "
            "file in this preview?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
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
            and self._failed_generation != self._page_generation
            and self._csp_failed_generation != self._page_generation
        ):
            self._committed_scene = self._scene
            self.scene_applied.emit(self._scene)

    def _apply_and_export(self) -> None:
        if (
            self._scene is not None
            and self._acknowledged_revision == self._revision
            and self._failed_generation != self._page_generation
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
        self._dispose_web_runtime()
        self._web_runtime_recreation_required = False
        self._failed_generation = None
        self._csp_failed_generation = None
        self._csp_diagnostics.clear()
        self._api_key = ""
        self._scene = None
        self._committed_scene = None
        self._measurement_points.clear()
        self._fit_scene_on_next_render = False
        self.navigate_tool_button.blockSignals(True)
        self.navigate_tool_button.setChecked(True)
        self.navigate_tool_button.blockSignals(False)
        self._tool_mode = "navigate"
        self.tool_help_label.setText("Drag the map to move around.")
        self._render_measurement_state()
        self._show_idle_state()


__all__ = [
    "MapPreviewBridge",
    "MapPreviewWidget",
    "PreviewLoopbackServer",
    "WEBENGINE_AVAILABLE",
]
