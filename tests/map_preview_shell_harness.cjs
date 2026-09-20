// Execute the production shell against observable Maps/DOM doubles. No network.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const writes = [], frames = new Map(), timers = new Map(), receipts = [], failures = [];
let serial = 0;
class Element {
  constructor(options={}) {
    this.children = []; this.dataset = {}; this.events = {}; this.parentElement = null;
    Object.assign(this, options);
    return new Proxy(this, {set(target, property, value) {
      if (!['parentElement', 'children', 'events', 'dataset'].includes(property)) {
        writes.push({element: target, property, value});
      }
      target[property] = value; return true;
    }});
  }
  append(element) { element.remove(); this.children.push(element); element.parentElement = this; }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter(item => item !== this);
      this.parentElement = null;
    }
  }
  addEventListener(name, fn) { this.events[name] = fn; }
}
class Map3DElement extends Element {}
class Polyline3DElement extends Element {}
class Polygon3DElement extends Element {}
class Marker3DElement extends Element {}
const host = new Element(), status = new Element();
const bridge = {
  shellReady() {}, renderStarted() {}, presentationStateChanged() {},
  renderAcknowledged(generation, revision) { receipts.push(revision); },
  renderFailed(...args) { failures.push(args); }
};
const context = {
  URL, Map, Set, console,
  document: {getElementById: id => id === 'map-host' ? host : status},
  qt: {webChannelTransport: {}},
  QWebChannel: function(_transport, callback) { callback({objects: {tasmeadBridge: bridge}}); },
  google: {maps: {importLibrary: async () => ({
    Map3DElement, Polyline3DElement, Polygon3DElement, Marker3DElement,
    PinElement: Element, AltitudeMode: {}, MapMode: {HYBRID: 'HYBRID'}
  })}},
  requestAnimationFrame(fn) { const id = ++serial; frames.set(id, fn); return id; },
  cancelAnimationFrame(id) { frames.delete(id); },
  setTimeout(fn) { const id = ++serial; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); }
};
context.window = context;
context.location = {search: '?generation=1'};
context.addEventListener = () => {};
vm.runInNewContext(input.script, context);
context.tasmeadGoogleReady();
const clone = value => JSON.parse(JSON.stringify(value));
const api = context.tasmead;
function submit(payload, revision, fit=false) {
  api.beginScene(revision, 1);
  api.appendSceneChunk(revision, JSON.stringify(payload));
  api.finishScene(revision, fit);
}
async function prepare() { for (let i = 0; i < 12; ++i) await Promise.resolve(); }
async function draw() {
  await prepare();
  const callbacks = [...frames.values()]; frames.clear();
  callbacks.forEach(fn => fn());
  assert.deepEqual(failures, []);
}
function steady(value=true) { host.children[0].events['gmp-steadychange']({isSteady: value}); }
function changes(property) { return writes.filter(item => item.property === property); }

(async () => {
  let payload = clone(input.payload);
  submit(payload, 1);
  await prepare();
  assert.equal(host.children.length, 0, 'wait for the scheduled drawing callback');
  assert.equal(frames.size, 1);
  await draw();
  assert.deepEqual(receipts, [], 'rAF does not acknowledge a changed scene');
  steady();
  assert.deepEqual(receipts, [1]);
  assert.equal(timers.size, 0);
  const map = host.children[0];
  const initialGeometry = map.children.find(item => item instanceof Polyline3DElement);
  assert.equal(map.children.length, 2, 'only selected geometry and anchor are attached');
  map.heading = 37;
  writes.length = 0;

  submit(clone(payload), 2);
  await draw();
  assert.equal(changes('path').length, 0);
  assert.equal(changes('position').length, 0);
  assert.equal(changes('strokeColor').length, 0);
  assert.equal(map.heading, 37, 'ordinary redraw preserves the camera');
  assert.deepEqual(receipts, [1, 2], 'no-op revisions acknowledge without a new steady event');
  assert.equal(frames.size, 0, 'no permanent animation loop');
  assert.equal(timers.size, 0);

  payload.traces[0].geometries[0].coordinates[0].altitude += 0.1;
  submit(payload, 3);
  await prepare();
  payload.traces[0].geometries[0].coordinates[0].altitude += 0.1;
  submit(payload, 4);
  await prepare();
  assert.equal(frames.size, 1, 'rapid revisions share one frame');
  await draw();
  assert.equal(changes('path').length, 1, 'only the latest changed path is assigned');
  assert.equal(changes('position').length, 0);
  assert.equal(changes('strokeColor').length, 0);
  assert.equal(map.children.find(item => item instanceof Polyline3DElement), initialGeometry);
  assert.equal(initialGeometry.path[0].altitude, payload.traces[0].geometries[0].coordinates[0].altitude);
  assert.deepEqual(receipts, [1, 2]);
  steady();
  assert.deepEqual(receipts, [1, 2, 4]);

  writes.length = 0;
  payload.traces[0].geometries[0].style.strokeColor = '#ff0000ff';
  submit(payload, 5); await draw(); steady();
  assert.equal(changes('strokeColor').length, 1);
  assert.equal(changes('path').length, 0, 'style changes do not upload coordinates');

  steady(false);
  submit(clone(payload), 6); await draw();
  assert.equal(receipts.at(-1), 5, 'no-op during camera motion still waits for steady');
  steady(); assert.equal(receipts.at(-1), 6);

  // A steady event from the committed scene must not acknowledge a queued revision.
  payload.traces[0].anchor.label = 'Moved';
  submit(payload, 7); await prepare(); steady();
  assert.equal(receipts.at(-1), 6);
  writes.length = 0;
  await draw();
  assert.equal(changes('path').length, 0);
  assert.equal(changes('label').length, 1);
  steady(); assert.equal(receipts.at(-1), 7);

  // Fit intent survives replacement of a queued scene.
  submit(payload, 8, true); await prepare();
  submit(payload, 9); await draw(); steady();
  assert.equal(map.heading, 0);
  assert.equal(receipts.at(-1), 9);

  payload.traces[1].geometries[0].coordinates[0].altitude += 1;
  submit(payload, 10); await draw();
  assert.equal(receipts.at(-1), 10, 'detached trace updates need no new map render event');

  api.setSelectedTrace(payload.traces[1].id);
  assert.equal(map.children.length, 2);
  assert.ok(!map.children.includes(initialGeometry));
  assert.equal(host.children[0], map);
  payload.traces = [payload.traces[1]];
  submit(payload, 11); await draw(); steady();
  assert.equal(map.children.length, 2, 'removed traces do not leave attached elements');
  const measurement = {points: [{lat: 51, lng: -1, altitude: 0}, {lat: 51.001, lng: -1, altitude: 0}]};
  api.setMeasurement(measurement);
  writes.length = 0;
  api.setMeasurement(clone(measurement));
  assert.equal(changes('path').length, 0, 'unchanged measurements do not upload their path');
  assert.equal(changes('position').length, 0);
  api.setMeasurement({points: []});
  assert.equal(map.children.length, 2);
  assert.equal(frames.size, 0);
  assert.equal(timers.size, 0);
  assert.deepEqual(failures, []);
  console.log('Retained geometry, coalescing, exact coordinates, camera, visibility and acknowledgements passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
