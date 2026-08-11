# Tasmead Display Tools

**Purpose:** To assist with planning and visualising aircraft displays for airshows.

- **Transpose to Airfield** – Quickly adjust KML flight paths for different target airfields using coordinates, runway headings, and elevation data.  
- **Debris Trajectory Simulation** – Estimate aircraft debris trajectories and impact zones based on flight paths, environmental conditions, and surface types.

---

## Key Features

- Drag & drop KML support for flight paths.  
- Save, load, and manage presets for airfields and simulation configurations.  
- Automatic unit conversion between metres and feet for altitude, terrain, and height fields.  
- Debris trajectory simulation with configurable physics (mass, drag, KTAS, surface type).  
- Detailed simulation summary including heading, air distance, ground distance, total planar distance, and number of impacts.  
- Optional Google Maps 3D preview with independent East, North, Up, and yaw adjustment for each trace.
- Export results to KML for visualisation in mapping tools such as Google Earth.  

---

## Installation

```bash
git clone https://github.com/WillCrook/Farnborough-Aircraft-Route-Converter.git
cd Farnborough-Aircraft-Route-Converter
python -m venv .venv
source .venv/bin/activate  # Mac/Linux
.venv\Scripts\activate     # Windows
pip install -r requirements.txt
python src/main.py
```

## Dependencies
Python 3.10+
[PyQt6](https://pypi.org/project/PyQt6/)￼
[PyQt6-WebEngine](https://pypi.org/project/PyQt6-WebEngine/)
[Pandas](https://pandas.pydata.org)

## Google Maps 3D preview

The preview is optional; transposition and debris KML export continue to work
without it. To use it:

1. Create a Google Cloud project with billing enabled.
2. Enable the **Maps JavaScript API** for that project.
3. Create a browser API key and restrict it to the Maps JavaScript API. For the
   desktop preview, allow the loopback referrer `http://127.0.0.1/*` (without a
   port, so the restriction can cover the preview's ephemeral local port).
4. In Tasmead Display Tools, open **Settings → Google Maps**, paste the key, and
   choose **Save key**.

The key is stored in the operating system's application settings. It is masked
in the UI but is not a secret: browser API keys are visible in requests sent to
Google. The application never writes the key to KML, presets, logs, or error
details. Preview also requires an internet connection and working WebGL.

The preview and KML exporter share the same WGS84 coordinates, quantisation,
altitude modes, topology, line widths, colours, and extrusion flags. Google
Maps 3D and Google Earth use different cameras and terrain renderers, so their
pixels are not guaranteed to match. KML terrain tessellation is represented by
Maps 3D's closest available geodesic-line behaviour, and Maps 3D does not expose
an independent KML PolyStyle colour for an extruded LineString curtain; these
renderer-only differences do not alter the exported vertices or KML values.

## Authors
Will Crook – Tasmead Display Tool
[GitHub](https://github.com/WillCrook)
￼
mkarachalios-1 – Debris Trajectory Calculations
[GitHub](https://github.com/mkarachalios-1/airshow-trajectory-app/blob/main/streamlit_app.py)￼

## License
This project is released under the GNU General Public License v3.0 (GPLv3).
You are free to use, modify, and distribute this software under the following conditions:
	•	Any distributed modifications or derivative works must also be licensed under GPLv3.
	•	The source code must always be made available.
	•	No warranty is provided; use at your own risk.
For the full license text, see the LICENSE file in this repository.

<img width="809" height="614" alt="Screenshot 2026-01-14 at 19 38 58" src="https://github.com/user-attachments/assets/8b54d580-a282-4881-b1a4-643ae0ec73cf" />
