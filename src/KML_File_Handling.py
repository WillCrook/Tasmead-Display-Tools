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
        