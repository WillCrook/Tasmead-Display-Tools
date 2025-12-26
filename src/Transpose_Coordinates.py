import os
import math
import sys
from KML_File_Handling import parse_kml

# def fetch_single_elevation(coordinate):
#     """
#     Fetch the ground elevation for a single (lat, lon) coordinate.
#     """
#     base_url = "https://api.open-elevation.com/api/v1/lookup"
#     response = requests.get(base_url, params={"locations": f"{coordinate[0]},{coordinate[1]}"})
#     if response.status_code == 200:
#         data = response.json()
#         if data.get('results'):
#             return data['results'][0]['elevation']
#         else:
#             raise Exception("No results in API response.")
#     else:
#         raise Exception(f"API Error: {response.status_code}, {response.text}")

def rotate_route(waypoints, target_lat, target_lon, target_heading):
    """
    Rotate waypoints to align the route with the specified runway heading, accounting for dynamic take-off points.
    """
    if len(waypoints) < 2:
        raise ValueError("At least two waypoints are needed to calculate the initial heading.")

    # Extract the start and second waypoints
    start_lat, start_lon, _ = waypoints[0]
    next_lat, next_lon, _ = waypoints[1]

    # Calculate the scaling factor for longitude based on the starting latitude
    lat_rad = math.radians(start_lat)
    source_lon_scale = math.cos(lat_rad)
    
    # Calculate the scaling factor for the target latitude
    target_lat_rad = math.radians(target_lat)
    target_lon_scale = math.cos(target_lat_rad)

    # Calculate the current heading (bearing) between the first two waypoints
    delta_lat = next_lat - start_lat
    delta_lon = (next_lon - start_lon) * source_lon_scale
    initial_heading = math.degrees(math.atan2(delta_lon, delta_lat)) % 360

    # Calculate the rotation angle required to align with the target runway heading
    rotation_angle = math.radians(target_heading - initial_heading)

    # Translate all waypoints so the first one matches the target location
    # We don't apply the target offset yet, we do it after rotation
    translated_waypoints = [
        (lat - start_lat, lon - start_lon, alt)
        for lat, lon, alt in waypoints
    ]

    # Apply rotation around the origin (which corresponds to the start point)
    rotated_waypoints = []
    for rel_lat, rel_lon, alt in translated_waypoints:
        # Scale longitude for rotation using SOURCE scale (converting to "meters")
        scaled_rel_lon = rel_lon * source_lon_scale

        # Apply rotation
        rotated_lat = (rel_lat * math.cos(rotation_angle) -
                       scaled_rel_lon * math.sin(rotation_angle))
        rotated_scaled_lon = (rel_lat * math.sin(rotation_angle) +
                              scaled_rel_lon * math.cos(rotation_angle))
        
        # Unscale longitude using TARGET scale and add target location
        final_lat = rotated_lat + target_lat
        final_lon = (rotated_scaled_lon / target_lon_scale) + target_lon

        rotated_waypoints.append((final_lat, final_lon, alt))

    return rotated_waypoints

def write_kml(file_path, coordinates, name_of_aircraft):
    """
    Write adjusted coordinates to a KML file with cyan lines and extended paths to the ground.
    """
    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
    <name>{name_of_aircraft} Adjusted Coordinates</name>
    <description>Created by Will and Uncle Rich with the help of ChatGPT using Python</description>
    <Style id="cyanLine">
        <LineStyle>
            <color>ff00ffff</color>
            <width>2</width>
        </LineStyle>
    </Style>
    <Placemark>
        <name>Path</name>
        <styleUrl>#cyanLine</styleUrl>
        <LineString>
            <altitudeMode>relativeToGround</altitudeMode>
            <coordinates>
"""
    for lat, lon, ele in coordinates:
        kml_content += f"                {lon},{lat},{ele}\n"

    kml_content += """            </coordinates>
        </LineString>
    </Placemark>
"""

    kml_content += "</Document>\n</kml>"

    with open(file_path, 'w') as file:
        file.write(kml_content)
    
def read_config(config_file):
    with open(config_file, 'r') as file:
        config = {}
        for line in file:
            line = line.strip()
            if ' = ' in line:
                key, value = line.split(' = ')
                value = value.strip().replace('\\', '')
                try:
                    config[key] = float(value)
                except ValueError:
                    print(f"Warning: Invalid value for {key}: {value}")
                    continue
        return config

def run_transposition(input_files, output_dir, target_lat, target_lon, target_heading, ground_reference_elevation=0):
    try:
        os.makedirs(output_dir, exist_ok=True)

        for kml_file_path in input_files:
            if kml_file_path.endswith(".kml") and os.path.isfile(kml_file_path):
                kml_file_name = os.path.basename(kml_file_path)
                print(f"Processing file: {kml_file_name}")

                waypoints = parse_kml(kml_file_path)
                if not waypoints:
                    print(f"Skipping file due to missing coordinates: {kml_file_name}")
                    continue            

                rotated_waypoints = rotate_route(waypoints, target_lat, target_lon, target_heading)
                adjusted_waypoints = [(lat, lon, ele - ground_reference_elevation) for lat, lon, ele in rotated_waypoints]
                
                write_kml(output_dir, adjusted_waypoints, str(kml_file_name[:-4]))

                print(f"Transposed coordinates saved to {output_kml_file}")

        print("All files processed successfully.")
    except Exception as e:
        print(f"Error: {e}")
        raise e

if __name__ == "__main__":
    try:
        # Determine the base path for files
        if getattr(sys, 'frozen', False):
            # If the app is bundled into a standalone executable
            app_path = os.path.dirname(sys.executable)
        else:
            # If the script is running normally
            app_path = os.path.dirname(os.path.abspath(__file__))

        # Set the directories
        input_dir = os.path.join(app_path, "Input_KML_Files")
        output_dir = os.path.join(app_path, "Output_KML_Files")
        os.makedirs(output_dir, exist_ok=True)

        # Location set to Farnborough
        config = read_config(os.path.join(app_path, "config.txt"))
        target_lat = config["target_lat"]
        target_lon = config["target_lon"]
        target_heading = config["target_heading"]

        for kml_file_name in os.listdir(input_dir):
            if kml_file_name.endswith(".kml"):
                kml_file_path = os.path.join(input_dir, kml_file_name)
                print(f"Processing file: {kml_file_name}")

                waypoints = parse_kml(kml_file_path)
                if not waypoints:
                    print(f"Skipping file due to missing coordinates: {kml_file_name}")
                    continue

                #first_coordinate = waypoints[0]
                # try:
                #     ground_reference_elevation = fetch_single_elevation(first_coordinate)
                # except Exception as e:
                #     print(f"Failed to fetch elevation for {kml_file_name}: {e}")
                #     continue
                ground_reference_elevation = 0  # Assuming sea level if elevation fetching is disabled
                
                rotated_waypoints = rotate_route(waypoints, target_lat, target_lon, target_heading)
                adjusted_waypoints = [(lat, lon, ele - ground_reference_elevation) for lat, lon, ele in rotated_waypoints]
                output_kml_file = os.path.join(output_dir, f"Farnborough_{kml_file_name}")
                write_kml(output_kml_file, adjusted_waypoints, str(kml_file_name[:-4]))

                print(f"Transposed coordinates saved to {output_kml_file}")

        print("All files processed successfully.")
    except Exception as e:
        print(f"Error: {e}")