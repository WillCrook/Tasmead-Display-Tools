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
[Pandas](https://pandas.pydata.org)

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


