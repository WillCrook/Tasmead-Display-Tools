import os
import requests
import xml.etree.ElementTree as ET
import math
import sys

def parse_kml(file_path):
    """
    Parse a KML file and extract waypoints as (lat, lon, alt).
    Supports both space-separated and comma-separated formats.
    """
    tree = ET.parse(file_path)
    root = tree.getroot()

    namespace = ''
    if root.tag.startswith('{'):
        namespace = root.tag.split('}')[0].strip('{')

    ns = {'default': namespace, 'gx': 'http://www.google.com/kml/ext/2.2'} if namespace else {}

    waypoints = []
    # Handle <coordinates> elements
    for coord_element in root.findall('.//default:coordinates', ns):
        coords_text = coord_element.text.strip()
        for coord in coords_text.split():
            try:
                lon, lat, ele = map(float, coord.split(','))
                waypoints.append((lat, lon, ele))
            except ValueError:
                continue  # Skip malformed coordinates

    # Handle <gx:coord> elements
    for coord_element in root.findall('.//gx:coord', ns):
        coords_text = coord_element.text.strip()
        try:
            lon, lat, ele = map(float, coords_text.split())
            waypoints.append((lat, lon, ele))
        except ValueError:
            continue  # Skip malformed coordinates

    return waypoints

def fetch_single_elevation(coordinate):
    """
    Fetch the ground elevation for a single (lat, lon) coordinate.
    """
    base_url = "https://api.open-elevation.com/api/v1/lookup"
    response = requests.get(base_url, params={"locations": f"{coordinate[0]},{coordinate[1]}"})
    if response.status_code == 200:
        data = response.json()
        if data.get('results'):
            return data['results'][0]['elevation']
        else:
            raise Exception("No results in API response.")
    else:
        raise Exception(f"API Error: {response.status_code}, {response.text}")

def rotate_route(waypoints, target_lat, target_lon, target_heading):
    """
    Rotate waypoints to align the route with the specified runway heading, accounting for dynamic take-off points.
    """
    if len(waypoints) < 2:
        raise ValueError("At least two waypoints are needed to calculate the initial heading.")

    # Extract the start and second waypoints
    start_lat, start_lon, _ = waypoints[0]
    next_lat, next_lon, _ = waypoints[1]

    # Calculate the current heading (bearing) between the first two waypoints
    delta_lat = next_lat - start_lat
    delta_lon = next_lon - start_lon
    initial_heading = math.degrees(math.atan2(delta_lon, delta_lat)) % 360

    # Calculate the rotation angle required to align with the target runway heading
    rotation_angle = math.radians(target_heading - initial_heading)

    # Translate all waypoints so the first one matches the target location
    translated_waypoints = [
        (lat - start_lat + target_lat, lon - start_lon + target_lon, alt)
        for lat, lon, alt in waypoints
    ]

    # Apply rotation around the target location
    rotated_waypoints = []
    for lat, lon, alt in translated_waypoints:
        # Translate point to origin for rotation
        rel_lat = lat - target_lat
        rel_lon = lon - target_lon

        # Apply rotation
        rotated_lat = (rel_lat * math.cos(rotation_angle) -
                       rel_lon * math.sin(rotation_angle)) + target_lat
        rotated_lon = (rel_lat * math.sin(rotation_angle) +
                       rel_lon * math.cos(rotation_angle)) + target_lon

        rotated_waypoints.append((rotated_lat, rotated_lon, alt))

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

                first_coordinate = waypoints[0]
                try:
                    ground_reference_elevation = fetch_single_elevation(first_coordinate)
                except Exception as e:
                    print(f"Failed to fetch elevation for {kml_file_name}: {e}")
                    continue

                rotated_waypoints = rotate_route(waypoints, target_lat, target_lon, target_heading)
                adjusted_waypoints = [(lat, lon, ele - ground_reference_elevation) for lat, lon, ele in rotated_waypoints]
                output_kml_file = os.path.join(output_dir, f"Farnborough_{kml_file_name}")
                write_kml(output_kml_file, adjusted_waypoints, str(kml_file_name[:-4]))

                print(f"Transposed coordinates saved to {output_kml_file}")

        print("All files processed successfully.")
    except Exception as e:
        print(f"Error: {e}")
