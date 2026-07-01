import xml.etree.ElementTree as ET

def load_last_two_points_from_kml(input_file):
        tree = ET.parse(input_file)
        root = tree.getroot()

        ns = {
            'kml': 'http://www.opengis.net/kml/2.2',
            'gx': 'http://www.google.com/kml/ext/2.2'
        }

        coord_elements = root.findall('.//gx:coord', ns)
        if len(coord_elements) < 2:
            raise ValueError("Not enough <gx:coord> elements in KML file.")

        # Take the last two coordinates
        penultimate = coord_elements[-2].text.strip().split()
        final = coord_elements[-1].text.strip().split()

        
        penultimate_lon = float(penultimate[0])
        penultimate_lat = float(penultimate[1])

        final_lon = float(final[0])
        final_lat = float(final[1])
        alt_m = float(final[2]) if len(final) > 2 else 0.0

        return (penultimate_lat, 
                penultimate_lon, 
                final_lat, 
                final_lon, 
                alt_m)

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