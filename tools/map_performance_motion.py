"""Bounded camera exercise/observation scripts for the standalone benchmark."""

import json


def motion_script(motion, seconds):
    # Drag modes only observe Maps' native input handling. No camera setters run.
    return """(() => {
      const b = window.__benchmark;
      const mode = MOTION, duration = DURATION;
      b.done = false;
      b.sample = {intervals: [], duration_ms: 0, heading_travel_degrees: 0,
        moving_ms: 0, right_drag_moves: 0, right_drag_movement_px: 0,
        hidden: document.hidden, focused_start: document.hasFocus()};
      const sample = b.sample;
      const dragged = event => {
        if ((event.buttons & 2) && event.isTrusted) {
          sample.right_drag_moves++;
          sample.right_drag_movement_px += Math.abs(event.movementX || 0);
          sample.input_target = event.target && event.target.tagName;
        }
      };
      document.addEventListener('pointermove', dragged, true);
      let start = null, previous = null, heading = Number(b.map.heading);
      function frame(now) {
        if (start === null) start = now;
        const elapsed = now - start;
        if (previous !== null) {
          const interval = now - previous;
          sample.intervals.push(interval);
          const current = Number(b.map.heading);
          const delta = Math.abs(((current - heading + 540) % 360) - 180);
          if (Number.isFinite(delta)) {
            sample.heading_travel_degrees += delta;
            if (delta > .01) sample.moving_ms += interval;
          }
          heading = current;
        }
        previous = now;
        sample.hidden ||= document.hidden;
        sample.duration_ms = elapsed;
        const phase = Math.min(1, elapsed / duration);
        if (mode === 'orbit' || mode === 'mixed') b.map.heading = phase * 360;
        if (mode === 'mixed') {
          const angle = phase * Math.PI * 2;
          b.map.center = {lat: b.base.lat + .003 * Math.sin(angle),
            lng: b.base.lng + .003 * (1 - Math.cos(angle)), altitude: b.base.altitude};
          b.map.range = 4000 + 800 * Math.sin(angle);
        }
        if (phase < 1) requestAnimationFrame(frame);
        else {
          document.removeEventListener('pointermove', dragged, true);
          sample.focused_end = document.hasFocus();
          b.done = true;
        }
      }
      requestAnimationFrame(frame);
      return true;
    })()""".replace("MOTION", json.dumps(motion)).replace("DURATION", str(seconds * 1000))
