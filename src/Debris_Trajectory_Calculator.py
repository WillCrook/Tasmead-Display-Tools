import math
import pandas as pd
from KML_File_Handling import load_last_two_points_from_kml

class DebrisTrajectoryCalculator:
    def __init__(self,mass_kg, area_m2, Cd, rho, g, dt, ktas, surface, input_file, output_file, include_ground_drag, terrain_m, slide_physics):

        #CONFIG INPUTS
        self.mass_kg = mass_kg
        self.area_m2 = area_m2
        self.Cd = Cd
        self.rho = rho
        self.g = g
        self.dt = dt
        self.ktas = ktas
        self.surface = surface
        self.vz_bounce_min = slide_physics
        self.include_ground_drag = include_ground_drag

        #FLIGHT INPUTS 
        self.terrain_m = terrain_m

        # Final two lead-in points (lon, lat, alt metres AMSL)
        self.penultimate_lon = 1.0581636
        self.penultimate_lat = 52.50358

        self.final_lon = 1.0577345
        self.final_lat = 52.50291
        self.alt_m = 172.582  # metres AMSL
        self.alt_ft = self.alt_m / 0.3048

        #file directorys
        self.input_file = input_file
        self.output_file = output_file

        # CALCULATION all credit goes to: https://github.com/mkarachalios-1/airshow-trajectory-app/blob/main/streamlit_app.py

    @staticmethod
    def bearing_deg(lat1, lon1, lat2, lon2):
        """Initial bearing (deg, 0..360) from (lat1,lon1) to (lat2,lon2)."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlam = math.radians(lon2 - lon1)
        y = math.sin(dlam) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    @staticmethod
    def en_exp(vn, e0, einf, vc):
        """Velocity-dependent COR: e(vn) = einf + (e0 - einf)*exp(-vn/vc)."""
        vn = max(0.0, vn)
        return max(0.0, min(1.0, einf + (e0 - einf) * math.exp(-vn / max(1e-6, vc))))

    SURFSETS = {
        "concrete": dict(mu_imp=0.55, mu_slide=0.50, e0=0.20, einf=0.05, vc=15.0),
        "asphalt":  dict(mu_imp=0.45, mu_slide=0.40, e0=0.18, einf=0.05, vc=12.0),
        "grass":    dict(mu_imp=0.35, mu_slide=0.55, e0=0.12, einf=0.03, vc=8.0),
    }

    def simulate_3d(
        self,
        m, A, Cd, rho, g, dt,
        alt_m, ktas, angle_deg, surface,
        vz0=0.0, include_ground_drag=True,
        vz_bounce_min=0.5, max_steps=300000
    ):
        """
        3D point-mass with quadratic drag, wind = 0.
        Axes: x (forward/nose), y (right), z (height above ground).
        Euler forward integration with impact, bounce, and slide.
        """

        V     = float(ktas) * 0.514444444  # kt -> m/s
        theta = math.radians(angle_deg)
        vx0, vy0 = V * math.cos(theta), V * math.sin(theta)

        print("Surface used:", surface)
        s = self.SURFSETS[surface]
        mu_imp, mu_slide, e0, einf, vc = s["mu_imp"], s["mu_slide"], s["e0"], s["einf"], s["vc"]

        K = 0.5 * rho * Cd * A / m

        t = 0.0
        x = y = 0.0
        z = alt_m
        vx, vy, vz = vx0, vy0, vz0
        airborne = True

        def clamp_eps(u, eps=1e-12):
            return 0.0 if abs(u) < eps else u

        rows = []

        for _ in range(max_steps):
            if airborne:
                vrelx, vrely, vrelz = vx, vy, vz
                vmag = math.sqrt(vrelx*vrelx + vrely*vrely + vrelz*vrelz)

                ax = -K * vmag * vrelx
                ay = -K * vmag * vrely
                az_acc =  g - K * vmag * vrelz

                vx_new = clamp_eps(vx + ax*dt)
                vy_new = clamp_eps(vy + ay*dt)
                vz_new = clamp_eps(vz + az_acc*dt)

                x_new = x + vx_new * dt
                y_new = y + vy_new * dt
                z_new = max(0.0, z - vz_new * dt)

                # impact?
                if z > 0.0 and z_new <= 0.0:
                    vn_pre = abs(vz_new)
                    eN = self.en_exp(vn_pre, e0, einf, vc)

                    vz_post = -eN * vz_new  # rebound upward => negative downward speed

                    vt_mag_pre = math.sqrt(vx_new*vx_new + vy_new*vy_new)
                    dv_t = mu_imp * (1.0 + eN) * vn_pre
                    if vt_mag_pre > 0.0:
                        scale = max(0.0, (vt_mag_pre - dv_t) / vt_mag_pre)
                        vx_post = vx_new * scale
                        vy_post = vy_new * scale
                    else:
                        vx_post = vy_post = 0.0

                    x, y, z = x_new, y_new, 0.0
                    vx, vy, vz = clamp_eps(vx_post), clamp_eps(vy_post), clamp_eps(vz_post)

                    rows.append(dict(t=t+dt, x=x, y=y, z=z, phase="air"))

                    if abs(vz) < vz_bounce_min:
                        airborne = False
                else:
                    x, y, z = x_new, y_new, z_new
                    vx, vy, vz = vx_new, vy_new, vz_new
                    rows.append(dict(t=t+dt, x=x, y=y, z=z, phase="air"))

            else:
                vt_mag = math.sqrt(vx*vx + vy*vy)
                if vt_mag <= 1e-6:
                    vx = vy = 0.0
                    rows.append(dict(t=t+dt, x=x, y=y, z=0.0, phase="slide"))
                    break

                ax_fric_x = -mu_slide * g * (vx / vt_mag)
                ax_fric_y = -mu_slide * g * (vy / vt_mag)

                ax_drag = ay_drag = 0.0
                if include_ground_drag:
                    vmag = vt_mag
                    ax_drag = -K * vmag * vx
                    ay_drag = -K * vmag * vy

                ax = ax_fric_x + ax_drag
                ay = ax_fric_y + ay_drag

                vx = clamp_eps(vx + ax * dt)
                vy = clamp_eps(vy + ay * dt)

                # prevent reversal
                if vx * (vx + ax*dt) < 0: vx = 0.0
                if vy * (vy + ay*dt) < 0: vy = 0.0

                x = x + vx * dt
                y = y + vy * dt
                z = 0.0

                rows.append(dict(t=t+dt, x=x, y=y, z=z, phase="slide"))

            t += dt
            if t > 3600.0:
                break

        return pd.DataFrame(rows)

    def run_debris_trajectory_simulation(self):
        az_deg = self.bearing_deg(self.penultimate_lat, self.penultimate_lon, self.final_lat, self.final_lon)
        self.az_world = math.radians(az_deg)

        #relative altitude above ground
        self.alt_m_relative = self.alt_m - self.terrain_m

        df = self.simulate_3d(
            m=self.mass_kg, A=self.area_m2, Cd=self.Cd, rho=self.rho, g=self.g, dt=self.dt,
            alt_m=self.alt_m_relative, ktas=self.ktas, angle_deg=0.0, surface=self.surface,
            vz0=0.0, include_ground_drag=self.include_ground_drag, vz_bounce_min=self.vz_bounce_min
        )

        R = 6371000.0
        coords_air = [(self.final_lon, self.final_lat, self.alt_m)]
        coords_ground = []

        first = True
        for _, row in df.iterrows():
            if first:
                first = False
                continue

            x = float(row["x"])
            y = float(row["y"])
            z = float(row["z"])
            
            east = x * math.sin(self.az_world) + y * math.sin(self.az_world + math.pi/2.0)
            north = x * math.cos(self.az_world) + y * math.cos(self.az_world + math.pi/2.0)

            dlat = (north / R) * 180.0 / math.pi
            dlon = (east / (R * math.cos(math.radians(self.final_lat)))) * 180.0 / math.pi

            lat = self.final_lat + dlat
            lon = self.final_lon + dlon
            
            #add terrain elevation back in for google earth
            alt_real = z + self.terrain_m

            if row["phase"] == "air":
                coords_air.append((lon, lat, alt_real))
            else:
                coords_ground.append((lon, lat, alt_real))

        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<kml xmlns="http://www.opengis.net/kml/2.2">\n')
            f.write('<Document>\n')
            f.write('<Style id="air_blue"><LineStyle><color>ffff0000</color><width>4</width></LineStyle></Style>\n')
            f.write('<Style id="ground_red"><LineStyle><color>ff0000ff</color><width>4</width></LineStyle></Style>\n')

            f.write('<Placemark><name>Airborne</name><styleUrl>#air_blue</styleUrl>\n')
            f.write('<LineString><altitudeMode>absolute</altitudeMode><coordinates>\n')
            for lon, lat, alt in coords_air:
                f.write(f"{lon:.7f},{lat:.7f},{alt:.3f}\n")
            f.write('</coordinates></LineString></Placemark>\n')

            if coords_ground:
                f.write('<Placemark><name>Ground run</name><styleUrl>#ground_red</styleUrl>\n')
                f.write('<LineString><altitudeMode>absolute</altitudeMode><coordinates>\n')
                for lon, lat, alt in coords_ground:
                    f.write(f"{lon:.7f},{lat:.7f},{alt:.3f}\n")
                f.write('</coordinates></LineString></Placemark>\n')

            f.write('</Document>\n</kml>\n')

        print(f"Wrote: {self.output_file}")
        print(f"Azimuth used (deg): {az_deg:.3f}")
        print(f"Total ground‑planar distance (m): {df['x'].iloc[-1]:.3f}")
        print(f"Ground distance to rest (m): {df[df['phase']=='slide']['x'].iloc[-1]:.3f}")
        print(f"Air distance to first impact (m): {df[df['phase']=='air']['x'].iloc[-1]:.3f}")